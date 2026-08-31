"""Tests for the polling loop components."""

from datetime import datetime, timezone

import sqlalchemy as sa

from seat_tracker.db import create_engine, init_db, sections, watches, upsert_section
from seat_tracker.models import SectionStatus
from seat_tracker.poller import get_watched_courses


def _seed_section_and_watch(conn, subject="COMP", catalog_number="311",
                            class_section="001", email="test@unc.edu"):
    """Helper: insert a section and an active watch for it."""
    result = conn.execute(
        sections.insert().values(
            term="2026 Fall", subject=subject, catalog_number=catalog_number,
            class_section=class_section, class_number="8433",
            description="Test", available_seats=10,
            last_checked_at=datetime.now(timezone.utc),
        )
    )
    section_id = result.inserted_primary_key[0]
    conn.execute(
        watches.insert().values(
            section_id=section_id, user_email=email,
            created_at=datetime.now(timezone.utc), active=True,
        )
    )
    return section_id


def test_get_watched_courses_deduplicates():
    """Multiple watches on different sections of the same course → one entry."""
    engine = create_engine(":memory:")
    init_db(engine)

    with engine.begin() as conn:
        # Two sections of COMP 311, watched by different users
        _seed_section_and_watch(conn, "COMP", "311", "001", "alice@unc.edu")
        _seed_section_and_watch(conn, "COMP", "311", "002", "bob@unc.edu")
        # One section of COMP 211
        _seed_section_and_watch(conn, "COMP", "211", "001", "alice@unc.edu")

        courses = get_watched_courses(conn)

    # Should get 2 distinct courses, not 3 rows
    assert len(courses) == 2
    subjects = {(s, cn) for (_, s, cn) in courses}
    assert ("COMP", "311") in subjects
    assert ("COMP", "211") in subjects


def test_inactive_watch_excluded():
    """A section with only inactive watches should not be polled."""
    engine = create_engine(":memory:")
    init_db(engine)

    with engine.begin() as conn:
        _seed_section_and_watch(conn, "COMP", "311", "001", "alice@unc.edu")
        # Deactivate the watch
        conn.execute(watches.update().values(active=False))

        courses = get_watched_courses(conn)

    assert len(courses) == 0


def test_no_watches_returns_empty():
    """No watches at all → empty list."""
    engine = create_engine(":memory:")
    init_db(engine)

    with engine.begin() as conn:
        courses = get_watched_courses(conn)

    assert courses == []
