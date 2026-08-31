"""Load test: compare sequential vs concurrent polling over 100 courses.

Usage:
    PYTHONPATH=. python scripts/load_test.py
"""

import time
from collections import defaultdict
from datetime import datetime, timezone

from seat_tracker.db import create_engine, init_db, sections, watches
from seat_tracker.models import SectionStatus
from seat_tracker.poller import run_poll_pass

NUM_COURSES = 100
SECTIONS_PER_COURSE = 3
WATCHERS_PER_SECTION = 5
SIMULATED_LATENCY = 0.05  # 50ms per scrape (real is ~3s)


def fake_check_seats(term, subject, catalog_number):
    time.sleep(SIMULATED_LATENCY)
    results = []
    course_num = int(catalog_number)
    for i in range(SECTIONS_PER_COURSE):
        seats = 0 if course_num % 2 == 0 else (course_num % 10) + 1
        results.append(SectionStatus(
            term=term, subject=subject, catalog_number=catalog_number,
            class_section=f"{i+1:03d}", class_number=str(8000 + course_num * 10 + i),
            description=f"Test Course {catalog_number}",
            available_seats=seats,
            instruction_type="In Person", schedule="MWF 10:00 AM",
        ))
    return results


send_count = 0

def fake_send(to, subject, body):
    global send_count
    send_count += 1


def make_engine():
    engine = create_engine(":memory:")
    init_db(engine)
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
    return engine


def run_test(label, max_workers):
    global send_count
    send_count = 0
    engine = make_engine()

    start = time.perf_counter()
    run_poll_pass(
        engine,
        scrape_fn=fake_check_seats,
        send_fn=fake_send,
        failures=defaultdict(int),
        suspended=set(),
        max_consecutive_failures=3,
        request_delay=0,
        max_workers=max_workers,
    )
    elapsed = time.perf_counter() - start

    print(f"\n{label}")
    print(f"  Total time:         {elapsed:.2f}s")
    print(f"  Notifications sent: {send_count}")
    print(f"  Avg per course:     {elapsed / NUM_COURSES * 1000:.1f}ms")

    # Project to real-world latency (3s per scrape instead of 50ms)
    scrape_time_real = NUM_COURSES * 3.0 / max_workers
    print(f"  Projected w/ 3s latency: ~{scrape_time_real:.0f}s per pass")

    return elapsed


def main():
    total_sections = NUM_COURSES * SECTIONS_PER_COURSE
    total_watches = total_sections * WATCHERS_PER_SECTION
    print(f"Load test: {NUM_COURSES} courses, {total_sections} sections, "
          f"{total_watches} watches")
    print(f"Simulated scrape latency: {SIMULATED_LATENCY*1000:.0f}ms per course")

    t_seq = run_test("SEQUENTIAL (max_workers=1)", max_workers=1)
    t_5 = run_test("CONCURRENT (max_workers=5)", max_workers=5)
    t_10 = run_test("CONCURRENT (max_workers=10)", max_workers=10)

    print(f"\n{'=' * 60}")
    print(f"SPEEDUP:")
    print(f"  1 → 5 workers:  {t_seq / t_5:.1f}x faster")
    print(f"  1 → 10 workers: {t_seq / t_10:.1f}x faster")
    print(f"\nReal-world projection (3s scrape latency, 100 courses):")
    print(f"  1 worker:  ~300s (5.0 min) — EXCEEDS 3-min interval")
    print(f"  5 workers: ~60s  (1.0 min) — fits in interval")
    print(f"  10 workers: ~30s (0.5 min) — fits with headroom")


if __name__ == "__main__":
    main()
