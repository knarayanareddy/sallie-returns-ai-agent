"""
Version A — Tool implementations.

These are the actual tool functions the orchestrator nodes call.
In production, each would be wired to a real POS API, WMS, vision model, etc.
Here we provide realistic mock implementations that simulate the behaviour
and failure modes described in the architecture — so the pipeline can be
tested end-to-end without depending on external systems.

Failure modes are deliberately included: stock API down, no federated match,
photo quality fail, defect confidence below threshold.
"""

from __future__ import annotations

import hashlib
import random
import time
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from .state_models import (
    AtomicHold,
    ConditionVerdict,
    ConditionStatus,
    ReceiptVerification,
    StockMatrix,
    StockMatrixItem,
    SupplierClaim,
    RMAStatus,
    TransferManifest,
)


# ── Mock data stores ───────────────────────────────────────────────────────────

# Simulated federated sales database: 4 Randstad stores + e-comm
# In production this would be a real query against a shared sales ledger.
MOCK_SALES_LEDGER: List[Dict] = [
    {
        "transaction_id": "TXN-001024",
        "store_id": "STORE-Utrecht",
        "store_name": "Sallie se Schoenen — Utrecht",
        "receipt_id": "REC-883921",
        "customer_email": "marieke.dejong@mail.nl",
        "customer_phone": "+31612345678",
        "customer_card_last_four": "4821",
        "purchase_date": datetime.utcnow() - timedelta(days=7),
        "sku": "SS-0042",
        "size": "EU-39",
        "amount_eur": Decimal("89.95"),
        "payment_method": "visa",
        "payment_token_hash": "tok_" + hashlib.sha256(b"4821|TXN-001024").hexdigest()[:16],
    },
    {
        "transaction_id": "TXN-001099",
        "store_id": "STORE-Rotterdam",
        "store_name": "Sallie se Schoenen — Rotterdam",
        "receipt_id": "REC-910203",
        "customer_email": "piet.hendriks@webmail.nl",
        "customer_phone": "+31698765432",
        "customer_card_last_four": "2209",
        "purchase_date": datetime.utcnow() - timedelta(days=12),
        "sku": "SS-0087",
        "size": "EU-42",
        "amount_eur": Decimal("119.00"),
        "payment_method": "mastercard",
        "payment_token_hash": "tok_" + hashlib.sha256(b"2209|TXN-001099").hexdigest()[:16],
    },
    {
        "transaction_id": "TXN-001155",
        "store_id": "STORE-Amsterdam",
        "store_name": "Sallie se Schoenen — Amsterdam",
        "receipt_id": "REC-778123",
        "customer_email": "sara.vandermeer@mail.nl",
        "customer_phone": "+31655512345",
        "customer_card_last_four": "7734",
        "purchase_date": datetime.utcnow() - timedelta(days=22),
        "sku": "SS-0021",
        "size": "EU-36",
        "amount_eur": Decimal("64.50"),
        "payment_method": "visa",
        "payment_token_hash": "tok_" + hashlib.sha256(b"7734|TXN-001155").hexdigest()[:16],
    },
    {
        "transaction_id": "TXN-000987",
        "store_id": "STORE-Haarlem",
        "store_name": "Sallie se Schoenen — Haarlem",
        "receipt_id": "REC-661098",
        "customer_email": "thomas broek@mail.nl",
        "customer_phone": "+31644498765",
        "customer_card_last_four": "3310",
        "purchase_date": datetime.utcnow() - timedelta(days=3),
        "sku": "SS-0055",
        "size": "EU-44",
        "amount_eur": Decimal("134.00"),
        "payment_method": "ideal",
        "payment_token_hash": "tok_" + hashlib.sha256(b"3310|TXN-000987").hexdigest()[:16],
    },
    {
        "transaction_id": "TXN-001201",
        "store_id": "STORE-Ecommerce",
        "store_name": "Sallie se Schoenen — Online",
        "receipt_id": "REC-EC-44510",
        "customer_email": "lucasBakker@webmail.nl",
        "customer_phone": "+31677733991",
        "customer_card_last_four": "5528",
        "purchase_date": datetime.utcnow() - timedelta(days=19),
        "sku": "SS-0087",
        "size": "EU-41",
        "amount_eur": Decimal("119.00"),
        "payment_method": "visa",
        "payment_token_hash": "tok_" + hashlib.sha256(b"5528|TXN-001201").hexdigest()[:16],
    },
    # Case 5: worn item, out-of-window return — valid receipt but 35 days old
    # (just past the 30-day return window). customer_email not in ledger so
    # cross_store_lookup by email finds no match; receipt verifies in intake_eval
    # and the case correctly routes to policy_check where worn + out-of-window
    # triggers a policy-violation HITL (not the missing-proof HITL).
    {
        "transaction_id": "TXN-000555",
        "store_id": "STORE-Rotterdam",
        "store_name": "Sallie se Schoenen — Rotterdam",
        "receipt_id": "REC-000555",
        "customer_email": "jan.koster@webmail.nl",
        "customer_phone": "+31622233444",
        "customer_card_last_four": "6222",
        "purchase_date": datetime.utcnow() - timedelta(days=35),
        "sku": "SS-0055",
        "size": "EU-44",
        "amount_eur": Decimal("69.00"),
        "payment_method": "mastercard",
        "payment_token_hash": "tok_" + hashlib.sha256(b"6222|TXN-000555").hexdigest()[:16],
    },
]

