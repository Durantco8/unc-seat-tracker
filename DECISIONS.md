# Decisions Log — UNC Seat Tracker

This file explains the *why* behind the technical choices made in this project, stage by stage. It's meant to be read before a technical interview as a refresher on decisions that are easy to make once but hard to re-explain from memory months later.

---

## Stage 1 — Scraper + persistence

### Why `upsert_section` returns `(section_id, old_available_seats)`
Stage 3's change detection needs to compare "what the seat count was" against "what it is now." Instead of doing a separate read-then-write (two queries), `upsert_section` returns the *previous* value as part of the same call that writes the *new* value. Both the read and the write happen inside one transaction (`engine.begin()`), so they're atomic — there's no window where another process could read a stale value between the read and the write. This avoids a classic read-then-write race condition.

### Why `create_engine(":memory:")` in tests
SQLite supports in-memory databases that exist only for the life of the connection. Tests get a fresh, isolated database with no disk I/O and no cleanup step — schema and upsert tests don't need a live server or file at all. This is why the test suite runs in a fraction of a second.

### Why the SQLite pragmas
- `PRAGMA foreign_keys=ON` — SQLite ignores foreign key constraints by default. Without this, the database would silently allow a `watch` to point at a `section` that doesn't exist. Turning it on makes the schema actually enforce referential integrity.
- `PRAGMA journal_mode=WAL` — SQLite's default mode locks the entire database during a write, blocking all readers until it finishes. WAL (Write-Ahead Logging) lets readers keep working while a write is in progress. This matters once a polling loop is writing and an API is reading at the same time (Stage 4 onward).

### Why SQLite now, Postgres later
SQLite was chosen for Stage 1–4 because it requires zero setup and is fast to iterate on solo. The known limitation is weaker support for concurrent writers than Postgres. The plan is to migrate to Postgres once true concurrent multi-worker polling is needed at a scale where SQLite's write-locking becomes a real bottleneck — a deliberate "start simple, upgrade when a real limit is hit" choice, not an oversight.

---

## Stage 2 — Polling loop

### Why deduplication happens at the course level, not the section level
`get_watched_courses()` joins `sections` to `watches`, filters for `active=True`, and returns `DISTINCT (term, subject, catalog_number)` — not distinct sections. This is because a single `check_seats()` call returns *all* sections for a course in one HTTP request. If 50 users are watching 10 different sections of COMP 311, it's still just one request to UNC's site, not ten. Deduplicating at the wrong granularity would have meant unnecessary duplicate requests.

### Why per-course error isolation
Each `check_seats()` call inside the polling pass is wrapped in its own try/except. If one course's request times out or fails, it's logged and the loop moves on to the next course rather than the whole pass failing. One flaky course shouldn't take down monitoring for every other watched course.

### Why consecutive-failure tracking and suspension
A `failures` dict counts consecutive failures per course; a success resets the count to 0. After 3 consecutive failures, that course is suspended for the life of the process. This prevents the poller from repeatedly hammering a permanently broken query (e.g. a course code that no longer exists) every cycle indefinitely.

### Why global backoff on a fully-failed pass
If *every* course fails in a single pass, that's a signal the server itself is likely down, not that individual courses are broken. In that case the sleep interval is doubled for that cycle — a direct implementation of the "back off instead of retrying aggressively" politeness constraint.

### Why `time.monotonic()` for pacing
Elapsed time is measured from the start of each pass, and only the *remaining* time is slept. If scraping 10 courses takes 20 seconds and the interval is 180s, the loop sleeps 160s, not the full 180s. This keeps passes evenly spaced regardless of how long the scraping itself takes, rather than drifting later over time.

### Why `run_poll_pass()` was extracted from the `while True` loop
The loop itself (sleep timing, backoff) is trivial glue logic. The actual logic — scrape, detect, notify — is where bugs hide and where correctness matters. Extracting that into a standalone `run_poll_pass()` function makes it directly callable and testable without needing to run (or mock) an infinite loop. The same "extract the testable part" pattern was later reused for `_parse_results` in the scraper and again for the Stage 4 integration tests.

