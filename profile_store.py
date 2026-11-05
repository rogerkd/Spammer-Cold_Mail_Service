"""Transactional per-profile SQLite storage with non-destructive legacy import."""

from __future__ import annotations

import csv
from contextlib import contextmanager, closing
import hashlib
import io
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 1
DEFAULT_CONTACT_FIELDS = ["name", "email", "title", "company"]
JSON_STATE_KEYS = {
    "one_off_draft.json": "one_off_drafts",
    "archived_threads.json": "archived_threads",
    "notification_read_state.json": "notification_read_ids",
    "thread_reply_state.json": "thread_reply_state",
    "gmail_reply_cursor.json": "gmail_reply_cursor",
}


class ProfileStoreError(RuntimeError):
    """Raised when profile data cannot be migrated or safely stored."""


class ProfileStore:
    """A small SQLite database scoped to one local user profile."""

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).resolve()
        self.database_path = self.data_dir / "app.sqlite3"
        self._legacy_imported = False
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.database_path, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=20000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=NORMAL")
        try:
            yield connection
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self):
        try:
            with closing(sqlite3.connect(self.database_path, timeout=20)) as connection:
                connection.execute("PRAGMA busy_timeout=20000")
                connection.execute("PRAGMA journal_mode=WAL")
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version > SCHEMA_VERSION:
                    raise ProfileStoreError(
                        f"Profile database schema {version} is newer than this app supports."
                    )
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS store_metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS migration_sources (
                        source_key TEXT PRIMARY KEY,
                        source_path TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        row_count INTEGER NOT NULL,
                        migrated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS contacts (
                        id INTEGER PRIMARY KEY,
                        position INTEGER NOT NULL,
                        email_key TEXT NOT NULL DEFAULT '',
                        row_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS contacts_email_idx ON contacts(email_key);
                    CREATE INDEX IF NOT EXISTS contacts_position_idx ON contacts(position);
                    CREATE TABLE IF NOT EXISTS tracker_rows (
                        id INTEGER PRIMARY KEY,
                        position INTEGER NOT NULL,
                        email_key TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT '',
                        timestamp TEXT NOT NULL DEFAULT '',
                        thread_id TEXT NOT NULL DEFAULT '',
                        row_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS tracker_email_idx ON tracker_rows(email_key);
                    CREATE INDEX IF NOT EXISTS tracker_status_time_idx
                        ON tracker_rows(status, timestamp);
                    CREATE INDEX IF NOT EXISTS tracker_thread_idx ON tracker_rows(thread_id);
                    CREATE TABLE IF NOT EXISTS suppressions (
                        email TEXT PRIMARY KEY COLLATE NOCASE,
                        added_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS app_state (
                        key TEXT PRIMARY KEY,
                        value_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    """
                )
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        except (sqlite3.Error, OSError) as exc:
            if isinstance(exc, ProfileStoreError):
                raise
            raise ProfileStoreError(f"Could not initialize profile database: {exc}") from exc

    @staticmethod
    def _source_digest(path):
        if path is None or not Path(path).is_file():
            return "", b""
        raw = Path(path).read_bytes()
        return hashlib.sha256(raw).hexdigest(), raw

    @staticmethod
    def _read_csv(path, encoding="utf-8-sig"):
        if path is None or not Path(path).is_file():
            return [], []
        try:
            raw = Path(path).read_bytes()
            text = raw.decode(encoding)
            reader = csv.DictReader(io.StringIO(text, newline=""))
            fields = list(reader.fieldnames or [])
            rows = []
            for raw_row in reader:
                row = {
                    str(key): (value if value is not None else "")
                    for key, value in raw_row.items()
                    if key is not None
                }
                rows.append(row)
            return fields, rows
        except (OSError, UnicodeError, csv.Error) as exc:
            raise ProfileStoreError(f"Could not read legacy CSV {path}: {exc}") from exc

    @staticmethod
    def _row_json(row):
        return json.dumps(row, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _row_from_json(raw, source):
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProfileStoreError(f"Profile database contains invalid {source} data.") from exc
        if not isinstance(value, dict):
            raise ProfileStoreError(f"Profile database contains invalid {source} data.")
        return value

    def _has_source(self, connection, source_key):
        return connection.execute(
            "SELECT 1 FROM migration_sources WHERE source_key=?", (source_key,)
        ).fetchone() is not None

    def _mark_source(self, connection, source_key, path, digest, row_count):
        connection.execute(
            "INSERT OR REPLACE INTO migration_sources"
            "(source_key,source_path,sha256,row_count,migrated_at) VALUES(?,?,?,?,?)",
            (
                source_key,
                self._portable_source_path(path) if path else "",
                digest,
                int(row_count),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )

    def _portable_source_path(self, path):
        """Store a profile-relative reference so moving the project stays safe."""
        absolute = Path(path).resolve()
        try:
            relative = absolute.relative_to(self.data_dir)
        except ValueError:
            relative = Path(os.path.relpath(absolute, self.data_dir))
        return relative.as_posix()

    def rebase_migration_paths(self):
        """Rebase old absolute migration audit paths to this moved profile."""
        updated = 0
        try:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    "SELECT source_key,source_path FROM migration_sources"
                ).fetchall()
                for row in rows:
                    source_path = str(row["source_path"] or "")
                    if not source_path or not Path(source_path).is_absolute():
                        continue
                    filename = Path(source_path).name
                    if filename == "sent_log.csv":
                        current_path = self.data_dir.parent / filename
                    else:
                        current_path = self.data_dir / filename
                    portable = self._portable_source_path(current_path)
                    connection.execute(
                        "UPDATE migration_sources SET source_path=? WHERE source_key=?",
                        (portable, row["source_key"]),
                    )
                    updated += 1
                connection.commit()
            return updated
        except sqlite3.Error as exc:
            raise ProfileStoreError(f"Could not update moved-profile path metadata: {exc}") from exc

    def ensure_legacy_imported(
        self,
        contacts_file=None,
        tracker_file=None,
        suppress_file=None,
        legacy_log_file=None,
    ):
        """Import legacy files once, atomically, and never modify their contents."""
        if self._legacy_imported:
            return
        contacts_file = Path(contacts_file or self.data_dir / "contacts.csv")
        tracker_file = Path(tracker_file or self.data_dir / "tracker.csv")
        suppress_file = Path(suppress_file or self.data_dir / "unsubscribed.txt")
        legacy_log_file = Path(legacy_log_file) if legacy_log_file else None

        with self.connect() as connection:
            needs_contacts = not self._has_source(connection, "contacts")
            needs_tracker = not self._has_source(connection, "tracker")
            needs_suppression = not self._has_source(connection, "suppressions")

        contact_fields, contacts = self._read_csv(contacts_file) if needs_contacts else ([], [])
        tracker_fields, tracker = self._read_csv(tracker_file) if needs_tracker else ([], [])
        tracker_digest, tracker_bytes = self._source_digest(tracker_file) if needs_tracker else ("", b"")
        log_fields, legacy_rows = ([], [])
        log_digest, log_bytes = ("", b"")
        if needs_tracker and legacy_log_file and legacy_log_file.is_file():
            log_fields, legacy_rows = self._read_csv(legacy_log_file, encoding="utf-8")
            log_digest, log_bytes = self._source_digest(legacy_log_file)
            has_send_history = any(
                str(row.get("status", "")).strip().lower() in ("sent", "unknown")
                and str(row.get("email", "")).strip()
                for row in tracker
            )
            if not has_send_history:
                tracker_fields, tracker = log_fields, legacy_rows
                tracker_source = legacy_log_file
            else:
                tracker_source = tracker_file
        elif needs_tracker:
            tracker_source = tracker_file

        suppression_hash, suppression_bytes = (
            self._source_digest(suppress_file) if needs_suppression else ("", b"")
        )
        suppressed = []
        if needs_suppression and suppress_file.is_file():
            try:
                suppressed = [
                    line.strip().casefold()
                    for line in suppression_bytes.decode("utf-8-sig").splitlines()
                    if line.strip()
                ]
            except UnicodeError as exc:
                raise ProfileStoreError(
                    f"Could not read legacy suppression file {suppress_file}: {exc}"
                ) from exc

        contacts_hash, contacts_bytes = (
            self._source_digest(contacts_file) if needs_contacts else ("", b"")
        )
        del contacts_bytes  # The digest is sufficient; CSV was decoded above.
        if needs_tracker:
            combined = (tracker_digest + log_digest).encode("ascii")
            tracker_hash = hashlib.sha256(combined).hexdigest()
        else:
            tracker_hash = ""

        try:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if needs_contacts and not self._has_source(connection, "contacts"):
                    connection.execute("DELETE FROM contacts")
                    connection.executemany(
                        "INSERT INTO contacts(position,email_key,row_json) VALUES(?,?,?)",
                        [
                            (
                                index,
                                str(row.get("email", "")).strip().casefold(),
                                self._row_json(row),
                            )
                            for index, row in enumerate(contacts)
                        ],
                    )
                    fields = contact_fields or DEFAULT_CONTACT_FIELDS
                    connection.execute(
                        "INSERT OR REPLACE INTO store_metadata(key,value) VALUES('contact_fields',?)",
                        (json.dumps(fields, ensure_ascii=False),),
                    )
                    if contacts_file.is_file():
                        self._mark_source(connection, "contacts", contacts_file, contacts_hash, len(contacts))
                if needs_tracker and not self._has_source(connection, "tracker"):
                    connection.execute("DELETE FROM tracker_rows")
                    connection.executemany(
                        "INSERT INTO tracker_rows(position,email_key,status,timestamp,thread_id,row_json)"
                        " VALUES(?,?,?,?,?,?)",
                        [
                            (
                                index,
                                str(row.get("email", "")).strip().casefold(),
                                str(row.get("status", "")),
                                str(row.get("timestamp", "")),
                                str(row.get("gmail_thread_id", "")),
                                self._row_json(row),
                            )
                            for index, row in enumerate(tracker)
                        ],
                    )
                    if tracker_file.is_file() or legacy_log_file and legacy_log_file.is_file():
                        self._mark_source(
                            connection,
                            "tracker",
                            tracker_source if tracker_source.is_file() else legacy_log_file,
                            tracker_hash,
                            len(tracker),
                        )
                if needs_suppression and not self._has_source(connection, "suppressions"):
                    connection.execute("DELETE FROM suppressions")
                    connection.executemany(
                        "INSERT OR IGNORE INTO suppressions(email,added_at) VALUES(?,?)",
                        [
                            (email, datetime.now(timezone.utc).isoformat(timespec="seconds"))
                            for email in suppressed
                        ],
                    )
                    if suppress_file.is_file():
                        self._mark_source(
                            connection, "suppressions", suppress_file, suppression_hash, len(suppressed)
                        )
                connection.commit()
            self._legacy_imported = True
        except (sqlite3.Error, OSError) as exc:
            raise ProfileStoreError(f"Could not import legacy profile data: {exc}") from exc

    def load_contacts(self, contacts_file=None, tracker_file=None, suppress_file=None, legacy_log_file=None):
        self.ensure_legacy_imported(contacts_file, tracker_file, suppress_file, legacy_log_file)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM store_metadata WHERE key='contact_fields'"
            ).fetchone()
            fields = json.loads(row["value"]) if row else list(DEFAULT_CONTACT_FIELDS)
            rows = [
                self._row_from_json(item["row_json"], "contact")
                for item in connection.execute(
                    "SELECT row_json FROM contacts ORDER BY position,id"
                )
            ]
        return fields, rows

    def save_contacts(self, fields, rows):
        normalized_fields = list(dict.fromkeys(str(field) for field in fields if field))
        if not normalized_fields:
            normalized_fields = list(DEFAULT_CONTACT_FIELDS)
        prepared = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Contact rows must be objects.")
            value = {str(key): ("" if cell is None else str(cell)) for key, cell in row.items()}
            prepared.append((
                len(prepared),
                str(value.get("email", "")).strip().casefold(),
                self._row_json(value),
            ))
        try:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM contacts")
                connection.executemany(
                    "INSERT INTO contacts(position,email_key,row_json) VALUES(?,?,?)", prepared
                )
                connection.execute(
                    "INSERT OR REPLACE INTO store_metadata(key,value) VALUES('contact_fields',?)",
                    (json.dumps(normalized_fields, ensure_ascii=False),),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO migration_sources"
                    "(source_key,source_path,sha256,row_count,migrated_at) VALUES('contacts','','',?,?)",
                    (len(prepared), datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )
                connection.commit()
        except sqlite3.Error as exc:
            raise ProfileStoreError(f"Could not save contacts: {exc}") from exc

    def delete_contacts(self, emails):
        normalized = {str(email).strip().casefold() for email in emails if str(email).strip()}
        if not normalized:
            return 0
        try:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                before = connection.total_changes
                connection.executemany(
                    "DELETE FROM contacts WHERE email_key=?", [(email,) for email in normalized]
                )
                removed = connection.total_changes - before
                connection.commit()
            return removed
        except sqlite3.Error as exc:
            raise ProfileStoreError(f"Could not remove contacts: {exc}") from exc

    def load_tracker_rows(self, tracker_file=None, contacts_file=None, suppress_file=None, legacy_log_file=None):
        self.ensure_legacy_imported(contacts_file, tracker_file, suppress_file, legacy_log_file)
        with self.connect() as connection:
            return [
                self._row_from_json(item["row_json"], "tracker")
                for item in connection.execute(
                    "SELECT row_json FROM tracker_rows ORDER BY position,id"
                )
            ]

    def save_tracker_rows(self, rows):
        prepared = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Tracker rows must be objects.")
            value = {str(key): ("" if cell is None else str(cell)) for key, cell in row.items()}
            prepared.append((
                len(prepared),
                str(value.get("email", "")).strip().casefold(),
                str(value.get("status", "")),
                str(value.get("timestamp", "")),
                str(value.get("gmail_thread_id", "")),
                self._row_json(value),
            ))
        try:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM tracker_rows")
                connection.executemany(
                    "INSERT INTO tracker_rows(position,email_key,status,timestamp,thread_id,row_json)"
                    " VALUES(?,?,?,?,?,?)",
                    prepared,
                )
                connection.execute(
                    "INSERT OR IGNORE INTO migration_sources"
                    "(source_key,source_path,sha256,row_count,migrated_at) VALUES('tracker','','',?,?)",
                    (len(prepared), datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )
                connection.commit()
        except sqlite3.Error as exc:
            raise ProfileStoreError(f"Could not save tracker history: {exc}") from exc

    def get_tracker_row(self, email, tracker_file=None, contacts_file=None, suppress_file=None, legacy_log_file=None):
        self.ensure_legacy_imported(contacts_file, tracker_file, suppress_file, legacy_log_file)
        with self.connect() as connection:
            item = connection.execute(
                "SELECT row_json FROM tracker_rows WHERE email_key=? ORDER BY position DESC,id DESC LIMIT 1",
                (str(email).strip().casefold(),),
            ).fetchone()
        return self._row_from_json(item["row_json"], "tracker") if item else None

    def save_tracker_row(self, row):
        if not isinstance(row, dict) or not str(row.get("email", "")).strip():
            raise ValueError("A tracker record must include an email address.")
        value = {str(key): ("" if cell is None else str(cell)) for key, cell in row.items()}
        email = str(value.get("email", "")).strip().casefold()
        try:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT position FROM tracker_rows WHERE email_key=? ORDER BY position,id LIMIT 1",
                    (email,),
                ).fetchone()
                position = (
                    int(existing["position"])
                    if existing else int(connection.execute(
                        "SELECT COALESCE(MAX(position),-1)+1 FROM tracker_rows"
                    ).fetchone()[0])
                )
                connection.execute("DELETE FROM tracker_rows WHERE email_key=?", (email,))
                connection.execute(
                    "INSERT INTO tracker_rows(position,email_key,status,timestamp,thread_id,row_json)"
                    " VALUES(?,?,?,?,?,?)",
                    (
                        position, email, str(value.get("status", "")),
                        str(value.get("timestamp", "")), str(value.get("gmail_thread_id", "")),
                        self._row_json(value),
                    ),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO migration_sources"
                    "(source_key,source_path,sha256,row_count,migrated_at) VALUES('tracker','','',1,?)",
                    (datetime.now(timezone.utc).isoformat(timespec="seconds"),),
                )
                connection.commit()
        except sqlite3.Error as exc:
            raise ProfileStoreError(f"Could not save tracker record: {exc}") from exc

    def load_suppressed(self, suppress_file=None, contacts_file=None, tracker_file=None, legacy_log_file=None):
        self.ensure_legacy_imported(contacts_file, tracker_file, suppress_file, legacy_log_file)
        with self.connect() as connection:
            return {
                row["email"] for row in connection.execute("SELECT email FROM suppressions")
            }

    def save_suppressed(self, emails):
        normalized = sorted({str(email).strip().casefold() for email in emails if str(email).strip()})
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM suppressions")
                connection.executemany(
                    "INSERT INTO suppressions(email,added_at) VALUES(?,?)",
                    [(email, now) for email in normalized],
                )
                connection.execute(
                    "INSERT OR IGNORE INTO migration_sources"
                    "(source_key,source_path,sha256,row_count,migrated_at) VALUES('suppressions','','',?,?)",
                    (len(normalized), now),
                )
                connection.commit()
        except sqlite3.Error as exc:
            raise ProfileStoreError(f"Could not save blocked addresses: {exc}") from exc

    def suppress_addresses(self, emails, contacts_file=None, tracker_file=None, suppress_file=None, legacy_log_file=None):
        normalized = {str(email).strip().casefold() for email in emails if str(email).strip()}
        if not normalized:
            return 0
        self.ensure_legacy_imported(contacts_file, tracker_file, suppress_file, legacy_log_file)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.executemany(
                    "INSERT OR IGNORE INTO suppressions(email,added_at) VALUES(?,?)",
                    [(email, now) for email in normalized],
                )
                before = connection.total_changes
                connection.executemany(
                    "DELETE FROM contacts WHERE email_key=?", [(email,) for email in normalized]
                )
                removed = connection.total_changes - before
                normalized_list = sorted(normalized)
                for offset in range(0, len(normalized_list), 500):
                    chunk = normalized_list[offset:offset + 500]
                    matching = connection.execute(
                        "SELECT id,row_json FROM tracker_rows WHERE email_key IN (%s)"
                        % ",".join("?" for _ in chunk),
                        tuple(chunk),
                    ).fetchall()
                    for item in matching:
                        row = self._row_from_json(item["row_json"], "tracker")
                        if str(row.get("followup_opt_in", "")).casefold() != "no":
                            row["followup_opt_in"] = "no"
                            connection.execute(
                                "UPDATE tracker_rows SET row_json=? WHERE id=?",
                                (self._row_json(row), item["id"]),
                            )
                connection.execute(
                    "INSERT OR IGNORE INTO migration_sources"
                    "(source_key,source_path,sha256,row_count,migrated_at) VALUES('suppressions','','',?,?)",
                    (connection.execute("SELECT COUNT(*) FROM suppressions").fetchone()[0], now),
                )
                connection.commit()
            return removed
        except sqlite3.Error as exc:
            raise ProfileStoreError(f"Could not update suppression records: {exc}") from exc

    @staticmethod
    def _state_key(path):
        path = Path(path)
        return JSON_STATE_KEYS.get(path.name, f"json:{path.name}")

    def load_json_state(self, legacy_file, default):
        legacy_file = Path(legacy_file)
        key = self._state_key(legacy_file)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM app_state WHERE key=?", (key,)
            ).fetchone()
        if row:
            try:
                return json.loads(row["value_json"])
            except json.JSONDecodeError as exc:
                raise ProfileStoreError(f"Saved state '{key}' is invalid JSON in SQLite.") from exc
        if not legacy_file.is_file():
            return default
        try:
            raw = legacy_file.read_bytes()
            value = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProfileStoreError(f"Could not import legacy state {legacy_file}: {exc}") from exc
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(raw).hexdigest()
        try:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT value_json FROM app_state WHERE key=?", (key,)
                ).fetchone()
                if existing:
                    value = json.loads(existing["value_json"])
                else:
                    connection.execute(
                        "INSERT INTO app_state(key,value_json,updated_at) VALUES(?,?,?)",
                        (key, encoded, datetime.now(timezone.utc).isoformat(timespec="seconds")),
                    )
                    self._mark_source(connection, f"state:{key}", legacy_file, digest, 1)
                connection.commit()
        except sqlite3.Error as exc:
            raise ProfileStoreError(f"Could not import legacy state {legacy_file}: {exc}") from exc
        return value

    def save_json_state(self, legacy_file, value):
        key = self._state_key(legacy_file)
        try:
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ProfileStoreError(f"Could not encode state '{key}': {exc}") from exc
        try:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO app_state(key,value_json,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                    (key, encoded, datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )
                connection.commit()
        except sqlite3.Error as exc:
            raise ProfileStoreError(f"Could not save state '{key}': {exc}") from exc

    def migration_report(self):
        with self.connect() as connection:
            sources = [dict(row) for row in connection.execute(
                "SELECT source_key,source_path,sha256,row_count,migrated_at "
                "FROM migration_sources ORDER BY source_key"
            )]
            counts = {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in ("contacts", "tracker_rows", "suppressions", "app_state")
            }
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            integrity_check = connection.execute("PRAGMA integrity_check").fetchone()[0]
        return {
            "database": str(self.database_path),
            "schema_version": version,
            "journal_mode": str(journal_mode).lower(),
            "integrity_check": str(integrity_check),
            "counts": counts,
            "sources": sources,
        }


_stores = {}
_stores_lock = threading.Lock()


def get_store(data_dir):
    resolved = Path(data_dir).resolve()
    key = str(resolved).casefold()
    with _stores_lock:
        store = _stores.get(key)
        if store is None:
            store = ProfileStore(resolved)
            _stores[key] = store
        return store


def copy_profile_database(source_path, target_path):
    """Copy a live SQLite profile safely, including any committed WAL pages."""
    source_path = Path(source_path)
    target_path = Path(target_path)
    if not source_path.is_file():
        return False
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        source = sqlite3.connect(source_path, timeout=20)
        try:
            target = sqlite3.connect(temporary_path, timeout=20)
            try:
                source.backup(target)
                target.commit()
                target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                target.execute("PRAGMA journal_mode=DELETE")
            finally:
                target.close()
        finally:
            source.close()
        temporary_path.replace(target_path)
    except sqlite3.Error as exc:
        temporary_path.unlink(missing_ok=True)
        raise ProfileStoreError(
            f"Could not preserve the existing profile database: {exc}"
        ) from exc
    except OSError:
        temporary_path.unlink(missing_ok=True)
        raise
    return True
