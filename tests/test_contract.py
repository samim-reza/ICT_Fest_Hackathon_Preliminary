"""Full API contract tests: every endpoint, business rule, and error code.

Run with ``pytest``. Uses unique org names per test run so it can be executed
repeatedly against the same database. Concurrency rules (double-booking,
quota, cancel races, rate limiting, reference codes) are exercised with real
threads.
"""
import base64
import concurrent.futures as cf
import csv
import io
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# ---------- helpers ----------

def _future(hours: float, minutes: int = 0) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(hours=hours, minutes=minutes)
    ).replace(second=0, microsecond=0).isoformat()


def _register(org, user, pw="pw12345"):
    return client.post(
        "/auth/register", json={"org_name": org, "username": user, "password": pw}
    )


def _login(org, user, pw="pw12345"):
    r = client.post(
        "/auth/login", json={"org_name": org, "username": user, "password": pw}
    )
    assert r.status_code == 200, r.text
    return r.json()


def _hdr(tokens_or_access):
    tok = tokens_or_access["access_token"] if isinstance(tokens_or_access, dict) else tokens_or_access
    return {"Authorization": f"Bearer {tok}"}


def _claims(token):
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def _book(headers, room_id, start, end):
    return client.post(
        "/bookings",
        json={"room_id": room_id, "start_time": start, "end_time": end},
        headers=headers,
    )


@pytest.fixture()
def org():
    """A fresh org with an admin and two members, plus a room."""
    name = f"org-{uuid.uuid4().hex[:10]}"
    _register(name, "admin")
    _register(name, "m1")
    _register(name, "m2")
    admin = _hdr(_login(name, "admin"))
    m1 = _hdr(_login(name, "m1"))
    m2 = _hdr(_login(name, "m2"))
    room = client.post(
        "/rooms",
        json={"name": "Main", "capacity": 4, "hourly_rate_cents": 1000},
        headers=admin,
    ).json()["id"]
    return {"name": name, "admin": admin, "m1": m1, "m2": m2, "room": room}


# ---------- health ----------

def test_health():
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


# ---------- registration / login (rules 8, 15) ----------

def test_register_roles_and_duplicate():
    name = f"org-{uuid.uuid4().hex[:10]}"
    r = _register(name, "alice")
    assert r.status_code == 201
    body = r.json()
    assert body["role"] == "admin"
    assert set(body) == {"user_id", "org_id", "username", "role"}

    r = _register(name, "bob")
    assert r.status_code == 201 and r.json()["role"] == "member"

    r = _register(name, "alice")
    assert r.status_code == 409 and r.json()["code"] == "USERNAME_TAKEN"

    # same username in a different org is fine
    r = _register(f"org-{uuid.uuid4().hex[:10]}", "alice")
    assert r.status_code == 201


def test_login_and_token_claims():
    name = f"org-{uuid.uuid4().hex[:10]}"
    reg = _register(name, "alice").json()
    tokens = _login(name, "alice")
    assert tokens["token_type"] == "bearer"

    ac = _claims(tokens["access_token"])
    rf = _claims(tokens["refresh_token"])
    assert ac["type"] == "access" and rf["type"] == "refresh"
    assert ac["sub"] == str(reg["user_id"]) and isinstance(ac["sub"], str)
    assert ac["org"] == reg["org_id"] and ac["role"] == "admin"
    assert ac["exp"] - ac["iat"] == 900
    assert rf["exp"] - rf["iat"] == 7 * 24 * 3600
    assert ac["jti"] and rf["jti"] and ac["jti"] != rf["jti"]

    r = client.post("/auth/login", json={"org_name": name, "username": "alice", "password": "bad"})
    assert r.status_code == 401 and r.json()["code"] == "INVALID_CREDENTIALS"
    r = client.post("/auth/login", json={"org_name": "no-such-org", "username": "alice", "password": "pw12345"})
    assert r.status_code == 401 and r.json()["code"] == "INVALID_CREDENTIALS"


