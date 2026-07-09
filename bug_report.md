# Bug Report

This report summarizes major defects found and fixed in the CoWork API implementation.

## 1) Access token TTL incorrect
- Files/lines: app/auth.py:49-58
- Bug: Access token lifetime was computed incorrectly in earlier code, violating 900-second rule.
- Fix: Set access lifetime to `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)` and kept `exp - iat == 900`.

## 2) Logout token revocation mismatch
- Files/lines: app/auth.py:80-123
- Bug: Revocation checks did not reliably invalidate access tokens after logout.
- Fix: Store and check revoked access token JTIs in persistent DB table (`RevokedToken`).

## 3) Refresh token reuse accepted
- Files/lines: app/auth.py:95-106, app/routers/auth.py:79-88
- Bug: Refresh tokens could be reused.
- Fix: Added persistent single-use refresh tracking (`UsedRefreshToken`) and return 401 on reuse.

## 4) Duplicate username registration race
- Files/lines: app/routers/auth.py:24-70
- Bug: Concurrent duplicate registration could raise unhandled DB `IntegrityError` instead of contract `409 USERNAME_TAKEN`.
- Fix: Serialized registration path (`BEGIN IMMEDIATE`), added safe duplicate mapping, and retry handling.

## 5) Datetime offset handling incorrect
- Files/lines: app/timeutils.py:5-14
- Bug: Offset timestamps were stripped, not converted to UTC.
- Fix: Convert using `astimezone(timezone.utc)` before naive storage.

## 6) Booking window validation bugs
- Files/lines: app/routers/bookings.py:82-92
- Bug: Missing strict checks for `end > start`, minimum duration, and strict future start.
- Fix: Enforced all rulebook constraints with `INVALID_BOOKING_WINDOW`.

## 7) Conflict rule and concurrency
- Files/lines: app/routers/bookings.py:40-48, 96-126
- Bug: Overlap behavior and race safety were not guaranteed.
- Fix: Used strict overlap condition and serialized create flow with `BEGIN IMMEDIATE`.

## 8) Booking quota semantics and race safety
- Files/lines: app/routers/bookings.py:51-66, 106-107
- Bug: Quota handling needed strict role alignment and concurrent safety.
- Fix: Quota check remains inside serialized booking creation and now applies to `member` role only (per rule text).

## 9) Rate limit race safety
- Files/lines: app/services/ratelimit.py:18-43
- Bug: Rolling-window rate limiting could race under concurrency.
- Fix: DB-backed event model (`RateLimitEvent`) with serialized check+insert via `BEGIN IMMEDIATE`.

## 10) Refund policy and rounding mismatches
- Files/lines: app/routers/bookings.py:210-224, app/services/refunds.py:14-26
- Bug: Tier boundaries and rounding could diverge from contract, and response/log mismatch risk existed.
- Fix: Correct tier logic (`>=48`, `24-48`, `<24`) and integer half-up rounding via shared `calculate_refund_amount`.

## 11) Cancellation concurrency (exactly one refund log)
- Files/lines: app/models.py:66-73, app/routers/bookings.py:193-227
- Bug: Concurrent cancel attempts could violate one-refund invariant.
- Fix: Added unique constraint on `refund_logs.booking_id` and serialized cancel flow with proper 409 mapping.

## 12) Pagination and ordering defects
- Files/lines: app/routers/bookings.py:136-147
- Bug: Ordering and paging could skip/repeat items.
- Fix: Ascending order by `(start_time, id)`, correct offset formula, dynamic limit usage.

## 13) Booking visibility and tenancy checks
- Files/lines: app/routers/bookings.py:157-172, 200-203
- Bug: Member could access/cancel others’ bookings in some paths.
- Fix: Enforced member-owner restriction with `404 BOOKING_NOT_FOUND`; admins retain org-wide access.

## 14) Usage report and availability staleness
- Files/lines: app/routers/bookings.py:127-129, 228-230, app/routers/rooms.py:57
- Bug: Cached report/availability could become stale after mutations.
- Fix: Invalidate relevant caches on create/cancel/room-create paths.

## 15) Export multi-tenancy leak risk
- Files/lines: app/routers/admin.py:71-77, app/services/export.py:19-41
- Bug: Export needed strict org scoping and room ownership checks.
- Fix: Added room ownership guard and org-scoped export query path.

## 16) Room stats consistency
- Files/lines: app/routers/rooms.py:104-117
- Bug: In-memory stats can diverge from bookings table.
- Fix: Stats endpoint now computes directly from DB confirmed bookings.

## 17) Liveness/deadlock risk in notifications
- Files/lines: app/services/notifications.py:9-31
- Bug: Lock ordering risk and blocking behavior could harm liveness.
- Fix: Unified single lock and removed blocking sleep path.

## Validation
- Full test suite passes: 9 passed.
- Added targeted concurrency tests for:
  - conflict/quota behavior
  - duplicate username race handling
  - admin/member registration race roles
  - member-only quota enforcement
