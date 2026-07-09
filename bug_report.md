# Bug Report

## 1. What We Did
For this preliminary challenge, we compared the broken baseline code (main branch) against our updated implementation and fixed all behavior that violated the rulebook and API contract.

Our goal was simple: fix bugs only, keep the API contract unchanged, and make sure logic still works under concurrency.

## 2. Quick Summary For Judges
- Total major bug categories fixed: 19
- Focus areas: auth, booking logic, refunds, concurrency safety, multi-tenancy, report consistency, liveness
- API contract preserved: paths, status codes, error codes, and response field names
- Validation result: 9 tests passed, including concurrency-focused checks

## 3. Detailed Bug Fixes

### 3.1 Access token expiry was wrong
- Where we found it: app/auth.py (create_access_token)
- What was wrong: Access token lifetime was effectively 15 hours instead of 15 minutes.
- Why it caused incorrect behavior: Violated the rule requiring exactly 900 seconds.
- How we fixed it: Changed lifetime calculation to 15 minutes exactly.
- How we verified it: JWT claim check in tests confirms exp - iat = 900.

### 3.2 Logout did not reliably invalidate access tokens
- Where we found it: app/auth.py (token revocation path)
- What was wrong: Revocation logic was inconsistent in older code and could fail to block reused tokens.
- Why it caused incorrect behavior: Logged-out tokens could still access protected endpoints.
- How we fixed it: Added persistent revoked-token tracking by jti (RevokedToken) and checked it on every authenticated request.
- How we verified it: Logout test now confirms subsequent access returns 401.

### 3.3 Refresh token reuse was accepted
- Where we found it: app/auth.py, app/routers/auth.py
- What was wrong: The same refresh token could be used multiple times.
- Why it caused incorrect behavior: Violated single-use refresh requirement.
- How we fixed it: Added persistent used-refresh token tracking (UsedRefreshToken); reuse now returns 401.
- How we verified it: Refresh endpoint test: first call 200, second call with same token 401.

### 3.4 Duplicate username registration behavior was wrong
- Where we found it: app/routers/auth.py
- What was wrong: Duplicate username previously returned success-like behavior in earlier code.
- Why it caused incorrect behavior: Violated required 409 USERNAME_TAKEN contract.
- How we fixed it: Return 409 USERNAME_TAKEN for duplicates.
- How we verified it: Registration duplicate test now passes.

### 3.5 Concurrent registration race could crash
- Where we found it: app/routers/auth.py
- What was wrong: Concurrent duplicate registration could raise unhandled DB IntegrityError.
- Why it caused incorrect behavior: Could return server failure instead of contract error.
- How we fixed it: Serialized registration transaction (BEGIN IMMEDIATE), safe duplicate mapping, and retry-safe flow.
- How we verified it: Concurrency test confirms one 201 and one 409 USERNAME_TAKEN.

### 3.6 Datetime offset normalization was incorrect
- Where we found it: app/timeutils.py
- What was wrong: Offset-aware input timestamps were stripped, not converted.
- Why it caused incorrect behavior: Shifted booking windows, conflicts, and refunds.
- How we fixed it: Converted to UTC before storing/comparing.
- How we verified it: Timezone input test confirms normalized UTC behavior.

### 3.7 Booking window validation was incomplete
- Where we found it: app/routers/bookings.py
- What was wrong: Earlier logic allowed invalid time windows and grace behavior.
- Why it caused incorrect behavior: Could accept invalid bookings.
- How we fixed it: Enforced strict future start, end > start, whole-hour duration, and 1..8 hour bounds.
- How we verified it: Validation tests for past, zero-duration, and non-whole-hour windows.

### 3.8 Double-booking logic and atomicity issues
- Where we found it: app/routers/bookings.py
- What was wrong: Overlap logic and create path were not robust under race conditions.
- Why it caused incorrect behavior: Back-to-back handling and conflict detection could fail under load.
- How we fixed it: Strict overlap predicate and serialized create transaction.
- How we verified it: Concurrency test confirms one success and one 409 conflict in parallel overlap attempts.

### 3.9 Booking quota concurrency and role alignment
- Where we found it: app/routers/bookings.py
- What was wrong: Quota checks were vulnerable to race and needed stricter role alignment.
- Why it caused incorrect behavior: Could exceed quota under concurrency or apply to wrong role.
- How we fixed it: Quota check inside serialized create transaction; applied to member role.
- How we verified it: Parallel quota test plus member-only quota regression test.