def test_refresh_rotation_single_use():
    name = f"org-{uuid.uuid4().hex[:10]}"
    _register(name, "alice")
    tokens = _login(name, "alice")

    r = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 200
    new = r.json()
    assert {"access_token", "refresh_token", "token_type"} <= set(new)
    assert client.get("/rooms", headers=_hdr(new)).status_code == 200

    # reuse of the old refresh token is rejected
    r = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 401
    # the new refresh token still works once
    r = client.post("/auth/refresh", json={"refresh_token": new["refresh_token"]})
    assert r.status_code == 200
    # an access token is not a refresh token
    r = client.post("/auth/refresh", json={"refresh_token": new["access_token"]})
    assert r.status_code == 401


def test_logout_revokes_only_presented_token():
    name = f"org-{uuid.uuid4().hex[:10]}"
    _register(name, "alice")
    t1, t2 = _login(name, "alice"), _login(name, "alice")
    assert client.get("/rooms", headers=_hdr(t1)).status_code == 200
    assert client.post("/auth/logout", headers=_hdr(t1)).status_code == 200
    assert client.get("/rooms", headers=_hdr(t1)).status_code == 401
    assert client.get("/rooms", headers=_hdr(t2)).status_code == 200


def test_bad_tokens_rejected():
    assert client.get("/rooms").status_code == 401
    assert client.get("/rooms", headers={"Authorization": "Bearer garbage"}).status_code == 401
    name = f"org-{uuid.uuid4().hex[:10]}"
    _register(name, "alice")
    tokens = _login(name, "alice")
    # refresh token cannot be used as an access token
    assert client.get("/rooms", headers=_hdr(tokens["refresh_token"])).status_code == 401


# ---------- rooms (rules 9, 13, 14) ----------

def test_rooms_crud_and_isolation(org):
    rooms = client.get("/rooms", headers=org["m1"]).json()
    assert [r["id"] for r in rooms] == [org["room"]]
    assert set(rooms[0]) == {"id", "org_id", "name", "capacity", "hourly_rate_cents"}

    r = client.post(
        "/rooms",
        json={"name": "x", "capacity": 1, "hourly_rate_cents": 1},
        headers=org["m1"],
    )
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"

    # cross-org room is invisible
    other = f"org-{uuid.uuid4().hex[:10]}"
    _register(other, "boss")
    oh = _hdr(_login(other, "boss"))
    assert client.get("/rooms", headers=oh).json() == []
    assert client.get(f"/rooms/{org['room']}/stats", headers=oh).status_code == 404
    assert (
        client.get(f"/rooms/{org['room']}/availability?date=2030-01-01", headers=oh).status_code
        == 404
    )


# ---------- booking validation (rules 1, 2) ----------

def test_booking_window_validation(org):
    cases = [
        (_future(-1), _future(1)),          # past start
        (_future(2), _future(2)),           # zero duration
        (_future(2), _future(1)),           # end before start
        (_future(2), _future(2, 30)),       # non-whole hours
        (_future(2), _future(11)),          # 9 hours
    ]
    for s, e in cases:
        r = _book(org["m1"], org["room"], s, e)
        assert r.status_code == 400 and r.json()["code"] == "INVALID_BOOKING_WINDOW", (s, e, r.text)

    # 8 hours is the max allowed
    r = _book(org["m1"], org["room"], _future(30), _future(38))
    assert r.status_code == 201, r.text
    assert r.json()["price_cents"] == 8000


def test_timezone_normalization(org):
    tz = timezone(timedelta(hours=5, minutes=30))
    start_utc = (datetime.now(timezone.utc) + timedelta(hours=40)).replace(second=0, microsecond=0)
    s = start_utc.astimezone(tz).isoformat()
    e = (start_utc + timedelta(hours=2)).astimezone(tz).isoformat()
    r = _book(org["admin"], org["room"], s, e)
    assert r.status_code == 201, r.text
    body = r.json()
    got = datetime.fromisoformat(body["start_time"])
    assert got.utcoffset() == timedelta(0)          # explicit UTC designator
    assert got == start_utc
    assert set(body) == {
        "id", "reference_code", "room_id", "user_id", "start_time",
        "end_time", "status", "price_cents", "created_at",
    }
    assert body["status"] == "confirmed"