---

## Stage 3 — Notifications

### Why `send_fn` is injectable (dependency injection)
Both the notifier and the poller accept a `send_fn` parameter that defaults to real SMTP sending. Tests inject a fake — e.g. a lambda that appends to a list, or one that raises an exception on demand. This lets the entire notification pipeline (creation, state transitions, retry logic, dead-lettering) be tested entirely in memory in well under a second, without sending real emails or depending on an SMTP server being reachable. This is a standard, nameable pattern (dependency injection) worth being able to describe by name in an interview.

### Why notifications are a state machine
Each notification moves through explicit states: `pending → sent` (happy path) or `pending → failed → failed → ... → dead` (repeated failure). Every transition is written to the database, so at any point you can query "show me all dead notifications" to find exactly what was lost and why, instead of failures disappearing silently.

### Why retries happen at the start of the next polling pass, not inline
If sending fails (e.g. the SMTP server is temporarily down), retrying immediately inline would block the polling pass on something unrelated to scraping. Instead, a failed notification is left in the `failed` state with a `next_retry_at` timestamp, and the *next* polling pass checks for retryable notifications before it polls any courses. A failed email never blocks scraping from happening.

### Why exponential backoff with a cap
Retries happen at 2, 4, 8, and 16 minutes, and after 5 total attempts the notification is marked `dead`. This gives transient failures (a brief SMTP outage) a fair chance to succeed on retry, while not hammering a broken mail server indefinitely.

---

## Stage 4 — REST API

### Why `engine.begin()` for writes and `engine.connect()` for reads
SQLAlchemy 2.0 connections "autobegin" — a transaction starts implicitly on first use. Calling `conn.begin()` explicitly on a connection that already has an active transaction throws an error. The fix: use `engine.begin()` for writes (returns a connection already in an explicit transaction, which commits automatically on exit) and `engine.connect()` for reads (autobegin is fine when nothing is being written). This is a real, easy-to-hit SQLAlchemy 2.0 gotcha, not an obvious API quirk — worth remembering it exists.

### Why the application factory pattern (`create_app()`)
Rather than a single global `app = Flask(...)`, a factory function creates a new app instance on demand. This is a Flask best practice specifically because it allows each test to spin up its own isolated app with its own `:memory:` database, so tests never share state or interfere with each other.

### Why on-demand scraping in `POST /watches`
When a watch is created for a course that hasn't been seen before, the API scrapes UNC immediately, adding roughly 3 seconds of latency to that request. The tradeoff is deliberate: it validates the course actually exists (returning a 404 for something like "COMP 999" instead of silently creating a watch for a nonexistent course) and populates real seat data immediately instead of waiting for the next poll cycle.

### Why email-as-auth for now (and why it's not real auth)
Operations are scoped with an `?email=` query parameter — e.g. `DELETE /watches/5?email=alice@unc.edu` only succeeds if watch 5 actually belongs to alice. This is explicitly *not* secure (anyone can guess or supply any email), but it's a deliberate, acknowledged simplification for Stage 4's scope. Real authentication (accounts, sessions/tokens) is planned as part of a future "public launch" phase, not an oversight in the current design.

---

## Testing philosophy that emerged across stages

A consistent pattern shows up in Stage 1, 3, and 4 independently: **wherever a component talks to something external or slow (a live HTTP scrape, a real SMTP send), that dependency is injected as a parameter with a real default and a fake used in tests.** This is why the full pipeline — scrape → detect → notify — can be proven correct in a fraction of a second with zero network calls, via the Stage 4 integration test, while still using the real implementation in production. This is worth naming explicitly as a design philosophy, not just a collection of individual choices, if asked about testing strategy in an interview.
