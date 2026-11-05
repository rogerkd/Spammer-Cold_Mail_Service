import csv
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import profile_store
import migrate_data
import cold_mailer
import dashboard
from unittest.mock import patch


def write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ProfileStoreMigrationTests(unittest.TestCase):
    def test_import_is_lossless_and_leaves_legacy_sources_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "data"
            contacts_path = data_dir / "contacts.csv"
            tracker_path = data_dir / "tracker.csv"
            suppress_path = data_dir / "unsubscribed.txt"
            legacy_log = root / "sent_log.csv"
            contact_rows = [
                {"name": "Zoë", "email": "ONE@example.com", "company": "Acme, Inc.", "custom": "x"},
                {"name": "Other", "email": "one@example.com", "company": "Second", "custom": "y"},
            ]
            tracker_rows = [
                {"email": "one@example.com", "status": "sent", "timestamp": "2026-11-01T10:00:00", "gmail_thread_id": "42", "detail": "{}"},
                {"email": "two@example.com", "status": "error", "timestamp": "2026-11-02T10:00:00", "gmail_thread_id": "", "detail": "SMTP error"},
            ]
            write_csv(contacts_path, ["name", "email", "company", "custom"], contact_rows)
            write_csv(tracker_path, ["email", "status", "timestamp", "gmail_thread_id", "detail"], tracker_rows)
            suppress_path.write_text("blocked@example.com\nBLOCKED@example.com\n", encoding="utf-8")
            write_csv(legacy_log, ["email", "status"], [{"email": "legacy@example.com", "status": "sent"}])
            sources = [contacts_path, tracker_path, suppress_path, legacy_log]
            before = {path: digest(path) for path in sources}

            store = profile_store.ProfileStore(data_dir)
            store.ensure_legacy_imported(contacts_path, tracker_path, suppress_path, legacy_log)
            fields, imported_contacts = store.load_contacts()
            imported_tracker = store.load_tracker_rows()
            imported_suppressed = store.load_suppressed()
            report = store.migration_report()

            self.assertEqual(fields, ["name", "email", "company", "custom"])
            self.assertEqual(imported_contacts, contact_rows)
            self.assertEqual(imported_tracker, tracker_rows)
            self.assertEqual(imported_suppressed, {"blocked@example.com"})
            self.assertEqual(report["counts"]["contacts"], 2)
            self.assertEqual(report["counts"]["tracker_rows"], 2)
            self.assertEqual(report["journal_mode"], "wal")
            self.assertEqual(report["integrity_check"], "ok")
            self.assertEqual({path: digest(path) for path in sources}, before)

    def test_legacy_sent_log_fallback_is_imported_without_overwriting_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "data"
            tracker = data_dir / "tracker.csv"
            legacy = root / "sent_log.csv"
            write_csv(tracker, ["email", "status"], [{"email": "bad@example.com", "status": "error"}])
            write_csv(legacy, ["email", "status"], [{"email": "sent@example.com", "status": "sent"}])
            tracker_before, legacy_before = digest(tracker), digest(legacy)
            store = profile_store.ProfileStore(data_dir)
            store.ensure_legacy_imported(
                data_dir / "contacts.csv", tracker, data_dir / "unsubscribed.txt", legacy
            )
            self.assertEqual(store.load_tracker_rows(), [{"email": "sent@example.com", "status": "sent"}])
            self.assertEqual(digest(tracker), tracker_before)
            self.assertEqual(digest(legacy), legacy_before)

    def test_migration_is_idempotent_after_database_becomes_authoritative(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            contacts = data_dir / "contacts.csv"
            write_csv(contacts, ["name", "email"], [{"name": "Before", "email": "a@example.com"}])
            store = profile_store.ProfileStore(data_dir)
            store.ensure_legacy_imported(contacts, data_dir / "tracker.csv", data_dir / "unsubscribed.txt")
            store.save_contacts(["name", "email"], [
                {"name": "Database", "email": "a@example.com"},
                {"name": "New", "email": "b@example.com"},
            ])
            write_csv(contacts, ["name", "email"], [{"name": "Stale source", "email": "a@example.com"}])
            store.ensure_legacy_imported(contacts, data_dir / "tracker.csv", data_dir / "unsubscribed.txt")
            self.assertEqual(store.load_contacts()[1], [
                {"name": "Database", "email": "a@example.com"},
                {"name": "New", "email": "b@example.com"},
            ])

    def test_absolute_migration_paths_rebase_after_project_move(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "new-project" / "data"
            contacts = data_dir / "contacts.csv"
            write_csv(contacts, ["name", "email"], [{"name": "A", "email": "a@example.com"}])
            store = profile_store.ProfileStore(data_dir)
            store.ensure_legacy_imported(
                contacts, data_dir / "tracker.csv", data_dir / "unsubscribed.txt"
            )
            with store.connect() as connection:
                connection.execute(
                    "UPDATE migration_sources SET source_path=? WHERE source_key='contacts'",
                    (r"C:\old\Downloads\cold_email_project\data\contacts.csv",),
                )
                connection.commit()
            self.assertEqual(store.rebase_migration_paths(), 1)
            contacts_source = next(
                item for item in store.migration_report()["sources"]
                if item["source_key"] == "contacts"
            )
            self.assertEqual(contacts_source["source_path"], "contacts.csv")

    def test_json_state_migrates_once_and_future_writes_stay_in_sqlite(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            legacy = data_dir / "one_off_draft.json"
            legacy.parent.mkdir(parents=True)
            initial = {"drafts": [{"draft_id": "old", "body": "saved"}]}
            legacy.write_text(json.dumps(initial), encoding="utf-8")
            original_hash = digest(legacy)
            store = profile_store.ProfileStore(data_dir)
            self.assertEqual(store.load_json_state(legacy, {}), initial)
            updated = {"drafts": [{"draft_id": "old", "body": "new"}]}
            store.save_json_state(legacy, updated)
            self.assertEqual(store.load_json_state(legacy, {}), updated)
            self.assertEqual(digest(legacy), original_hash)
            self.assertEqual(store.migration_report()["counts"]["app_state"], 1)

    def test_suppression_updates_are_atomic_and_scope_is_per_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = profile_store.ProfileStore(root / "profile-one" / "data")
            second = profile_store.ProfileStore(root / "profile-two" / "data")
            first.save_contacts(["name", "email"], [{"name": "A", "email": "a@example.com"}])
            first.save_tracker_rows([{
                "email": "a@example.com", "status": "sent", "followup_opt_in": "yes",
                "timestamp": "2026-11-01", "gmail_thread_id": "123",
            }])
            removed = first.suppress_addresses(
                {"A@example.com"},
                root / "profile-one" / "data" / "contacts.csv",
                root / "profile-one" / "data" / "tracker.csv",
                root / "profile-one" / "data" / "unsubscribed.txt",
            )
            self.assertEqual(removed, 1)
            self.assertEqual(first.load_suppressed(), {"a@example.com"})
            self.assertEqual(first.load_contacts()[1], [])
            self.assertEqual(first.load_tracker_rows()[0]["followup_opt_in"], "no")
            self.assertEqual(second.load_suppressed(), set())

    def test_wal_allows_reader_snapshot_during_writer_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = profile_store.ProfileStore(Path(temporary) / "data")
            store.save_contacts(["name", "email"], [{"name": "First", "email": "a@example.com"}])
            reader = sqlite3.connect(store.database_path, timeout=2)
            writer = sqlite3.connect(store.database_path, timeout=2)
            try:
                reader.execute("BEGIN")
                self.assertEqual(reader.execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 1)
                writer.execute(
                    "INSERT INTO contacts(position,email_key,row_json) VALUES(?,?,?)",
                    (1, "b@example.com", '{"name":"Second","email":"b@example.com"}'),
                )
                writer.commit()
                self.assertEqual(reader.execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 1)
                reader.commit()
                self.assertEqual(reader.execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 2)
            finally:
                reader.close()
                writer.close()

    def test_online_database_copy_keeps_profile_data_isolated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = profile_store.ProfileStore(root / "source" / "data")
            source.save_contacts(["name", "email"], [{"name": "Source", "email": "a@example.com"}])
            target_path = root / "target" / "data" / "app.sqlite3"
            self.assertTrue(profile_store.copy_profile_database(source.database_path, target_path))
            target = profile_store.ProfileStore(target_path.parent)
            self.assertEqual(target.load_contacts()[1], [{"name": "Source", "email": "a@example.com"}])
            source.save_contacts(["name", "email"], [{"name": "Changed", "email": "a@example.com"}])
            self.assertEqual(target.load_contacts()[1][0]["name"], "Source")

    def test_migration_command_verifies_full_profile_and_keeps_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "accounts" / "profile-a" / "data"
            write_csv(data_dir / "contacts.csv", ["name", "email"], [
                {"name": "A", "email": "a@example.com"},
            ])
            write_csv(data_dir / "tracker.csv", ["email", "status"], [
                {"email": "a@example.com", "status": "sent"},
            ])
            (data_dir / "unsubscribed.txt").write_text("blocked@example.com\n", encoding="utf-8")
            (data_dir / "archived_threads.json").write_text(
                '["a@example.com"]', encoding="utf-8"
            )
            preserved = {
                path: digest(path) for path in data_dir.iterdir() if path.is_file()
            }
            report = migrate_data.migrate_data_directory(data_dir)
            self.assertEqual(report["counts"]["contacts"], 1)
            self.assertEqual(report["counts"]["tracker_rows"], 1)
            self.assertEqual(report["counts"]["suppressions"], 1)
            self.assertEqual(report["migrated_state_files"], ["archived_threads.json"])
            self.assertTrue(all(
                not Path(item["source_path"]).is_absolute()
                for item in report["sources"] if item["source_path"]
            ))
            self.assertEqual({path: digest(path) for path in preserved}, preserved)
            store = profile_store.get_store(data_dir)
            store.save_contacts(["name", "email"], [
                {"name": "A", "email": "a@example.com"},
                {"name": "B", "email": "b@example.com"},
            ])
            second_report = migrate_data.migrate_data_directory(data_dir)
            self.assertEqual(second_report["counts"]["contacts"], 2)

    def test_mailer_tracker_writes_use_database_and_leave_csv_snapshot_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            tracker = data_dir / "tracker.csv"
            row = dict.fromkeys(cold_mailer.TRACKER_FIELDS, "")
            row.update(
                email="a@example.com", status="sent",
                timestamp="2026-11-01T10:00:00", send_type="campaign",
            )
            write_csv(tracker, cold_mailer.TRACKER_FIELDS, [row])
            original_hash = digest(tracker)
            with patch.object(cold_mailer, "CONTACTS_FILE", data_dir / "contacts.csv"), \
                    patch.object(cold_mailer, "TRACKER_FILE", tracker), \
                    patch.object(cold_mailer, "SUPPRESS_FILE", data_dir / "unsubscribed.txt"), \
                    patch.object(cold_mailer, "LEGACY_LOG_FILE", data_dir.parent / "sent_log.csv"):
                cold_mailer.write_log(
                    "a@example.com", "replied", "new reply", event_at="2026-11-02T10:00:00"
                )
                saved = next(
                    item for item in cold_mailer.load_tracker_rows()
                    if item["email"] == "a@example.com"
                )
            self.assertEqual(saved["followup_status"], "replied")
            self.assertEqual(saved["followup_detail"], "new reply")
            self.assertEqual(digest(tracker), original_hash)

    def test_dashboard_contact_reads_and_exports_use_sqlite(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            contacts = data_dir / "contacts.csv"
            write_csv(contacts, ["name", "email", "company"], [
                {"name": "Existing", "email": "old@example.com", "company": "Old Co"},
            ])
            original_hash = digest(contacts)
            with patch.object(cold_mailer, "CONTACTS_FILE", contacts), \
                    patch.object(cold_mailer, "TRACKER_FILE", data_dir / "tracker.csv"), \
                    patch.object(cold_mailer, "SUPPRESS_FILE", data_dir / "unsubscribed.txt"), \
                    patch.object(cold_mailer, "LEGACY_LOG_FILE", data_dir.parent / "sent_log.csv"):
                fields, rows = dashboard.read_raw_contacts()
                dashboard.save_raw_contacts(fields, [
                    *rows,
                    {"name": "New", "email": "new@example.com", "title": "Engineer", "company": "New Co"},
                ])
                saved_fields, saved_rows = dashboard.read_raw_contacts()
                export_text = dashboard.contact_export_bytes(saved_fields, saved_rows).decode("utf-8-sig")
            self.assertEqual(len(saved_rows), 2)
            self.assertIn("new@example.com", export_text)
            self.assertEqual(digest(contacts), original_hash)


if __name__ == "__main__":
    unittest.main()
