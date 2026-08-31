"""Tests for the REST API.

Uses Flask's test client — no real HTTP server, no live scraping
(except the one test that validates the on-demand scrape flow).
"""

from datetime import datetime, timezone

import sqlalchemy as sa

from seat_tracker.api import create_app
from seat_tracker.db import init_db, sections, watches, notifications, upsert_section
from seat_tracker.models import SectionStatus


def _make_app():
    """Create an app backed by an in-memory database."""
    app = create_app(db_path=":memory:")
    return app


def _seed_section(app, seats=10):
    """Insert a section directly into the database."""
    engine = app.config["DB_ENGINE"]
    status = SectionStatus(
        term="2026 Fall", subject="COMP", catalog_number="311",
        class_section="001", class_number="8433",
        description="Computer Organization", available_seats=seats,
        instruction_type="In Person", schedule="TTH 03:30 PM",
    )
    with engine.begin() as conn:
        section_id, _ = upsert_section(conn, status)
    return section_id


def _seed_two_sections(app):
    """Insert two sections (001 and 002) for COMP 311."""
    engine = app.config["DB_ENGINE"]
    for section, seats in [("001", 10), ("002", 3)]:
        status = SectionStatus(
            term="2026 Fall", subject="COMP", catalog_number="311",
            class_section=section, class_number="8433",
            description="Computer Organization", available_seats=seats,
            instruction_type="In Person", schedule="TTH 03:30 PM",
        )
        with engine.begin() as conn:
            upsert_section(conn, status)


# ── POST /watches ───────────────────────────────────────────────────────

def test_create_watch_for_existing_section():
    app = _make_app()
    section_id = _seed_section(app)

    with app.test_client() as client:
        resp = client.post("/watches", json={
            "email": "alice@unc.edu",
            "term": "2026 Fall",
            "subject": "COMP",
            "catalog_number": "311",
            "class_section": "001",
        })

    assert resp.status_code == 201
    data = resp.get_json()
    assert data["section"]["available_seats"] == 10


def test_create_duplicate_watch_returns_409():
    app = _make_app()
    _seed_section(app)

    with app.test_client() as client:
        body = {
            "email": "alice@unc.edu",
            "term": "2026 Fall",
            "subject": "COMP",
            "catalog_number": "311",
            "class_section": "001",
        }
        client.post("/watches", json=body)
        resp = client.post("/watches", json=body)

    assert resp.status_code == 409


def test_create_watch_missing_fields_returns_400():
    app = _make_app()

    with app.test_client() as client:
        resp = client.post("/watches", json={"email": "alice@unc.edu"})

    assert resp.status_code == 400
    assert "Missing fields" in resp.get_json()["error"]


def test_create_watch_invalid_section_returns_404():
    """A section that doesn't exist on UNC's site should return 404."""
    app = _make_app()

    with app.test_client() as client:
        resp = client.post("/watches", json={
            "email": "alice@unc.edu",
            "term": "2026 Fall",
            "subject": "COMP",
            "catalog_number": "999",
            "class_section": "001",
        })

    # Either 404 (no sections found) or 502 (scrape failed) is acceptable
    assert resp.status_code in (404, 502)


# ── GET /watches ────────────────────────────────────────────────────────

def test_list_watches():
    app = _make_app()
    _seed_section(app)

    with app.test_client() as client:
        client.post("/watches", json={
            "email": "alice@unc.edu",
            "term": "2026 Fall",
            "subject": "COMP",
            "catalog_number": "311",
            "class_section": "001",
        })
        resp = client.get("/watches?email=alice@unc.edu")

    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) == 1
    assert data[0]["section"]["subject"] == "COMP"


def test_list_watches_missing_email_returns_400():
    app = _make_app()

    with app.test_client() as client:
        resp = client.get("/watches")

    assert resp.status_code == 400


# ── DELETE /watches/<id> ────────────────────────────────────────────────

