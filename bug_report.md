# Bug report — CoWork API

## Auth (`app/auth.py`, `app/routers/auth.py`)
1. **Access-token lifetime** — `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES * 60)` made tokens live 900 minutes instead of 900 seconds. Fixed to `timedelta(minutes=15)`.
2. **Logout never worked** — the revocation check compared the token's `sub` against a set of revoked `jti`s, so no token was ever treated as revoked. Now compares `jti`.
3. **Refresh tokens were reusable** — added `consume_refresh_token`: each refresh jti is recorded on use and reuse returns 401.
4. **Duplicate registration returned 200** with the existing user's data instead of `409 USERNAME_TAKEN`. Also added `IntegrityError` handling so concurrent registrations of the same username (or same new org name) are handled correctly instead of 500ing.

## Datetime handling (`app/timeutils.py`)
5. **Offset-carrying input was not converted to UTC** — `dt.replace(tzinfo=None)` just dropped the offset (e.g. `10:00+05:00` stored as `10:00`). Now `astimezone(timezone.utc)` first.

## Booking creation (`app/routers/bookings.py`)
6. **300-second grace window for past start times** (`start <= now - 300s`) — spec requires strictly-future start. Now `start <= now → 400`.
7. **Missing min-duration / end>start checks** — 0-hour and negative-duration bookings passed. Added `end <= start` and `duration < 1` → `400 INVALID_BOOKING_WINDOW`.
8. **Overlap check was inclusive** (`b.start <= end and start <= b.end`), rejecting legal back-to-back bookings. Now strict `existing.start < new.end AND new.start < existing.end`, done in SQL.
9. **Double-booking / quota race** — conflict and quota were plain check-then-write in Python with artificial sleeps widening the window. Conflict + quota + insert now run under a module-level lock, and the planted `time.sleep` "warmup/audit" pauses were removed.
10. **Quota applied per spec** — kept the (now, now+24h] window logic but scoped it to members per rule 4, and made it race-free (see above).

## Booking read/list (`app/routers/bookings.py`)
11. **`GET /bookings/{id}` returned `created_at` as `start_time`** — stray overwrite removed.
12. **Members could read any booking in their org** — added owner check (non-admin + not owner → `404 BOOKING_NOT_FOUND`), matching the existing cancel path.
13. **Pagination broken** — `offset(page * limit)` skipped the first page, `.limit(10)` ignored the `limit` param, and ordering was `start_time DESC`. Now `(page-1)*limit`, `limit(limit)`, ascending start_time with id tiebreak.

## Cancellation & refunds (`app/routers/bookings.py`, `app/services/refunds.py`)
14. **Refund tiers wrong** — exactly-48h notice fell into the 50% tier (`int(hours) > 48`), and the <24h tier refunded 50% instead of 0%. Now `notice >= 48h → 100`, `>= 24h → 50`, else `0`.
15. **Rounding** — response used banker's `round()` and the RefundLog used truncating `int()` float math, so the two could disagree and half-cents rounded wrong. Now one integer computation `(price_cents * percent + 50) // 100` (half-up), and `log_refund` stores the exact amount passed in.
16. **Concurrent-cancel race** — status check, refund insert (committed early), a planted sleep, then status commit allowed double cancels / duplicate refund logs. Now an atomic `UPDATE ... WHERE status='confirmed'`; losers get `409 ALREADY_CANCELLED`, and the refund log commits in the same transaction, so exactly one RefundLog exists.

## Concurrency services (`app/services/*`)
17. **Rate limiter race** (`ratelimit.py`) — unsynchronized read-modify-write with a sleep in the middle undercounted concurrent requests. Now guarded by a lock; sleep removed.
18. **Reference-code race** (`reference.py`) — counter read, 0.12s sleep, then write produced duplicate codes under concurrency. Now lock-protected increment.
19. **Notification deadlock** (`notifications.py`) — `notify_created` took email→audit locks while `notify_cancelled` took audit→email, deadlocking (and hanging the service) when a create and cancel overlapped. Locks are no longer nested.
20. **Room stats drift** (`stats.py` / `routers/rooms.py`) — in-memory counters had a read-sleep-write race and could permanently diverge from the DB. `/rooms/{id}/stats` now aggregates count/revenue directly from confirmed bookings, so it's always consistent.

## Caching (`app/cache.py` call sites)
21. **Stale caches** — booking creation didn't invalidate the usage-report cache, and cancellation didn't invalidate the availability cache, so both endpoints could serve stale data. Both mutations now invalidate both caches.

## Admin (`app/routers/admin.py`, `app/services/export.py`)
22. **Cross-org export leak** — `include_all=true&room_id=<other org's room>` used `fetch_bookings_raw`, which had no org filter, exposing another org's bookings. All export paths now go through the org-scoped query.
23. **Usage-report range** — only `YYYY-MM-DD` was accepted and the `to` bound was day-based. Now accepts full ISO datetimes too, with inclusive `[from, to]` semantics (date-only `to` still covers the whole day).

## Verification
- Original smoke test passes.
- A 56-assertion end-to-end script (functional + concurrency: concurrent same-slot booking, concurrent quota, concurrent cancel, rate-limit burst, reference uniqueness, tz normalization, refund tiers/rounding, pagination, multi-tenancy isolation, cache freshness, token lifecycle) passes against a live uvicorn server.
