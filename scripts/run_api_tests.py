#!/usr/bin/env python3
"""Comprehensive API contract tests against the hackathon specification."""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Use an isolated sqlite file for this run.
DB_PATH = os.path.join(ROOT, "test_run.db")
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASS.append(name)
    else:
        FAIL.append(f"{name}: {detail}")


def _future(hours: int, minute: int = 0) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(hours=hours)
    ).replace(minute=minute, second=0, microsecond=0).isoformat()


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def setup_org_admin() -> tuple[str, dict]:
    org = _unique("org")
    client.post(
        "/auth/register",
        json={"org_name": org, "username": "admin", "password": "pass12345"},
    )
    login = client.post(
        "/auth/login",
        json={"org_name": org, "username": "admin", "password": "pass12345"},
    )
    token = login.json()["access_token"]
    return org, {"Authorization": f"Bearer {token}"}


def run_health():
    r = client.get("/health")
    check("GET /health status 200", r.status_code == 200, str(r.status_code))
    check("GET /health body", r.json() == {"status": "ok"}, str(r.json()))


def run_auth_register_login() -> dict:
    org = _unique("acme")
    r = client.post(
        "/auth/register",
        json={"org_name": org, "username": "alice", "password": "pw12345"},
    )
    check("POST /auth/register new org -> 201", r.status_code == 201, str(r.status_code))
    body = r.json()
    check("register returns admin role", body.get("role") == "admin", str(body))
    check(
        "register has user_id/org_id/username",
        all(k in body for k in ("user_id", "org_id", "username")),
        str(body),
    )

    dup = client.post(
        "/auth/register",
        json={"org_name": org, "username": "bob", "password": "pw12345"},
    )
    check(
        "POST /auth/register known org -> 201 member",
        dup.status_code == 201 and dup.json().get("role") == "member",
        str(dup.status_code),
    )

    taken = client.post(
        "/auth/register",
        json={"org_name": org, "username": "alice", "password": "other"},
    )
    check(
        "POST /auth/register duplicate username -> 409 USERNAME_TAKEN",
        taken.status_code == 409 and taken.json().get("code") == "USERNAME_TAKEN",
        f"{taken.status_code} {taken.text}",
    )

    bad = client.post(
        "/auth/login",
        json={"org_name": org, "username": "alice", "password": "wrong"},
    )
    check(
        "POST /auth/login bad creds -> 401 INVALID_CREDENTIALS",
        bad.status_code == 401 and bad.json().get("code") == "INVALID_CREDENTIALS",
        f"{bad.status_code} {bad.text}",
    )

    ok = client.post(
        "/auth/login",
        json={"org_name": org, "username": "alice", "password": "pw12345"},
    )
    check("POST /auth/login success", ok.status_code == 200, str(ok.status_code))
    data = ok.json()
    check(
        "login returns tokens",
        all(k in data for k in ("access_token", "refresh_token", "token_type")),
        str(data),
    )
    check('login token_type is "bearer"', data.get("token_type") == "bearer", str(data))
    return data


def run_auth_refresh_logout(tokens: dict):
    refresh = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    check("POST /auth/refresh success", refresh.status_code == 200, f"{refresh.status_code} {refresh.text}")
    new_tokens = refresh.json()

    reuse = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    check(
        "POST /auth/refresh reuse old token -> 401",
        reuse.status_code == 401,
        f"{reuse.status_code} {reuse.text}",
    )

    headers = {"Authorization": f"Bearer {new_tokens['access_token']}"}
    logout = client.post("/auth/logout", headers=headers)
    check("POST /auth/logout success", logout.status_code == 200, str(logout.status_code))

    after = client.get("/rooms", headers=headers)
    check("access token invalid after logout -> 401", after.status_code == 401, str(after.status_code))


