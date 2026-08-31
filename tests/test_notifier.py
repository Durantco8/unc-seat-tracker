"""Tests for change detection and notification pipeline.

All tests use in-memory SQLite and a fake send function — no SMTP, no live scraping.
"""

from datetime import datetime, timezone

import sqlalchemy as sa

from seat_tracker.db import (
    create_engine, init_db, sections, watches, notifications,
    upsert_section, get_pending_notifications, MAX_ATTEMPTS,
)
from seat_tracker.models import SectionStatus
from seat_tracker.notifier import create_seat_notifications, process_pending_notifications


def _make_engine():
    engine = create_engine(":memory:")
    init_db(engine)
    return engine


def _make_status(seats=0):
    return SectionStatus(
        term="2026 Fall", subject="COMP", catalog_number="311",
        class_section="001", class_number="8433",
        description="Computer Organization", available_seats=seats,
        instruction_type="In Person", schedule="TTH 03:30 PM",
    )


def _seed_section_with_watch(engine, seats=0, email="alice@unc.edu"):
    """Insert a section with the given seat count and an active watch."""
    status = _make_status(seats)
    with engine.begin() as conn:
        section_id, _ = upsert_section(conn, status)
        conn.execute(watches.insert().values(
            section_id=section_id, user_email=email,
            created_at=datetime.now(timezone.utc), active=True,
        ))
    return section_id


# ── Change detection ────────────────────────────────────────────────────────

def test_notification_created_on_zero_to_positive():
    """0 → 5 seats should create a notification for each watcher."""
    engine = _make_engine()
    section_id = _seed_section_with_watch(engine, seats=0)

    with engine.begin() as conn:
        created = create_seat_notifications(conn, section_id, old_seats=0, new_seats=5)
        assert created == 1

        rows = conn.execute(sa.select(notifications)).fetchall()
        assert len(rows) == 1
        assert rows[0].status == "pending"
        assert "5 seat(s) open" in rows[0].message


def test_no_notification_on_positive_to_positive():
    """5 → 8 seats should NOT create a notification."""
    engine = _make_engine()
    section_id = _seed_section_with_watch(engine, seats=5)

    with engine.begin() as conn:
        created = create_seat_notifications(conn, section_id, old_seats=5, new_seats=8)
        assert created == 0


def test_no_notification_on_positive_to_zero():
    """5 → 0 seats should NOT notify (class filling up isn't actionable)."""
    engine = _make_engine()
    section_id = _seed_section_with_watch(engine, seats=5)

    with engine.begin() as conn:
        created = create_seat_notifications(conn, section_id, old_seats=5, new_seats=0)
        assert created == 0


def test_multiple_watchers_get_separate_notifications():
    """Two watchers on the same section → two notifications."""
    engine = _make_engine()
    section_id = _seed_section_with_watch(engine, seats=0, email="alice@unc.edu")
    with engine.begin() as conn:
        conn.execute(watches.insert().values(
            section_id=section_id, user_email="bob@unc.edu",
            created_at=datetime.now(timezone.utc), active=True,
        ))

    with engine.begin() as conn:
        created = create_seat_notifications(conn, section_id, old_seats=0, new_seats=3)
        assert created == 2


# ── Sending ─────────────────────────────────────────────────────────────────

def test_successful_send_marks_sent():
    """A successful send_fn should mark the notification as 'sent'."""
    engine = _make_engine()
    section_id = _seed_section_with_watch(engine, seats=0)

    sent_to = []

    def fake_send(to, subject, body):
        sent_to.append((to, subject, body))

    with engine.begin() as conn:
        create_seat_notifications(conn, section_id, old_seats=0, new_seats=2)

    with engine.begin() as conn:
        sent, failed = process_pending_notifications(conn, send_fn=fake_send)
        assert sent == 1
        assert failed == 0

    assert len(sent_to) == 1
    assert sent_to[0][0] == "alice@unc.edu"
    assert "COMP 311" in sent_to[0][1]

    # Verify it's marked sent in the database
    with engine.begin() as conn:
        row = conn.execute(sa.select(notifications)).first()
        assert row.status == "sent"
        assert row.sent_at is not None
        assert row.attempts == 1


def test_failed_send_marks_failed_with_retry():
    """A failed send should mark 'failed' and schedule a retry."""
    engine = _make_engine()
    section_id = _seed_section_with_watch(engine, seats=0)

    def failing_send(to, subject, body):
        raise ConnectionError("SMTP down")

    with engine.begin() as conn:
        create_seat_notifications(conn, section_id, old_seats=0, new_seats=1)

    with engine.begin() as conn:
        sent, failed = process_pending_notifications(conn, send_fn=failing_send)
        assert sent == 0
        assert failed == 1

    with engine.begin() as conn:
        row = conn.execute(sa.select(notifications)).first()
        assert row.status == "failed"
        assert row.attempts == 1
        assert row.next_retry_at is not None


def test_dead_letter_after_max_attempts():
    """After MAX_ATTEMPTS failures, notification should be marked 'dead'."""
    engine = _make_engine()
    section_id = _seed_section_with_watch(engine, seats=0)

    def failing_send(to, subject, body):
        raise ConnectionError("still down")

    with engine.begin() as conn:
        create_seat_notifications(conn, section_id, old_seats=0, new_seats=1)

    # Simulate repeated failures — manually set attempts to MAX_ATTEMPTS - 1
    with engine.begin() as conn:
        conn.execute(
            notifications.update().values(
                attempts=MAX_ATTEMPTS - 1,
                status="failed",
                next_retry_at=datetime.now(timezone.utc),
            )
        )

    with engine.begin() as conn:
        sent, failed = process_pending_notifications(conn, send_fn=failing_send)
        assert failed == 1

    with engine.begin() as conn:
        row = conn.execute(sa.select(notifications)).first()
        assert row.status == "dead"
        assert row.next_retry_at is None  # no more retries