def test_delete_watch():
    app = _make_app()
    _seed_section(app)

    with app.test_client() as client:
        client.post("/watches", json={
            "email": "alice@unc.edu",
            "term": "2026 Fall",
            "subject": "COMP",
            "catalog_number": "311",
            "class_section": "001",
        })
        # Get the watch id
        watches_resp = client.get("/watches?email=alice@unc.edu")
        watch_id = watches_resp.get_json()[0]["id"]

        resp = client.delete(f"/watches/{watch_id}?email=alice@unc.edu")

    assert resp.status_code == 200

    # Verify it's gone
    with app.test_client() as client:
        resp = client.get("/watches?email=alice@unc.edu")
        assert len(resp.get_json()) == 0


def test_delete_nonexistent_watch_returns_404():
    app = _make_app()

    with app.test_client() as client:
        resp = client.delete("/watches/999?email=alice@unc.edu")

    assert resp.status_code == 404


# ── GET /notifications ──────────────────────────────────────────────────

def test_list_notifications_empty():
    app = _make_app()

    with app.test_client() as client:
        resp = client.get("/notifications?email=alice@unc.edu")

    assert resp.status_code == 200
    assert resp.get_json() == []


def test_list_notifications_with_data():
    app = _make_app()
    _seed_section(app)
    engine = app.config["DB_ENGINE"]

    with app.test_client() as client:
        resp = client.post("/watches", json={
            "email": "alice@unc.edu",
            "term": "2026 Fall",
            "subject": "COMP",
            "catalog_number": "311",
            "class_section": "001",
        })
        assert resp.status_code == 201

    # Get the watch id from the database
    with engine.connect() as conn:
        watch = conn.execute(sa.select(watches)).first()

    # Insert a notification directly
    with engine.begin() as conn:
        conn.execute(notifications.insert().values(
            watch_id=watch.id,
            message="Seats available!",
            sent_at=datetime.now(timezone.utc),
            status="sent",
            attempts=1,
        ))

    with app.test_client() as client:
        resp = client.get("/notifications?email=alice@unc.edu")

    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) == 1
    assert data[0]["status"] == "sent"


# ── POST /watches/course ───────────────────────────────────────────────

def test_course_watch_creates_watches_for_all_sections():
    app = _make_app()
    _seed_two_sections(app)

    with app.test_client() as client:
        resp = client.post("/watches/course", json={
            "email": "alice@unc.edu",
            "term": "2026 Fall",
            "subject": "COMP",
            "catalog_number": "311",
        })

    assert resp.status_code == 201
    data = resp.get_json()
    assert data["message"] == "Created 2 watch(es)"
    assert len(data["sections"]) == 2
    assert all(s["status"] == "created" for s in data["sections"])


def test_course_watch_partial_duplicate():
    """If already watching one section, it should still create the other."""
    app = _make_app()
    _seed_two_sections(app)

    with app.test_client() as client:
        # Watch section 001 first
        client.post("/watches", json={
            "email": "alice@unc.edu",
            "term": "2026 Fall",
            "subject": "COMP",
            "catalog_number": "311",
            "class_section": "001",
        })

        # Now watch the whole course
        resp = client.post("/watches/course", json={
            "email": "alice@unc.edu",
            "term": "2026 Fall",
            "subject": "COMP",
            "catalog_number": "311",
        })

    assert resp.status_code == 201
    data = resp.get_json()
    assert data["message"] == "Created 1 watch(es), 1 already existed"

    statuses = {s["class_section"]: s["status"] for s in data["sections"]}
    assert statuses["001"] == "already_watching"
    assert statuses["002"] == "created"


def test_course_watch_all_duplicates():
    """If already watching all sections, return 409."""
    app = _make_app()
    _seed_two_sections(app)

    with app.test_client() as client:
        client.post("/watches/course", json={
            "email": "alice@unc.edu",
            "term": "2026 Fall",
            "subject": "COMP",
            "catalog_number": "311",
        })
        resp = client.post("/watches/course", json={
            "email": "alice@unc.edu",
            "term": "2026 Fall",
            "subject": "COMP",
            "catalog_number": "311",
        })

    assert resp.status_code == 409
    assert "Already watching all sections" in resp.get_json()["message"]


def test_course_watch_missing_fields():
    app = _make_app()

    with app.test_client() as client:
        resp = client.post("/watches/course", json={"email": "alice@unc.edu"})

    assert resp.status_code == 400
