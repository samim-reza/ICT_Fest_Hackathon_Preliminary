"""Refund bookkeeping.

When a booking is cancelled a refund is calculated from its price and the
applicable notice tier, then written to the refund ledger with a processed
status. Amounts are stored in whole cents.
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..models import RefundLog


def calculate_refund_amount(price_cents: int, percent: int) -> int:
    # Round half-up in cents using integer math.
    return (price_cents * percent + 50) // 100


def log_refund(db: Session, booking_id: int, amount_cents: int) -> RefundLog:
    entry = RefundLog(
        booking_id=booking_id,
        amount_cents=amount_cents,
        status="processed",
        processed_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(entry)
    return entry
