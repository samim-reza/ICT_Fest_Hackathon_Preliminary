"""Contract-oriented test coverage for the CoWork API."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import uuid

import jwt
from fastapi.testclient import TestClient

from app.config import JWT_ALGORITHM, JWT_SECRET
from app.main import app

client = TestClient(app)


def _unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _future_aligned(hours: int) -> datetime:
    now = datetime.now(timezone.utc)
    base = now.replace(minute=0, second=0, microsecond=0)
    if base <= now:
        base = base + timedelta(hours=1)
    return base + timedelta(hours=hours)


def _register(org_name: str, username: str, password: str = "pw12345"):
    return client.post(
        "/auth/register",
        json={"org_name": org_name, "username": username, "password": password},
    )


def _login(org_name: str, username: str, password: str = "pw12345"):
    return client.post(
        "/auth/login",
        json={"org_name": org_name, "username": username, "password": password},
    )


def _auth_headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def _create_room(headers: dict, name: str = "Focus Room", rate: int = 1000) -> int:
    resp = client.post(
        "/rooms",
        json={"name": name, "capacity": 4, "hourly_rate_cents": rate},
        headers=headers,
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def _create_booking(headers: dict, room_id: int, start: datetime, end: datetime):
    return client.post(
        "/bookings",
        json={
            "room_id": room_id,
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
        },
        headers=headers,
    )


def test_auth_contract_and_registration_uniqueness():
    org = _unique_name("org-auth")

    reg = _register(org, "alice")
    assert reg.status_code == 201
    assert reg.json()["role"] == "admin"

    duplicate = _register(org, "alice")
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "USERNAME_TAKEN"

    login = _login(org, "alice")
    assert login.status_code == 200
    access = login.json()["access_token"]
    refresh = login.json()["refresh_token"]

    access_claims = jwt.decode(access, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    refresh_claims = jwt.decode(refresh, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    assert access_claims["exp"] - access_claims["iat"] == 900
    assert refresh_claims["exp"] - refresh_claims["iat"] == 7 * 24 * 3600

    first_refresh = client.post("/auth/refresh", json={"refresh_token": refresh})
    assert first_refresh.status_code == 200
    reused_refresh = client.post("/auth/refresh", json={"refresh_token": refresh})
    assert reused_refresh.status_code == 401

    headers = _auth_headers(access)
    logout = client.post("/auth/logout", headers=headers)
    assert logout.status_code == 200
    after_logout = client.get("/rooms", headers=headers)
    assert after_logout.status_code == 401


def test_booking_window_timezone_and_conflict_rules():
    org = _unique_name("org-booking")
    assert _register(org, "admin").status_code == 201
    login = _login(org, "admin")
    assert login.status_code == 200
    headers = _auth_headers(login.json()["access_token"])
    room_id = _create_room(headers, rate=1001)

    start_utc = _future_aligned(30)
    end_utc = start_utc + timedelta(hours=1)
    plus6 = timezone(timedelta(hours=6))

    booking = _create_booking(
        headers,
        room_id,
        start_utc.astimezone(plus6),
        end_utc.astimezone(plus6),
    )
    assert booking.status_code == 201
    assert booking.json()["start_time"] == start_utc.isoformat()

    now = datetime.now(timezone.utc)
    past = client.post(
        "/bookings",
        json={
            "room_id": room_id,
            "start_time": (now - timedelta(minutes=1)).isoformat(),
            "end_time": (now + timedelta(hours=1)).isoformat(),
        },
        headers=headers,
    )
    assert past.status_code == 400

    equal_window = _create_booking(headers, room_id, _future_aligned(40), _future_aligned(40))
    assert equal_window.status_code == 400

    half_hour = client.post(
        "/bookings",
        json={
            "room_id": room_id,
            "start_time": _future_aligned(42).isoformat(),
            "end_time": (_future_aligned(42) + timedelta(minutes=30)).isoformat(),
        },
        headers=headers,
    )
    assert half_hour.status_code == 400

    back_to_back = _create_booking(headers, room_id, end_utc, end_utc + timedelta(hours=1))
    assert back_to_back.status_code == 201

    overlap = _create_booking(headers, room_id, start_utc, start_utc + timedelta(hours=2))
    assert overlap.status_code == 409
    assert overlap.json()["code"] == "ROOM_CONFLICT"


def test_booking_listing_visibility_and_detail_fields():
    org = _unique_name("org-visibility")
    assert _register(org, "admin").status_code == 201
    admin_login = _login(org, "admin")
    admin_headers = _auth_headers(admin_login.json()["access_token"])
    room_id = _create_room(admin_headers)

    assert _register(org, "member").status_code == 201
    member_login = _login(org, "member")
    member_headers = _auth_headers(member_login.json()["access_token"])

    member_bookings = []
    for hour in (40, 42, 44):
        created = _create_booking(
            member_headers,
            room_id,
            _future_aligned(hour),
            _future_aligned(hour + 1),
        )
        assert created.status_code == 201
        member_bookings.append(created.json())

    admin_booking = _create_booking(
        admin_headers,
        room_id,
        _future_aligned(60),
        _future_aligned(61),
    )
    assert admin_booking.status_code == 201

    page1 = client.get("/bookings?page=1&limit=2", headers=member_headers)
    page2 = client.get("/bookings?page=2&limit=2", headers=member_headers)
    assert page1.status_code == 200
    assert page2.status_code == 200

    items1 = page1.json()["items"]
    items2 = page2.json()["items"]
    assert len(items1) == 2
    assert len(items2) == 1

    ids1 = {item["id"] for item in items1}
    ids2 = {item["id"] for item in items2}
    assert ids1.isdisjoint(ids2)

    all_starts = [item["start_time"] for item in items1 + items2]
    assert all_starts == sorted(all_starts)

    hidden = client.get(f"/bookings/{admin_booking.json()['id']}", headers=member_headers)
    assert hidden.status_code == 404

    detail = client.get(f"/bookings/{member_bookings[0]['id']}", headers=member_headers)
    assert detail.status_code == 200
    assert detail.json()["start_time"] == member_bookings[0]["start_time"]
    assert detail.json()["start_time"] != detail.json()["created_at"]


def test_cancel_refund_tiers_and_rounding():
    org = _unique_name("org-refund")
    assert _register(org, "admin").status_code == 201
    login = _login(org, "admin")
    headers = _auth_headers(login.json()["access_token"])
    room_id = _create_room(headers, rate=1001)

    over_48 = _create_booking(headers, room_id, _future_aligned(50), _future_aligned(51))
    mid_tier = _create_booking(headers, room_id, _future_aligned(30), _future_aligned(31))
    under_24 = _create_booking(headers, room_id, _future_aligned(5), _future_aligned(6))
    assert over_48.status_code == 201
    assert mid_tier.status_code == 201
    assert under_24.status_code == 201

    cancel_over_48 = client.post(f"/bookings/{over_48.json()['id']}/cancel", headers=headers)
    cancel_mid = client.post(f"/bookings/{mid_tier.json()['id']}/cancel", headers=headers)
    cancel_under_24 = client.post(f"/bookings/{under_24.json()['id']}/cancel", headers=headers)
    assert cancel_over_48.status_code == 200
    assert cancel_mid.status_code == 200
    assert cancel_under_24.status_code == 200

    assert cancel_over_48.json()["refund_percent"] == 100
    assert cancel_over_48.json()["refund_amount_cents"] == 1001
    assert cancel_mid.json()["refund_percent"] == 50
    assert cancel_mid.json()["refund_amount_cents"] == 501
    assert cancel_under_24.json()["refund_percent"] == 0
    assert cancel_under_24.json()["refund_amount_cents"] == 0

    already_cancelled = client.post(f"/bookings/{under_24.json()['id']}/cancel", headers=headers)
    assert already_cancelled.status_code == 409
    assert already_cancelled.json()["code"] == "ALREADY_CANCELLED"

    detail = client.get(f"/bookings/{mid_tier.json()['id']}", headers=headers)
    assert detail.status_code == 200
    refunds = detail.json()["refunds"]
    assert len(refunds) == 1
    assert refunds[0]["amount_cents"] == 501


def test_usage_report_availability_stats_and_export_tenant_isolation():
    org_a = _unique_name("org-a")
    assert _register(org_a, "admina").status_code == 201
    login_a = _login(org_a, "admina")
    headers_a = _auth_headers(login_a.json()["access_token"])
    room_a = _create_room(headers_a, rate=1000)

    start = _future_aligned(36)
    end = start + timedelta(hours=1)
    day = start.date().isoformat()

    initial_report = client.get(f"/admin/usage-report?from={day}&to={day}", headers=headers_a)
    assert initial_report.status_code == 200
    assert initial_report.json()["rooms"][0]["confirmed_bookings"] == 0

    booking = _create_booking(headers_a, room_a, start, end)
    assert booking.status_code == 201

    report_after_create = client.get(f"/admin/usage-report?from={day}&to={day}", headers=headers_a)
    assert report_after_create.status_code == 200
    assert report_after_create.json()["rooms"][0]["confirmed_bookings"] == 1
    assert report_after_create.json()["rooms"][0]["revenue_cents"] == 1000

    availability = client.get(f"/rooms/{room_a}/availability?date={day}", headers=headers_a)
    assert availability.status_code == 200
    assert len(availability.json()["busy"]) == 1

    stats_before_cancel = client.get(f"/rooms/{room_a}/stats", headers=headers_a)
    assert stats_before_cancel.status_code == 200
    assert stats_before_cancel.json()["total_confirmed_bookings"] == 1
    assert stats_before_cancel.json()["total_revenue_cents"] == 1000

    cancelled = client.post(f"/bookings/{booking.json()['id']}/cancel", headers=headers_a)
    assert cancelled.status_code == 200

    availability_after_cancel = client.get(f"/rooms/{room_a}/availability?date={day}", headers=headers_a)
    assert availability_after_cancel.status_code == 200
    assert len(availability_after_cancel.json()["busy"]) == 0

    stats_after_cancel = client.get(f"/rooms/{room_a}/stats", headers=headers_a)
    assert stats_after_cancel.status_code == 200
    assert stats_after_cancel.json()["total_confirmed_bookings"] == 0
    assert stats_after_cancel.json()["total_revenue_cents"] == 0

    report_after_cancel = client.get(f"/admin/usage-report?from={day}&to={day}", headers=headers_a)
    assert report_after_cancel.status_code == 200
    assert report_after_cancel.json()["rooms"][0]["confirmed_bookings"] == 0
    assert report_after_cancel.json()["rooms"][0]["revenue_cents"] == 0

    org_b = _unique_name("org-b")
    assert _register(org_b, "adminb").status_code == 201
    login_b = _login(org_b, "adminb")
    headers_b = _auth_headers(login_b.json()["access_token"])

    cross_export = client.get(f"/admin/export?room_id={room_a}&include_all=true", headers=headers_b)
    assert cross_export.status_code == 404
    assert cross_export.json()["code"] == "ROOM_NOT_FOUND"


def test_concurrent_conflict_and_quota_enforcement():
    org = _unique_name("org-concurrent")
    assert _register(org, "admin").status_code == 201
    assert _register(org, "member").status_code == 201
    admin_token = _login(org, "admin").json()["access_token"]
    member_token = _login(org, "member").json()["access_token"]
    room_id = _create_room(_auth_headers(admin_token))

    def create_in_thread(start: datetime, end: datetime):
        with TestClient(app) as local_client:
            return local_client.post(
                "/bookings",
                json={
                    "room_id": room_id,
                    "start_time": start.isoformat(),
                    "end_time": end.isoformat(),
                },
                headers=_auth_headers(member_token),
            )

    conflict_start = _future_aligned(70)
    conflict_end = conflict_start + timedelta(hours=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: create_in_thread(conflict_start, conflict_end), [0, 1]))
    statuses = [r.status_code for r in responses]
    assert statuses.count(201) == 1
    assert statuses.count(409) == 1

    windows = [2, 4, 6, 8]
    with ThreadPoolExecutor(max_workers=4) as pool:
        quota_responses = list(
            pool.map(
                lambda h: create_in_thread(_future_aligned(h), _future_aligned(h + 1)),
                windows,
            )
        )

    quota_statuses = [r.status_code for r in quota_responses]
    assert quota_statuses.count(201) == 3
    quota_errors = [r.json().get("code") for r in quota_responses if r.status_code == 409]
    assert quota_errors == ["QUOTA_EXCEEDED"]


def test_booking_quota_applies_to_members_only():
    org = _unique_name("org-member-quota")
    assert _register(org, "admin").status_code == 201
    assert _register(org, "member").status_code == 201

    admin_headers = _auth_headers(_login(org, "admin").json()["access_token"])
    member_headers = _auth_headers(_login(org, "member").json()["access_token"])

    room_id = _create_room(admin_headers)

    # Member: 4th booking within 24h must fail with QUOTA_EXCEEDED.
    member_statuses = []
    for hour in [2, 4, 6, 8]:
        start = _future_aligned(hour)
        end = _future_aligned(hour + 1)
        resp = _create_booking(member_headers, room_id, start, end)
        member_statuses.append((resp.status_code, resp.json().get("code")))

    assert [s for s, _ in member_statuses].count(201) == 3
    assert member_statuses[-1] == (409, "QUOTA_EXCEEDED")

    # Admin: same window should not be blocked by member quota rule.
    admin_statuses = []
    for hour in [10, 12, 14, 16]:
        start = _future_aligned(hour)
        end = _future_aligned(hour + 1)
        resp = _create_booking(admin_headers, room_id, start, end)
        admin_statuses.append(resp.status_code)

    assert admin_statuses == [201, 201, 201, 201]


def test_registration_concurrent_duplicate_username_returns_409():
    org = _unique_name("org-register-race")

    def register_same(_):
        with TestClient(app) as local_client:
            resp = local_client.post(
                "/auth/register",
                json={"org_name": org, "username": "same", "password": "pw12345"},
            )
            return resp.status_code, resp.json().get("code")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(register_same, [0, 1]))

    statuses = [status for status, _ in results]
    assert statuses.count(201) == 1
    assert statuses.count(409) == 1
    codes_409 = [code for status, code in results if status == 409]
    assert codes_409 == ["USERNAME_TAKEN"]


def test_registration_concurrent_new_org_assigns_admin_then_member():
    org = _unique_name("org-register-role-race")

    def register_user(username):
        with TestClient(app) as local_client:
            resp = local_client.post(
                "/auth/register",
                json={"org_name": org, "username": username, "password": "pw12345"},
            )
            return resp.status_code, resp.json().get("role")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(register_user, ["u1", "u2"]))

    statuses = [status for status, _ in results]
    roles = sorted([role for _, role in results])
    assert statuses == [201, 201]
    assert roles == ["admin", "member"]