def test_room_not_found_and_cross_org_room(org):
    r = _book(org["m1"], 999999, _future(2), _future(3))
    assert r.status_code == 404 and r.json()["code"] == "ROOM_NOT_FOUND"

    other = f"org-{uuid.uuid4().hex[:10]}"
    _register(other, "boss")
    oh = _hdr(_login(other, "boss"))
    oroom = client.post(
        "/rooms", json={"name": "o", "capacity": 1, "hourly_rate_cents": 100}, headers=oh
    ).json()["id"]
    r = _book(org["m1"], oroom, _future(2), _future(3))
    assert r.status_code == 404 and r.json()["code"] == "ROOM_NOT_FOUND"


# ---------- overlap (rule 3) ----------

def test_overlap_and_back_to_back(org):
    assert _book(org["m1"], org["room"], _future(50), _future(52)).status_code == 201
    for s, e in [(_future(51), _future(53)), (_future(49), _future(51)), (_future(49), _future(53)), (_future(50), _future(52))]:
        r = _book(org["m2"], org["room"], s, e)
        assert r.status_code == 409 and r.json()["code"] == "ROOM_CONFLICT", (s, e, r.text)
    # back-to-back on both sides is allowed
    assert _book(org["m2"], org["room"], _future(52), _future(53)).status_code == 201
    assert _book(org["m2"], org["room"], _future(49), _future(50)).status_code == 201
    # cancelled bookings do not block
    b = _book(org["m1"], org["room"], _future(60), _future(61)).json()
    client.post(f"/bookings/{b['id']}/cancel", headers=org["m1"])
    assert _book(org["m2"], org["room"], _future(60), _future(61)).status_code == 201


def test_concurrent_same_slot_only_one_wins(org):
    users = [org["admin"], org["m1"], org["m2"]] * 3
    s, e = _future(70), _future(71)
    with cf.ThreadPoolExecutor(len(users)) as ex:
        res = list(ex.map(lambda h: _book(h, org["room"], s, e), users))
    codes = sorted(r.status_code for r in res)
    assert codes.count(201) == 1, codes
    assert all(c in (201, 409) for c in codes)
    assert all(r.json()["code"] == "ROOM_CONFLICT" for r in res if r.status_code == 409)


# ---------- quota (rule 4) ----------

def test_quota_member_3_in_24h(org):
    for i in range(3):
        assert _book(org["m2"], org["room"], _future(1 + i), _future(2 + i)).status_code == 201
    r = _book(org["m2"], org["room"], _future(10), _future(11))
    assert r.status_code == 409 and r.json()["code"] == "QUOTA_EXCEEDED"
    # outside the 24h window is fine
    assert _book(org["m2"], org["room"], _future(30), _future(31)).status_code == 201
    # cancelling frees quota
    b = client.get("/bookings", headers=org["m2"]).json()["items"][0]
    client.post(f"/bookings/{b['id']}/cancel", headers=org["m2"])
    assert _book(org["m2"], org["room"], _future(10), _future(11)).status_code == 201


def test_quota_concurrent(org):
    slots = [(_future(i + 1), _future(i + 2)) for i in range(8)]
    with cf.ThreadPoolExecutor(8) as ex:
        res = list(ex.map(lambda se: _book(org["m1"], org["room"], se[0], se[1]), slots))
    codes = [r.status_code for r in res]
    assert codes.count(201) == 3, codes
    assert all(c in (201, 409) for c in codes)


# ---------- rate limit (rule 5) ----------