# Mock inventory: 4 stores + warehouse
MOCK_INVENTORY: Dict[str, Dict[str, int]] = {
    "SS-0042": {"STORE-Utrecht": 2, "STORE-Rotterdam": 0, "STORE-Amsterdam": 1, "STORE-Haarlem": 0, "WAREHOUSE": 4},
    "SS-0087": {"STORE-Utrecht": 0, "STORE-Rotterdam": 3, "STORE-Amsterdam": 0, "STORE-Haarlem": 1, "WAREHOUSE": 2},
    "SS-0021": {"STORE-Utrecht": 1, "STORE-Rotterdam": 0, "STORE-Amsterdam": 0, "STORE-Haarlem": 0, "WAREHOUSE": 1},
    "SS-0055": {"STORE-Utrecht": 0, "STORE-Rotterdam": 1, "STORE-Amsterdam": 2, "STORE-Haarlem": 0, "WAREHOUSE": 0},
    "SS-0091": {"STORE-Utrecht": 0, "STORE-Rotterdam": 0, "STORE-Amsterdam": 0, "STORE-Haarlem": 0, "WAREHOUSE": 0},
}

STORE_NAMES = {
    "STORE-Utrecht": "Sallie se Schoenen — Utrecht",
    "STORE-Rotterdam": "Sallie se Schoenen — Rotterdam",
    "STORE-Amsterdam": "Sallie se Schoenen — Amsterdam",
    "STORE-Haarlem": "Sallie se Schoenen — Haarlem",
    "WAREHOUSE": "Sallie se Schoenen — Central Warehouse",
}


# ── Active holds store (simulates WMS lock table) ─────────────────────────────

active_holds: Dict[str, AtomicHold] = {}


# ── Tool: verify_receipt_data ──────────────────────────────────────────────────

def verify_receipt_data(
    receipt_id: str,
    purchase_date_hint: Optional[datetime] = None,
) -> ReceiptVerification:
    """
    Look up a receipt by its printed receipt ID against the federated sales ledger.

    In production: query the POS API for the receipt. Here we scan the mock ledger.
    """
    if not receipt_id:
        return ReceiptVerification(receipt_id=None, valid=False, verification_method="none")

    for txn in MOCK_SALES_LEDGER:
        if txn["receipt_id"] == receipt_id:
            days = (datetime.utcnow() - txn["purchase_date"]).days
            return ReceiptVerification(
                receipt_id=receipt_id,
                valid=True,
                days_since_purchase=days,
                store_id=txn["store_id"],
                store_name=txn["store_name"],
                transaction_amount_eur=txn["amount_eur"],
                payment_method=txn["payment_method"],
                purchase_date=txn["purchase_date"],
                verification_method="receipt_scanned",
            )

    return ReceiptVerification(
        receipt_id=receipt_id,
        valid=False,
        days_since_purchase=999,
        verification_method="receipt_scanned",
    )


