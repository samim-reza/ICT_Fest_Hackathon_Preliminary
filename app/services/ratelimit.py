"""Per-user rolling-window rate limiting for booking creation."""
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..errors import AppError
from ..models import RateLimitEvent

_WINDOW_SECONDS = 60
_MAX_REQUESTS = 20


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def record_and_check(db: Session, user_id: int) -> None:
    now = _utcnow_naive()
    cutoff = now - timedelta(seconds=_WINDOW_SECONDS)

    db.query(RateLimitEvent).filter(
        RateLimitEvent.user_id == user_id,
        RateLimitEvent.created_at <= cutoff,
    ).delete(synchronize_session=False)

    count = (
        db.query(RateLimitEvent)
        .filter(
            RateLimitEvent.user_id == user_id,
            RateLimitEvent.created_at > cutoff,
        )
        .count()
    )

    # All attempts count toward the rolling window, including rejected ones.
    db.add(RateLimitEvent(user_id=user_id, created_at=now))
    db.commit()

    if count + 1 > _MAX_REQUESTS:
        raise AppError(429, "RATE_LIMITED", "Too many booking requests")
