"""REST API for managing watches and viewing notifications."""

from datetime import datetime, timezone

from flask import Flask, jsonify, request

import sqlalchemy as sa

from seat_tracker.db import (
    create_engine,
    delete_watch,
    get_notifications_for_user,
    get_section_by_identity,
    get_watches_for_user,
    init_db,
    upsert_section,
    watches,
)
from seat_tracker.scraper import check_seats


def create_app(db_path: str = "seat_tracker.db") -> Flask:
    """Application factory — creates and configures the Flask app."""
    app = Flask(__name__)
    engine = create_engine(db_path)
    init_db(engine)

    # Store the engine on the app so tests can access it
    app.config["DB_ENGINE"] = engine

    # ── POST /watches ───────────────────────────────────────────────────

    @app.post("/watches")
    def add_watch():
        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body must be JSON"}), 400

        required = ["email", "term", "subject", "catalog_number", "class_section"]
        missing = [f for f in required if not data.get(f)]
        if missing:
            return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

        email = data["email"]
        term = data["term"]
        subject = data["subject"].upper()
        catalog_number = data["catalog_number"]
        class_section = data["class_section"]

        # Check if the section already exists in our database
        with engine.connect() as conn:
            section = get_section_by_identity(
                conn, term, subject, catalog_number, class_section,
            )

        if section is None:
            # Scrape to populate sections and validate the section exists
            try:
                results = check_seats(term, subject, catalog_number)
            except Exception:
                return jsonify({"error": "Failed to look up section from UNC"}), 502

            if not results:
                return jsonify({
                    "error": f"No sections found for {subject} {catalog_number} in {term}"
                }), 404

            with engine.begin() as conn:
                for status in results:
                    upsert_section(conn, status)

            with engine.connect() as conn:
                section = get_section_by_identity(
                    conn, term, subject, catalog_number, class_section,
                )

            if section is None:
                return jsonify({
                    "error": f"Section {class_section} not found for "
                             f"{subject} {catalog_number} in {term}"
                }), 404

        # Create the watch
        try:
            with engine.begin() as conn:
                conn.execute(
                    watches.insert().values(
                        section_id=section.id,
                        user_email=email,
                        created_at=datetime.now(timezone.utc),
                        active=True,
                    )
                )
        except sa.exc.IntegrityError:
            return jsonify({"error": "You are already watching this section"}), 409

        return jsonify({
            "message": "Watch created",
            "section": {
                "subject": section.subject,
                "catalog_number": section.catalog_number,
                "class_section": section.class_section,
                "description": section.description,
                "available_seats": section.available_seats,
            },
        }), 201

    # ── GET /watches?email=... ──────────────────────────────────────────

    @app.get("/watches")
    def list_watches():
        email = request.args.get("email")
        if not email:
            return jsonify({"error": "Query parameter 'email' is required"}), 400

        with engine.connect() as conn:
            rows = get_watches_for_user(conn, email)

        return jsonify([
            {
                "id": row.id,
                "active": bool(row.active),
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "section": {
                    "term": row.term,
                    "subject": row.subject,
                    "catalog_number": row.catalog_number,
                    "class_section": row.class_section,
                    "description": row.description,
                    "available_seats": row.available_seats,
                },
            }
            for row in rows
        ])

    # ── DELETE /watches/<id>?email=... ──────────────────────────────────

    @app.delete("/watches/<int:watch_id>")
    def remove_watch(watch_id):
        email = request.args.get("email")
        if not email:
            return jsonify({"error": "Query parameter 'email' is required"}), 400

        with engine.begin() as conn:
            deleted = delete_watch(conn, watch_id, email)

        if not deleted:
            return jsonify({"error": "Watch not found"}), 404

        return jsonify({"message": "Watch deleted"}), 200

    # ── GET /notifications?email=... ────────────────────────────────────

    @app.get("/notifications")
    def list_notifications():
        email = request.args.get("email")
        if not email:
            return jsonify({"error": "Query parameter 'email' is required"}), 400

        with engine.connect() as conn:
            rows = get_notifications_for_user(conn, email)

        return jsonify([
            {
                "id": row.id,
                "message": row.message,
                "sent_at": row.sent_at.isoformat() if row.sent_at else None,
                "status": row.status,
                "attempts": row.attempts,
                "section": f"{row.subject} {row.catalog_number} section {row.class_section}",
            }
            for row in rows
        ])

    return app
