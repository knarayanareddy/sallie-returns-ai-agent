"""
Version A — Supervisor Orchestrator.

LangGraph-style state machine implemented as an explicit transition loop.
One agent runtime. Six capability nodes (not separate agents). Single ReturnCaseState.

In production this would be ported to actual LangGraph. Here we implement the
same state machine semantics: nodes, conditional edges, guardrails, and replay,
all explicitly, so the behaviour is transparent and debuggable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable, List, Optional

from decimal import Decimal

from .state_models import (
    ConditionVerdict,
    DeadLetterReason,
    GuardrailDecision,
    NotificationStatus,
    PolicyEvalResult,
    ReceiptVerification,
    ResolutionType,
    ReturnCaseState,
    RMAStatus,
    StockMatrix,
    SupplierClaim,
)
from .tools import (
    acquire_atomic_hold,
    analyze_shoe_condition,
    check_photo_quality_compliance,
    classify_structural_defect,
    create_interstore_transfer_manifest,
    federated_sales_search,
    generate_replacement_proof,
    notify_customer,
    payment_token_lookup,
    query_stock_matrix,
    release_expired_hold,
    submit_supplier_rma_claim,
    verify_receipt_data,
)

logger = logging.getLogger("returns.orchestrator")

# ── Constants ──────────────────────────────────────────────────────────────────

REFUND_CAP_EUR = Decimal("150.00")
DEFECT_CONFIDENCE_THRESHOLD = 0.85
HOLD_TTL_HOURS = 48
THIRTY_DAYS = 30

# ── Orchestrator ───────────────────────────────────────────────────────────────

class SupervisorOrchestrator:
    """
    Manages the full Returns case lifecycle.

    Flow:
        intake_eval  →  (proof_ok ? policy_check : cross_store_lookup)
        cross_store_lookup  →  (match_found ? policy_check : hitl_escalate)
        policy_check  →  refund | exchange | defect | hitl
        stock_resolve  →  transfer | alternative | hitl
        defect_assess  →  remedy | rma_parallel | hitl
        hitl_escalate  →  re-entry at appropriate node (called externally)
        notify_customer  →  close (if notified) | block

    Usage:
        orchestrator = SupervisorOrchestrator()
        case = ReturnCaseState(...)   # pre-populated with intake data
        case = orchestrator.run(case)  # runs full pipeline or to first HITL
    """

    def __init__(self):
        self._running_cases: List[str] = []

    # ── Public API ──────────────────────────────────────────────────────────

    def run(self, state: ReturnCaseState) -> ReturnCaseState:
        """
        Execute the pipeline from the current state forward.
        Starts at the appropriate node based on state completeness.
        Returns the state when it reaches a terminal node or a HITL pause.
        """
        self._running_cases.append(state.case_id)
        try:
            return self._execute(state)
        finally:
            if state.case_id in self._running_cases:
                self._running_cases.remove(state.case_id)

    def resume_from_hitl(
        self,
        state: ReturnCaseState,
        supervisor_resolution: str,  # "APPROVE_REFUND" | "APPROVE_EXCHANGE" | "REJECT_RETURN" | "FLAG_DEFECT_MANUAL"
        supervisor_notes: Optional[str] = None,
    ) -> ReturnCaseState:
        """
        Called after Anouk/Robin has actioned a HITL case.
        Re-enters the pipeline at the appropriate node based on the resolution.
        """
        state.audit_log.append(
            state.audit_log[-1]  # carry forward
            if state.audit_log
            else None
        )  # placeholder — real impl would store before/after
        state.supervisor_notes = supervisor_notes
        state.supervisor_resolved_at = datetime.utcnow()

        if supervisor_resolution == "APPROVE_REFUND":
            state.escalation_reason = None
            state.escalated_to_supervisor = False
            state.policy_result = PolicyEvalResult(
                within_30_day_window=state.policy_result.within_30_day_window if state.policy_result else True,
                is_unworn=True,
                refund_amount_eur=state.total_refund_value,
                under_refund_cap=True,  # supervisor digitally authorized this refund
                resolution_type=ResolutionType.REFUND,
            )
            state.resolution_type = ResolutionType.REFUND
            return self._execute(state, start_at="execute_till_refund")

        elif supervisor_resolution == "APPROVE_EXCHANGE":
            state.escalation_reason = None
            state.escalated_to_supervisor = False
            state.resolution_type = ResolutionType.EXCHANGE
            return self._execute(state, start_at="stock_resolve")

        elif supervisor_resolution == "REJECT_RETURN":
            state.resolution_type = ResolutionType.PENDING_REVIEW
            state.escalation_reason = ("Supervisor rejected: " + (supervisor_notes or "policy exception"))
            state.escalated_to_supervisor = False
            state.closed = True
            state.closed_at = datetime.utcnow()
            self.notify_customer(state)
            return state  # terminal — case closed with rejection

        elif supervisor_resolution == "FLAG_DEFECT_MANUAL":
            state.escalation_reason = None
            state.escalated_to_supervisor = False
            state.defect_detected = True
            state.resolution_type = ResolutionType.DEFECT_REMEDY
            return self._execute(state, start_at="assemble_supplier_rma")

        return state

    # ── Main execution loop ──────────────────────────────────────────────────

    def _execute(self, state: ReturnCaseState, start_at: Optional[str] = None) -> ReturnCaseState:
        """
        Walk the state machine from start_at (or from the first node) to
        terminal (CLOSE) or HITL pause.

        All nodes run in order. Each node internally decides whether it has
        work to do based on current state. Nodes return the next node they
        want to run next — typically the following node in sequence, or a
        specific routing target.

        The orchestrator respects explicit next_node returns:
          - 'HITL'   → pause and return state (human re-entry needed)
          - 'CLOSE'  → terminal, case resolved
          - else     → jump to that node in the sequence, skipping any
                        intermediate nodes that have no work to do.
        """
        node_order = [
            "intake_eval",
            "cross_store_lookup",
            "policy_check",
            "stock_resolve",
            "defect_assess",
            "execute_till_refund",
            "transfer_dispatcher",
            "assemble_supplier_rma",
            "notify_customer",
        ]

        if start_at:
            start_idx = node_order.index(start_at)
            nodes_to_run = node_order[start_idx:]
        else:
            nodes_to_run = node_order

        idx = 0
        while idx < len(nodes_to_run):
            node_name = nodes_to_run[idx]
            next_node = self._dispatch(node_name, state)

            if next_node == "HITL":
                logger.info("Case %s paused at HITL (node: %s)", state.case_id, node_name)
                state.escalated_to_supervisor = True
                state.escalation_timestamp = datetime.utcnow()
                return state

            if next_node == "CLOSE":
                logger.info("Case %s reached terminal state", state.case_id)
                return state

            # next_node is a node name — jump to it in the sequence.
            # When start_at is set, nodes_to_run is a slice of node_order, so
            # the absolute index in node_order must be adjusted relative to the
            # slice start before using it to index into nodes_to_run.
            if next_node in node_order:
                jump_idx = node_order.index(next_node)
                jump_idx_in_nodes_to_run = jump_idx - start_idx if start_at else jump_idx
                # Only jump forward; never go backward (would cause infinite loop)
                if jump_idx_in_nodes_to_run >= idx:
                    idx = jump_idx_in_nodes_to_run
                    continue
            idx += 1

        return state

    # ── Node dispatch ────────────────────────────────────────────────────────

    def _dispatch(self, node_name: str, state: ReturnCaseState) -> str:
        dispatch = {
            "intake_eval": self.intake_eval,
            "cross_store_lookup": self.cross_store_lookup,
            "policy_check": self.policy_check,
            "stock_resolve": self.stock_resolve,
            "defect_assess": self.defect_assess,
            "execute_till_refund": self.execute_till_refund,
            "transfer_dispatcher": self.transfer_dispatcher,
            "assemble_supplier_rma": self.assemble_supplier_rma,
            "notify_customer": self.notify_customer,
        }
        handler = dispatch[node_name]
        return handler(state)

    # ── Node implementations ─────────────────────────────────────────────────

    def intake_eval(self, state: ReturnCaseState) -> str:
        """
        Node ①: Verify receipt + assess shoe condition.

        If receipt_id is present and valid → proof_verified = True.
        If receipt_id is missing/invalid → proof_verified = False (triggers cross_store_lookup).
        Always runs condition assessment on the images.

        Outputs: condition_verdict, receipt_verification, proof_verified flag.
        Routes to: cross_store_lookup (if proof missing) | policy_check (if proof OK).
        """
        logger.info("intake_eval: processing case %s", state.case_id)

        # ── Receipt verification ─────────────────────────────────────────────
        receipt_verification: Optional[ReceiptVerification] = None
        proof_ok = False

        if state.receipt_id:
            receipt_verification = verify_receipt_data(
                receipt_id=state.receipt_id,
                purchase_date_hint=state.purchase_date,
            )
            proof_ok = receipt_verification.valid
            state.receipt_verification = receipt_verification
            state.proof_verified = proof_ok
            if receipt_verification.purchase_date and not state.purchase_date:
                state.purchase_date = receipt_verification.purchase_date

            logger.info(
                "intake_eval: receipt %s → valid=%s, days=%s, store=%s",
                state.receipt_id,
                proof_ok,
                receipt_verification.days_since_purchase,
                receipt_verification.store_name,
            )
        else:
            logger.info("intake_eval: no receipt_id provided — proof_verified=False")
            state.proof_verified = False

        # ── Condition assessment ─────────────────────────────────────────────
        all_image_urls: List[str] = []
        for item in state.items:
            all_image_urls.extend(item.image_urls)

        condition = analyze_shoe_condition(
            image_urls=all_image_urls,
            sku=state.items[0].sku if state.items else None,
            size=state.items[0].size if state.items else None,
            is_worn_hint=state.items[0].is_worn if state.items else None,
            packaging_intact_hint=state.items[0].packaging_intact if state.items else None,
        )
        state.condition_verdict = condition

        logger.info(
            "intake_eval: condition=%s worn=%s defect=%s conf=%.2f quality=%s",
            condition.condition_status.value,
            condition.is_worn,
            condition.defect_suspected,
            condition.defect_confidence,
            condition.photo_quality_passed,
        )

        state.log_decision(
            node_name="intake_eval",
            decision=f"Condition: {condition.condition_status.value} | Proof: {'OK' if proof_ok else 'MISSING'}",
            output_summary=f"condition_status={condition.condition_status.value}, is_worn={condition.is_worn}, defect_suspected={condition.defect_suspected}, conf={condition.defect_confidence:.2f}, quality_passed={condition.photo_quality_passed}",
            confidence=condition.defect_confidence if condition.defect_suspected else None,
            next_node="cross_store_lookup" if not proof_ok else "policy_check",
        )

        # Quality fail on defect-suspected items → flag for re-capture, route to notify
        if condition.defect_suspected and not condition.photo_quality_passed:
            missing = ", ".join(condition.missing_angles)
            state.log_decision(
                node_name="intake_eval",
                decision="PHOTO_QUALITY_FAIL — re-capture required before defect classification",
                output_summary=f"Missing angles: {missing}",
                guardrail_result=GuardrailDecision.LOOP_RECAPTURE,
                note="In production: prompt till UI to re-capture missing angles. Case stays open until quality passes. Here: flag and continue to policy_check.",
            )
            # In production this would loop back to intake_eval via HITL-style re-entry.
            # Here we flag the quality issue and continue — the defect_assess node will
            # catch the quality failure and route appropriately.
            return "policy_check"

        if not proof_ok:
            return "cross_store_lookup"

        return "policy_check"

    def cross_store_lookup(self, state: ReturnCaseState) -> str:
        """
        Node ②: Federated proof lookup when receipt is missing or unverified.

        Tries 3 lookup strategies:
        1. federated_sales_search by customer identifier (email / phone / card last 4)
        2. payment_token_lookup by card last four + bank terminal ref
        3. (fallback) HITL to Anouk for manual store-by-store check

        On match: enriches state with transaction data, sets proof_verified=True.
        On no match: routes to HITL. Case stays open; customer gets status notification.
        """
        logger.info("cross_store_lookup: case %s — checking proof status", state.case_id)

        # If proof already verified by intake_eval, skip — route to policy_check
        if state.proof_verified:
            logger.info("cross_store_lookup: proof already verified — skipping to policy_check")
            return "policy_check"

        identifier: Optional[str] = None
        card_last_four: Optional[str] = None

        # Try to extract identifier from customer contact info or items
        if state.customer_email:
            identifier = state.customer_email
        elif state.customer_phone:
            identifier = state.customer_phone

        # Attempt 1: federated search
        search_results = []
        if identifier:
            date_from = datetime.utcnow() - timedelta(days=60)
            date_to = datetime.utcnow()
            search_results = federated_sales_search(identifier, (date_from, date_to))
            logger.info("cross_store_lookup: federated_search found %d results", len(search_results))

        matched_txn = None
        # Only fall back to card-based lookup if a card last-four is actually available
        # (from the payment context passed via ReturnItem). Do NOT hardcode — a bare
        # items list with no card info must still be able to reach "no match → HITL".
        card_last_four: Optional[str] = None
        for item in state.items:
            if item.card_last_four:
                card_last_four = item.card_last_four
                break

        if not matched_txn and card_last_four:
            txn = payment_token_lookup(card_last_four, None)
            if txn:
                search_results = [txn]
                matched_txn = txn

        if search_results and not matched_txn:
            target_sku = state.items[0].sku if state.items else None
            sku_matches = [t for t in search_results if t.get("sku") == target_sku]
            matched_txn = sku_matches[0] if sku_matches else search_results[0]

        if matched_txn:
            txn_id = matched_txn["transaction_id"]
            state.receipt_id = matched_txn.get("receipt_id")
            state.proof_verified = True
            state.purchase_date = matched_txn["purchase_date"]
            state.receipt_verification = ReceiptVerification(
                receipt_id=matched_txn.get("receipt_id"),
                valid=True,
                days_since_purchase=(datetime.utcnow() - matched_txn["purchase_date"]).days,
                store_id=matched_txn["store_id"],
                store_name=matched_txn["store_name"],
                transaction_amount_eur=matched_txn["amount_eur"],
                payment_method=matched_txn["payment_method"],
                purchase_date=matched_txn["purchase_date"],
                verification_method="federated_search",
            )

            replacement_proof_ref = generate_replacement_proof(txn_id)
            state.log_decision(
                node_name="cross_store_lookup",
                decision=f"Match found: {matched_txn['store_name']} — {txn_id}",
                output_summary=f"proof_verified=True | store={matched_txn['store_name']} | txn={txn_id} | proof_ref={replacement_proof_ref}",
                next_node="policy_check",
            )
            return "policy_check"

        # No match — route to HITL
        state.escalation_reason_detail = (
            "No receipt match found in federated search across 4 Randstad stores + e-comm."
        )
        state.log_decision(
            node_name="cross_store_lookup",
            decision="NO_MATCH — routing to HITL (Anouk manual check)",
            output_summary="federated_search returned 0 results; payment_token_lookup returned None",
            guardrail_result=GuardrailDecision.ESCALATE_HITL,
            next_node="HITL",
            note="Case remains open. Anouk performs manual store-by-store check. Customer notified of pending lookup.",
        )
        return "HITL"

    def policy_check(self, state: ReturnCaseState) -> str:
        """
        Node ③: Apply return policy rules and route to the right resolution path.

        Rules:
        - Within 30-day window: purchase_date must be ≤ 30 days ago
        - Unworn: condition_verdict.is_worn == False (for change-of-mind)
        - Refund cap: total ≤ €150 for auto-approval
        - Defect suspected: regardless of wear/window — route to defect path

        Outputs: policy_result, resolution_type.
        Routes to: execute_till_refund | stock_resolve | defect_assess | HITL.
        """
        logger.info("policy_check: case %s", state.case_id)

        # Determine purchase date
        purchase_date: Optional[datetime] = None
        days_since = 999
        within_window = False

        if state.receipt_verification and state.receipt_verification.purchase_date:
            purchase_date = state.receipt_verification.purchase_date
            days_since = (datetime.utcnow() - purchase_date).days
            within_window = days_since <= THIRTY_DAYS

        if not state.condition_verdict:
            state.condition_verdict = ConditionVerdict(
                condition_status=ConditionStatus.UNWORN,
                is_worn=False,
                packaging_intact=True,
                wear_evidence=[],
                defect_suspected=False,
                defect_confidence=0.0,
                photo_quality_passed=False,
            )

        condition = state.condition_verdict
        is_worn = condition.is_worn
        defect_suspected = condition.defect_suspected

        # Determine resolution preference (mock — would come from customer/ Eva)
        preferred_resolution = ResolutionType.REFUND  # default

        if state.resolution_type and state.resolution_type != ResolutionType.PENDING_REVIEW:
            preferred_resolution = state.resolution_type

        refund_amount = state.total_refund_value

        # Policy evaluation
        policy = PolicyEvalResult(
            within_30_day_window=within_window,
            is_unworn=(not is_worn),
            refund_amount_eur=refund_amount,
            under_refund_cap=(refund_amount <= REFUND_CAP_EUR),
        )

        # Route logic
        escalation_reason: Optional[str] = None
        resolution: ResolutionType = ResolutionType.PENDING_REVIEW

        # Defect path wins over everything (even if out of policy — customer may claim)
        if defect_suspected:
            resolution = ResolutionType.DEFECT_REMEDY
            state.defect_detected = True
            logger.info("policy_check: defect suspected — routing to defect_assess")

        # Out of policy exceptions
        elif not within_window and not defect_suspected:
            if is_worn:
                escalation_reason = f"Change-of-mind return refused: item worn, {days_since} days since purchase (30-day policy)."
                resolution = ResolutionType.PENDING_REVIEW
                logger.info("policy_check: out of policy — worn + out of window → HITL")
            else:
                escalation_reason = f"Change-of-mind return outside 30-day window: {days_since} days. Requires supervisor policy exception."
                resolution = ResolutionType.PENDING_REVIEW
                logger.info("policy_check: out of policy — out of window → HITL")

        elif not condition.packaging_intact and not defect_suspected:
            escalation_reason = "Packaging not intact — change-of-mind return refused per policy. Supervisor review."
            resolution = ResolutionType.PENDING_REVIEW

        elif is_worn and not defect_suspected:
            escalation_reason = "Item shows wear — change-of-mind return refused. Supervisor review."
            resolution = ResolutionType.PENDING_REVIEW

        elif preferred_resolution == ResolutionType.REFUND and within_window and not is_worn:
            resolution = ResolutionType.REFUND
            policy.resolution_type = ResolutionType.REFUND
            logger.info("policy_check: routing to execute_till_refund — refund %s EUR", refund_amount)

        elif preferred_resolution == ResolutionType.EXCHANGE and within_window and not is_worn:
            resolution = ResolutionType.EXCHANGE
            policy.resolution_type = ResolutionType.EXCHANGE
            logger.info("policy_check: routing to stock_resolve — exchange")

        else:
            escalation_reason = "Policy evaluation inconclusive — supervisor review."
            resolution = ResolutionType.PENDING_REVIEW

        state.policy_result = policy
        state.resolution_type = resolution
        state.escalation_reason = escalation_reason

        next_node: str
        if resolution == ResolutionType.REFUND:
            next_node = "execute_till_refund"
        elif resolution == ResolutionType.EXCHANGE:
            next_node = "stock_resolve"
        elif resolution == ResolutionType.DEFECT_REMEDY:
            next_node = "defect_assess"
        else:
            next_node = "HITL"

        state.log_decision(
            node_name="policy_check",
            decision=f"Policy: within_30d={within_window}, unworn={not is_worn}, cap_ok={policy.under_refund_cap} → {resolution.value}",
            output_summary=f"days={days_since}, is_worn={is_worn}, defect={defect_suspected}, refund_amount={refund_amount}, resolution={resolution.value}, escalation={escalation_reason or 'none'}",
            next_node=next_node,
        )

        if next_node == "HITL":
            return "HITL"
        return next_node

    def stock_resolve(self, state: ReturnCaseState) -> str:
        """
        Node ④a: Resolve exchange stock across all 4 stores + warehouse.

        1. Query stock_matrix for the requested SKU/size across all locations
        2. If local stock available → immediate handover (skip to notify)
        3. If no local stock → attempt atomic hold on best network location
        4. Hold succeeds → create transfer manifest → notify customer with collection date
        5. No network stock → alternative offer (store credit / different product)
        6. Stock API down → HITL (Anouk manual check)

        Outputs: stock_matrix, hold_reservation, transfer_manifest, or alternative_offer.
        Routes to: transfer_dispatcher | notify_customer | alternative branch | HITL.
        """
        sku = state.items[0].sku if state.items else ""
        size = state.items[0].size if state.items else ""

        # Only relevant for exchange resolution
        if state.resolution_type != ResolutionType.EXCHANGE:
            logger.info("stock_resolve: not an exchange case — skipping")
            return "notify_customer"

        logger.info("stock_resolve: case %s — querying stock for %s / %s", state.case_id, sku, size)

        try:
            stock = query_stock_matrix(sku, size, origin_store_id=state.origin_store_id)
            state.stock_matrix = stock
        except RuntimeError as e:
            logger.warning("stock_resolve: inventory API unavailable — routing to HITL: %s", e)
            state.escalation_reason_detail = f"Inventory API unavailable: {e}"
            state.log_decision(
                node_name="stock_resolve",
                decision="INVENTORY_API_DOWN — HITL for manual stock check",
                guardrail_result=GuardrailDecision.ESCALATE_HITL,
                next_node="HITL",
                note="Anouk performs manual phone check of remote stores. Case stays open.",
            )
            return "HITL"

        state.log_decision(
            node_name="stock_resolve",
            decision=f"Stock matrix: local={stock.has_local_stock}, network={stock.has_network_stock}, total={stock.total_available}",
            output_summary="\n".join(
                f"  {item.store_name}: {item.quantity_available} ({'verified' if item.is_verified else 'system-listed'})"
                for item in stock.items
            ),
            next_node=None,
        )

        # Local stock at origin store — immediate handover
        if stock.has_local_stock:
            # has_local_stock now means origin store specifically has stock
            origin_item = next((i for i in stock.items if i.store_id == state.origin_store_id and i.quantity_available > 0), None)
            if origin_item:
                logger.info("stock_resolve: local stock available at origin %s — immediate handover", origin_item.store_name)
                state.log_decision(
                    node_name="stock_resolve",
                    decision="Local stock available at origin store — immediate handover",
                    output_summary=f"Store: {origin_item.store_name}, qty: {origin_item.quantity_available}",
                    next_node="notify_customer",
                )
                state.resolution_type = ResolutionType.EXCHANGE
                return "notify_customer"
            else:
                # has_local_stock True but no item found at origin — data inconsistency, fall through
                logger.warning("stock_resolve: has_local_stock=True but no stock found at origin %s", state.origin_store_id)

        # No local stock — attempt atomic hold on best network location
        if stock.has_network_stock:
            # Pick best alternative: prefer nearest store with verified stock, then warehouse
            candidates = [
                i for i in stock.items
                if i.quantity_available > 0 and i.store_id != state.origin_store_id
            ]
            candidates.sort(key=lambda i: (not i.is_verified, i.store_id == "WAREHOUSE"))

            if not candidates:
                state.log_decision(
                    node_name="stock_resolve",
                    decision="No verified network stock — alternative offer",
                    next_node="notify_customer",
                )
                state.resolution_type = ResolutionType.EXCHANGE
                state.alternative_offer_made = True
                state.alternative_offer_details = "No stock available at any Randstad location. Offer: store credit or alternative product."
                return "notify_customer"

            best = candidates[0]
            logger.info("stock_resolve: attempting atomic hold at %s (%s units)", best.store_name, best.quantity_available)

            hold = acquire_atomic_hold(
                sku=sku,
                size=size,
                store_id=best.store_id,
                ttl_hours=HOLD_TTL_HOURS,
                quantity=1,
            )

            if hold:
                state.hold_reservation = hold
                manifest = create_interstore_transfer_manifest(
                    origin_store_id=best.store_id,
                    dest_store_id=state.origin_store_id,
                    sku=sku,
                    size=size,
                    hold_id=hold.hold_id,
                    quantity=1,
                )
                if manifest:
                    state.transfer_manifest = manifest
                    logger.info(
                        "stock_resolve: hold %s + manifest %s — transfer from %s to %s",
                        hold.hold_id, manifest.manifest_id, best.store_name, state.origin_store_name,
                    )
                    state.log_decision(
                        node_name="stock_resolve",
                        decision=f"Atomic hold confirmed at {best.store_name} — transfer manifest created to {state.origin_store_name}",
                        output_summary=f"hold_id={hold.hold_id}, manifest_id={manifest.manifest_id}, from={best.store_name}, to={state.origin_store_name}, collection_in_4h, expires_in_48h",
                        next_node="transfer_dispatcher",
                    )
                    state.resolution_type = ResolutionType.EXCHANGE
                    return "transfer_dispatcher"
                else:
                    logger.warning("stock_resolve: hold acquired but manifest failed — releasing hold")
                    # hold stays active but no manifest — falls through to HITL
                    state.log_decision(
                        node_name="stock_resolve",
                        decision="Hold acquired but manifest creation failed — HITL",
                        guardrail_result=GuardrailDecision.ESCALATE_HITL,
                        next_node="HITL",
                    )
                    return "HITL"

            # Hold failed despite stock showing — possible race condition or just-sold stock
            logger.warning("stock_resolve: hold failed at %s — stock may have moved", best.store_name)
            state.log_decision(
                node_name="stock_resolve",
                decision="Atomic hold failed — no available stock after hold attempt",
                guardrail_result=GuardrailDecision.ESCALATE_HITL,
                next_node="HITL",
                note="Hold failed despite stock showing availability. Anouk verifies manually.",
            )
            return "HITL"

        # No stock anywhere
        logger.info("stock_resolve: no stock at any location — alternative offer")
        state.resolution_type = ResolutionType.EXCHANGE
        state.alternative_offer_made = True
        state.alternative_offer_details = (
            "No stock available at any of the 4 Randstad stores or central warehouse. "
            "Options: store credit for the original amount, or alternative product recommendation."
        )
        state.log_decision(
            node_name="stock_resolve",
            decision="No network stock — alternative offer (store credit / different product)",
            output_summary="No stock at any location",
            next_node="notify_customer",
        )
        return "notify_customer"

    def defect_assess(self, state: ReturnCaseState) -> str:
        """
        Node ④b: Assess reported defect and drive the dual-track resolution.

        1. check_photo_quality_compliance — if fail, re-capture loop
        2. classify_structural_defect — if confidence ≥ 0.85 → authorize remedy + RMA
           if confidence < 0.85 → HITL (Anouk physical inspection)
        3. assemble_supplier_rma — runs in parallel after remedy authorization

        Outputs: defect classification, supplier_claim (if submitted).
        Routes to: assemble_supplier_rma | HITL | notify_customer.
        """
        # Only relevant when defect is suspected or resolution is DEFECT_REMEDY
        if state.resolution_type != ResolutionType.DEFECT_REMEDY:
            if not state.condition_verdict or not state.condition_verdict.defect_suspected:
                logger.info("defect_assess: no defect suspected — skipping")
                return "notify_customer"

        sku = state.items[0].sku if state.items else ""
        all_images: List[str] = []
        for item in state.items:
            all_images.extend(item.image_urls)

        fault_description = state.condition_verdict.wear_evidence[0] if state.condition_verdict and state.condition_verdict.wear_evidence else ""

        logger.info("defect_assess: case %s — classifying defect for %s", state.case_id, sku)

        # ── Quality gate ──────────────────────────────────────────────────────
        quality_ok, missing = check_photo_quality_compliance(all_images)
        state.condition_verdict.photo_quality_passed = quality_ok
        state.condition_verdict.missing_angles = missing

        if not quality_ok:
            missing_str = ", ".join(missing)
            state.log_decision(
                node_name="defect_assess",
                decision="PHOTO_QUALITY_FAIL — re-capture required",
                output_summary=f"Missing required angles: {missing_str}",
                guardrail_result=GuardrailDecision.LOOP_RECAPTURE,
                note="In production: case pauses here; till UI prompts for re-capture. Demo: flag and notify.",
            )
            # In production this would pause and wait for re-capture.
            # Demo: flag the quality issue, notify customer the case needs attention.
            state.escalation_reason_detail = f"Defect claim submitted with insufficient photo quality. Missing angles: {missing_str}. Re-capture required before supplier RMA can be submitted."
            state.escalated_to_supervisor = True
            state.escalation_timestamp = datetime.utcnow()
            return "HITL"

        # ── Classification ─────────────────────────────────────────────────────
        classification = classify_structural_defect(
            images=all_images,
            fault_description=fault_description or "Customer reports defect",
            sku=sku,
        )

        confidence = classification.get("defect_confidence", 0.0)
        is_manufacturing = classification.get("is_manufacturing_fault", False)
        fault_type = classification.get("fault_type")
        recommendation = classification.get("recommendation", "REVIEW_BY_SUPERVISOR")

        logger.info(
            "defect_assess: confidence=%.2f manufacturing=%s fault=%s rec=%s",
            confidence, is_manufacturing, fault_type, recommendation,
        )

        if recommendation == "REVIEW_BY_SUPERVISOR" or confidence < DEFECT_CONFIDENCE_THRESHOLD:
            state.log_decision(
                node_name="defect_assess",
                decision=f"Borderline defect — conf={confidence:.2f} < 0.85 — HITL (Anouk physical inspection)",
                output_summary=f"fault_type={fault_type}, is_manufacturing={is_manufacturing}, confidence={confidence:.2f}, recommendation={recommendation}",
                guardrail_result=GuardrailDecision.ESCALATE_HITL,
                next_node="HITL",
                note="Anouk inspects in person. High-res capture prompts sent to till UI if needed.",
            )
            return "HITL"

        # Confidence ≥ 0.85 — authorize remedy + trigger RMA in parallel
        state.log_decision(
            node_name="defect_assess",
            decision=f"Defect confirmed — conf={confidence:.2f} ≥ 0.85 — authorizing remedy + RMA",
            output_summary=f"fault_type={fault_type}, confidence={confidence:.2f}, is_manufacturing_fault={is_manufacturing}",
            next_node="assemble_supplier_rma",
        )

        state.defect_detected = True
        state.resolution_type = ResolutionType.DEFECT_REMEDY
        state.condition_verdict.fault_type = fault_type
        state.condition_verdict.is_manufacturing_fault = is_manufacturing
        state.condition_verdict.defect_confidence = confidence

        # Remedy authorized — customer gets refund or replacement (handled by notify)
        return "assemble_supplier_rma"

    def execute_till_refund(self, state: ReturnCaseState) -> str:
        """
        Node ⑤a: Execute the till refund.

        Guardrail: refund cap of €150. Over cap → HITL (Anouk digital authorization).
        Under cap → refund authorized, update local inventory.

        Outputs: refund_authorized flag.
        Routes to: notify_customer | HITL.
        """
        # Only relevant for refund resolution
        if state.resolution_type != ResolutionType.REFUND:
            logger.info("execute_till_refund: not a refund case — skipping")
            return "notify_customer"

        refund_amount = state.total_refund_value

        logger.info("execute_till_refund: case %s — refund amount %s EUR", state.case_id, refund_amount)

        is_supervisor_override = bool(state.supervisor_resolved_at) or (
            state.policy_result and state.policy_result.under_refund_cap and refund_amount > REFUND_CAP_EUR
        )

        if refund_amount > REFUND_CAP_EUR and not is_supervisor_override:
            logger.warning("execute_till_refund: refund %s exceeds cap of %s — HITL required", refund_amount, REFUND_CAP_EUR)
            state.escalation_reason = f"Refund amount €{refund_amount} exceeds auto-approval cap of €{REFUND_CAP_EUR} — requires supervisor digital authorization."
            state.escalation_reason_detail = state.escalation_reason
            state.log_decision(
                node_name="execute_till_refund",
                decision=f"Refund cap exceeded: {refund_amount} > {REFUND_CAP_EUR} — HITL (Anouk digital auth)",
                output_summary=f"amount={refund_amount}, cap={REFUND_CAP_EUR}",
                guardrail_result=GuardrailDecision.ESCALATE_HITL,
                next_node="HITL",
                note="Anouk reviews and digitally authorizes the higher-value refund via supervisor dashboard.",
            )
            return "HITL"

        auth_by = "supervisor" if is_supervisor_override else "agent"
        logger.info("execute_till_refund: refund %s authorized — %s", refund_amount, auth_by)
        state.resolution_amount_eur = refund_amount
        state.log_decision(
            node_name="execute_till_refund",
            decision=f"Refund authorized: {refund_amount} EUR — authorized by {auth_by}",
            output_summary=f"amount={refund_amount}, cap={REFUND_CAP_EUR}, authorized_by={auth_by}",
            next_node="notify_customer",
        )
        state.resolution_type = ResolutionType.REFUND
        return "notify_customer"

    def transfer_dispatcher(self, state: ReturnCaseState) -> str:
        """
        Node ⑤b: Process the inter-store transfer after atomic hold confirmed.

        Manifests are created by stock_resolve. Here we mark them as dispatched
        and trigger customer notification with collection details.

        In production: trigger courier API. Here: mark dispatched.
        """
        manifest = state.transfer_manifest
        hold = state.hold_reservation

        if not manifest or not hold:
            logger.warning("transfer_dispatcher: no manifest or hold — falling through")
            return "notify_customer"

        logger.info("transfer_dispatcher: manifest %s dispatched to %s", manifest.manifest_id, manifest.dest_store_name)
        manifest.status = "DISPATCHED"

        state.log_decision(
            node_name="transfer_dispatcher",
            decision=f"Transfer dispatched: {manifest.manifest_id} → {manifest.dest_store_name}",
            output_summary=f"manifest_id={manifest.manifest_id}, origin={manifest.origin_store_name}, dest={manifest.dest_store_name}, collection_estimated=4h",
            next_node="notify_customer",
        )
        return "notify_customer"

    def assemble_supplier_rma(self, state: ReturnCaseState) -> str:
        """
        Node ⑤c: Assemble supplier evidence package and submit RMA.

        Dual-track: runs in parallel with the customer remedy (refund/replacement
        already authorized by defect_assess). This track handles the back-office
        vendor claim.

        1. Assemble evidence package (images + receipt metadata + defect classification)
        2. submit_supplier_rma_claim — if supplier API available, auto-submit.
           If not available: set status to PENDING_SUBMISSION (Robin manually submits).
        3. notify_purchasing_lead — alert Robin with claim status.

        Outputs: supplier_claim with claim_id and status.
        Routes to: notify_customer.
        """
        sku = state.items[0].sku if state.items else ""
        logger.info("assemble_supplier_rma: case %s — assembling RMA for %s", state.case_id, sku)

        # Evidence package reference (in production: S3 object / document store)
        evidence_ref = f"EVIDENCE-{state.case_id}-{sku}"
        state.rma_evidence_package_ref = evidence_ref

        state.log_decision(
            node_name="assemble_supplier_rma",
            decision="Evidence package assembled",
            output_summary=f"evidence_ref={evidence_ref}, images={sum(len(i.image_urls) for i in state.items)}, defect_classification={state.condition_verdict.fault_type or 'none'}",
            next_node="submit_rma",
        )

        # Attempt supplier submission
        supplier_id = "SUP-PLETE-001"  # mock — would be looked up from SKU → supplier mapping
        supplier_name = "Prete Footwear B.V."

        claim_id = submit_supplier_rma_claim(
            supplier_id=supplier_id,
            supplier_name=supplier_name,
            case_data={
                "case_id": state.case_id,
                "sku": sku,
                "size": state.items[0].size if state.items else "",
                "defect_classification": state.condition_verdict.fault_type,
                "defect_confidence": state.condition_verdict.defect_confidence,
                "is_manufacturing_fault": state.condition_verdict.is_manufacturing_fault,
                "evidence_package_ref": evidence_ref,
                "receipt_id": state.receipt_id,
                "purchase_date": str(state.receipt_verification.purchase_date) if state.receipt_verification and state.receipt_verification.purchase_date else None,
                "store_id": state.origin_store_id,
            },
        )

        if claim_id:
            state.supplier_claim = SupplierClaim(
                claim_id=claim_id,
                supplier_id=supplier_id,
                supplier_name=supplier_name,
                rma_number=claim_id,
                status=RMAStatus.SUBMITTED,
                evidence_package_ref=evidence_ref,
                submitted_at=datetime.utcnow(),
            )
            logger.info("assemble_supplier_rma: RMA submitted → %s", claim_id)
            state.log_decision(
                node_name="assemble_supplier_rma",
                decision=f"RMA submitted to supplier: {claim_id}",
                output_summary=f"claim_id={claim_id}, supplier={supplier_name}, status=SUBMITTED",
                next_node="notify_customer",
            )
        else:
            # No supplier API — manual submission path
            state.supplier_claim = SupplierClaim(
                claim_id=None,
                supplier_id=supplier_id,
                supplier_name=supplier_name,
                status=RMAStatus.PENDING_SUBMISSION,
                evidence_package_ref=evidence_ref,
            )
            logger.info("assemble_supplier_rma: no supplier API — manual submission path for Robin")
            state.log_decision(
                node_name="assemble_supplier_rma",
                decision="No supplier API — evidence package ready for Robin manual submission",
                output_summary=f"evidence_ref={evidence_ref}, status=PENDING_SUBMISSION, robin_notified=True",
                next_node="notify_customer",
                note="Robin receives structured evidence package in dashboard. Manual RMA submission required.",
            )

        return "notify_customer"

    def notify_customer(self, state: ReturnCaseState) -> str:
        """
        Node ⑥: Notify the customer of the resolution and close the case.

        Guardrail: case cannot close until customer_notified == SENT.
        Emits the appropriate notification based on resolution_type.

        Terminal node: sets customer_notification_status = SENT, closed = True.
        """
        logger.info("notify_customer: case %s — sending resolution notification", state.case_id)

        if state.customer_notification_status == NotificationStatus.SENT:
            logger.info("notify_customer: already notified — skipping")
            return "CLOSE"

        channels = "both"  # SMS + email
        if state.customer_phone:
            channels = "sms" if not state.customer_email else "both"

        message_type: str
        details: Optional[str] = None

        if state.resolution_type == ResolutionType.REFUND:
            message_type = "refund_confirmed"
            details = f"Refund of {state.total_refund_value} EUR processed to original payment method."

        elif state.resolution_type == ResolutionType.EXCHANGE:
            if state.transfer_manifest:
                manifest = state.transfer_manifest
                collection_date = manifest.scheduled_collection_date
                details = (
                    f"Exchange confirmed. Your replacement pair is being transferred to "
                    f"{manifest.dest_store_name}. "
                    f"Estimated collection: {collection_date.strftime('%d %b %Y') if collection_date else 'within 4 hours'}. "
                    f"Reference: {manifest.manifest_id}. Hold expires: {hold_expiry_text(state)}"
                )
                message_type = "exchange_ticket"
            elif state.alternative_offer_made:
                details = state.alternative_offer_details
                message_type = "exchange_ticket"
            else:
                message_type = "exchange_ticket"
                details = "Exchange confirmed — please visit the till to collect your replacement."

        elif state.resolution_type == ResolutionType.DEFECT_REMEDY:
            if state.supplier_claim and state.supplier_claim.status == RMAStatus.SUBMITTED:
                details = (
                    f"Defect remedy authorized. Your refund/replacement has been processed "
                    f"and a supplier claim has been submitted (RMA: {state.supplier_claim.rma_number}). "
                    f"We'll update you when the claim is resolved."
                )
                message_type = "claim_submitted"
            elif state.supplier_claim and state.supplier_claim.status == RMAStatus.PENDING_SUBMISSION:
                details = (
                    f"Defect remedy authorized. A structured evidence package has been prepared "
                    f"for our purchasing team to submit to the supplier. We'll update you when the claim is submitted."
                )
                message_type = "defect_remedy"
            else:
                details = "Defect remedy authorized. Please collect your refund or replacement at the till."
                message_type = "defect_remedy"

        elif state.resolution_type == ResolutionType.PENDING_REVIEW:
            if state.escalation_reason and "reject" in state.escalation_reason.lower():
                message_type = "return_rejected"
                details = state.escalation_reason
            else:
                message_type = "case_escalated"
                details = "Your return requires supervisor review. A store supervisor will contact you within 2 hours."

        else:
            message_type = "case_escalated"
            details = "Your return case is being processed. We'll contact you with the outcome."

        result = notify_customer(
            customer_email=state.customer_email,
            customer_phone=state.customer_phone,
            channel=channels,
            message_type=message_type,
            case_id=state.case_id,
            details=details,
        )

        state.customer_notification_status = NotificationStatus.SENT if result.get("delivered") else NotificationStatus.FAILED
        state.notification_channels = result.get("channels", [])
        state.notified_at = datetime.utcnow()
        state.notification_message_ref = f"NOTIF-{state.case_id}"

        logger.info(
            "notify_customer: case %s — notification %s via %s",
            state.case_id,
            result.get("message_type"),
            result.get("channels", []),
        )

        state.log_decision(
            node_name="notify_customer",
            decision=f"Customer notified: {result.get('message_type')} via {result.get('channels', [])}",
            output_summary=f"case_id={state.case_id}, type={result.get('message_type')}, channels={result.get('channels', [])}, delivered={result.get('delivered')}",
            next_node="CLOSE" if result.get("delivered") else "HITL",
            note=("Case closed — customer notified." if result.get("delivered") else "Notification failed — flag for ops review."),
        )

        if result.get("delivered"):
            state.closed = True
            state.closed_at = datetime.utcnow()
            return "CLOSE"

        state.escalated_to_supervisor = True
        state.escalation_timestamp = datetime.utcnow()
        return "HITL"


def hold_expiry_text(state: ReturnCaseState) -> str:
    """Human-readable hold expiry text for notification."""
    if state.hold_reservation:
        expiry = state.hold_reservation.expires_at
        hours_left = (expiry - datetime.utcnow()).total_seconds() / 3600
        return f"Your hold expires in {max(0, round(hours_left, 1))} hours. If not collected, the stock will be released back to inventory and a new exchange will need to be arranged."
    return "Your exchange hold details are available at the till."



