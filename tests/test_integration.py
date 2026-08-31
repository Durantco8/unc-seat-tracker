"""End-to-end integration test: poll → detect change → send notification.

Everything is faked (scraper, email sender, in-memory database).
No network calls, no real SMTP, fully repeatable.
"""

from collections import defaultdict
from datetime import datetime, timezone

import sqlalchemy as sa

from seat_tracker.db import (
    create_engine, init_db, notifications, sections, upsert_section, watches,
)
from seat_tracker.models import SectionStatus
from seat_tracker.poller import run_poll_pass


def test_full_pipeline_zero_to_positive():
    """Two poll passes: first returns 0 seats, second returns 5.

    After the second pass, a notification should be created and sent.
    """
    # ── Setup ───────────────────────────────────────────────────────────

    engine = create_engine(":memory:")
    init_db(engine)

    # Fake scraper: returns 0 seats for seed + pass 1, then 5 for pass 2
    call_count = 0

    def fake_check_seats(term, subject, catalog_number):
        nonlocal call_count
        call_count += 1
        seats = 0 if call_count <= 2 else 5
        return [
            SectionStatus(
                term=term,
                subject=subject,
                catalog_number=catalog_number,
                class_section="001",
                class_number="8433",
                description="Computer Organization",
                available_seats=seats,
                instruction_type="In Person",
                schedule="TTH 03:30 PM",
            ),
        ]

    # Fake email sender: records what was sent
    emails_sent = []

    def fake_send(to, subject, body):
        emails_sent.append({"to": to, "subject": subject, "body": body})

    # Seed the database: insert the section (via first scrape) and a watch.
    # We need a section row to exist before we can create a watch (FK constraint),
    # so we manually insert one with 0 seats.
    initial = fake_check_seats("2026 Fall", "COMP", "311")
    assert call_count == 1  # sanity check

    with engine.begin() as conn:
        section_id, _ = upsert_section(conn, initial[0])
        conn.execute(
            watches.insert().values(
                section_id=section_id,
                user_email="alice@unc.edu",
                created_at=datetime.now(timezone.utc),
                active=True,
            )
        )

    # Shared state for the poller
    failures = defaultdict(int)
    suspended = set()
    poll_kwargs = dict(
        scrape_fn=fake_check_seats,
        send_fn=fake_send,
        failures=failures,
        suspended=suspended,
        max_consecutive_failures=3,
        request_delay=0,
    )

    # ── Pass 1: scraper returns 0 seats (same as current) ───────────────

    run_poll_pass(engine, **poll_kwargs)
    assert call_count == 2  # one scrape call for this pass

    # No transition happened (0 → 0), so no notifications
    with engine.connect() as conn:
        notif_rows = conn.execute(sa.select(notifications)).fetchall()
    assert len(notif_rows) == 0
    assert len(emails_sent) == 0

    # ── Pass 2: scraper returns 5 seats (0 → 5 transition) ─────────────

    run_poll_pass(engine, **poll_kwargs)
    assert call_count == 3

    # A notification should have been created AND sent
    with engine.connect() as conn:
        notif_rows = conn.execute(sa.select(notifications)).fetchall()

    assert len(notif_rows) == 1
    assert notif_rows[0].status == "sent"
    assert notif_rows[0].attempts == 1

    # The fake sender should have been called
    assert len(emails_sent) == 1
    assert emails_sent[0]["to"] == "alice@unc.edu"
    assert "COMP 311" in emails_sent[0]["subject"]
    assert "5 seat(s) open" in emails_sent[0]["body"]

    # Verify the section in the database now shows 5 seats
    with engine.connect() as conn:
        section = conn.execute(sa.select(sections)).first()
    assert section.available_seats == 5


def test_no_notification_when_seats_stay_positive():
    """If seats go from 5 → 8, no notification should be sent."""
    engine = create_engine(":memory:")
    init_db(engine)

    call_count = 0

    def fake_check_seats(term, subject, catalog_number):
        nonlocal call_count
        call_count += 1
        seats = 5 if call_count <= 2 else 8  # seed + pass1 = 5, pass2 = 8
        return [
            SectionStatus(
                term=term, subject=subject, catalog_number=catalog_number,
                class_section="001", class_number="8433",
                description="Computer Organization", available_seats=seats,
                instruction_type="In Person", schedule="TTH 03:30 PM",
            ),
        ]

    emails_sent = []

    # Seed with 5 seats
    initial = fake_check_seats("2026 Fall", "COMP", "311")
    with engine.begin() as conn:
        section_id, _ = upsert_section(conn, initial[0])
        conn.execute(watches.insert().values(
            section_id=section_id, user_email="bob@unc.edu",
            created_at=datetime.now(timezone.utc), active=True,
        ))

    poll_kwargs = dict(
        scrape_fn=fake_check_seats,
        send_fn=lambda to, subj, body: emails_sent.append(1),
        failures=defaultdict(int),
        suspended=set(),
        max_consecutive_failures=3,
        request_delay=0,
    )

    # Pass 1: 5 → 5 (no change)
    run_poll_pass(engine, **poll_kwargs)
    # Pass 2: 5 → 8 (increase, but not from 0)
    run_poll_pass(engine, **poll_kwargs)

    assert len(emails_sent) == 0

    with engine.connect() as conn:
        notif_rows = conn.execute(sa.select(notifications)).fetchall()
    assert len(notif_rows) == 0


def test_multiple_watchers_both_notified():
    """Two users watching the same section should both get notified on 0→>0."""
    engine = create_engine(":memory:")
    init_db(engine)

    call_count = 0

    def fake_check_seats(term, subject, catalog_number):
        nonlocal call_count
        call_count += 1
        seats = 0 if call_count <= 2 else 3  # seed + pass1 = 0, pass2 = 3
        return [
            SectionStatus(
                term=term, subject=subject, catalog_number=catalog_number,
                class_section="001", class_number="8433",
                description="Computer Organization", available_seats=seats,
                instruction_type="In Person", schedule="TTH 03:30 PM",
            ),
        ]

    emails_sent = []

    def fake_send(to, subject, body):
        emails_sent.append(to)

    # Seed with 0 seats, two watchers
    initial = fake_check_seats("2026 Fall", "COMP", "311")
    with engine.begin() as conn:
        section_id, _ = upsert_section(conn, initial[0])
        for email in ["alice@unc.edu", "bob@unc.edu"]:
            conn.execute(watches.insert().values(
                section_id=section_id, user_email=email,
                created_at=datetime.now(timezone.utc), active=True,
            ))

    poll_kwargs = dict(
        scrape_fn=fake_check_seats,
        send_fn=fake_send,
        failures=defaultdict(int),
        suspended=set(),
        max_consecutive_failures=3,
        request_delay=0,
    )

    # Pass 1: 0 → 0
    run_poll_pass(engine, **poll_kwargs)
    assert len(emails_sent) == 0

    # Pass 2: 0 → 3
    run_poll_pass(engine, **poll_kwargs)
    assert len(emails_sent) == 2
    assert set(emails_sent) == {"alice@unc.edu", "bob@unc.edu"}
