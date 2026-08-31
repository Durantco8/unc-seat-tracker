"""Live smoke test — hits the real UNC class-search endpoint.

Run with:  python -m pytest tests/test_scraper_live.py -v -s
"""

from seat_tracker.scraper import check_seats


def test_comp_311_returns_sections():
    """COMP 311 should have at least one section in Fall 2026."""
    sections = check_seats("2026 Fall", "COMP", "311")

    assert len(sections) > 0, "Expected at least one section for COMP 311"

    for s in sections:
        assert s.subject == "COMP"
        assert s.catalog_number == "311"
        assert isinstance(s.available_seats, int)
        assert s.available_seats >= 0

    # Print results so we can visually inspect on first run
    print(f"\n{'='*60}")
    print(f"COMP 311 — 2026 Fall — {len(sections)} section(s) found")
    print(f"{'='*60}")
    for s in sections:
        print(
            f"  Section {s.class_section} ({s.instruction_type}): "
            f"{s.available_seats} seats available  |  {s.schedule}"
        )