### 3.10 Rate limiting could race under concurrency
- Where we found it: app/services/ratelimit.py
- What was wrong: In-memory rolling window was race-prone.
- Why it caused incorrect behavior: 20-per-60s rule could be bypassed or miscounted.
- How we fixed it: DB-backed RateLimitEvent model with serialized check+insert in one transaction.
- How we verified it: Stress probe confirms 21st request gets 429 RATE_LIMITED.

### 3.11 Refund tier boundaries and rounding were incorrect
- Where we found it: app/routers/bookings.py, app/services/refunds.py
- What was wrong: Earlier tier logic and rounding could diverge from rulebook and each other.
- Why it caused incorrect behavior: Wrong refund amounts and possible mismatch between response and RefundLog.
- How we fixed it: Implemented exact tier boundaries and integer half-up rounding shared by response and log path.
- How we verified it: Refund policy tests cover >=48h, 24-48h, and <24h tiers and expected cent amounts.

### 3.12 Concurrent cancellation could violate one-refund invariant
- Where we found it: app/models.py, app/routers/bookings.py
- What was wrong: Parallel cancellation attempts could create duplicate refund records.
- Why it caused incorrect behavior: Violated exactly-one RefundLog entry requirement.
- How we fixed it: Unique constraint on refund_logs.booking_id and serialized cancellation logic.
- How we verified it: Repeat-cancel returns 409 ALREADY_CANCELLED and detail shows a single refund entry.

### 3.13 Reference code uniqueness risk
- Where we found it: app/services/reference.py, app/models.py, app/routers/bookings.py
- What was wrong: Reference generation had race/duplication risk in earlier design.
- Why it caused incorrect behavior: Could violate unique booking reference rule.
- How we fixed it: Collision-resistant generation plus unique DB constraint and guarded assignment.
- How we verified it: Multi-booking checks confirmed unique reference values.

### 3.14 Pagination and ordering were broken
- Where we found it: app/routers/bookings.py
- What was wrong: Wrong sort direction, wrong offset formula, hardcoded page limit.
- Why it caused incorrect behavior: Skipped/repeated items and wrong order.
- How we fixed it: Ascending order by start_time then id, offset=(page-1)*limit, and dynamic limit.
- How we verified it: Pagination tests confirm no overlap between pages and correct ordering.

### 3.15 Booking visibility and member restrictions
- Where we found it: app/routers/bookings.py
- What was wrong: Member access checks were incomplete in older flow.
- Why it caused incorrect behavior: Members could access others' bookings in some paths.
- How we fixed it: Enforced member-owner checks with 404 BOOKING_NOT_FOUND.
- How we verified it: Visibility tests for member/member and admin/member access paths.

### 3.16 Usage report and availability cache staleness
- Where we found it: app/routers/bookings.py, app/routers/rooms.py, app/cache.py
- What was wrong: Read caches could serve stale data after write operations.
- Why it caused incorrect behavior: Violated immediate consistency requirements.
- How we fixed it: Added cache invalidation on create/cancel/room-create write paths.
- How we verified it: Report and availability tests confirm immediate updates after create/cancel.

### 3.17 Export multi-tenancy leakage risk
- Where we found it: app/routers/admin.py, app/services/export.py
- What was wrong: Export path could expose data across organizations in older logic.
- Why it caused incorrect behavior: Violated strict tenant isolation.
- How we fixed it: Added org ownership guard for room_id and org-scoped export query.
- How we verified it: Cross-org export attempt test returns 404 ROOM_NOT_FOUND.

### 3.18 Room stats could diverge from real bookings
- Where we found it: app/routers/rooms.py
- What was wrong: In-memory counters could drift from DB truth.
- Why it caused incorrect behavior: Stats endpoint could return inconsistent totals.
- How we fixed it: Stats now derived directly from confirmed bookings in DB.
- How we verified it: Stats checks after create and cancel match expected counts and revenue.

### 3.19 Liveness and deadlock risk
- Where we found it: app/services/notifications.py and multiple hot paths
- What was wrong: Older lock ordering and sleep behavior could stall the service.
- Why it caused incorrect behavior: Risked hangs under concurrent traffic.
- How we fixed it: Unified notification lock order and removed blocking sleep traps in critical paths.
- How we verified it: Concurrency-focused API tests complete without service hang.

## 4. Validation Evidence
- Full automated test suite: 9 passed.
- Added and passed targeted concurrency tests for:
  - conflict + quota under parallel requests
  - duplicate username registration race
  - concurrent first registration role assignment (admin/member)
  - member-only quota enforcement

## 5. Final Note
We intentionally kept the API contract unchanged while fixing bugs. The focus stayed on correctness and concurrency safety, not feature expansion or unrelated refactoring.