def test_rate_limit_20_per_60s():
    name = f"org-{uuid.uuid4().hex[:10]}"
    _register(name, "burst")
    _register(name, "calm")
    h = _hdr(_login(name, "burst"))
    room = client.post(
        "/rooms", json={"name": "rl", "capacity": 1, "hourly_rate_cents": 100},
        headers=h,
    ).json()["id"]

    # a successful booking counts toward the limit too
    assert _book(h, room, _future(200), _future(201)).status_code == 201

    # 24 more requests (invalid window, still counted): total 25 -> 5 over limit
    payload = {"room_id": room, "start_time": _future(-2), "end_time": _future(-1)}
    with cf.ThreadPoolExecutor(10) as ex:
        res = list(ex.map(lambda _: client.post("/bookings", json=payload, headers=h), range(24)))
    codes = [r.status_code for r in res]
    assert codes.count(429) == 5, sorted(codes)
    assert all(r.json()["code"] == "RATE_LIMITED" for r in res if r.status_code == 429)

    # once over the limit, every further POST /bookings is 429...
    assert _book(h, room, _future(202), _future(203)).status_code == 429
    # ...but the same user's other endpoints are not rate limited
    assert client.get("/bookings", headers=h).status_code == 200
    assert client.get("/rooms", headers=h).status_code == 200
    assert client.get(f"/rooms/{room}/stats", headers=h).status_code == 200

    # and another user in the same org is unaffected
    other = _hdr(_login(name, "calm"))
    assert _book(other, room, _future(210), _future(211)).status_code == 201


# ---------- reference codes (rule 7) ----------

def test_reference_codes_unique_under_concurrency(org):
    slots = [(_future(80 + i), _future(81 + i)) for i in range(6)]
    with cf.ThreadPoolExecutor(6) as ex:
        res = list(ex.map(lambda se: _book(org["admin"], org["room"], se[0], se[1]), slots))
    refs = [r.json()["reference_code"] for r in res if r.status_code == 201]
    assert len(refs) == 6 and len(set(refs)) == 6


# ---------- cancellation & refunds (rule 6) ----------

def test_refund_tiers_and_rounding(org):
    # 999-cent room so 50% of 999 = 499.5 -> rounds up to 500
    room999 = client.post(
        "/rooms", json={"name": "odd", "capacity": 1, "hourly_rate_cents": 999},
        headers=org["admin"],
    ).json()["id"]

    for hours, pct, expected in [(60, 100, 999), (30, 50, 500), (5, 0, 0)]:
        b = _book(org["m1"], room999, _future(hours), _future(hours + 1)).json()
        r = client.post(f"/bookings/{b['id']}/cancel", headers=org["m1"])
        assert r.status_code == 200, r.text
        body = r.json()
        assert body == {
            "id": b["id"], "status": "cancelled",
            "refund_percent": pct, "refund_amount_cents": expected,
        }
        detail = client.get(f"/bookings/{b['id']}", headers=org["m1"]).json()
        assert detail["status"] == "cancelled"
        assert len(detail["refunds"]) == 1
        assert detail["refunds"][0]["amount_cents"] == expected
        assert detail["refunds"][0]["status"] == "processed"

    # cancelling again -> 409
    r = client.post(f"/bookings/{b['id']}/cancel", headers=org["m1"])
    assert r.status_code == 409 and r.json()["code"] == "ALREADY_CANCELLED"


def test_cancel_permissions(org):
    b = _book(org["m1"], org["room"], _future(40), _future(41)).json()
    # another member cannot cancel (or even see) it
    r = client.post(f"/bookings/{b['id']}/cancel", headers=org["m2"])
    assert r.status_code == 404 and r.json()["code"] == "BOOKING_NOT_FOUND"
    # cross-org admin cannot
    other = f"org-{uuid.uuid4().hex[:10]}"
    _register(other, "boss")
    r = client.post(f"/bookings/{b['id']}/cancel", headers=_hdr(_login(other, "boss")))
    assert r.status_code == 404
    # same-org admin can
    assert client.post(f"/bookings/{b['id']}/cancel", headers=org["admin"]).status_code == 200


