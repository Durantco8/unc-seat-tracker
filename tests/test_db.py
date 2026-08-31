"""Test database schema and upsert logic."""

import sqlalchemy as sa

from seat_tracker.db import create_engine, init_db, sections, upsert_section
from seat_tracker.models import SectionStatus


def test_schema_creates_tables():
    """All three tables should be created in a fresh in-memory database."""
    engine = create_engine(":memory:")
    init_db(engine)

    inspector = sa.inspect(engine)
    table_names = inspector.get_table_names()
    assert "sections" in table_names
    assert "watches" in table_names
    assert "notifications" in table_names


def test_upsert_insert_then_update():
    """First upsert inserts (old_seats=None), second updates (old_seats=value)."""
    engine = create_engine(":memory:")
    init_db(engine)

    status = SectionStatus(
        term="2026 Fall",
        subject="COMP",
        catalog_number="311",
        class_section="001",
        class_number="8433",
        description="Computer Organization",
        available_seats=5,
        instruction_type="In Person",
        schedule="TTH 03:30 PM",
    )

    with engine.begin() as conn:
        section_id, old_seats = upsert_section(conn, status)
        assert old_seats is None  # first time — it's an insert
        assert section_id == 1

    # Scrape again with a different seat count
    status.available_seats = 0

    with engine.begin() as conn:
        section_id, old_seats = upsert_section(conn, status)
        assert old_seats == 5  # previous value before this update
        assert section_id == 1  # same row

        # Verify the stored value is now 0
        row = conn.execute(
            sa.select(sections.c.available_seats)
            .where(sections.c.id == section_id)
        ).first()
        assert row.available_seats == 0


def test_upsert_with_live_scrape():
    """Scrape COMP 311 live, store results, read them back."""
    from seat_tracker.scraper import check_seats

    engine = create_engine(":memory:")
    init_db(engine)

    results = check_seats("2026 Fall", "COMP", "311")
    assert len(results) > 0

    with engine.begin() as conn:
        for status in results:
            section_id, old_seats = upsert_section(conn, status)
            assert old_seats is None  # first scrape, all inserts
            assert section_id > 0

        # Read back and verify
        rows = conn.execute(sa.select(sections)).fetchall()
        assert len(rows) == len(results)

        for row in rows:
            print(
                f"  Section {row.class_section}: "
                f"{row.available_seats} seats (db id={row.id})"
            )
