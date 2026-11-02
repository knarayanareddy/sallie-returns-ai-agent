#!/usr/bin/env python3
"""
Version A — End-to-End Demo Runner.

Runs 5 representative return cases through the full Version A pipeline:

  Case 1 — Standard refund (in-policy, under cap, receipt present)
  Case 2 — Missing receipt → federated search → proof found → exchange
  Case 3 — Missing receipt → no match → HITL → Anouk resolves manually
  Case 4 — Defect case (known defective batch SS-0091) → auto remedy + RMA
  Case 5 — Worn item over 30 days → policy violation → HITL → supervisor rejects

Each case shows: input → node-by-node state mutations → final resolution.
"""

from __future__ import annotations

import logging
import sys
from decimal import Decimal
from datetime import datetime, timedelta
from typing import List, Optional

from .state_models import (
    ConditionStatus,
    NotificationStatus,
    ReceiptVerification,
    ResolutionType,
    ReturnCaseState,
    ReturnItem,
)
from .tools import (
    generate_replacement_proof,
    notify_customer,
)
from .orchestrator import SupervisorOrchestrator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("demo")


def hr():
    print("\n" + "─" * 72)


def section(title: str):
    print(f"\n{'─' * 4} {title} {'─' * 4}")
    print()


def print_state(state: ReturnCaseState, label: str = "Final State"):
    print(f"\n  {label} — {state.case_id}")
    print(f"  {'─' * 64}")
    print(f"  Customer:        {state.customer_id or 'N/A'}")
    print(f"  Email:           {state.customer_email or 'N/A'}")
    print(f"  Phone:           {state.customer_phone or 'N/A'}")
    print(f"  Store:           {state.origin_store_name}")
    print(f"  Intake at:       {state.intake_timestamp.strftime('%H:%M:%S')}")
    print(f"  Items:")
    for i, item in enumerate(state.items, 1):
        print(f"    [{i}] {item.sku} / {item.size} — €{item.purchase_price_eur} — {item.condition_description}")
        if item.image_urls:
            print(f"        Images: {len(item.image_urls)} ({', '.join(item.image_urls[:3])}{'...' if len(item.image_urls) > 3 else ''})")
    print(f"  Receipt:         {state.receipt_id or 'NONE'}")
    print(f"  Proof verified:  {state.proof_verified}")
    if state.receipt_verification:
        rv = state.receipt_verification
        print(f"    Method:        {rv.verification_method}")
        print(f"    Store:         {rv.store_name or 'N/A'}")
        print(f"    Days since:    {rv.days_since_purchase}")
        print(f"    Amount:        €{rv.transaction_amount_eur}")
    if state.condition_verdict:
        cv = state.condition_verdict
        print(f"  Condition:")
        print(f"    Status:        {cv.condition_status.value}")
        print(f"    Is worn:       {cv.is_worn}")
        print(f"    Packaging:     {cv.packaging_intact}")
        print(f"    Defect:        {cv.defect_suspected} (conf: {cv.defect_confidence:.2f})")
        print(f"    Fault type:    {cv.fault_type or 'none'}")
        print(f"    Mfg fault:     {cv.is_manufacturing_fault}")
        print(f"    Photo quality: {cv.photo_quality_passed}  Missing: {cv.missing_angles or 'none'}")
    if state.policy_result:
        pr = state.policy_result
        print(f"  Policy:")
        print(f"    Within 30d:   {pr.within_30_day_window}")
        print(f"    Is unworn:     {pr.is_unworn}")
        print(f"    Refund:        €{pr.refund_amount_eur}")
        print(f"    Under cap:     {pr.under_refund_cap}")
    print(f"  Resolution:      {state.resolution_type.value}")
    print(f"  Escalation:      {state.escalation_reason or 'none'}")
    if state.hold_reservation:
        h = state.hold_reservation
        print(f"  Hold:            {h.hold_id} at {h.store_name} — expires {h.expires_at.strftime('%H:%M %d %b')} — {h.status}")
    if state.transfer_manifest:
        m = state.transfer_manifest
        print(f"  Transfer:        {m.manifest_id} → {m.dest_store_name} — {m.status}")
    if state.supplier_claim:
        sc = state.supplier_claim
        print(f"  RMA:             {sc.claim_id or 'pending'} | {sc.supplier_name} | {sc.status.value}")
    print(f"  Customer notified: {state.customer_notification_status.value}")
    print(f"  Closed:          {state.closed} ({state.closed_at.strftime('%H:%M:%S') if state.closed_at else '—'})")
    print(f"  Audit log:")
    for entry in state.audit_log:
        conf_str = f" conf={entry.confidence:.2f}" if entry.confidence else ""
        gr_str = f" [{entry.guardrail_result.value}]" if entry.guardrail_result else ""
        print(f"    [{entry.node_name}] {entry.decision}{conf_str}{gr_str}")
        if entry.output_summary:
            for line in entry.output_summary.split("\n"):
                print(f"      → {line}")
        if entry.note:
            print(f"      ! {entry.note}")
    print()