def test_concurrent_cancel_single_refund(org):
    b = _book(org["m1"], org["room"], _future(90), _future(91)).json()
    with cf.ThreadPoolExecutor(6) as ex:
        res = list(ex.map(lambda _: client.post(f"/bookings/{b['id']}/cancel", headers=org["m1"]), range(6)))
    codes = [r.status_code for r in res]
    assert codes.count(200) == 1 and codes.count(409) == 5, codes
    winner = next(r for r in res if r.status_code == 200)
    detail = client.get(f"/bookings/{b['id']}", headers=org["m1"]).json()
    assert len(detail["refunds"]) == 1
    assert detail["refunds"][0]["amount_cents"] == winner.json()["refund_amount_cents"]


# ---------- booking visibility & detail (rules 9, 10) ----------

def test_booking_detail_and_visibility(org):
    b = _book(org["m1"], org["room"], _future(45), _future(46)).json()
    r = client.get(f"/bookings/{b['id']}", headers=org["m1"])
    assert r.status_code == 200
    body = r.json()
    assert body["start_time"] == b["start_time"]  # not created_at
    assert body["refunds"] == []
    # other member -> 404, admin -> 200, cross-org -> 404
    assert client.get(f"/bookings/{b['id']}", headers=org["m2"]).status_code == 404
    assert client.get(f"/bookings/{b['id']}", headers=org["admin"]).status_code == 200
    other = f"org-{uuid.uuid4().hex[:10]}"
    _register(other, "boss")
    assert client.get(f"/bookings/{b['id']}", headers=_hdr(_login(other, "boss"))).status_code == 404
    assert client.get("/bookings/999999", headers=org["m1"]).status_code == 404


# ---------- pagination (rule 11) ----------

def test_pagination_no_skip_no_repeat(org):
    created = []
    for i in range(5):
        r = _book(org["m2"], org["room"], _future(100 + 2 * i), _future(101 + 2 * i))
        created.append(r.json()["id"])
    p1 = client.get("/bookings?page=1&limit=2", headers=org["m2"]).json()
    p2 = client.get("/bookings?page=2&limit=2", headers=org["m2"]).json()
    p3 = client.get("/bookings?page=3&limit=2", headers=org["m2"]).json()
    assert p1["total"] == 5 and p1["page"] == 1 and p1["limit"] == 2
    all_items = p1["items"] + p2["items"] + p3["items"]
    ids = [i["id"] for i in all_items]
    assert sorted(ids) == sorted(created) and len(set(ids)) == 5
    starts = [i["start_time"] for i in all_items]
    assert starts == sorted(starts)
    # only the caller's own bookings appear
    assert all(client.get(f"/bookings/{i}", headers=org["m2"]).status_code == 200 for i in ids)
    m1_list = client.get("/bookings", headers=org["m1"]).json()
    assert not set(ids) & {i["id"] for i in m1_list["items"]}
    # defaults
    d = client.get("/bookings", headers=org["m2"]).json()
    assert d["page"] == 1 and d["limit"] == 10
    # limit bounds are enforced by validation
    assert client.get("/bookings?limit=101", headers=org["m2"]).status_code == 422
    assert client.get("/bookings?page=0", headers=org["m2"]).status_code == 422


# ---------- availability (rule 13) ----------

def test_availability_fresh_and_sorted(org):
    s = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(hours=48)
    day = s.date().isoformat()
    b1 = _book(org["m1"], org["room"], s.isoformat(), (s + timedelta(hours=1)).isoformat()).json()
    b2 = _book(org["m2"], org["room"], (s + timedelta(hours=2)).isoformat(), (s + timedelta(hours=3)).isoformat()).json()
    av = client.get(f"/rooms/{org['room']}/availability?date={day}", headers=org["m1"]).json()
    assert av["room_id"] == org["room"] and av["date"] == day
    busy = av["busy"]
    assert [x["start_time"] for x in busy] == sorted(x["start_time"] for x in busy)
    assert len(busy) >= 2
    # cancel reflects immediately
    client.post(f"/bookings/{b2['id']}/cancel", headers=org["m2"])
    av2 = client.get(f"/rooms/{org['room']}/availability?date={day}", headers=org["m1"]).json()
    assert len(av2["busy"]) == len(busy) - 1
    assert not any(x["start_time"] == b2["start_time"] for x in av2["busy"])


# ---------- stats (rule 14) ----------

