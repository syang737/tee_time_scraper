"""SQLite storage. Single user, low volume -- no ORM needed."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from . import config
from .models import TeeTimeSlot, Watch

SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    label         TEXT NOT NULL DEFAULT '',
    courses       TEXT NOT NULL,          -- JSON list of course keys
    days          TEXT NOT NULL,          -- JSON list of weekday keys
    horizon_days  INTEGER NOT NULL DEFAULT 14,
    specific_date TEXT,
    time_start    TEXT,
    time_end      TEXT,
    min_players   INTEGER NOT NULL DEFAULT 0,
    holes         TEXT NOT NULL DEFAULT 'any',
    active        INTEGER NOT NULL DEFAULT 1,
    ntfy_topic    TEXT,                   -- null = use the server-wide topic
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seen_slots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id       INTEGER NOT NULL,
    slot_key       TEXT NOT NULL,
    course_key     TEXT NOT NULL,
    date           TEXT NOT NULL,
    time           TEXT NOT NULL,
    holes          INTEGER,
    available_spots INTEGER NOT NULL,
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    last_notified_at TEXT,
    notify_count   INTEGER NOT NULL DEFAULT 0,
    still_open     INTEGER NOT NULL DEFAULT 1,
    UNIQUE (watch_id, slot_key),
    FOREIGN KEY (watch_id) REFERENCES watches (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_seen_slots_watch ON seen_slots (watch_id, last_seen_at);

CREATE TABLE IF NOT EXISTS poll_status (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    last_poll_at   TEXT,
    last_success_at TEXT,
    last_error     TEXT,
    poll_count     INTEGER NOT NULL DEFAULT 0
);
"""


def to_iso(moment: dt.datetime) -> str:
    """Normalize any datetime to a UTC ISO string for storage."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def now_iso() -> str:
    return to_iso(dt.datetime.now(dt.timezone.utc))


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR IGNORE INTO poll_status (id) VALUES (1)")
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns that arrived after a database was first created."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(poll_status)")}
    if "last_purge_at" not in existing:
        conn.execute("ALTER TABLE poll_status ADD COLUMN last_purge_at TEXT")

    existing = {row["name"] for row in conn.execute("PRAGMA table_info(watches)")}
    if "ntfy_topic" not in existing:
        # Null means "use the server-wide topic", so watches created before
        # per-watch topics existed keep sending exactly where they did.
        conn.execute("ALTER TABLE watches ADD COLUMN ntfy_topic TEXT")


# --------------------------------------------------------------------------
# watches
# --------------------------------------------------------------------------


def _row_to_watch(row: sqlite3.Row) -> Watch:
    return Watch(
        id=row["id"],
        label=row["label"],
        courses=json.loads(row["courses"]),
        days=json.loads(row["days"]),
        horizon_days=row["horizon_days"],
        specific_date=row["specific_date"],
        time_start=row["time_start"],
        time_end=row["time_end"],
        min_players=row["min_players"],
        holes=row["holes"],
        active=bool(row["active"]),
        ntfy_topic=row["ntfy_topic"],
        created_at=row["created_at"],
    )


def list_watches(active_only: bool = False) -> list[Watch]:
    sql = "SELECT * FROM watches"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY active DESC, id DESC"
    with connect() as conn:
        return [_row_to_watch(r) for r in conn.execute(sql)]


def get_watch(watch_id: int) -> Watch | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM watches WHERE id = ?", (watch_id,)).fetchone()
    return _row_to_watch(row) if row else None


def save_watch(watch: Watch) -> int:
    fields = (
        watch.label,
        json.dumps(watch.courses),
        json.dumps(watch.days),
        watch.horizon_days,
        watch.specific_date,
        watch.time_start,
        watch.time_end,
        watch.min_players,
        watch.holes,
        int(watch.active),
        watch.ntfy_topic,
    )
    with connect() as conn:
        if watch.id:
            conn.execute(
                """UPDATE watches SET label=?, courses=?, days=?, horizon_days=?,
                   specific_date=?, time_start=?, time_end=?, min_players=?,
                   holes=?, active=?, ntfy_topic=? WHERE id=?""",
                (*fields, watch.id),
            )
            return watch.id
        cur = conn.execute(
            """INSERT INTO watches (label, courses, days, horizon_days, specific_date,
               time_start, time_end, min_players, holes, active, ntfy_topic, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (*fields, now_iso()),
        )
        return int(cur.lastrowid)


def set_watch_active(watch_id: int, active: bool) -> None:
    with connect() as conn:
        conn.execute("UPDATE watches SET active = ? WHERE id = ?", (int(active), watch_id))


def delete_watch(watch_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM seen_slots WHERE watch_id = ?", (watch_id,))
        conn.execute("DELETE FROM watches WHERE id = ?", (watch_id,))


# --------------------------------------------------------------------------
# seen slots
# --------------------------------------------------------------------------


def get_seen_slot(watch_id: int, slot_key: str) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM seen_slots WHERE watch_id = ? AND slot_key = ?",
            (watch_id, slot_key),
        ).fetchone()


