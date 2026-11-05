"""Non-destructive migration of profile CSV/JSON data into per-profile SQLite."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

import profile_store


JSON_FILES = (
    "one_off_draft.json",
    "archived_threads.json",
    "notification_read_state.json",
    "thread_reply_state.json",
    "gmail_reply_cursor.json",
)


def discover_data_directories(root):
    root = Path(root).resolve()
    candidates = [root / "data"]
    for folder_name in ("accounts", "local_profiles"):
        parent = root / folder_name
        if parent.is_dir():
            candidates.extend(
                profile / "data"
                for profile in parent.iterdir()
                if profile.is_dir() and (profile / "data").is_dir()
            )
    return sorted({path for path in candidates if path.is_dir()}, key=lambda path: str(path).casefold())


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def migrate_data_directory(data_dir):
    data_dir = Path(data_dir).resolve()
    profile_dir = data_dir.parent
    source_paths = [
        data_dir / "contacts.csv",
        data_dir / "tracker.csv",
        data_dir / "unsubscribed.txt",
        profile_dir / "sent_log.csv",
        *(data_dir / filename for filename in JSON_FILES),
    ]
    before_hashes = {
        str(path): _sha256(path) for path in source_paths if path.is_file()
    }

    store = profile_store.get_store(data_dir)
    migrated_before = {
        item["source_key"] for item in store.migration_report()["sources"]
    }
    store.ensure_legacy_imported(
        data_dir / "contacts.csv",
        data_dir / "tracker.csv",
        data_dir / "unsubscribed.txt",
        profile_dir / "sent_log.csv",
    )
    imported_state = []
    for filename in JSON_FILES:
        legacy_path = data_dir / filename
        if legacy_path.is_file():
            store.load_json_state(legacy_path, None)
            imported_state.append(filename)

    rebased_paths = store.rebase_migration_paths()

    report = store.migration_report()
    if report["integrity_check"].lower() != "ok":
        raise RuntimeError(
            f"SQLite integrity check failed: {report['integrity_check']}"
        )
    report["migrated_state_files"] = imported_state
    report["preserved_source_files"] = len(before_hashes)
    report["rebased_migration_paths"] = rebased_paths
    changed_sources = [
        source for source, digest in before_hashes.items()
        if not Path(source).is_file() or _sha256(source) != digest
    ]
    if changed_sources:
        raise RuntimeError(
            "Legacy source files changed during migration: " + ", ".join(changed_sources)
        )

    expected_counts = {
        item["source_key"]: item["row_count"] for item in report["sources"]
    }
    actual_counts = report["counts"]
    for source_key, table, count_key in (
        ("contacts", "contacts", "contacts"),
        ("tracker", "tracker_rows", "tracker_rows"),
        ("suppressions", "suppressions", "suppressions"),
    ):
        expected = (
            expected_counts.get(source_key)
            if source_key not in migrated_before else None
        )
        if expected is not None and int(expected) != int(actual_counts[count_key]):
            raise RuntimeError(
                f"Migration verification failed for {table}: expected {expected}, "
                f"found {actual_counts[count_key]}."
            )
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Import legacy profile data into SQLite without changing source files."
    )
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parent,
        help="Project root containing data/, accounts/, and local_profiles/.",
    )
    args = parser.parse_args(argv)
    reports = []
    failures = []
    for data_dir in discover_data_directories(args.root):
        try:
            reports.append(migrate_data_directory(data_dir))
        except Exception as exc:
            failures.append({"data_dir": str(data_dir), "error": f"{type(exc).__name__}: {exc}"})
    print(json.dumps({"profiles": reports, "errors": failures}, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