def run_rooms_and_bookings(headers: dict) -> tuple[int, dict]:
    room = client.post(
        "/rooms",
        json={"name": "Focus", "capacity": 4, "hourly_rate_cents": 1500},
        headers=headers,
    )
    check("POST /rooms admin -> 201", room.status_code == 201, str(room.status_code))
    room_id = room.json()["id"]

    rooms = client.get("/rooms", headers=headers)
    check(
        "GET /rooms lists org rooms",
        rooms.status_code == 200 and any(r["id"] == room_id for r in rooms.json()),
        str(rooms.json()),
    )

    start = _future(50)
    end = _future(52)
    booking = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": start, "end_time": end},
        headers=headers,
    )
    check("POST /bookings create -> 201", booking.status_code == 201, f"{booking.status_code} {booking.text}")
    b = booking.json()
    check("booking price_cents = rate * hours", b.get("price_cents") == 3000, str(b))
    check("booking has reference_code", bool(b.get("reference_code")), str(b))

    listing = client.get("/bookings", headers=headers)
    check("GET /bookings success", listing.status_code == 200, str(listing.status_code))
    lst = listing.json()
    check(
        "GET /bookings has pagination fields",
        all(k in lst for k in ("items", "page", "limit", "total")),
        str(lst),
    )
    check("GET /bookings total >= 1", lst["total"] >= 1, str(lst))
    if lst["items"]:
        check(
            "GET /bookings sorted ascending by start_time",
            lst["items"] == sorted(lst["items"], key=lambda x: (x["start_time"], x["id"])),
            str([i["start_time"] for i in lst["items"]]),
        )

    detail = client.get(f"/bookings/{b['id']}", headers=headers)
    check("GET /bookings/{id} success", detail.status_code == 200, str(detail.status_code))
    det = detail.json()
    check("GET /bookings/{id} includes refunds", "refunds" in det, str(det))
    check(
        "GET /bookings/{id} start_time matches booking",
        det.get("start_time") == b.get("start_time"),
        f"detail={det.get('start_time')} booking={b.get('start_time')}",
    )

    date = datetime.fromisoformat(start.replace("Z", "+00:00")).date().isoformat()
    avail = client.get(f"/rooms/{room_id}/availability", params={"date": date}, headers=headers)
    check("GET /rooms/{id}/availability success", avail.status_code == 200, str(avail.status_code))
    av = avail.json()
    check("availability has busy intervals", len(av.get("busy", [])) >= 1, str(av))

    stats = client.get(f"/rooms/{room_id}/stats", headers=headers)
    check("GET /rooms/{id}/stats success", stats.status_code == 200, str(stats.status_code))
    st = stats.json()
    check("stats count >= 1", st.get("total_confirmed_bookings", 0) >= 1, str(st))
    check("stats revenue matches bookings", st.get("total_revenue_cents", 0) >= 3000, str(st))

    return room_id, b


def run_booking_validation(headers: dict, room_id: int):
    past = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": _future(-2), "end_time": _future(-1)},
        headers=headers,
    )
    check(
        "POST /bookings past start -> 400 INVALID_BOOKING_WINDOW",
        past.status_code == 400 and past.json().get("code") == "INVALID_BOOKING_WINDOW",
        f"{past.status_code} {past.text}",
    )

    bad_duration = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": _future(60), "end_time": _future(61, minute=30)},
        headers=headers,
    )
    check(
        "POST /bookings non-whole hours -> 400",
        bad_duration.status_code == 400,
        f"{bad_duration.status_code} {bad_duration.text}",
    )

    conflict = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": _future(50), "end_time": _future(52)},
        headers=headers,
    )
    check(
        "POST /bookings overlap -> 409 ROOM_CONFLICT",
        conflict.status_code == 409 and conflict.json().get("code") == "ROOM_CONFLICT",
        f"{conflict.status_code} {conflict.text}",
    )

    # Back-to-back should be allowed
    back_to_back = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": _future(52), "end_time": _future(54)},
        headers=headers,
    )
    check(
        "POST /bookings back-to-back allowed -> 201",
        back_to_back.status_code == 201,
        f"{back_to_back.status_code} {back_to_back.text}",
    )


def run_cancel_refund(headers: dict, room_id: int):
    start = _future(72)
    end = _future(74)
    booking = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": start, "end_time": end},
        headers=headers,
    )
    bid = booking.json()["id"]
    cancel = client.post(f"/bookings/{bid}/cancel", headers=headers)
    check("POST /bookings/{id}/cancel success", cancel.status_code == 200, f"{cancel.status_code} {cancel.text}")
    c = cancel.json()
    check("cancel status cancelled", c.get("status") == "cancelled", str(c))
    check("cancel 48h+ notice -> 100% refund", c.get("refund_percent") == 100, str(c))

    detail = client.get(f"/bookings/{bid}", headers=headers)
    if detail.status_code == 200 and detail.json().get("refunds"):
        refund_amt = detail.json()["refunds"][0]["amount_cents"]
        check(
            "cancel refund_amount_cents matches RefundLog",
            c.get("refund_amount_cents") == refund_amt,
            f"cancel={c.get('refund_amount_cents')} log={refund_amt}",
        )

    again = client.post(f"/bookings/{bid}/cancel", headers=headers)
    check(
        "POST /bookings/{id}/cancel again -> 409 ALREADY_CANCELLED",
        again.status_code == 409 and again.json().get("code") == "ALREADY_CANCELLED",
        f"{again.status_code} {again.text}",
    )


