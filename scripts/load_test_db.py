"""Profile the database operations specifically.

Strip out scrape latency to isolate DB bottlenecks.
"""

import time
from datetime import datetime, timezone

from seat_tracker.db import (
    create_engine, init_db, sections, watches, notifications,
    upsert_section, get_active_watchers, create_notification,
    get_pending_notifications, mark_notification_sent,
)
from seat_tracker.models import SectionStatus
from seat_tracker.poller import get_watched_courses

NUM_COURSES = 100
SECTIONS_PER_COURSE = 3
WATCHERS_PER_SECTION = 5


def seed(engine):
    with engine.begin() as conn:
        for c in range(NUM_COURSES):
            cat = str(100 + c)
            for s in range(SECTIONS_PER_COURSE):
                r = conn.execute(sections.insert().values(
                    term="2026 Fall", subject="COMP", catalog_number=cat,
                    class_section=f"{s+1:03d}", class_number=str(8000+c*10+s),
                    description=f"Course {cat}", available_seats=0,
                    last_checked_at=datetime.now(timezone.utc),
                ))
                sid = r.inserted_primary_key[0]
                for w in range(WATCHERS_PER_SECTION):
                    conn.execute(watches.insert().values(
                        section_id=sid, user_email=f"s{w}@unc.edu",
                        created_at=datetime.now(timezone.utc), active=True,
                    ))


def benchmark(label, fn, iterations=1):
    start = time.perf_counter()
    result = fn()
    elapsed = time.perf_counter() - start
    print(f"  {label}: {elapsed*1000:.1f}ms")
    return result, elapsed


def main():
    engine = create_engine(":memory:")
    init_db(engine)
    seed(engine)
    total_sections = NUM_COURSES * SECTIONS_PER_COURSE

    print(f"Database: {total_sections} sections, "
          f"{total_sections * WATCHERS_PER_SECTION} watches\n")

    # 1. Query watched courses
    print("1. get_watched_courses (DISTINCT query over 1500 watches):")
    with engine.begin() as conn:
        benchmark("  query", lambda: get_watched_courses(conn))

    # 2. Upsert all sections (300 sections, each a SELECT + UPDATE)
    print("\n2. upsert_section x300 (SELECT + UPDATE per section):")
    statuses = []
    for c in range(NUM_COURSES):
        cat = str(100 + c)
        for s in range(SECTIONS_PER_COURSE):
            statuses.append(SectionStatus(
                term="2026 Fall", subject="COMP", catalog_number=cat,
                class_section=f"{s+1:03d}", class_number=str(8000+c*10+s),
                description=f"Course {cat}", available_seats=5,
                instruction_type="In Person", schedule="MWF 10:00 AM",
            ))

    # Current approach: one transaction per course (as the poller does it)
    def upsert_per_course():
        for c in range(NUM_COURSES):
            with engine.begin() as conn:
                for s in range(SECTIONS_PER_COURSE):
                    idx = c * SECTIONS_PER_COURSE + s
                    upsert_section(conn, statuses[idx])

    benchmark("  per-course transactions", upsert_per_course)

    # Alternative: one big transaction for all sections
    def upsert_single_txn():
        with engine.begin() as conn:
            for st in statuses:
                st.available_seats = 10  # change to trigger update
                upsert_section(conn, st)

    benchmark("  single transaction", upsert_single_txn)

    # 3. get_active_watchers — called once per section with a seat change
    print("\n3. get_active_watchers x300 (one query per section):")

    def query_all_watchers():
        total = 0
        with engine.begin() as conn:
            for sid in range(1, total_sections + 1):
                watchers = get_active_watchers(conn, sid)
                total += len(watchers)
        return total

    (watcher_count, _) = benchmark("  300 individual queries", query_all_watchers)
    print(f"    returned {watcher_count} total watchers")

    # 4. create_notification — one INSERT per watcher (750 inserts)
    print("\n4. create_notification x750:")

    def create_all_notifications():
        with engine.begin() as conn:
            for sid in range(1, total_sections + 1):
                watchers = get_active_watchers(conn, sid)
                for w in watchers:
                    create_notification(conn, w.id, "Seats open!")

    benchmark("  750 individual INSERTs", create_all_notifications)

    # 5. get_pending_notifications (joined query across all tables)
    print("\n5. get_pending_notifications (750 pending rows, 3-table join):")
    with engine.begin() as conn:
        benchmark("  query", lambda: get_pending_notifications(conn))

    # 6. mark_notification_sent x750
    print("\n6. mark_notification_sent x750:")

    def mark_all_sent():
        with engine.begin() as conn:
            pending = get_pending_notifications(conn)
            for row in pending:
                mark_notification_sent(conn, row.id)

    benchmark("  750 individual UPDATEs", mark_all_sent)


if __name__ == "__main__":
    main()
