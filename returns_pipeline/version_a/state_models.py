"""
Version A — ReturnCaseState & supporting types.

Single shared source of truth for every return case in the pipeline.
All six nodes read and write to this state. No separate agent runtimes.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


# ── Enums ──────────────────────────────────────────────────────────────────────

class ResolutionType(str, Enum):
    REFUND = "REFUND"
    EXCHANGE = "EXCHANGE"
    DEFECT_REMEDY = "DEFECT_REMEDY"
    PENDING_REVIEW = "PENDING_REVIEW"


class ConditionStatus(str, Enum):
    UNWORN = "UNWORN"
    LIGHT_WEAR = "LIGHT_WEAR"
    SIGNIFICANT_WEAR = "SIGNIFICANT_WEAR"
    DEFECTIVE = "DEFECTIVE"


class NotificationStatus(str, Enum):
    NOT_SENT = "NOT_SENT"
    SENT = "SENT"
    FAILED = "FAILED"


class RMAStatus(str, Enum):
    NOT_SUBMITTED = "NOT_SUBMITTED"
    ASSEMBLING = "ASSEMBLING"
    SUBMITTED = "SUBMITTED"
    PENDING_SUBMISSION = "PENDING_SUBMISSION"  # manual submit path (no supplier API)
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class DeadLetterReason(str, Enum):
    NONE = "NONE"
    LOCK_EXPIRED = "LOCK_EXPIRED"
    TRANSFER_FAILED = "TRANSFER_FAILED"
    CUSTOMER_ABANDONED = "CUSTOMER_ABANDONED"


class GuardrailDecision(str, Enum):
    PASSED = "PASSED"
    ESCALATE_HITL = "ESCALATE_HITL"
    BLOCK_CLOSE = "BLOCK_CLOSE"
    LOOP_RECAPTURE = "LOOP_RECAPTURE"


# ── Supporting types ───────────────────────────────────────────────────────────

class ReturnItem(BaseModel):
    sku: str
    size: str
    condition_description: str = ""
    image_urls: List[str] = Field(default_factory=list)
    is_worn: bool = False
    packaging_intact: bool = True
    purchase_price_eur: Decimal = Decimal("0.00")
    card_last_four: Optional[str] = None  # from payment context; enables payment_token_lookup path


class ConditionVerdict(BaseModel):
    """Structured output from the CV condition assessment."""

    condition_status: ConditionStatus
    is_worn: bool
    packaging_intact: bool
    wear_evidence: List[str] = Field(default_factory=list)  # human-readable findings
    defect_suspected: bool = False
    defect_confidence: float = 0.0
    is_manufacturing_fault: bool = False
    fault_type: Optional[str] = None  # e.g. "SOLE_DETACHMENT", "STITCH_SPLIT", "LEATHER_PEELING"
    photo_quality_passed: bool = False
    missing_angles: List[str] = Field(default_factory=list)
    cv_model_version: str = "unknown"


class ReceiptVerification(BaseModel):
    receipt_id: Optional[str] = None
    valid: bool = False
    days_since_purchase: int = 999
    store_id: Optional[str] = None
    store_name: Optional[str] = None
    transaction_amount_eur: Decimal = Decimal("0.00")
    payment_method: Optional[str] = None
    purchase_date: Optional[datetime] = None
    verification_method: str = "none"  # "receipt_scanned" | "federated_search" | "payment_token" | "manual"


class PolicyEvalResult(BaseModel):
    within_30_day_window: bool
    is_unworn: bool
    refund_amount_eur: Decimal = Decimal("0.00")
    under_refund_cap: bool = True
    resolution_type: ResolutionType = ResolutionType.PENDING_REVIEW
    escalation_reason: Optional[str] = None


class StockMatrixItem(BaseModel):
    store_id: str
    store_name: str
    quantity_available: int = 0
    is_verified: bool = False  # physically verified vs. system-listed


class StockMatrix(BaseModel):
    sku: str
    size: str
    items: List[StockMatrixItem] = Field(default_factory=list)
    total_available: int = 0
    has_local_stock: bool = False
    has_network_stock: bool = False


class AtomicHold(BaseModel):
    hold_id: str
    sku: str
    size: str
    store_id: str
    store_name: str
    quantity: int = 1
    ttl_seconds: int = 48 * 3600
    acquired_at: datetime
    expires_at: datetime
    status: str = "ACTIVE"  # ACTIVE | EXPIRED | CLAIMED | RELEASED


class TransferManifest(BaseModel):
    manifest_id: str
    origin_store_id: str
    origin_store_name: str
    dest_store_id: str
    dest_store_name: str
    sku: str
    size: str
    quantity: int = 1
    hold_id: str
    status: str = "CREATED"  # CREATED | DISPATCHED | DELIVERED | EXPIRED
    scheduled_collection_date: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SupplierClaim(BaseModel):
    claim_id: Optional[str] = None
    supplier_id: str
    supplier_name: str
    rma_number: Optional[str] = None
    status: RMAStatus = RMAStatus.NOT_SUBMITTED
    evidence_package_ref: Optional[str] = None
    submitted_at: Optional[datetime] = None
    approved_at: Optional[datetime] = None
    credit_eur: Optional[Decimal] = None


class AgentDecisionLogEntry(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    node_name: str
    decision: str
    output_summary: str = ""
    confidence: Optional[float] = None
    guardrail_result: Optional[GuardrailDecision] = None
    next_node: Optional[str] = None
    note: Optional[str] = None


# ── Core state ─────────────────────────────────────────────────────────────────

class ReturnCaseState(BaseModel):
    """
    Version A single-writer state.
    Mutated sequentially by the six orchestrator nodes.
    Case cannot close until customer_notified == SENT.
    """

    # Identity — set at intake
    case_id: str = Field(default_factory=lambda: f"RC-{uuid4().hex[:8].upper()}")
    customer_id: Optional[str] = None
    customer_email: Optional[str] = None
    customer_phone: Optional[str] = None
    origin_store_id: str = ""
    origin_store_name: str = ""
    intake_timestamp: datetime = Field(default_factory=datetime.utcnow)
    purchase_date: Optional[datetime] = None  # manually provided if receipt not yet verified

    # Items
    items: List[ReturnItem] = Field(default_factory=list)

    # Receipt / proof
    receipt_id: Optional[str] = None
    proof_verified: bool = False
    receipt_verification: Optional[ReceiptVerification] = None

    # Condition assessment
    condition_verdict: Optional[ConditionVerdict] = None

    # Policy evaluation
    policy_result: Optional[PolicyEvalResult] = None

    # Resolution routing
    resolution_type: Optional[ResolutionType] = None
    resolution_amount_eur: Decimal = Decimal("0.00")
    escalation_reason: Optional[str] = None

    # Stock / logistics (exchange path)
    stock_matrix: Optional[StockMatrix] = None
    hold_reservation: Optional[AtomicHold] = None
    transfer_manifest: Optional[TransferManifest] = None
    alternative_offer_made: bool = False
    alternative_offer_details: Optional[str] = None

    # Defect / RMA (defect path)
    defect_detected: bool = False
    supplier_claim: Optional[SupplierClaim] = None
    rma_evidence_package_ref: Optional[str] = None

    # HITL
    escalated_to_supervisor: bool = False
    escalation_timestamp: Optional[datetime] = None
    escalation_reason_detail: Optional[str] = None
    supervisor_notes: Optional[str] = None
    supervisor_resolved_at: Optional[datetime] = None

    # Notification
    customer_notification_status: NotificationStatus = NotificationStatus.NOT_SENT
    notification_channels: List[str] = Field(default_factory=list)
    notified_at: Optional[datetime] = None
    notification_message_ref: Optional[str] = None

    # Dead letter
    dead_letter_reason: DeadLetterReason = DeadLetterReason.NONE
    dead_letter_timestamp: Optional[datetime] = None

    # Lifecycle
    closed: bool = False
    closed_at: Optional[datetime] = None

    # Audit
    audit_log: List[AgentDecisionLogEntry] = Field(default_factory=list)

    # ── Convenience ──────────────────────────────────────────────────────────

    @property
    def total_refund_value(self) -> Decimal:
        return sum((item.purchase_price_eur for item in self.items), Decimal("0.00"))

    @property
    def is_within_30_days(self) -> bool:
        if self.policy_result:
            return self.policy_result.within_30_day_window
        return False

    @property
    def has_active_hold(self) -> bool:
        if self.hold_reservation and self.hold_reservation.status == "ACTIVE":
            return True
        return False

    @property
    def active_hold_expires_in_seconds(self) -> int:
        if self.hold_reservation and self.hold_reservation.status == "ACTIVE":
            remaining = (self.hold_reservation.expires_at - datetime.utcnow()).total_seconds()
            return max(0, int(remaining))
        return 0

    def log_decision(
        self,
        node_name: str,
        decision: str,
        output_summary: str = "",
        confidence: Optional[float] = None,
        guardrail_result: Optional[GuardrailDecision] = None,
        next_node: Optional[str] = None,
        note: Optional[str] = None,
    ) -> None:
        """Append an audit entry — called by every node after its decision."""
        entry = AgentDecisionLogEntry(
            node_name=node_name,
            decision=decision,
            output_summary=output_summary,
            confidence=confidence,
            guardrail_result=guardrail_result,
            next_node=next_node,
            note=note,
        )
        self.audit_log.append(entry)

    def serialize_for_supervisor(self) -> dict:
        """Get a clean dict for the supervisor dashboard."""
        return {
            "case_id": self.case_id,
            "customer_id": self.customer_id,
            "origin_store": self.origin_store_name,
            "items_summary": [
                {"sku": i.sku, "size": i.size, "price": str(i.purchase_price_eur), "images": len(i.image_urls)}
                for i in self.items
            ],
            "proof_verified": self.proof_verified,
            "receipt_id": self.receipt_id,
            "condition_summary": (
                {
                    "status": self.condition_verdict.condition_status.value if self.condition_verdict else "NOT_ASSESSED",
                    "is_worn": self.condition_verdict.is_worn if self.condition_verdict else None,
                    "defect_suspected": self.condition_verdict.defect_suspected if self.condition_verdict else None,
                    "confidence": self.condition_verdict.defect_confidence if self.condition_verdict else None,
                    "fault_type": self.condition_verdict.fault_type if self.condition_verdict else None,
                    "photo_quality_passed": self.condition_verdict.photo_quality_passed if self.condition_verdict else None,
                }
            ),
            "policy_result": (
                {
                    "within_30_days": self.policy_result.within_30_day_window if self.policy_result else None,
                    "is_unworn": self.policy_result.is_unworn if self.policy_result else None,
                    "resolution_type": self.policy_result.resolution_type.value if self.policy_result else None,
                    "escalation_reason": self.policy_result.escalation_reason if self.policy_result else None,
                    "refund_amount_eur": str(self.policy_result.refund_amount_eur) if self.policy_result else None,
                }
            ),
            "resolution_type": self.resolution_type.value,
            "hold_active": self.has_active_hold,
            "hold_expires_in_hours": round(self.active_hold_expires_in_seconds / 3600, 1) if self.has_active_hold else 0,
            "supplier_claim_status": self.supplier_claim.status.value if self.supplier_claim else "NONE",
            "escalated": self.escalated_to_supervisor,
            "time_in_pipeline_hours": round((datetime.utcnow() - self.intake_timestamp).total_seconds() / 3600, 1),
            "customer_notified": self.customer_notification_status.value,
            "closed": self.closed,
        }
