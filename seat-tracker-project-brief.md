# UNC Class Seat Tracker & Notifier — Project Brief

## Purpose statement

Every registration cycle, UNC students refresh ConnectCarolina repeatedly hoping a seat opens up in a full class. There is no way to passively wait for a seat to open — you either watch manually or miss it.

This project is a backend system that watches specific class sections and notifies a student the moment a seat becomes available, so they never have to manually refresh again.

Beyond solving a real, personally-felt problem, the project is designed to demonstrate backend engineering depth appropriate for a big tech (FAANG-tier) software engineering internship application. The interesting engineering problems are:

- **Efficient polling at scale** — watching many sections without redundant or excessive requests to the source, especially when multiple users watch the same section
- **A reliable notification pipeline** — delivering alerts (email/SMS) with retry logic so a transient failure doesn't mean a missed seat opening
- **Deduplication and state tracking** — knowing whether a seat opening is *new* since the last check, not re-notifying on every poll
- **Clean data modeling** — sections, watches (who is watching what), and notification history as a coherent schema

The goal is a project the author will actually use next registration cycle, and can also speak to fluently and specifically in a technical interview — including the tradeoffs made and the failure modes handled.

## Data source (confirmed working, as of Aug 31, 2026)

UNC publishes a public class search tool that does not require login (separate from ConnectCarolina, which is behind Onyen auth and should not be scraped or automated against).

- **Search UI (for humans):** `https://reports.unc.edu/class-search/`
- **No `robots.txt` exists at this domain** (confirmed via direct check — 404 on `reports.unc.edu/robots.txt`). There is no explicit crawl policy, so we self-impose respectful limits (see "Politeness constraints" below) rather than relying on a published rule.
- **Underlying request confirmed via browser DevTools (Network tab):**
  - **Endpoint:** `POST https://reports.unc.edu/class-search/`
  - **Type:** Full HTML page response (not a JSON API) — the form submits and the server re-renders the page with a results table embedded in the HTML
  - **Response size:** ~17.3 kB typical, `Content-Type: text/html; charset=utf-8`
  - **Form fields (POST body):**
    - `csrfmiddlewaretoken` — required, dynamic, tied to a session cookie (confirms the backend is Django)
    - `term` — e.g. `2026 Fall`
    - `subject` — e.g. `COMP`
    - `catalog_number` — e.g. `311`
  - **Response contains a results table** with columns including: Subject, Catalog Number, Class Section, Class Number, Description, Term, Hours, Meeting Dates, Schedule, Instruction Type, and — critically — **Available Seats**.

### Why this matters
This is a fully scriptable data source. No browser automation (e.g. Playwright) is required — a plain HTTP client that manages cookies and a CSRF token is sufficient. This was verified as the highest-risk unknown before committing to the project, and it checks out.

## Confirmed integration pattern

1. `GET https://reports.unc.edu/class-search/` using a persistent session (e.g. Python `requests.Session()`) — this sets the session cookie and returns HTML containing a hidden `<input name="csrfmiddlewaretoken">` field.
2. Parse the CSRF token out of that HTML (e.g. with BeautifulSoup).
3. `POST` to the same URL with the session (so the cookie rides along), sending `csrfmiddlewaretoken`, `term`, `subject`, and `catalog_number` as form data.
4. Parse the returned HTML table to extract section-level data, in particular `Available Seats` per `Class Section`.
5. Wrap this as a reusable function, e.g. `check_seats(term, subject, catalog_number) -> list[SectionStatus]`.

## Politeness constraints (self-imposed, since no robots.txt exists)

- Identify the script with a real, descriptive `User-Agent` (e.g. `unc-seat-tracker/0.1 (student project; contact: <email>)`)
- Poll each watched section no more frequently than every 3–5 minutes
- Deduplicate polling: if multiple users watch the same section, poll it once per interval and notify all interested watchers from that single result — not once per watcher
- Back off immediately and stop polling if the server ever returns errors, rate-limit responses, or CAPTCHA/block pages, rather than retrying aggressively

## System scope (staged build plan)

**Stage 1 — Core polling + storage**
- Data model: `sections` (term, subject, catalog_number, class_section, available_seats, last_checked_at), `watches` (user, section reference, created_at), `notifications` (watch reference, sent_at, status)
- A single script/service that, given a section, performs the GET→POST→parse flow above and returns current seat count
- Store results in a database (Postgres or SQLite to start)

**Stage 2 — Multi-section polling loop**
- A scheduler/worker loop that iterates over all currently-watched sections on an interval
- Deduplicate: group watches by unique section so each section is polled once regardless of watcher count

**Stage 3 — Change detection + notification**
- Compare newly-polled seat count to the last known value; only trigger a notification on a `0 → >0` transition (or any meaningful increase), not on every poll
- Notification delivery (email via SMTP/SendGrid, or SMS via Twilio), with retry + backoff on delivery failure, and a dead-letter/failure log so a failed notification isn't silently lost

**Stage 4 — API + watch management**
- Simple REST API: submit a new watch (`POST /watches`), list current watches, remove a watch, view notification history
- This is what a frontend (or just `curl`/Postman for the demo) talks to

**Stage 5 (stretch) — Dashboard / polish**
- Small web UI to add/remove watched sections and see status
- Load-test / stress-test the polling loop against many simulated watched sections to surface and fix bottlenecks — this becomes a strong interview story ("I found X was slow under Y load, here's how I fixed it")

## What's already been validated (do not re-litigate in Claude Code)

- The data source works, is publicly accessible without login, and returns real seat data (confirmed live against COMP 311 and COMP 211 sections in Fall 2026 term)
- The request pattern (session + CSRF token + POST + HTML parse) is fully understood and requires no browser automation
- There is no published robots.txt restricting this path

## What Claude Code should help with next

Starting point: Stage 1. Build the `check_seats()` function using `requests.Session()` + BeautifulSoup against the confirmed endpoint and field names above, verify it returns correct live data for a known section, then move to the data model and Stage 2 polling loop.