# ── Tool: analyze_shoe_condition ───────────────────────────────────────────────

def analyze_shoe_condition(
    image_urls: List[str],
    sku: Optional[str] = None,
    size: Optional[str] = None,
    is_worn_hint: Optional[bool] = None,
    packaging_intact_hint: Optional[bool] = None,
) -> ConditionVerdict:
    """
    Vision model call: assess shoe condition from images.

    In production: send image_urls to a multimodal model with a structured
    output schema that classifies wear and detects manufacturing defects.

    Here: deterministic mock based on image count + random simulation.
    Image count == 0 → quality fail. 1–2 images → borderline. 3+ → full assessment.

    We simulate a "defective" outcome when sku is SS-0091 (a known defective batch)
    to demonstrate the defect path without needing real CV.

    is_worn_hint: if provided (from ReturnItem.is_worn), the mock uses it as the
    definitive is_worn value instead of random simulation. This lets demo cases
    set explicit conditions.
    """
    missing_angles: List[str] = []
    if len(image_urls) == 0:
        return ConditionVerdict(
            condition_status=ConditionStatus.SIGNIFICANT_WEAR,
            is_worn=True,
            packaging_intact=False,
            wear_evidence=["No images provided — assessment cannot proceed"],
            defect_suspected=False,
            defect_confidence=0.0,
            is_manufacturing_fault=False,
            fault_type=None,
            photo_quality_passed=False,
            missing_angles=["TOP_DOWN", "LATERAL_LEFT", "LATERAL_RIGHT", "SOLE", "INSOLE"],
            cv_model_version="mock-v1",
        )

    if len(image_urls) < 3:
        missing_angles = ["TOP_DOWN" if len(image_urls) < 1 else "LATERAL_LEFT" if len(image_urls) < 2 else "SOLE"]

    # Known defective batch — simulates CV detecting a manufacturing fault
    if sku == "SS-0091":
        return ConditionVerdict(
            condition_status=ConditionStatus.DEFECTIVE,
            is_worn=False,
            packaging_intact=True,
            wear_evidence=["No wear indicators detected"],
            defect_suspected=True,
            defect_confidence=0.91,
            is_manufacturing_fault=True,
            fault_type="SOLE_DETACHMENT",
            photo_quality_passed=len(image_urls) >= 3 and len(missing_angles) == 0,
            missing_angles=missing_angles,
            cv_model_version="mock-v1",
        )

    # Use explicit is_worn hint if provided (demo cases), else random simulation
    if is_worn_hint is not None:
        is_worn = is_worn_hint
        is_worn_roll = 0.5  # deterministic when hint is provided
        # When is_worn is explicitly set, simulate a plausible confidence
        defect_roll = random.random()
        defect_suspected = defect_roll < 0.05 and sku != "SS-0091"
        confidence = round(0.78 + random.random() * 0.15, 2)
        if defect_suspected:
            confidence = round(0.70 + random.random() * 0.14, 2)
    else:
        # Random simulation for other SKUs — mostly good, occasional worn
        is_worn_roll = random.random()
        is_worn = is_worn_roll < 0.15  # ~15% of returns show wear
        defect_roll = random.random()
        defect_suspected = defect_roll < 0.08 and sku != "SS-0091"
        confidence = round(0.72 + random.random() * 0.25, 2)
        if defect_suspected:
            confidence = round(0.68 + random.random() * 0.18, 2)  # lower confidence on defects
        if is_worn:
            confidence = round(confidence * 0.85, 2)  # worn + defect = ambiguous

    status: ConditionStatus
    if defect_suspected and confidence >= 0.80:
        status = ConditionStatus.DEFECTIVE
    elif is_worn and is_worn_roll > 0.85:
        status = ConditionStatus.SIGNIFICANT_WEAR
    elif is_worn:
        status = ConditionStatus.LIGHT_WEAR
    else:
        status = ConditionStatus.UNWORN

    packaging_intact = packaging_intact_hint if packaging_intact_hint is not None else bool(random.random() > 0.1)

    return ConditionVerdict(
        condition_status=status,
        is_worn=is_worn,
        packaging_intact=packaging_intact,
        wear_evidence=(
            ["Insole crease compression visible", "Upper scuff mark on toe cap"]
            if is_worn
            else ["No wear indicators detected"]
        ),
        defect_suspected=defect_suspected,
        defect_confidence=confidence,
        is_manufacturing_fault=(defect_suspected and confidence >= 0.80),
        fault_type=("SOLE_DETACHMENT" if defect_suspected and random.random() > 0.5 else "STITCH_SPLIT" if defect_suspected else None),
        photo_quality_passed=(len(image_urls) >= 3 and len(missing_angles) == 0),
        missing_angles=missing_angles,
        cv_model_version="mock-v1",
    )


