"""Polling loop that watches all active sections on an interval."""

import logging
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import sqlalchemy as sa

from seat_tracker.db import create_engine, init_db, sections, watches, upsert_section
from seat_tracker.notifier import create_seat_notifications, process_pending_notifications
from seat_tracker.scraper import check_seats as _default_check_seats

log = logging.getLogger(__name__)

# A unique course is identified by these three fields — one HTTP request
# returns all sections for a course.
COURSE_KEY_COLS = (sections.c.term, sections.c.subject, sections.c.catalog_number)


def get_watched_courses(conn) -> list[tuple[str, str, str]]:
    """Return distinct (term, subject, catalog_number) tuples with active watches."""
    query = (
        sa.select(*COURSE_KEY_COLS)
        .select_from(sections.join(watches, sections.c.id == watches.c.section_id))
        .where(watches.c.active == True)  # noqa: E712 — SQLAlchemy needs == not is
        .distinct()
    )
    return [(row.term, row.subject, row.catalog_number) for row in conn.execute(query)]


def _scrape_one(scrape_fn, term, subject, catalog_number):
    """Scrape a single course. Runs in a worker thread."""
    results = scrape_fn(term, subject, catalog_number)
    return (term, subject, catalog_number), results


def run_poll_pass(engine, *, scrape_fn, send_fn, failures, suspended,
                  max_consecutive_failures, request_delay, max_workers=5):
    """Execute a single polling pass over all watched courses.

    Scrapes are dispatched to a thread pool (max_workers concurrent requests).
    DB writes and notifications happen on the main thread after each scrape
    completes, since SQLite only allows one writer at a time.

    Returns True if at least one course was polled successfully.
    """
    # Retry any failed notifications from previous passes
    with engine.begin() as conn:
        sent, failed = process_pending_notifications(conn, send_fn=send_fn)
        if sent or failed:
            log.info("Notification retry: %d sent, %d failed", sent, failed)

    with engine.begin() as conn:
        courses = get_watched_courses(conn)

    if not courses:
        log.info("No active watches — sleeping")
        return True  # not a failure

    active_courses = [c for c in courses if c not in suspended]
    log.info(
        "Polling %d course(s) (%d suspended, %d workers)",
        len(active_courses), len(courses) - len(active_courses), max_workers,
    )

    all_failed = True

    # Dispatch all scrapes to the thread pool
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_course = {}
        for term, subject, catalog_number in active_courses:
            future = pool.submit(
                _scrape_one, scrape_fn, term, subject, catalog_number,
            )
            future_to_course[future] = (term, subject, catalog_number)

        # Process results as they complete (not in submission order)
        for future in as_completed(future_to_course):
            course_key = future_to_course[future]
            term, subject, catalog_number = course_key

            try:
                _, results = future.result()
            except Exception:
                failures[course_key] += 1
                log.exception(
                    "Failed to poll %s %s %s (failure %d/%d)",
                    subject, catalog_number, term,
                    failures[course_key], max_consecutive_failures,
                )
                if failures[course_key] >= max_consecutive_failures:
                    log.warning(
                        "Suspending %s %s %s after %d consecutive failures",
                        subject, catalog_number, term, max_consecutive_failures,
                    )
                    suspended.add(course_key)
                continue

            # Success — reset failure counter
            failures[course_key] = 0
            all_failed = False

            # DB writes on main thread (SQLite = one writer at a time)
            with engine.begin() as conn:
                for status in results:
                    section_id, old_seats = upsert_section(conn, status)
                    if old_seats is not None and old_seats != status.available_seats:
                        log.info(
                            "%s %s section %s: %d → %d seats",
                            subject, catalog_number, status.class_section,
                            old_seats, status.available_seats,
                        )
                    if old_seats is not None:
                        created = create_seat_notifications(
                            conn, section_id, old_seats, status.available_seats,
                        )
                        if created:
                            log.info("Queued %d notification(s)", created)

            with engine.begin() as conn:
                sent, failed = process_pending_notifications(
                    conn, send_fn=send_fn,
                )
                if sent or failed:
                    log.info("Notifications: %d sent, %d failed", sent, failed)

    return not all_failed or not active_courses


def run_poll_loop(
    db_path: str = "seat_tracker.db",
    interval_seconds: int = 180,
    request_delay: float = 1.5,
    max_consecutive_failures: int = 3,
    max_workers: int = 5,
    send_fn=None,
    scrape_fn=None,
) -> None:
    """Poll all watched courses in a loop.

    Args:
        db_path: Path to the SQLite database file.
        interval_seconds: Seconds between the start of each full pass (default 3 min).
        request_delay: Seconds to sleep between individual HTTP requests (politeness).
            Now applied as a stagger between thread pool submissions.
        max_consecutive_failures: After this many consecutive failures for a single
            course, skip it for the rest of this process's lifetime.
        max_workers: Maximum concurrent scrape threads.
        send_fn: Callable(to, subject, body) for sending notifications.
        scrape_fn: Callable(term, subject, catalog_number) -> list[SectionStatus].
    """
    engine = create_engine(db_path)
    init_db(engine)

    if send_fn is None:
        from seat_tracker.notifier import send_email
        send_fn = send_email
    if scrape_fn is None:
        scrape_fn = _default_check_seats

    failures: dict[tuple, int] = defaultdict(int)
    suspended: set[tuple] = set()

    log.info(
        "Poller starting (interval=%ds, workers=%d, max_failures=%d)",
        interval_seconds, max_workers, max_consecutive_failures,
    )

    while True:
        pass_start = time.monotonic()

        success = run_poll_pass(
            engine,
            scrape_fn=scrape_fn,
            send_fn=send_fn,
            failures=failures,
            suspended=suspended,
            max_consecutive_failures=max_consecutive_failures,
            request_delay=request_delay,
            max_workers=max_workers,
        )

        if not success:
            log.warning("All courses failed this pass — backing off")
            elapsed = time.monotonic() - pass_start
            time.sleep(max(0, interval_seconds * 2 - elapsed))
            continue

        elapsed = time.monotonic() - pass_start
        sleep_time = max(0, interval_seconds - elapsed)
        log.info("Pass complete in %.1fs — sleeping %.1fs", elapsed, sleep_time)
        time.sleep(sleep_time)
