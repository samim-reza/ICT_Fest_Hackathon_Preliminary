"""Administrative reporting and export endpoints."""
from datetime import datetime, time, timedelta

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .. import cache
from ..auth import require_admin
from ..database import get_db
from ..errors import AppError
from ..models import Booking, Room, User
from ..services.export import generate_export
from ..timeutils import parse_input_datetime

router = APIRouter(prefix="/admin", tags=["admin"])


def _parse_bound(value: str, end_of_day: bool) -> datetime:
    """Parse a range bound as an inclusive UTC instant.

    Date-only values cover the whole UTC day, so the ``to`` bound extends to
    the end of that day. Full datetimes are used as-is (normalized to UTC).
    """
    try:
        day = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return parse_input_datetime(value)
    bound = datetime.combine(day, time.min)
    if end_of_day:
        bound = bound + timedelta(days=1) - timedelta(microseconds=1)
    return bound


@router.get("/usage-report")
def usage_report(
    frm: str = Query(..., alias="from"),
    to: str = Query(...),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    cached = cache.get_report(admin.org_id, frm, to)
    if cached is not None:
        return cached

    try:
        range_start = _parse_bound(frm, end_of_day=False)
        range_end = _parse_bound(to, end_of_day=True)
    except ValueError:
        raise AppError(400, "INVALID_BOOKING_WINDOW", "Invalid date range")

    rooms = db.query(Room).filter(Room.org_id == admin.org_id).order_by(Room.id.asc()).all()
    room_rows = []
    for room in rooms:
        bookings = (
            db.query(Booking)
            .filter(
                Booking.room_id == room.id,
                Booking.status == "confirmed",
                Booking.start_time >= range_start,
                Booking.start_time <= range_end,
            )
            .all()
        )
        room_rows.append(
            {
                "room_id": room.id,
                "room_name": room.name,
                "confirmed_bookings": len(bookings),
                "revenue_cents": sum(b.price_cents for b in bookings),
            }
        )

    result = {"from": frm, "to": to, "rooms": room_rows}
    cache.set_report(admin.org_id, frm, to, result)
    return result


@router.get("/export")
def export(
    room_id: int | None = Query(None),
    include_all: bool = Query(False),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    csv_body = generate_export(db, admin.org_id, admin.id, room_id, include_all)
    return Response(content=csv_body, media_type="text/csv")