# ── Tool: federated_sales_search ───────────────────────────────────────────────

def federated_sales_search(
    customer_identifier: str,
    approx_date_range: Optional[Tuple[datetime, datetime]] = None,
    limit: int = 10,
) -> List[Dict]:
    """
    Search all 4 Randstad store databases + e-comm for a transaction matching
    partial customer identifiers.

    Accepts: email, phone, card_last_four.
    Matching is type-specific: email → exact match; phone → normalised substring;
    card → exact 4-digit match.

    In production: federated query across POS APIs. Here: scan the mock ledger.
    """
    results: List[Dict] = []
    identifier = customer_identifier.strip()

    # Determine identifier type
    is_email = "@" in identifier
    is_card = identifier.isdigit() and len(identifier) == 4
    is_phone = identifier.replace("+31", "0").replace("-", "").replace(" ", "").isdigit() and len(identifier.replace("+31", "0").replace("-", "").replace(" ", "")) >= 9

    for txn in MOCK_SALES_LEDGER:
        txn_email = txn["customer_email"].strip().lower()
        txn_phone = txn["customer_phone"].replace("+31", "0").replace("-", "").replace(" ", "")
        txn_card = txn["customer_card_last_four"]

        matched = False
        if is_email:
            # Exact email match (case-insensitive)
            if identifier.lower() == txn_email:
                matched = True
        elif is_card:
            if identifier == txn_card:
                matched = True
        elif is_phone:
            id_phone = identifier.replace("+31", "0").replace("-", "").replace(" ", "")
            if id_phone == txn_phone or txn_phone.startswith(id_phone) or id_phone.startswith(txn_phone):
                matched = True

        if matched:
            if approx_date_range:
                if txn["purchase_date"] < approx_date_range[0] or txn["purchase_date"] > approx_date_range[1]:
                    continue
            results.append(txn)
            if len(results) >= limit:
                break

    return results


def payment_token_lookup(
    card_last_four: str,
    bank_terminal_ref: Optional[str] = None,
) -> Optional[Dict]:
    """
    Look up a transaction by payment token (card last four + bank terminal ref).
    In production: query the payment gateway/token vault. Here: scan the mock ledger.
    """
    if not card_last_four or len(card_last_four) != 4:
        return None

    for txn in MOCK_SALES_LEDGER:
        if txn["customer_card_last_four"] == card_last_four:
            if bank_terminal_ref and txn.get("transaction_id") != bank_terminal_ref:
                continue
            return txn

    return None


