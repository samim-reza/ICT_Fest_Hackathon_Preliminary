# Bug Report

## Updated Fixed Bugs

### 1) Access token expiration incorrect
- Main branch behavior: `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES * 60)` (15 hours).
- Updated fix: `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)` in `app/auth.py`.
- Rule impact: Rule 8 (exact 900-second access token lifetime).

### 2) Logout revocation ineffective
- Main branch behavior: revoked set stored `jti`, but check used `sub`.
- Updated fix: persistent revoked-token table (`RevokedToken`) and JTI-based checks in `app/auth.py`.
- Rule impact: Rule 8 (logout must immediately invalidate presented access token).

### 3) Refresh token reuse accepted
- Main branch behavior: refresh endpoint issued new tokens without invalidating old refresh token.
- Updated fix: persistent used-refresh tracking (`UsedRefreshToken`) and 401 on reuse in `app/auth.py` + `app/routers/auth.py`.
- Rule impact: Rule 8 (single-use refresh token).

### 4) Duplicate username handling wrong
- Main branch behavior: duplicate register returned existing user object (201 path).
- Updated fix: deterministic `409 USERNAME_TAKEN` in `app/routers/auth.py`.
- Rule impact: Rule 15.

### 5) Concurrent register race could crash
- Main branch behavior: duplicate username race could raise unhandled DB `IntegrityError`.
- Updated fix: serialized registration transaction (`BEGIN IMMEDIATE`) + safe duplicate mapping in `app/routers/auth.py`.
- Rule impact: Rule 15 and Rule 16 stability.

### 6) Datetime offset conversion incorrect
- Main branch behavior: stripped timezone via `replace(tzinfo=None)`.
- Updated fix: normalize with `astimezone(timezone.utc).replace(tzinfo=None)` in `app/timeutils.py`.
- Rule impact: Rule 1.

### 7) Booking window validation defects
- Main branch behavior: allowed grace window in past, incomplete duration validation.
- Updated fix: strict `start > now`, `end > start`, whole-hour duration, and 1..8-hour bounds in `app/routers/bookings.py`.
- Rule impact: Rule 2.

### 8) Double-booking logic and race safety
- Main branch behavior: wrong overlap condition (`<=`) and non-atomic conflict path.
- Updated fix: strict overlap (`existing.start < new.end` and `new.start < existing.end`) and serialized create flow (`BEGIN IMMEDIATE`) in `app/routers/bookings.py`.
- Rule impact: Rule 3 (including concurrent requests).

### 9) Quota rule and concurrency
- Main branch behavior: quota path vulnerable to race and role semantics were unclear.
- Updated fix: quota check inside serialized create transaction and applied to `member` role only in `app/routers/bookings.py`.
- Rule impact: Rule 4 and Rule 15 wording alignment.

### 10) Rate-limit concurrency gaps
- Main branch behavior: in-memory bucket with race-prone updates.
- Updated fix: DB-backed rolling window (`RateLimitEvent`) + serialized check/insert (`BEGIN IMMEDIATE`) in `app/services/ratelimit.py`.
- Rule impact: Rule 5 (must hold under concurrency).

### 11) Refund tiers and rounding incorrect
- Main branch behavior: wrong tier boundary/else path and inconsistent rounding implementation.
- Updated fix: exact tier logic (`>=48`, `24-48`, `<24`) and integer half-up rounding with shared calculator in `app/routers/bookings.py` + `app/services/refunds.py`.
- Rule impact: Rule 6.

### 12) Concurrent cancel could violate one-refund invariant
- Main branch behavior: cancel path not robust to concurrent duplicate attempts.
- Updated fix: unique constraint on `refund_logs.booking_id` plus serialized cancel flow and 409 mapping in `app/models.py` + `app/routers/bookings.py`.
- Rule impact: Rule 6 (exactly one refund log entry).

### 13) Reference code uniqueness risk
- Main branch behavior: in-memory counter and race window could generate duplicates.
- Updated fix: collision-resistant code generation + DB uniqueness on booking `reference_code` with guarded assignment path.
- Rule impact: Rule 7.

### 14) Pagination and ordering broken
- Main branch behavior: descending order, wrong offset formula, hardcoded limit.
- Updated fix: ascending by `(start_time, id)`, `offset((page-1)*limit)`, dynamic `limit(limit)` in `app/routers/bookings.py`.
- Rule impact: Rule 11.

### 15) Booking visibility and tenant boundaries
- Main branch behavior: member could read/cancel other members in same org on some paths.
- Updated fix: member-owner checks returning `404 BOOKING_NOT_FOUND`; admin org-wide access preserved in `app/routers/bookings.py`.
- Rule impact: Rules 9 and 10.

### 16) Usage report and availability cache staleness
- Main branch behavior: stale report/availability after create/cancel flows.
- Updated fix: cache invalidation on mutating paths (`create booking`, `cancel booking`, `create room`) in routers.
- Rule impact: Rules 12 and 13 (immediate consistency).

### 17) Export multi-tenancy leak risk
- Main branch behavior: export logic could expose cross-org data via room id path.
- Updated fix: room ownership guard in admin route and org-scoped export query in service.
- Rule impact: Rule 9.

### 18) Room stats inconsistency risk
- Main branch behavior: stats could diverge from persisted bookings due to in-memory state.
- Updated fix: stats endpoint computes from DB confirmed bookings in `app/routers/rooms.py`.
- Rule impact: Rule 14.

### 19) Liveness/deadlock and sleep traps
- Main branch behavior: lock-order inversion and blocking sleep patterns in critical paths.
- Updated fix: single-order notification lock and removal of blocking sleeps from critical logic.
- Rule impact: Rule 16.

## Validation Evidence
- Full automated test suite: `9 passed`.
- Added concurrency-focused tests for:
  - booking conflict + quota under parallel requests
  - duplicate username registration race
  - concurrent first registration role assignment (admin/member)
  - member-only quota enforcement

## Remaining Known Risks
- No high-confidence contract violations are currently known from this comparison.
- Final recommendation before submission: run a fresh Docker build with a clean data volume and execute smoke checks against all major endpoints.