def test_stats_consistent_after_burst(org):
    room = client.post(
        "/rooms", json={"name": "stat", "capacity": 2, "hourly_rate_cents": 700},
        headers=org["admin"],
    ).json()["id"]
    slots = [(_future(120 + i), _future(121 + i)) for i in range(6)]
    with cf.ThreadPoolExecutor(6) as ex:
        res = list(ex.map(lambda se: _book(org["admin"], room, se[0], se[1]), slots))
    ids = [r.json()["id"] for r in res if r.status_code == 201]
    client.post(f"/bookings/{ids[0]}/cancel", headers=org["admin"])
    st = client.get(f"/rooms/{room}/stats", headers=org["m1"]).json()
    assert st == {
        "room_id": room,
        "total_confirmed_bookings": len(ids) - 1,
        "total_revenue_cents": 700 * (len(ids) - 1),
    }


# ---------- usage report (rule 12) ----------

def test_usage_report(org):
    r = client.get("/admin/usage-report?from=2030-01-01&to=2030-01-31", headers=org["m1"])
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"

    room2 = client.post(
        "/rooms", json={"name": "empty", "capacity": 1, "hourly_rate_cents": 100},
        headers=org["admin"],
    ).json()["id"]
    b = _book(org["m1"], org["room"], _future(48), _future(50)).json()
    day = datetime.fromisoformat(b["start_time"]).date()
    frm, to = day.isoformat(), day.isoformat()
    rep = client.get(f"/admin/usage-report?from={frm}&to={to}", headers=org["admin"]).json()
    assert rep["from"] == frm and rep["to"] == to
    by_room = {x["room_id"]: x for x in rep["rooms"]}
    assert room2 in by_room and by_room[room2] == {
        "room_id": room2, "room_name": "empty", "confirmed_bookings": 0, "revenue_cents": 0,
    }
    row = by_room[org["room"]]
    assert row["confirmed_bookings"] >= 1 and row["revenue_cents"] >= b["price_cents"]

    # reflects a cancel immediately
    client.post(f"/bookings/{b['id']}/cancel", headers=org["m1"])
    rep2 = client.get(f"/admin/usage-report?from={frm}&to={to}", headers=org["admin"]).json()
    row2 = {x["room_id"]: x for x in rep2["rooms"]}[org["room"]]
    assert row2["confirmed_bookings"] == row["confirmed_bookings"] - 1
    assert row2["revenue_cents"] == row["revenue_cents"] - b["price_cents"]

    # cross-org admin sees only their own (empty) rooms
    other = f"org-{uuid.uuid4().hex[:10]}"
    _register(other, "boss")
    rep3 = client.get(
        f"/admin/usage-report?from={frm}&to={to}", headers=_hdr(_login(other, "boss"))
    ).json()
    assert rep3["rooms"] == []


# ---------- export ----------

def test_export(org):
    b1 = _book(org["admin"], org["room"], _future(140), _future(141)).json()
    b2 = _book(org["m1"], org["room"], _future(142), _future(143)).json()

    assert client.get("/admin/export", headers=org["m1"]).status_code == 403

    def rows(params=""):
        text = client.get(f"/admin/export{params}", headers=org["admin"]).text
        lines = text.strip().splitlines()
        assert lines[0] == "id,reference_code,room_id,user_id,start_time,end_time,status,price_cents"
        return list(csv.DictReader(io.StringIO(text)))

    own = rows()
    ids = {int(r["id"]) for r in own}
    assert b1["id"] in ids and b2["id"] not in ids  # default: caller's own only

    allrows = rows("?include_all=true")
    ids = {int(r["id"]) for r in allrows}
    assert b1["id"] in ids and b2["id"] in ids

    # cross-org admin cannot pull this org's room even with include_all
    other = f"org-{uuid.uuid4().hex[:10]}"
    _register(other, "boss")
    text = client.get(
        f"/admin/export?room_id={org['room']}&include_all=true",
        headers=_hdr(_login(other, "boss")),
    ).text
    assert len(text.strip().splitlines()) == 1  # header only