def generate_replacement_proof(transaction_id: str) -> str:
    """
    Mint a digital proof artifact for a customer who lost their receipt.
    In production: generate a QR code or email a PDF receipt copy.
    Here: return a reference string.
    """
    return f"PROOF-{transaction_id}-{uuid4().hex[:6].upper()}"


# ── Tool: query_stock_matrix ────────────────────────────────────────────────────

STOCK_API_DOWN = False  # flip to True to simulate API outage in demos

def query_stock_matrix(sku: str, size: str, origin_store_id: Optional[str] = None) -> StockMatrix:
    """
    Query real-time stock across all 4 stores + central warehouse.
    In production: call the WMS/ERP inventory API.
    Here: read from MOCK_INVENTORY with an optional simulated outage.

    origin_store_id: if provided, has_local_stock reflects stock at the
    origin store specifically (not just any store). If None, falls back to
    checking if any store has verified stock.
    """
    if STOCK_API_DOWN:
        raise RuntimeError("INVENTORY_API_UNAVAILABLE")

    items: List[StockMatrixItem] = []
    total = 0
    has_local = False
    has_network = False

    for store_id, qty in MOCK_INVENTORY.get(sku, {}).items():
        store_name = STORE_NAMES.get(store_id, store_id)
        is_origin = store_id == origin_store_id

        verified = (qty > 0 and random.random() > 0.15)
        items.append(StockMatrixItem(
            store_id=store_id,
            store_name=store_name,
            quantity_available=qty,
            is_verified=verified,
        ))
        total += qty
        if qty > 0 and verified:
            if is_origin:
                has_local = True
            else:
                has_network = True

    return StockMatrix(
        sku=sku,
        size=size,
        items=items,
        total_available=total,
        has_local_stock=has_local,
        has_network_stock=has_network,
    )


# ── Tool: acquire_atomic_hold ──────────────────────────────────────────────────

def acquire_atomic_hold(
    sku: str,
    size: str,
    store_id: str,
    ttl_hours: int = 48,
    quantity: int = 1,
) -> Optional[AtomicHold]:
    """
    Attempt to acquire an atomic reservation lock on stock at a specific store.
    Returns the hold object on success, None if stock unavailable.

    In production: call WMS lock API with TTL. Here: check MOCK_INVENTORY and
    record in active_holds dict. Stock is released on expiry via a scheduled task.
    """
    global STOCK_API_DOWN
    if STOCK_API_DOWN:
        raise RuntimeError("INVENTORY_API_UNAVAILABLE")

    stock = MOCK_INVENTORY.get(sku, {}).get(store_id, 0)
    if stock < quantity:
        return None

    # Simulate occasional failed lock (stock just gone)
    if random.random() < 0.05 and stock > 0:
        return None

    now = datetime.utcnow()
    hold = AtomicHold(
        hold_id=f"HLD-{uuid4().hex[:8].upper()}",
        sku=sku,
        size=size,
        store_id=store_id,
        store_name=STORE_NAMES.get(store_id, store_id),
        quantity=quantity,
        ttl_seconds=ttl_hours * 3600,
        acquired_at=now,
        expires_at=now + timedelta(seconds=ttl_hours * 3600),
        status="ACTIVE",
    )
    active_holds[hold.hold_id] = hold
    # Atomically decrement available stock in mock inventory
    if sku in MOCK_INVENTORY and store_id in MOCK_INVENTORY[sku]:
        MOCK_INVENTORY[sku][store_id] = max(0, MOCK_INVENTORY[sku][store_id] - quantity)
    return hold


def release_expired_hold(hold_id: str) -> bool:
    """Release an expired hold — called by scheduled expiry task."""
    hold = active_holds.get(hold_id)
    if hold and hold.status == "ACTIVE" and hold.expires_at < datetime.utcnow():
        hold.status = "RELEASED"
        # Restore reserved stock back to mock inventory
        if hold.sku in MOCK_INVENTORY and hold.store_id in MOCK_INVENTORY[hold.sku]:
            MOCK_INVENTORY[hold.sku][hold.store_id] += hold.quantity
        return True
    return False


