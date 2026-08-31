"""Notification delivery — create, send, and retry email notifications."""

import logging
import os
import smtplib
from email.message import EmailMessage

from seat_tracker.db import (
    create_notification,
    get_active_watchers,
    get_pending_notifications,
    mark_notification_failed,
    mark_notification_sent,
)

log = logging.getLogger(__name__)

# SMTP config from environment variables — keeps credentials out of code
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")


def send_email(to: str, subject: str, body: str) -> None:
    """Send an email via SMTP.  Raises on failure."""
    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)


def create_seat_notifications(conn, section_id: int, old_seats: int,
                              new_seats: int) -> int:
    """If seats transitioned from 0 → >0, create a notification for each watcher.

    Returns the number of notifications created.
    """
    if old_seats != 0 or new_seats <= 0:
        return 0

    watchers = get_active_watchers(conn, section_id)
    if not watchers:
        return 0

    # We need section info for the message — grab it from the first watcher's join
    # or just query it directly
    from seat_tracker.db import sections
    import sqlalchemy as sa

    section = conn.execute(
        sa.select(sections).where(sections.c.id == section_id)
    ).first()

    message = (
        f"Seats available! {section.subject} {section.catalog_number} "
        f"section {section.class_section} ({section.description}) "
        f"now has {new_seats} seat(s) open."
    )

    count = 0
    for watch in watchers:
        create_notification(conn, watch.id, message)
        count += 1
        log.info("Notification queued for %s: %s", watch.user_email, message)

    return count


def process_pending_notifications(conn, send_fn=send_email) -> tuple[int, int]:
    """Send all pending/retryable notifications.

    Args:
        conn: Database connection (inside a transaction).
        send_fn: Callable(to, subject, body) — injectable for testing.

    Returns:
        (sent_count, failed_count)
    """
    pending = get_pending_notifications(conn)
    sent = 0
    failed = 0

    for row in pending:
        subject = f"Seats open: {row.subject} {row.catalog_number} section {row.class_section}"

        try:
            send_fn(row.user_email, subject, row.message)
            mark_notification_sent(conn, row.id)
            sent += 1
            log.info("Notification sent to %s (id=%d)", row.user_email, row.id)
        except Exception:
            mark_notification_failed(conn, row.id, row.attempts)
            failed += 1
            log.exception(
                "Notification failed for %s (id=%d, attempt %d)",
                row.user_email, row.id, row.attempts + 1,
            )

    return sent, failed