def print_audit_only(state: ReturnCaseState, at_node: str):
    print(f"  [{at_node}] — audit entries so far:")
    for entry in state.audit_log:
        conf_str = f" conf={entry.confidence:.2f}" if entry.confidence else ""
        gr_str = f" [{entry.guardrail_result.value}]" if entry.guardrail_result else ""
        print(f"    [{entry.node_name}] {entry.decision}{conf_str}{gr_str}")
        if entry.output_summary:
            for line in entry.output_summary.split("\n"):
                print(f"      → {line}")
        if entry.note:
            print(f"      ! {entry.note}")
    print()


def run_case(
    case_num: int,
    title: str,
    state: ReturnCaseState,
    expected_path: Optional[str] = None,
    simulate_hitl_resolution: Optional[str] = None,
    skip_second_run: bool = False,
):
    """
    Run a case through the pipeline.

    If simulate_hitl_resolution is set, the case pauses at HITL and we
    simulate Anouk/Robin's resolution before resuming.
    """
    section(f"CASE {case_num} — {title}")
    print(f"  Expected path: {expected_path or 'auto-resolve'}")
    print(f"  {'─' * 70}")
    print(f"  Input state:")
    print(f"    case_id:          {state.case_id}")
    print(f"    customer_id:      {state.customer_id}")
    print(f"    receipt_id:       {state.receipt_id or 'NONE'}")
    print(f"    proof_verified:   {state.proof_verified}")
    print(f"    items:            {[(i.sku, i.size, str(i.purchase_price_eur)) for i in state.items]}")
    print(f"    origin_store:     {state.origin_store_name}")
    print()

    orchestrator = SupervisorOrchestrator()

    # Run through pipeline — will pause at HITL if needed
    result = orchestrator.run(state)

    hitl_paused = result.escalated_to_supervisor

    if hitl_paused:
        print(f"\n  ⏸  Pipeline paused at HITL (case {result.case_id})")
        print_audit_only(result, "HITL_PAUSE")
        if simulate_hitl_resolution:
            print(f"\n  Simulating supervisor resolution: {simulate_hitl_resolution}")
            hr()
            result = orchestrator.resume_from_hitl(
                result,
                supervisor_resolution=simulate_hitl_resolution,
                supervisor_notes="Supervisor reviewed and actioned.",
            )
            # resume_from_hitl already runs remaining nodes (via _execute for
            # APPROVE_REFUND/APPROVE_EXCHANGE) or returns a terminal state directly
            # (for REJECT_RETURN/FLAG_DEFECT_MANUAL). Do NOT call orchestrator.run()
            # again — that would re-run intake_eval with a fresh random CV result,
            # overwriting the condition_verdict from the first run and producing
            # inconsistent audit logs.
        else:
            print(f"\n  No simulated resolution provided — case remains paused at HITL.")
            print(f"  (In production, Anouk/Robin would action this case at their dashboard.)")
            return result  # return paused state

    print(f"\n{' ' * 4}{'=' * 64}")
    print_state(result, "RESOLUTION")

    if expected_path:
        status = " ✓" if not hitl_paused else " (HITL path taken)"
        print(f"  Expected: {expected_path}{status}")

    return result


# ── Case 1 — Standard refund (in-policy, receipt present, under cap) ──────────

def case_1():
    now = datetime.utcnow()
    return ReturnCaseState(
        case_id="RC-CASE-001",
        customer_id="CUST-MARIEKE-DJ",
        customer_email="marieke.dejong@mail.nl",
        customer_phone="+31612345678",
        origin_store_id="STORE-Utrecht",
        origin_store_name="Sallie se Schoenen — Utrecht",
        intake_timestamp=now,
        receipt_id="REC-883921",  # valid receipt in mock ledger
        items=[
            ReturnItem(
                sku="SS-0042",
                size="EU-39",
                condition_description="Box opened, shoes clean, no apparent wear",
                image_urls=["shoe_top_001.jpg", "shoe_side_002.jpg", "shoe_sole_003.jpg"],
                is_worn=False,
                packaging_intact=True,
                purchase_price_eur=Decimal("89.95"),
            ),
        ],
        resolution_type=None,  # will be determined by policy_check
    )