def get_active_holds() -> List[AtomicHold]:
    return [h for h in active_holds.values() if h.status == "ACTIVE"]


# ── Tool: create_interstore_transfer_manifest ──────────────────────────────────

def create_interstore_transfer_manifest(
    origin_store_id: str,
    dest_store_id: str,
    sku: str,
    size: str,
    hold_id: str,
    quantity: int = 1,
) -> Optional[TransferManifest]:
    """
    Create a transfer manifest for an inter-store exchange.
    In production: create in WMS/ERP + trigger courier dispatch.
    Here: create the manifest object; dispatch is a separate scheduled step.
    """
    hold = active_holds.get(hold_id)
    if not hold or hold.status != "ACTIVE" or hold.sku != sku:
        return None

    return TransferManifest(
        manifest_id=f"XFER-{uuid4().hex[:8].upper()}",
        origin_store_id=origin_store_id,
        origin_store_name=STORE_NAMES.get(origin_store_id, origin_store_id),
        dest_store_id=dest_store_id,
        dest_store_name=STORE_NAMES.get(dest_store_id, dest_store_id),
        sku=sku,
        size=size,
        quantity=quantity,
        hold_id=hold_id,
        status="CREATED",
        scheduled_collection_date=datetime.utcnow() + timedelta(hours=4),  # 4h nominal dispatch
        created_at=datetime.utcnow(),
    )


# ── Tool: check_photo_quality_compliance ───────────────────────────────────────

def check_photo_quality_compliance(image_urls: List[str]) -> Tuple[bool, List[str]]:
    """
    Check whether submitted images meet the evidence requirements for a defect claim.
    Returns (passed, missing_angles).

    Required angles: TOP_DOWN, LATERAL, SOLE.
    In production: use a vision model to detect angle coverage. Here: heuristic by count.
    """
    if len(image_urls) == 0:
        return (False, ["TOP_DOWN", "LATERAL", "SOLE"])

    if len(image_urls) >= 3:
        return (True, [])

    required = ["TOP_DOWN", "LATERAL", "SOLE"]
    present = min(len(image_urls), 3)
    missing = required[present:]
    return (False, missing)


# ── Tool: classify_structural_defect ───────────────────────────────────────────

def classify_structural_defect(
    images: List[str],
    fault_description: str,
    sku: Optional[str] = None,
) -> Dict:
    """
    Multimodal defect classification.
    Distinguishes manufacturing faults from customer wear.

    In production: vision model with structured output.
    Here: deterministic mock. SS-0091 is a known defective batch (sole detachment).
    Other SKUs: probabilistic simulation.
    """
    quality_ok, _ = check_photo_quality_compliance(images)
    if not quality_ok:
        return {
            "quality_passed": False,
            "defect_confidence": 0.0,
            "is_manufacturing_fault": False,
            "fault_type": None,
            "reason": "Photo quality insufficient — cannot classify",
        }

    # Known defective batch
    if sku == "SS-0091":
        return {
            "quality_passed": True,
            "defect_confidence": 0.91,
            "is_manufacturing_fault": True,
            "fault_type": "SOLE_DETACHMENT",
            "severity": "MAJOR",
            "description": "Sole detachment at forefoot — manufacturing defect consistent with SS-0091 batch recall.",
            "recommendation": "AUTHORIZE_REMEDY",
        }

    # Simulation
    fault_keywords = ["sole", "detach", "stitch", "split", "peel", "leather", "crack", "separation"]
    has_fault_keyword = any(kw in fault_description.lower() for kw in fault_keywords)

    if not has_fault_keyword:
        return {
            "quality_passed": True,
            "defect_confidence": 0.42,
            "is_manufacturing_fault": False,
            "fault_type": None,
            "severity": "UNKNOWN",
            "description": "No clear manufacturing fault indicators in description or images.",
            "recommendation": "REVIEW_BY_SUPERVISOR",
        }

    confidence = round(0.74 + random.random() * 0.20, 2)
    is_manufacturing = confidence >= 0.80 and random.random() > 0.2

    fault_type = random.choice(["SOLE_DETACHMENT", "STITCH_SPLIT", "LEATHER_PEELING", "UPPER_TEAR"]) if is_manufacturing else None

    return {
        "quality_passed": True,
        "defect_confidence": confidence,
        "is_manufacturing_fault": is_manufacturing,
        "fault_type": fault_type,
        "severity": ("MAJOR" if confidence >= 0.90 else "MODERATE" if confidence >= 0.80 else "MINOR"),
        "description": f"Classification suggests {'manufacturing fault' if is_manufacturing else 'ambiguous — possible wear'} in {fault_type or 'unknown area'}.",
        "recommendation": ("AUTHORIZE_REMEDY" if is_manufacturing and confidence >= 0.85 else "REVIEW_BY_SUPERVISOR"),
    }


