"""Database setup and query helpers using SQLAlchemy Core + SQLite."""

from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

# ── Schema ──────────────────────────────────────────────────────────────────

metadata = sa.MetaData()

sections = sa.Table(
    "sections",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("term", sa.Text, nullable=False),
    sa.Column("subject", sa.Text, nullable=False),
    sa.Column("catalog_number", sa.Text, nullable=False),
    sa.Column("class_section", sa.Text, nullable=False),
    sa.Column("class_number", sa.Text, nullable=False),
    sa.Column("description", sa.Text, nullable=False),
    sa.Column("available_seats", sa.Integer, nullable=False),
    sa.Column("last_checked_at", sa.DateTime, nullable=False),
    sa.UniqueConstraint(
        "term", "subject", "catalog_number", "class_section",
        name="uq_section_identity",
    ),
)

watches = sa.Table(
    "watches",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "section_id", sa.Integer,
        sa.ForeignKey("sections.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("user_email", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime, nullable=False),
    sa.Column("active", sa.Boolean, nullable=False, server_default=sa.text("1")),
    sa.UniqueConstraint("section_id", "user_email", name="uq_watch_identity"),
)

notifications = sa.Table(
    "notifications",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "watch_id", sa.Integer,
        sa.ForeignKey("watches.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("message", sa.Text, nullable=False),
    sa.Column("sent_at", sa.DateTime),              # null until actually sent
    sa.Column("status", sa.Text, nullable=False),   # pending / sent / failed / dead
    sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
    sa.Column("next_retry_at", sa.DateTime),        # null = no retry needed
)


def create_engine(db_path: str = "seat_tracker.db") -> sa.engine.Engine:
    """Create a SQLite engine with WAL mode and foreign key enforcement."""
    engine = sa.create_engine(f"sqlite:///{db_path}")

    # SQLite doesn't enforce foreign keys by default — you have to turn it on
    # per-connection.  WAL (Write-Ahead Logging) mode allows concurrent reads
    # while a write is in progress, which matters once we have a polling loop
    # and an API reading at the same time.
    @sa.event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def init_db(engine: sa.engine.Engine) -> None:
    """Create all tables if they don't already exist."""
    metadata.create_all(engine)


# ── Query helpers ───────────────────────────────────────────────────────────

def upsert_section(conn, status) -> tuple[int, int | None]:
    """Insert or update a section from a SectionStatus scrape result.

    Returns (section_id, old_available_seats).
    old_available_seats is None if the section is newly inserted.
    """
    now = datetime.now(timezone.utc)

    # Read current state (if any) before overwriting
    row = conn.execute(
        sa.select(sections.c.id, sections.c.available_seats).where(
            sections.c.term == status.term,
            sections.c.subject == status.subject,
            sections.c.catalog_number == status.catalog_number,
            sections.c.class_section == status.class_section,
        )
    ).first()

    if row is not None:
        conn.execute(
            sections.update()
            .where(sections.c.id == row.id)
            .values(
                available_seats=status.available_seats,
                last_checked_at=now,
                description=status.description,
                class_number=status.class_number,
            )
        )
        return row.id, row.available_seats

    result = conn.execute(
        sections.insert().values(
            term=status.term,
            subject=status.subject,
            catalog_number=status.catalog_number,
            class_section=status.class_section,
            class_number=status.class_number,
            description=status.description,
            available_seats=status.available_seats,
            last_checked_at=now,
        )
    )
    return result.inserted_primary_key[0], None


def get_section_by_identity(conn, term, subject, catalog_number, class_section):
    """Look up a section by its natural key. Returns the row or None."""
    return conn.execute(
        sa.select(sections).where(
            sections.c.term == term,
            sections.c.subject == subject,
            sections.c.catalog_number == catalog_number,
            sections.c.class_section == class_section,
        )
    ).first()


def get_watches_for_user(conn, user_email: str) -> list[sa.Row]:
    """Return all watches for a user, joined with section info."""
    return conn.execute(
        sa.select(
            watches.c.id,
            watches.c.user_email,
            watches.c.active,
            watches.c.created_at,
            sections.c.term,
            sections.c.subject,
            sections.c.catalog_number,
            sections.c.class_section,
            sections.c.description,
            sections.c.available_seats,
        )
        .select_from(watches.join(sections, watches.c.section_id == sections.c.id))
        .where(watches.c.user_email == user_email)
        .order_by(watches.c.created_at.desc())
    ).fetchall()


def delete_watch(conn, watch_id: int, user_email: str) -> bool:
    """Delete a watch by id, scoped to the user's email. Returns True if deleted."""
    result = conn.execute(
        watches.delete().where(
            watches.c.id == watch_id,
            watches.c.user_email == user_email,
        )
    )
    return result.rowcount > 0


def get_notifications_for_user(conn, user_email: str) -> list[sa.Row]:
    """Return notification history for a user, most recent first."""
    return conn.execute(
        sa.select(
            notifications.c.id,
            notifications.c.message,
            notifications.c.sent_at,
            notifications.c.status,
            notifications.c.attempts,
            sections.c.subject,
            sections.c.catalog_number,
            sections.c.class_section,
        )
        .select_from(
            notifications
            .join(watches, notifications.c.watch_id == watches.c.id)
            .join(sections, watches.c.section_id == sections.c.id)
        )
        .where(watches.c.user_email == user_email)
        .order_by(notifications.c.id.desc())
    ).fetchall()


def get_active_watchers(conn, section_id: int) -> list[sa.Row]:
    """Return all active watches for a given section."""
    return conn.execute(
        sa.select(watches).where(
            watches.c.section_id == section_id,
            watches.c.active == True,  # noqa: E712
        )
    ).fetchall()


def create_notification(conn, watch_id: int, message: str) -> int:
    """Insert a pending notification. Returns the notification id."""
    result = conn.execute(
        notifications.insert().values(
            watch_id=watch_id,
            message=message,
            status="pending",
        )
    )
    return result.inserted_primary_key[0]


def get_pending_notifications(conn) -> list[sa.Row]:
    """Return notifications that are ready to send or retry.

    Includes 'pending' (never attempted) and 'failed' (past their retry time).
    """
    now = datetime.now(timezone.utc)
    return conn.execute(
        sa.select(
            notifications,
            watches.c.user_email,
            sections.c.subject,
            sections.c.catalog_number,
            sections.c.class_section,
            sections.c.description,
        )
        .select_from(
            notifications
            .join(watches, notifications.c.watch_id == watches.c.id)
            .join(sections, watches.c.section_id == sections.c.id)
        )
        .where(
            sa.or_(
                notifications.c.status == "pending",
                sa.and_(
                    notifications.c.status == "failed",
                    notifications.c.next_retry_at <= now,
                ),
            )
        )
    ).fetchall()


MAX_ATTEMPTS = 5


def mark_notification_sent(conn, notification_id: int) -> None:
    """Mark a notification as successfully sent."""
    conn.execute(
        notifications.update()
        .where(notifications.c.id == notification_id)
        .values(
            status="sent",
            sent_at=datetime.now(timezone.utc),
            attempts=notifications.c.attempts + 1,
            next_retry_at=None,
        )
    )


def mark_notification_failed(conn, notification_id: int, attempts: int) -> None:
    """Mark a notification as failed, with backoff or dead-letter."""
    new_attempts = attempts + 1
    if new_attempts >= MAX_ATTEMPTS:
        status = "dead"
        next_retry = None
    else:
        status = "failed"
        # Exponential backoff: 2^attempts minutes (2, 4, 8, 16 min)
        backoff_seconds = (2 ** new_attempts) * 60
        next_retry = datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds)

    conn.execute(
        notifications.update()
        .where(notifications.c.id == notification_id)
        .values(
            status=status,
            attempts=new_attempts,
            next_retry_at=next_retry,
        )
    )