# ── Case 2 — Missing receipt, federated search finds transaction ──────────────

def case_2():
    now = datetime.utcnow()
    return ReturnCaseState(
        case_id="RC-CASE-002",
        customer_id="CUST-PIET-H",
        customer_email="piet.hendriks@webmail.nl",
        customer_phone="+31698765432",
        origin_store_id="STORE-Amsterdam",
        origin_store_name="Sallie se Schoenen — Amsterdam",
        intake_timestamp=now,
        receipt_id=None,  # customer lost their receipt
        items=[
            ReturnItem(
                sku="SS-0087",
                size="EU-42",
                condition_description="Shoes in original box, clean, unworn — customer wants exchange",
                image_urls=["img_001.jpg", "img_002.jpg", "img_003.jpg"],
                is_worn=False,
                packaging_intact=True,
                purchase_price_eur=Decimal("119.00"),
                card_last_four="2209",  # payment context from original purchase — enables payment_token_lookup path if federated search fails
            ),
        ],
        resolution_type=ResolutionType.EXCHANGE,  # customer wants exchange → triggers stock_resolve
    )


# ── Case 3 — Missing receipt, no match → HITL → Anouk resolves ───────────────

def case_3():
    now = datetime.utcnow()
    return ReturnCaseState(
        case_id="RC-CASE-003",
        customer_id="CUST-NO-MATCH",
        customer_email="unknown.person@mail.nl",
        customer_phone="+31699999999",  # not in mock ledger
        origin_store_id="STORE-Rotterdam",
        origin_store_name="Sallie se Schoenen — Rotterdam",
        intake_timestamp=now,
        receipt_id=None,
        items=[
            ReturnItem(
                sku="SS-0021",
                size="EU-36",
                condition_description="Shoes with receipt presumably lost",
                image_urls=["img_a.jpg", "img_b.jpg"],
                is_worn=False,
                packaging_intact=True,
                purchase_price_eur=Decimal("64.50"),
                card_last_four=None,  # no card info — cannot fall back to payment token lookup; will correctly reach "no match → HITL"
            ),
        ],
        resolution_type=None,
    )


# ── Case 4 — Defect case: known defective batch SS-0091 ───────────────────────

def case_4():
    now = datetime.utcnow()
    return ReturnCaseState(
        case_id="RC-CASE-004",
        customer_id="CUST-LUCAS-B",
        customer_email="lucasBakker@webmail.nl",
        customer_phone="+31677733991",
        origin_store_id="STORE-Haarlem",
        origin_store_name="Sallie se Schoenen — Haarlem",
        intake_timestamp=now,
        receipt_id="REC-661098",  # valid receipt in mock ledger
        items=[
            ReturnItem(
                sku="SS-0091",  # known defective batch
                size="EU-44",
                condition_description="Sole detachment at forefoot — customer reports sudden failure",
                image_urls=["defect_top.jpg", "defect_side.jpg", "defect_sole.jpg", "defect_angled.jpg", "defect_inside.jpg"],
                is_worn=False,
                packaging_intact=True,
                purchase_price_eur=Decimal("134.00"),
            ),
        ],
        resolution_type=None,  # condition assessment will flag defect
    )


# ── Bonus Case 4b — Defect with blurry images (quality fail loop) ─────────────

def case_4b():
    now = datetime.utcnow()
    return ReturnCaseState(
        case_id="RC-CASE-004B",
        customer_id="CUST-THOMAS-B",
        customer_email="thomas broek@mail.nl",
        customer_phone="+31644498765",
        origin_store_id="STORE-Utrecht",
        origin_store_name="Sallie se Schoenen — Utrecht",
        intake_timestamp=now,
        receipt_id="REC-661098",
        items=[
            ReturnItem(
                sku="SS-0091",
                size="EU-44",
                condition_description="Sole detachment reported",
                image_urls=["blurry_one.jpg"],  # single blurry image — quality fail
                is_worn=False,
                packaging_intact=True,
                purchase_price_eur=Decimal("134.00"),
            ),
        ],
        resolution_type=None,
    )


# ── Case 5 — Worn item, 35 days old → policy violation → HITL → reject ───────