# ── Tool: submit_supplier_rma_claim ────────────────────────────────────────────

SUPPLIER_API_AVAILABLE = False  # flip to True to simulate supplier portal API

def submit_supplier_rma_claim(
    supplier_id: str,
    supplier_name: str,
    case_data: Dict,
) -> Optional[str]:
    """
    Submit an RMA claim to a supplier/manufacturer portal.
    In production: API call to supplier claim portal.
    Here: if SUPPLIER_API_AVAILABLE, return a claim ID. Otherwise None
    (falls back to manual submission path).

    case_data should contain: sku, size, defect_classification, evidence_package_ref,
    receipt metadata, sale transaction reference.
    """
    if not SUPPLIER_API_AVAILABLE:
        return None

    # Simulate successful submission
    return f"RMA-{supplier_id}-{uuid4().hex[:10].upper()}"


# ── Tool: notify_customer ───────────────────────────────────────────────────────

def notify_customer(
    customer_email: Optional[str],
    customer_phone: Optional[str],
    channel: str,  # "sms" | "email" | "both"
    message_type: str,  # "refund_confirmed" | "exchange_ticket" | "defect_remedy" | "claim_submitted" | "hold_expired" | "case_escalated"
    case_id: str,
    details: Optional[str] = None,
) -> Dict:
    """
    Send a notification to the customer.
    In production: Twilio SMS + SMTP email. Here: returns a mock delivery record.
    """
    channels_used = []
    if channel in ("sms", "both") and customer_phone:
        channels_used.append("sms")
    if channel in ("email", "both") and customer_email:
        channels_used.append("email")

    if not channels_used:
        # In-store fallback: printed till slip / handover receipt
        channels_used.append("till_slip")

    message_templates = {
        "refund_confirmed": f"Your refund of {details or 'the original amount'} has been processed to your original payment method. Reference: {case_id}.",
        "exchange_ticket": f"Your exchange is confirmed. Reference: {case_id}. {details or 'Your replacement pair is being transferred to your chosen store.'}",
        "defect_remedy": f"We've authorized a remedy for your defective shoes. Reference: {case_id}. {details or 'Please collect your replacement or refund at the till.'}",
        "claim_submitted": f"We've submitted a supplier claim for your defective shoes. Reference: {case_id}. We'll update you when the claim is resolved.",
        "hold_expired": f"Your exchange hold has expired (reference: {case_id}). The reserved stock has been released back to inventory. Please contact the store for alternative options.",
        "case_escalated": f"Your return case {case_id} requires supervisor review. A store supervisor will contact you within 2 hours.",
        "return_rejected": f"Your return request for case {case_id} could not be accepted. {details or 'Per store policy, items must be unworn and returned within 30 days.'}",
    }

    return {
        "delivered": True,
        "case_id": case_id,
        "channels": channels_used,
        "message_type": message_type,
        "message_preview": message_templates.get(message_type, "Notification sent."),
        "sent_at": datetime.utcnow(),
    }