def run_admin_endpoints(headers: dict, room_id: int):
    today = datetime.now(timezone.utc).date().isoformat()
    report = client.get("/admin/usage-report", params={"from": today, "to": today}, headers=headers)
    check("GET /admin/usage-report success", report.status_code == 200, f"{report.status_code} {report.text}")
    rep = report.json()
    check("usage report has rooms array", "rooms" in rep, str(rep))
    check(
        "usage report includes all org rooms",
        any(r.get("room_id") == room_id for r in rep.get("rooms", [])),
        str(rep),
    )

    export = client.get("/admin/export", headers=headers)
    check("GET /admin/export success", export.status_code == 200, str(export.status_code))
    check(
        "export CSV header exact",
        export.text.splitlines()[0]
        == "id,reference_code,room_id,user_id,start_time,end_time,status,price_cents",
        export.text.splitlines()[0] if export.text else "",
    )


def run_multi_tenancy():
    _, h1 = setup_org_admin()
    _, h2 = setup_org_admin()

    r1 = client.post("/rooms", json={"name": "R1", "capacity": 2, "hourly_rate_cents": 1000}, headers=h1)
    room1 = r1.json()["id"]

    cross = client.get(f"/rooms/{room1}/stats", headers=h2)
    check(
        "cross-org room id -> 404 ROOM_NOT_FOUND",
        cross.status_code == 404 and cross.json().get("code") == "ROOM_NOT_FOUND",
        f"{cross.status_code} {cross.text}",
    )


def run_member_forbidden_admin():
    org = _unique("member-org")
    client.post("/auth/register", json={"org_name": org, "username": "admin", "password": "pw12345"})
    client.post("/auth/register", json={"org_name": org, "username": "member", "password": "pw12345"})
    login = client.post("/auth/login", json={"org_name": org, "username": "member", "password": "pw12345"})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    create_room = client.post(
        "/rooms",
        json={"name": "Denied", "capacity": 1, "hourly_rate_cents": 500},
        headers=headers,
    )
    check(
        "member POST /rooms -> 403 FORBIDDEN",
        create_room.status_code == 403 and create_room.json().get("code") == "FORBIDDEN",
        f"{create_room.status_code} {create_room.text}",
    )


def run_timezone_parsing(headers: dict, room_id: int):
    start_local = (datetime.now(timezone.utc) + timedelta(hours=80)).replace(
        minute=0, second=0, microsecond=0
    )
    end_local = start_local + timedelta(hours=2)
    offset_start = start_local.astimezone(timezone(timedelta(hours=6))).isoformat()
    offset_end = end_local.astimezone(timezone(timedelta(hours=6))).isoformat()
    r = client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": offset_start, "end_time": offset_end},
        headers=headers,
    )
    check("POST /bookings offset datetime accepted", r.status_code == 201, f"{r.status_code} {r.text}")
    if r.status_code == 201:
        body = r.json()
        expected_utc = start_local.isoformat().replace("+00:00", "+00:00")
        check(
            "offset converted to UTC in response",
            body["start_time"] == expected_utc,
            f"got={body['start_time']} expected={expected_utc}",
        )


def run_unauthenticated():
    r = client.get("/rooms")
    check("GET /rooms without token -> 401", r.status_code == 401, str(r.status_code))


def main():
    run_health()
    run_unauthenticated()
    tokens = run_auth_register_login()
    run_auth_refresh_logout(tokens)
    _, headers = setup_org_admin()
    room_id, _ = run_rooms_and_bookings(headers)
    run_booking_validation(headers, room_id)
    run_cancel_refund(headers, room_id)
    run_admin_endpoints(headers, room_id)
    run_multi_tenancy()
    run_member_forbidden_admin()
    run_timezone_parsing(headers, room_id)

    print("=" * 60)
    print(f"API TEST RESULTS: {len(PASS)} passed, {len(FAIL)} failed")
    print("=" * 60)
    if PASS:
        print("\nPASSED:")
        for p in PASS:
            print(f"  + {p}")
    if FAIL:
        print("\nFAILED:")
        for f in FAIL:
            print(f"  - {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