def upsert_slot(
    watch_id: int,
    slot: TeeTimeSlot,
    notified: bool,
    now: dt.datetime | None = None,
) -> None:
    """Record that we just saw this slot open, optionally noting a notification.

    ``now`` comes from the poller so the stored timestamps and the cooldown
    comparison that reads them back are on the same clock.
    """
    stamp = to_iso(now) if now is not None else now_iso()
    with connect() as conn:
        conn.execute(
            """INSERT INTO seen_slots (watch_id, slot_key, course_key, date, time,
                   holes, available_spots, first_seen_at, last_seen_at,
                   last_notified_at, notify_count, still_open)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,1)
               ON CONFLICT (watch_id, slot_key) DO UPDATE SET
                   last_seen_at = excluded.last_seen_at,
                   available_spots = excluded.available_spots,
                   still_open = 1,
                   last_notified_at = CASE WHEN ? THEN excluded.last_notified_at
                                           ELSE seen_slots.last_notified_at END,
                   notify_count = seen_slots.notify_count + ?""",
            (
                watch_id,
                slot.slot_key,
                slot.course_key,
                slot.date_str,
                slot.time_str,
                slot.holes,
                slot.available_spots,
                stamp,
                stamp,
                stamp if notified else None,
                1 if notified else 0,
                int(notified),
                int(notified),
            ),
        )


def close_missing_slots(watch_id: int, open_slot_keys: list[str]) -> None:
    """Mark slots we previously saw open but that are gone from the response."""
    with connect() as conn:
        if open_slot_keys:
            placeholders = ",".join("?" * len(open_slot_keys))
            conn.execute(
                f"""UPDATE seen_slots SET still_open = 0
                    WHERE watch_id = ? AND still_open = 1
                      AND slot_key NOT IN ({placeholders})""",
                (watch_id, *open_slot_keys),
            )
        else:
            conn.execute(
                "UPDATE seen_slots SET still_open = 0 WHERE watch_id = ? AND still_open = 1",
                (watch_id,),
            )


def recent_slots(watch_id: int | None = None, limit: int = 50) -> list[sqlite3.Row]:
    sql = "SELECT s.*, w.label AS watch_label FROM seen_slots s JOIN watches w ON w.id = s.watch_id"
    params: list[object] = []
    if watch_id is not None:
        sql += " WHERE s.watch_id = ?"
        params.append(watch_id)
    sql += " ORDER BY s.last_seen_at DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        return list(conn.execute(sql, params))


# --------------------------------------------------------------------------
# poller health
# --------------------------------------------------------------------------


def record_poll(error: str | None) -> None:
    stamp = now_iso()
    with connect() as conn:
        conn.execute(
            """UPDATE poll_status SET last_poll_at = ?, poll_count = poll_count + 1,
                   last_error = ?,
                   last_success_at = CASE WHEN ? IS NULL THEN ? ELSE last_success_at END
               WHERE id = 1""",
            (stamp, error, error, stamp),
        )


def get_poll_status() -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM poll_status WHERE id = 1").fetchone()


# --------------------------------------------------------------------------
# retention
# --------------------------------------------------------------------------


def purge_old_slots(now: dt.datetime, retention_days: int) -> int:
    """Drop slot history we last saw more than ``retention_days`` ago.

    Keyed on ``last_seen_at``, deliberately -- not on the tee time's own
    date. ``seen_slots`` is not just history, it is the live dedup state: a
    slot that is still open still gets its ``last_seen_at`` refreshed every
    cycle, so it can never be purged out from under us and re-alert as a
    fresh opening. Only rows the poller has stopped seeing age out.
    """
    cutoff = to_iso(now - dt.timedelta(days=retention_days))
    with connect() as conn:
        cur = conn.execute("DELETE FROM seen_slots WHERE last_seen_at < ?", (cutoff,))
        deleted = cur.rowcount
        conn.execute(
            "UPDATE poll_status SET last_purge_at = ? WHERE id = 1", (to_iso(now),)
        )

    if deleted:
        # SQLite keeps freed pages for reuse rather than returning them to
        # the filesystem; VACUUM is what actually shrinks the file.
        with connect() as conn:
            conn.execute("VACUUM")
    return deleted


def due_for_purge(now: dt.datetime) -> bool:
    """True when a purge hasn't run in the last day."""
    status = get_poll_status()
    if status is None:
        return False
    last = status["last_purge_at"] if "last_purge_at" in status.keys() else None
    if not last:
        return True
    try:
        previous = dt.datetime.fromisoformat(last)
    except ValueError:
        return True
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=dt.timezone.utc)
    return now.astimezone(dt.timezone.utc) - previous >= dt.timedelta(days=1)


def storage_stats() -> dict[str, int]:
    """Row count and on-disk size, for the dashboard."""
    import os

    with connect() as conn:
        slots = conn.execute("SELECT COUNT(*) AS n FROM seen_slots").fetchone()["n"]
    try:
        size = os.path.getsize(config.DB_PATH)
    except OSError:
        size = 0
    return {"slot_rows": slots, "db_bytes": size}