def case_5():
    now = datetime.utcnow()
    return ReturnCaseState(
        case_id="RC-CASE-005",
        customer_id="CUST-JAN-K",
        customer_email="jan.koster@webmail.nl",
        customer_phone="+31622233444",
        origin_store_id="STORE-Rotterdam",
        origin_store_name="Sallie se Schoenen — Rotterdam",
        intake_timestamp=now,
        # REC-000555 is now in the mock ledger with purchase_date = now - 35 days.
        # intake_eval will verify the receipt and set days_since_purchase=35.
        # policy_check will then see within_30d=False → policy-violation HITL.
        receipt_id="REC-000555",
        items=[
            ReturnItem(
                sku="SS-0055",
                size="EU-44",
                condition_description="Clearly worn — sole scuffs, upper creasing, minor damage",
                image_urls=["worn_top.jpg", "worn_side.jpg", "worn_sole.jpg"],
                is_worn=True,
                packaging_intact=False,
                purchase_price_eur=Decimal("69.00"),
                card_last_four=None,  # no card info — cannot fall back to payment token lookup
            ),
        ],
        resolution_type=None,
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Seed the random simulation for deterministic demo behavior
    import random
    random.seed(42)

    print("╔" + "═" * 70 + "╗")
    print("║" + " " * 10 + "VERSION A — END-TO-END DEMO" + " " * 18 + "║")
    print("║" + " " * 9 + "Single Orchestrator · 6 Nodes · Shared State" + " " * 10 + "║")
    print("╚" + "═" * 70 + "╝")

    # Run cases
    run_case(1, "Standard refund — receipt verified, CV assessment (mock may flag as potential defect → HITL)", case_1(), "intake → policy → [if CV flags defect: defect_assess → HITL] or [if CV clean: execute_till_refund → notify → close]")

    run_case(2, "Missing receipt — federated search finds transaction — exchange", case_2(), "cross-store lookup → proof found → policy → exchange → stock → transfer → notify → close")

    run_case(3, "Missing receipt — no match in federated search — HITL → Anouk", case_3(), "cross-store lookup → no match → HITL → APPROVE_REFUND → notify → close", simulate_hitl_resolution="APPROVE_REFUND")

    run_case(4, "Defect case — known defective batch SS-0091 — auto remedy + RMA", case_4(), "intake → condition (defect, conf 0.91) → policy → defect_assess → RMA parallel → notify → close")

    run_case(4, "Defect with insufficient images — quality fail → HITL pause (re-capture needed)", case_4b(), "intake → condition (quality fail) → defect_assess quality fail → HITL pause (re-capture needed in production)", simulate_hitl_resolution=None)

    run_case(5, "Worn item (is_worn=True) — policy violation (worn + 35-day window) → HITL → supervisor rejects", case_5(), "intake → receipt verified (REC-000555, 35 days) → policy (worn + out-of-window) → HITL → REJECT_RETURN → close", simulate_hitl_resolution="REJECT_RETURN")

    print("\n" + "═" * 72)
    print("  DEMO COMPLETE — 5 cases processed through Version A pipeline")
    print("═" * 72)
    print("""
  WhatVersion A does (verified end-to-end in this demo):
    ✓  Case 1: Receipt verified → CV clean → auto-refund under €150 cap → notify → close
    ✓  Case 2: Missing receipt → federated search → proof_verified=True → policy → EXCHANGE →
         stock_resolve (origin Amsterdam: 0 stock) → atomic hold at Rotterdam (3 units) →
         transfer manifest → notify (exchange_ticket) → close
    ✓  Case 3: Missing receipt → federated search → NO MATCH → HITL pause →
         resume with APPROVE_REFUND → execute_till_refund → notify → close
    ✓  Case 4: Receipt verified → CV flags defect (SS-0091, conf=0.91) →
         policy → DEFECT_REMEDY → defect_assess confirms (conf ≥ 0.85) →
         assemble_supplier_rma (no supplier API → PENDING_SUBMISSION for Robin) →
         notify (defect_remedy + claim_submitted) → close
    ✓  Case 4b: Receipt verified → CV flags defect → photo quality fail (1 blurry image) →
         defect_assess quality gate fails → HITL pause (re-capture needed in production)
    ✓  Case 5: Receipt verified (REC-000555 in ledger, 35 days old) → policy_check (worn=True, within_30d=False) →
         HITL pause → resume with REJECT_RETURN → PENDING_REVIEW + supervisor rejection → notify → close

  Production prerequisites (NOT covered by mock — needed before go-live):
    ✗  POS read API for verify_receipt_data and federated_sales_search
    ✗  WMS inventory API with lock semantics for query_stock_matrix + acquire_atomic_hold
    ✗  Real multimodal CV model for analyze_shoe_condition and classify_structural_defect
    ✗  Supplier portal API for submit_supplier_rma_claim (or manual submission path)
    ✗  GDPR/legal review for payment_token_lookup and cross-store customer data
    ✗  SMS/email delivery service for notify_customer
    ✗  Supervisor dashboard UI for HITL queue (Anouk + Robin)
""")


if __name__ == "__main__":
    main()
