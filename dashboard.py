#!/usr/bin/env python3
"""Local-only browser dashboard for the cold email mailer."""

import argparse
import configparser
import base64
import copy
import csv
from contextlib import contextmanager
from http.cookies import SimpleCookie
import hashlib
import hmac
import io
import imaplib
import json
import mimetypes
import os
import queue
import re
import shutil
import smtplib
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
import webbrowser
import zipfile
from collections import OrderedDict
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath
from urllib.parse import parse_qs, urlparse

import cold_mailer as mailer
from local_auth import LocalAuth, SESSION_LIFETIME_SECONDS
import profile_store

BASE = Path(__file__).resolve().parent
HTML_FILE = BASE / "static" / "dashboard.html"
SCRIPT_FILE = BASE / "cold_mailer.py"
PROFILE_REGISTRY_FILE = BASE / "data" / "profiles.json"
PROFILE_ROOT = BASE / "accounts"
AUTH_DB_FILE = BASE / "data" / "local_auth.sqlite3"
MAX_JSON_BYTES = 12 * 1024 * 1024
MAX_RESUME_BYTES = 8 * 1024 * 1024
MAX_CONTACT_IMPORT_BYTES = 8 * 1024 * 1024
MAX_CONTACT_IMPORT_ROWS = 100_000
MAX_CONTACT_IMPORT_UNPACKED_BYTES = 64 * 1024 * 1024
CONTACT_IMPORT_STAGE_TTL_SECONDS = 20 * 60
CONTACT_IMPORT_STAGES_PER_ACCOUNT = 3
MAX_ONE_OFF_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_ONE_OFF_ATTACHMENTS = 10
ONE_OFF_DRAFT_FILE = BASE / "data" / "one_off_draft.json"
ARCHIVED_THREADS_FILE = BASE / "data" / "archived_threads.json"
NOTIFICATION_READ_STATE_FILE = BASE / "data" / "notification_read_state.json"
one_off_drafts_lock = threading.RLock()
archived_threads_lock = threading.RLock()
notification_state_lock = threading.RLock()
thread_cache_lock = threading.Lock()
contact_import_lock = threading.RLock()
contact_import_stages = {}
thread_cache = OrderedDict()
thread_activity = OrderedDict()
THREAD_CACHE_TTL_SECONDS = 45
THREAD_CACHE_MAX_ENTRIES = 12
THREAD_CACHE_MAX_CHARS = 1_000_000
DELIVERY_CHECK_INTERVAL_SECONDS = 15 * 60
DELIVERY_LOOKBACK_DAYS = 30
REPLY_CHECK_INTERVAL_SECONDS = 3 * 60
REPLY_WATCH_RECONNECT_SECONDS = 15
CONTACT_FIELDS = ["name", "email", "title", "company"]
profile_lock = threading.RLock()
active_profile = None
active_profile_id = ""
auth_store = None
auth_failures = {}
auth_failures_lock = threading.Lock()
AUTH_COOKIE_NAME = "outreach_session"
AUTH_FAILURE_WINDOW_SECONDS = 15 * 60
AUTH_FAILURE_LIMIT = 8
profile_changed_event = threading.Event()
delivery_check_requested = threading.Event()
jobs = {}
jobs_lock = threading.Lock()
campaign_processes = {}
campaign_process_lock = threading.Lock()
active_job_id = None
active_delivery_job_id = None
active_read_job_ids = set()
event_clients = set()
event_clients_lock = threading.Lock()
event_revision = 0
delivery_monitor = {
    "state": "starting",
    "interval_seconds": DELIVERY_CHECK_INTERVAL_SECONDS,
    "lookback_days": DELIVERY_LOOKBACK_DAYS,
    "last_started_at": "",
    "last_completed_at": "",
    "last_successful_at": "",
    "next_check_at": "",
    "error": "",
}
delivery_monitor_stop = threading.Event()
reply_monitor_stop = threading.Event()
reply_check_requested = threading.Event()
reply_job_finished = threading.Event()
reply_monitor = {
    "state": "starting",
    "interval_seconds": REPLY_CHECK_INTERVAL_SECONDS,
    "last_started_at": "",
    "last_completed_at": "",
    "last_successful_at": "",
    "next_check_at": "",
    "error": "",
    "push_state": "starting",
    "push_last_event_at": "",
    "push_error": "",
}


def account_directory(account_id):
    if not re.fullmatch(r"[a-f0-9]{32}", str(account_id or "")):
        raise ValueError("Account identity is invalid.")
    root = PROFILE_ROOT.resolve()
    target = (root / account_id).resolve()
    if target.parent != root:
        raise ValueError("Account data path is invalid.")
    return target


def create_account_data(account_id, copy_legacy=False):
    root = account_directory(account_id)
    root.mkdir(parents=True, exist_ok=False)
    data_dir = root / "data"
    templates_dir = root / "templates"
    data_dir.mkdir()
    templates_dir.mkdir()
    source_root = BASE
    if copy_legacy and PROFILE_REGISTRY_FILE.is_file():
        try:
            old_registry = json.loads(PROFILE_REGISTRY_FILE.read_text(encoding="utf-8"))
            old_id = str(old_registry.get("active_profile_id", ""))
            old_root = BASE / "local_profiles" / old_id
            if re.fullmatch(r"[a-f0-9]{32}", old_id) and old_root.is_dir():
                source_root = old_root
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    if copy_legacy:
        source_data = source_root / "data"
        if source_data.is_dir():
            shutil.copytree(
                source_data,
                data_dir,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(
                    "profiles.json", "local_auth.sqlite3", "app.sqlite3*"
                ),
            )
            profile_store.copy_profile_database(
                source_data / "app.sqlite3", data_dir / "app.sqlite3"
            )
        source_templates = source_root / "templates"
        if source_templates.is_dir():
            shutil.copytree(source_templates, templates_dir, dirs_exist_ok=True)
        for filename in (".env", "config.ini", "sent_log.csv"):
            source = source_root / filename
            if source.is_file():
                shutil.copy2(source, root / filename)
    else:
        (templates_dir / "template.txt").write_text(
            "Name: Your name\nSubject: Exploring opportunities at {company}\n\n"
            "Hi {first_name},\n\n[Write your outreach message here.]\n\n"
            "Best,\n{name}\n",
            encoding="utf-8",
        )
        (templates_dir / "followup_template.txt").write_text(
            "Subject: Re: {subject}\n\nHi {first_name},\n\n"
            "[Write a concise follow-up message here.]\n\nBest,\n{name}\n",
            encoding="utf-8",
        )
        source_config = BASE / "config.ini"
        if source_config.is_file():
            shutil.copy2(source_config, root / "config.ini")
        else:
            (root / "config.ini").write_text(
                "[settings]\nemails_per_day = 10\ngap_min_seconds = 45\n"
                "gap_max_seconds = 120\nmax_followup_weeks = 1\n"
                "followup_interval_days = 7\n",
                encoding="utf-8",
            )
        (root / ".env").write_text("sender_email=\napp_password=\n", encoding="utf-8")
    if not (root / ".env").exists():
        (root / ".env").write_text("sender_email=\napp_password=\n", encoding="utf-8")
    if not (root / "config.ini").exists():
        (root / "config.ini").write_text(
            "[settings]\nemails_per_day = 10\ngap_min_seconds = 45\n"
            "gap_max_seconds = 120\nmax_followup_weeks = 1\n"
            "followup_interval_days = 7\n",
            encoding="utf-8",
        )
    if not (templates_dir / "template.txt").exists():
        (templates_dir / "template.txt").write_text(
            "Name: Your name\nSubject: Exploring opportunities at {company}\n\n"
            "Hi {first_name},\n\n[Write your outreach message here.]\n\n"
            "Best,\n{name}\n",
            encoding="utf-8",
        )
    if not (templates_dir / "followup_template.txt").exists():
        (templates_dir / "followup_template.txt").write_text(
            "Subject: Re: {subject}\n\nHi {first_name},\n\n"
            "[Write a concise follow-up message here.]\n\nBest,\n{name}\n",
            encoding="utf-8",
        )
    (data_dir / "resume").mkdir(parents=True, exist_ok=True)
    return root


def activate_account(user, copy_legacy=False):
    global active_profile, active_profile_id
    account_id = str(user["id"])
    root = account_directory(account_id)
    if not root.is_dir():
        create_account_data(account_id, copy_legacy=copy_legacy)
    active_profile = dict(user)
    active_profile_id = account_id
    mailer.configure_profile_directory(root)
    try:
        removed_blocked = mailer.remove_contacts(mailer.load_suppressed())
        if removed_blocked:
            print(f"Removed {removed_blocked:,} blocked contact(s); tracker history was kept.")
    except Exception as exc:
        print(f"Could not reconcile blocked contacts: {type(exc).__name__}: {exc}", file=sys.stderr)
    global ONE_OFF_DRAFT_FILE, ARCHIVED_THREADS_FILE, NOTIFICATION_READ_STATE_FILE
    ONE_OFF_DRAFT_FILE = root / "data" / "one_off_draft.json"
    ARCHIVED_THREADS_FILE = root / "data" / "archived_threads.json"
    NOTIFICATION_READ_STATE_FILE = root / "data" / "notification_read_state.json"
    with thread_cache_lock:
        thread_cache.clear()
        thread_activity.clear()
    with jobs_lock:
        delivery_monitor.update({
            "state": "starting", "last_started_at": "", "last_completed_at": "",
            "last_successful_at": "", "next_check_at": "", "error": "",
        })
        reply_monitor.update({
            "state": "starting", "last_started_at": "", "last_completed_at": "",
            "last_successful_at": "", "next_check_at": "", "error": "",
            "push_state": "starting", "push_last_event_at": "", "push_error": "",
        })
    profile_changed_event.set()
    delivery_check_requested.set()
    reply_check_requested.set()
    publish_event("account", {"account_id": account_id})


def deactivate_account():
    global active_profile, active_profile_id, ONE_OFF_DRAFT_FILE
    global ARCHIVED_THREADS_FILE, NOTIFICATION_READ_STATE_FILE
    active_profile = None
    active_profile_id = ""
    mailer.configure_profile_directory(BASE)
    ONE_OFF_DRAFT_FILE = BASE / "data" / "one_off_draft.json"
    ARCHIVED_THREADS_FILE = BASE / "data" / "archived_threads.json"
    NOTIFICATION_READ_STATE_FILE = BASE / "data" / "notification_read_state.json"
    with thread_cache_lock:
        thread_cache.clear()
        thread_activity.clear()
    profile_changed_event.set()
    delivery_check_requested.set()
    reply_check_requested.set()
    publish_event("account", {"account_id": ""})


def initialize_auth():
    global auth_store
    PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
    auth_store = LocalAuth(AUTH_DB_FILE)


def read_raw_contacts():
    fields, stored_rows = mailer.load_raw_contacts()
    rows = []
    for raw in stored_rows:
        row = {
            str(key): ("" if value is None else str(value)).strip()
            for key, value in raw.items() if key
        }
        for key in CONTACT_FIELDS:
            row.setdefault(key, "")
        rows.append(row)
    return fields, rows


def save_raw_contacts(fields, rows):
    fields = list(dict.fromkeys(str(field) for field in fields if field))
    for key in CONTACT_FIELDS:
        if key not in fields:
            fields.append(key)
    mailer.save_raw_contacts(fields, rows)


CONTACT_IMPORT_ALIASES = {
    "name": {"name", "full name", "fullname", "contact name", "person", "recipient", "candidate"},
    "first_name": {"first name", "firstname", "given name"},
    "last_name": {"last name", "lastname", "surname", "family name"},
    "email": {"email", "email address", "e mail", "e mail address", "mail", "recipient email", "email id"},
    "title": {"title", "job title", "position", "role", "designation"},
    "company": {"company", "company name", "organization", "organisation", "employer"},
}
MAX_CONTACT_IMPORT_COLUMNS = 128
MAX_CONTACT_IMPORT_CELLS = 2_000_000


def _import_cell_text(value):
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return unicodedata.normalize("NFKC", str(value)).replace("\x00", "").strip()


def _import_contact_text(value, limit=200):
    value = re.sub(r"[\x00-\x1f\x7f]+", " ", _import_cell_text(value))
    return re.sub(r"\s+", " ", value).strip()[:limit]


def _import_column_name(index):
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return f"Column {name}"


def _contact_import_stage(token):
    if not re.fullmatch(r"[a-f0-9]{32}", str(token or "")):
        raise ValueError("This import has expired. Choose the file again.")
    with contact_import_lock:
        stage = contact_import_stages.get(token)
        if not stage:
            raise ValueError("This import has expired. Choose the file again.")
        if stage.get("profile_id") != active_profile_id:
            raise ValueError("This import belongs to a different local account. Choose the file again.")
        if time.time() - stage["created_at"] > CONTACT_IMPORT_STAGE_TTL_SECONDS:
            contact_import_stages.pop(token, None)
            try:
                stage["path"].unlink(missing_ok=True)
            except OSError:
                pass
            raise ValueError("This import has expired. Choose the file again.")
        return dict(stage)


def _cleanup_contact_import_stages(directory):
    cutoff = time.time() - CONTACT_IMPORT_STAGE_TTL_SECONDS
    with contact_import_lock:
        expired = [token for token, stage in contact_import_stages.items()
                   if stage["created_at"] < cutoff]
        for token in expired:
            stage = contact_import_stages.pop(token, None)
            if stage:
                try:
                    stage["path"].unlink(missing_ok=True)
                except OSError:
                    pass
        directory.mkdir(parents=True, exist_ok=True)
        files = sorted(directory.glob("*"), key=lambda item: item.stat().st_mtime)
        for path in files:
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                continue


def stage_contact_import(filename, encoded_content):
    safe_name = PureWindowsPath(str(filename or "")).name.strip()
    suffix = Path(safe_name).suffix.lower()
    if suffix not in (".csv", ".xls", ".xlsx", ".xlsm"):
        raise ValueError("Choose a CSV, Excel (.xls), or Excel (.xlsx/.xlsm) file.")
    try:
        content = base64.b64decode(encoded_content, validate=True)
    except (ValueError, TypeError):
        raise ValueError("The uploaded file could not be read. Try saving it again.") from None
    if not content or len(content) > MAX_CONTACT_IMPORT_BYTES:
        raise ValueError("Choose a file smaller than 8 MB.")
    if suffix == ".csv":
        if b"\x00" in content and not content.startswith((b"\xff\xfe", b"\xfe\xff")):
            raise ValueError("This CSV contains unsupported binary data.")
    elif suffix == ".xls":
        if not content.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
            raise ValueError("This .xls file does not look like a valid Excel workbook.")
    else:
        if not content.startswith(b"PK"):
            raise ValueError("This Excel file does not look like a valid workbook.")
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                entries = archive.infolist()
                unpacked = sum(item.file_size for item in entries)
                if len(entries) > 10_000 or unpacked > MAX_CONTACT_IMPORT_UNPACKED_BYTES:
                    raise ValueError("This workbook expands beyond the safe import limit.")
                if any(item.file_size > 0 and item.compress_size == 0 for item in entries):
                    raise ValueError("This workbook contains an invalid compressed file.")
                if any(item.compress_size and item.file_size / item.compress_size > 250 for item in entries):
                    raise ValueError("This workbook uses an unsafe compression ratio.")
                if archive.testzip() is not None:
                    raise ValueError("This workbook is damaged. Try saving a fresh copy.")
        except zipfile.BadZipFile:
            raise ValueError("This Excel file is damaged. Try saving a fresh copy.") from None

    directory = mailer.DATA_DIR / ".contact_import_staging"
    _cleanup_contact_import_stages(directory)
    with contact_import_lock:
        account_stages = sum(
            1 for item in contact_import_stages.values()
            if item.get("profile_id") == active_profile_id
        )
        if account_stages >= CONTACT_IMPORT_STAGES_PER_ACCOUNT:
            raise ValueError("Finish or close an open contact import before starting another.")
        token = uuid.uuid4().hex
        path = directory / f"{token}{suffix}"
        path.write_bytes(content)
        stage = {
            "path": path, "filename": safe_name, "created_at": time.time(),
            "profile_id": active_profile_id,
        }
        contact_import_stages[token] = stage
    return token, stage


def _read_contact_import_table(path, suffix, requested_sheet="", has_header=True):
    raw_rows = []
    sheets = []
    selected_sheet = ""
    if suffix == ".csv":
        content = path.read_bytes()
        text = None
        for encoding in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
            try:
                text = content.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise ValueError("The CSV text encoding is not supported. Save it as UTF-8 and retry.")
        sample = text[:8192]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        try:
            cell_count = 0
            for row_number, row in enumerate(csv.reader(io.StringIO(text, newline=""), dialect), 1):
                if len(row) > MAX_CONTACT_IMPORT_COLUMNS:
                    raise ValueError("The file has more than 128 columns.")
                cell_count += len(row)
                if cell_count > MAX_CONTACT_IMPORT_CELLS:
                    raise ValueError("The file has too many cells to import safely.")
                raw_rows.append((row_number, [_import_cell_text(value) for value in row]))
                if len(raw_rows) > MAX_CONTACT_IMPORT_ROWS + 1:
                    raise ValueError("The file has more than 100,000 rows. Split it into smaller files.")
        except csv.Error as exc:
            raise ValueError(f"The CSV structure is invalid: {exc}") from None
    elif suffix == ".xls":
        try:
            import xlrd
        except ImportError:
            raise ValueError("Excel .xls support is not installed. Run: py -m pip install -r requirements.txt") from None
        try:
            workbook = xlrd.open_workbook(str(path), on_demand=True, ragged_rows=True)
        except Exception as exc:
            raise ValueError(f"Could not open this Excel file: {type(exc).__name__}.") from None
        try:
            sheets = list(workbook.sheet_names())
            if not sheets:
                raise ValueError("This workbook has no worksheets.")
            selected_sheet = requested_sheet if requested_sheet in sheets else sheets[0]
            sheet = workbook.sheet_by_name(selected_sheet)
            if sheet.ncols > MAX_CONTACT_IMPORT_COLUMNS:
                raise ValueError("The worksheet has more than 128 columns.")
            if sheet.nrows > MAX_CONTACT_IMPORT_ROWS + 1:
                raise ValueError("The worksheet has more than 100,000 rows. Split it into smaller files.")
            if sheet.nrows * sheet.ncols > MAX_CONTACT_IMPORT_CELLS:
                raise ValueError("The worksheet is too large to import safely.")
            for index in range(sheet.nrows):
                values = []
                for cell in sheet.row(index):
                    value = cell.value
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        try:
                            value = xlrd.xldate_as_datetime(value, workbook.datemode)
                        except (ValueError, OverflowError):
                            value = ""
                    values.append(_import_cell_text(value))
                raw_rows.append((index + 1, values))
        finally:
            workbook.release_resources()
    else:
        try:
            from openpyxl import load_workbook
        except ImportError:
            raise ValueError("Excel .xlsx support is not installed. Run: py -m pip install -r requirements.txt") from None
        try:
            workbook = load_workbook(
                filename=path, read_only=True, data_only=True, keep_links=False,
            )
        except Exception as exc:
            raise ValueError(f"Could not open this Excel file: {type(exc).__name__}.") from None
        try:
            sheets = [sheet.title for sheet in workbook.worksheets if sheet.sheet_state == "visible"]
            if not sheets:
                sheets = list(workbook.sheetnames)
            if not sheets:
                raise ValueError("This workbook has no worksheets.")
            selected_sheet = requested_sheet if requested_sheet in sheets else sheets[0]
            sheet = workbook[selected_sheet]
            max_row = sheet.max_row or 0
            max_column = sheet.max_column or 0
            if max_row > MAX_CONTACT_IMPORT_ROWS + 1:
                raise ValueError("The worksheet has more than 100,000 rows. Split it into smaller files.")
            if max_column > MAX_CONTACT_IMPORT_COLUMNS:
                raise ValueError("The worksheet has more than 128 columns.")
            if max_row * max_column > MAX_CONTACT_IMPORT_CELLS:
                raise ValueError("The worksheet is too large to import safely.")
            cell_count = 0
            for index, row in enumerate(
                sheet.iter_rows(max_row=max_row, max_col=max_column, values_only=True),
                1,
            ):
                values = [_import_cell_text(value) for value in row]
                cell_count += sum(value != "" for value in values)
                if cell_count > MAX_CONTACT_IMPORT_CELLS:
                    raise ValueError("The worksheet is too large to import safely.")
                raw_rows.append((index, values))
        finally:
            workbook.close()

    first_data = next((index for index, (_, row) in enumerate(raw_rows)
                       if any(value for value in row)), None)
    if first_data is None:
        raise ValueError("This file has no contact rows.")
    first_row_number, first_row = raw_rows[first_data]
    width = max(1, min(MAX_CONTACT_IMPORT_COLUMNS, max(len(row) for _, row in raw_rows)))
    if has_header:
        columns = []
        seen = set()
        for index in range(width):
            value = first_row[index] if index < len(first_row) else ""
            label = value or _import_column_name(index + 1)
            base = label
            duplicate = 2
            while label.casefold() in seen:
                label = f"{base} ({duplicate})"
                duplicate += 1
            seen.add(label.casefold())
            columns.append(label)
        data_rows = [
            (number, row[:width] + [""] * max(0, width - len(row)))
            for number, row in raw_rows[first_data + 1:]
        ]
    else:
        columns = [_import_column_name(index + 1) for index in range(width)]
        data_rows = [
            (number, row[:width] + [""] * max(0, width - len(row)))
            for number, row in raw_rows[first_data:]
        ]
    if not data_rows:
        raise ValueError("This file has a header but no contact rows.")
    if len(data_rows) * width > MAX_CONTACT_IMPORT_CELLS:
        raise ValueError("The worksheet is too large to import safely.")
    return sheets, selected_sheet, columns, data_rows


def _suggest_contact_mapping(columns):
    normalized = [re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.casefold())).strip()
                  for value in columns]
    mapping = {field: [] for field in CONTACT_FIELDS}
    for field in CONTACT_FIELDS:
        aliases = CONTACT_IMPORT_ALIASES[field]
        position = next((index for index, value in enumerate(normalized) if value in aliases), None)
        if position is not None:
            mapping[field] = [position]
    if not mapping["name"]:
        first = next((i for i, value in enumerate(normalized) if value in CONTACT_IMPORT_ALIASES["first_name"]), None)
        last = next((i for i, value in enumerate(normalized) if value in CONTACT_IMPORT_ALIASES["last_name"]), None)
        if first is not None and last is not None:
            mapping["name"] = [first, last]
        elif first is not None:
            mapping["name"] = [first]
    return mapping


def _validated_contact_mapping(mapping, columns):
    if not isinstance(mapping, dict):
        raise ValueError("Match your file columns to the contact fields.")
    result = {}
    for field in CONTACT_FIELDS:
        value = mapping.get(field, [])
        if isinstance(value, (str, int)):
            value = [value] if str(value).strip() not in ("", "skip", "none") else []
        if not isinstance(value, list) or len(value) > 2:
            raise ValueError("Choose at most two source columns for Name and one for each other field.")
        indexes = []
        for item in value:
            try:
                index = int(item)
            except (TypeError, ValueError):
                raise ValueError("A column mapping is invalid.") from None
            if not 0 <= index < len(columns) or index in indexes:
                raise ValueError("A column mapping is invalid.")
            indexes.append(index)
        if field != "name" and len(indexes) > 1:
            raise ValueError(f"Choose only one source column for {field.title()}.")
        result[field] = indexes
    if not result["name"] or not result["email"]:
        raise ValueError("Name and Email must both be mapped before previewing.")
    return result


def _normalize_import_email(value):
    value = unicodedata.normalize("NFKC", str(value or "")).strip()
    if value.lower().startswith("mailto:"):
        value = value[7:].strip()
    bracketed = re.search(r"<([^<>]+)>", value)
    if bracketed:
        value = bracketed.group(1).strip()
    if value.count("@") == 1:
        local, domain = value.rsplit("@", 1)
        try:
            domain = domain.encode("idna").decode("ascii")
        except UnicodeError:
            return ""
        value = f"{local}@{domain}"
    value = value.casefold()
    return value if mailer.EMAIL_RE.fullmatch(value) else ""


def inspect_contact_import(token, sheet_name, has_header, mapping):
    stage = _contact_import_stage(token)
    if not isinstance(has_header, bool):
        raise ValueError("Choose whether the first row contains column names.")
    suffix = stage["path"].suffix.lower()
    sheets, selected_sheet, columns, rows = _read_contact_import_table(
        stage["path"], suffix, str(sheet_name or ""), has_header,
    )
    column_mapping = _validated_contact_mapping(mapping, columns)
    fields, existing_rows = read_raw_contacts()
    existing = {str(row.get("email", "")).strip().casefold() for row in existing_rows}
    suppressed = {email.casefold() for email in mailer.load_suppressed()}
    seen = set()
    contacts = []
    invalid = duplicates = blocked = empty = 0
    issue_samples = []
    for row_number, values in rows:
        if not any(values):
            empty += 1
            continue
        name_parts = [_import_contact_text(values[index]) for index in column_mapping["name"] if values[index]]
        name = " ".join(name_parts).strip()
        email_values = [values[index] for index in column_mapping["email"]]
        email = _normalize_import_email(email_values[0] if email_values else "")
        contact = {
            "name": name[:200],
            "email": email,
            "title": _import_contact_text(values[column_mapping["title"][0]]) if column_mapping["title"] else "",
            "company": _import_contact_text(values[column_mapping["company"][0]]) if column_mapping["company"] else "",
        }
        reason = ""
        if not name or not email or any(ord(char) < 32 or ord(char) == 127 for char in name):
            invalid += 1
            reason = "Name or email is missing or invalid"
        elif email in suppressed:
            blocked += 1
            reason = "Address is blocked"
        elif email in existing or email in seen:
            duplicates += 1
            reason = "Already in contacts or repeated in this file"
        else:
            contacts.append(contact)
            seen.add(email)
        if reason and len(issue_samples) < 8:
            issue_samples.append({"row": row_number, "email": email or "n/a", "reason": reason})
    samples = contacts[:8]
    return {
        "token": token,
        "filename": stage["filename"],
        "sheets": sheets,
        "sheet": selected_sheet,
        "has_header": has_header,
        "columns": columns,
        "suggested_mapping": _suggest_contact_mapping(columns),
        "total_rows": len(rows),
        "ready": len(contacts),
        "invalid": invalid,
        "duplicates": duplicates,
        "blocked": blocked,
        "empty": empty,
        "samples": samples,
        "issues": issue_samples,
        "_contacts": contacts,
        "_fields": fields,
        "_existing_rows": existing_rows,
    }


def preview_contact_import(token, sheet_name="", has_header=True):
    stage = _contact_import_stage(token)
    if not isinstance(has_header, bool):
        raise ValueError("Choose whether the first row contains column names.")
    sheets, selected_sheet, columns, rows = _read_contact_import_table(
        stage["path"], stage["path"].suffix.lower(), str(sheet_name or ""), has_header,
    )
    return {
        "token": token,
        "filename": stage["filename"],
        "sheets": sheets,
        "sheet": selected_sheet,
        "has_header": has_header,
        "columns": columns,
        "suggested_mapping": _suggest_contact_mapping(columns),
        "sample_rows": [values for _, values in rows[:6]],
        "total_rows": len(rows),
    }


def discard_contact_import(token):
    if not re.fullmatch(r"[a-f0-9]{32}", str(token or "")):
        return
    with contact_import_lock:
        stage = contact_import_stages.pop(token, None)
    if stage:
        try:
            stage["path"].unlink(missing_ok=True)
        except OSError:
            pass


def contact_export_bytes(fields, rows):
    output = io.StringIO(newline="")
    writer = csv.writer(output, dialect="excel")
    headers = list(dict.fromkeys(fields or CONTACT_FIELDS))
    for key in CONTACT_FIELDS:
        if key not in headers:
            headers.append(key)
    writer.writerow(headers)
    for row in rows:
        values = []
        for key in headers:
            value = str(row.get(key, "") or "")
            if value.lstrip().startswith(("=", "+", "-", "@")):
                value = "'" + value
            values.append(value)
        writer.writerow(values)
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def validate_one_off(data):
    contact = {
        key: str(data.get(key, "")).strip()
        for key in CONTACT_FIELDS
    }
    contact["name"] = contact["name"] or "Unknown"
    contact["title"] = contact["title"] or "Hr"
    contact["email"] = contact["email"].lower()
    if any(ord(char) < 32 or ord(char) == 127 for char in contact["name"]):
        raise ValueError("Name cannot contain control characters.")
    if not mailer.EMAIL_RE.fullmatch(contact["email"]):
        raise ValueError("Enter a valid recipient email address.")
    for field in ("title", "company"):
        if any(ord(char) < 32 or ord(char) == 127 for char in contact[field]):
            raise ValueError(f"{field.title()} cannot contain control characters.")
    subject = str(data.get("subject", "")).strip()
    body = str(data.get("body", "")).strip()
    if not subject or "\r" in subject or "\n" in subject:
        raise ValueError("Enter a subject on one line.")
    if len(subject) > 998:
        raise ValueError("Subject must be 998 characters or fewer.")
    if not body:
        raise ValueError("Message body is required.")
    if len(body) > 50000:
        raise ValueError("Message body must be 50,000 characters or fewer.")
    if any(ord(char) < 32 and char not in "\r\n\t" for char in body):
        raise ValueError("Message body contains unsupported control characters.")
    html_body = str(data.get("html_body", ""))
    if html_body:
        html_body, _ = mailer.sanitize_email_html(html_body)
    followup_opt_in = data.get("followup_opt_in") is True
    return contact, subject, body, html_body, followup_opt_in


def validate_one_off_attachments(raw_attachments):
    if not isinstance(raw_attachments, list) or len(raw_attachments) > MAX_ONE_OFF_ATTACHMENTS:
        raise ValueError(
            f"Choose no more than {MAX_ONE_OFF_ATTACHMENTS} attachments."
        )
    attachments = []
    total_size = 0
    names = set()
    for item in raw_attachments:
        if not isinstance(item, dict):
            raise ValueError("Attachment data is invalid.")
        name = str(item.get("name", "")).strip()
        name = PureWindowsPath(name.replace("/", "\\")).name
        if (
            not name or len(name) > 180
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
        ):
            raise ValueError("Attachment filename is invalid.")
        if name.casefold() in names:
            raise ValueError(f"Attachment filename is duplicated: {name}")
        names.add(name.casefold())
        try:
            content = base64.b64decode(item.get("content", ""), validate=True)
        except (ValueError, TypeError):
            raise ValueError(f"Attachment data is invalid: {name}") from None
        if not content:
            raise ValueError(f"Attachment is empty: {name}")
        total_size += len(content)
        if total_size > MAX_ONE_OFF_ATTACHMENT_BYTES:
            raise ValueError("Attachments must total 8 MB or less.")
        mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        maintype, subtype = mime_type.split("/", 1)
        attachments.append((name, content, maintype, subtype))
    return attachments


def normalize_one_off_draft(data):
    if not isinstance(data, dict):
        raise ValueError("Draft data must be an object.")
    contact = {
        key: str(data.get(key, "")).strip()
        for key in CONTACT_FIELDS
    }
    contact["name"] = contact["name"] or "Unknown"
    contact["title"] = contact["title"] or "Hr"
    contact["email"] = contact["email"].lower()
    if contact["email"] and not mailer.EMAIL_RE.fullmatch(contact["email"]):
        raise ValueError("Enter a valid recipient email address.")
    for field in ("name", "title", "company"):
        if any(ord(char) < 32 or ord(char) == 127 for char in contact[field]):
            raise ValueError(f"{field.title()} cannot contain control characters.")
    subject = str(data.get("subject", "")).strip()
    if "\r" in subject or "\n" in subject or len(subject) > 998:
        raise ValueError("Subject must be one line and 998 characters or fewer.")
    body = str(data.get("body", ""))
    if len(body) > 50000:
        raise ValueError("Message body must be 50,000 characters or fewer.")
    if any(ord(char) < 32 and char not in "\r\n\t" for char in body):
        raise ValueError("Message body contains unsupported control characters.")
    html_body = str(data.get("html_body", ""))
    if html_body:
        html_body, _ = mailer.sanitize_email_html(html_body)
    raw_attachments = data.get("attachments", [])
    validate_one_off_attachments(raw_attachments)
    return {
        **contact,
        "subject": subject,
        "body": body,
        "html_body": html_body,
        "attachments": raw_attachments,
        "followup_opt_in": data.get("followup_opt_in") is True,
    }


def read_one_off_draft():
    drafts = read_one_off_drafts()
    return drafts[-1] if drafts else None


def read_one_off_drafts():
    with one_off_drafts_lock:
        stored = profile_store.get_store(ONE_OFF_DRAFT_FILE.parent).load_json_state(
            ONE_OFF_DRAFT_FILE, {"drafts": []}
        )
        if isinstance(stored, dict) and isinstance(stored.get("drafts"), list):
            source_drafts = stored["drafts"]
        elif isinstance(stored, dict):
            source_drafts = [dict(stored, draft_id="legacy")]
        else:
            raise ValueError("Saved drafts have an invalid format.")
        drafts = []
        seen_draft_ids = set()
        for source in source_drafts:
            if not isinstance(source, dict):
                raise ValueError("A saved draft has an invalid format.")
            draft = normalize_one_off_draft(source)
            draft["draft_id"] = str(source.get("draft_id") or uuid.uuid4().hex)
            if draft["draft_id"] in seen_draft_ids:
                drafts = [item for item in drafts if item["draft_id"] != draft["draft_id"]]
            seen_draft_ids.add(draft["draft_id"])
            draft["updated_at"] = str(source.get("updated_at", ""))
            drafts.append(draft)
        return drafts


def save_one_off_draft(data):
    if not isinstance(data, dict):
        raise ValueError("Draft data must be an object.")
    draft_id = str(data.get("draft_id", "")).strip()
    if not draft_id:
        draft_id = "legacy"
    if data.get("action") == "clear":
        if not draft_id or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", draft_id):
            raise ValueError("Choose a valid draft to remove.")
    else:
        if not draft_id or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", draft_id):
            raise ValueError("Draft ID is missing or invalid.")
        draft = normalize_one_off_draft(data)
        draft["draft_id"] = draft_id
        draft["updated_at"] = datetime.now().isoformat(timespec="seconds")
    with one_off_drafts_lock:
        drafts = read_one_off_drafts()
        if data.get("action") == "clear":
            drafts = [item for item in drafts if item["draft_id"] != draft_id]
        else:
            existing_index = next(
                (index for index, item in enumerate(drafts)
                 if item["draft_id"] == draft_id),
                None,
            )
            if existing_index is None:
                drafts.append(draft)
            else:
                drafts[existing_index] = draft
        profile_store.get_store(ONE_OFF_DRAFT_FILE.parent).save_json_state(
            ONE_OFF_DRAFT_FILE, {"drafts": drafts}
        )


def read_archived_threads():
    with archived_threads_lock:
        stored = profile_store.get_store(ARCHIVED_THREADS_FILE.parent).load_json_state(
            ARCHIVED_THREADS_FILE, []
        )
        if not isinstance(stored, list):
            raise ValueError("Archived conversation data has an invalid format.")
        return {
            str(email).strip().lower() for email in stored
            if mailer.EMAIL_RE.fullmatch(str(email).strip().lower())
        }


def save_archived_threads(emails):
    normalized = sorted({str(email).strip().lower() for email in emails})
    if any(not mailer.EMAIL_RE.fullmatch(email) for email in normalized):
        raise ValueError("Archived conversation data contains an invalid address.")
    with archived_threads_lock:
        profile_store.get_store(ARCHIVED_THREADS_FILE.parent).save_json_state(
            ARCHIVED_THREADS_FILE, normalized
        )


def load_read_notification_ids():
    with notification_state_lock:
        try:
            stored = profile_store.get_store(NOTIFICATION_READ_STATE_FILE.parent).load_json_state(
                NOTIFICATION_READ_STATE_FILE, []
            )
        except (OSError, json.JSONDecodeError, profile_store.ProfileStoreError):
            return set()
        if not isinstance(stored, list):
            return set()
        return {
            value for value in stored
            if isinstance(value, str) and value and len(value) <= 512
        }


def save_read_notification_ids(ids):
    normalized = list(dict.fromkeys(
        value for value in ids
        if isinstance(value, str) and value and len(value) <= 512
    ))[-5000:]
    with notification_state_lock:
        profile_store.get_store(NOTIFICATION_READ_STATE_FILE.parent).save_json_state(
            NOTIFICATION_READ_STATE_FILE, normalized
        )


def mark_notifications_read(ids):
    with notification_state_lock:
        current = load_read_notification_ids()
        current.update(ids)
        save_read_notification_ids(current)


def activity_timestamp(value):
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0


def publish_event(event_type, payload=None):
    global event_revision
    with event_clients_lock:
        event_revision += 1
        event = {
            "revision": event_revision,
            "type": event_type,
            "payload": payload or {},
        }
        stale = []
        for client in event_clients:
            try:
                client.put_nowait(event)
            except queue.Full:
                try:
                    client.get_nowait()
                    client.put_nowait(event)
                except (queue.Empty, queue.Full):
                    stale.append(client)
        for client in stale:
            event_clients.discard(client)


def publish_job(job_id, compact=False):
    with jobs_lock:
        job = jobs.get(job_id)
        snapshot = dict(job) if job else None
        if snapshot:
            output = job.get("output", [])
            snapshot["output"] = list(output[-20:] if compact else output)
            snapshot["timer"] = copy.deepcopy(job.get("timer"))
            snapshot["progress"] = copy.deepcopy(job.get("progress"))
    if snapshot:
        publish_event("job", snapshot)


@contextmanager
def tracker_write_guard(handler, action):
    with jobs_lock:
        if active_job_id or active_delivery_job_id:
            handler.send_json(409, {
                "error": f"Finish the active mail operation before you {action}."
            })
            yield False
            return
        yield True


def save_one_off_contact(contact):
    fields, rows = read_raw_contacts()
    existing = next(
        (row for row in rows if row["email"].lower() == contact["email"]), None
    )
    if existing:
        existing.update(contact)
    else:
        for key in CONTACT_FIELDS:
            if key not in fields:
                fields.append(key)
        rows.append(contact)
    save_raw_contacts(fields, rows)


def read_settings():
    defaults = {
        "emails_per_day": 10,
        "gap_min_seconds": 45,
        "gap_max_seconds": 120,
        "max_followup_weeks": 1,
        "followup_interval_days": 7,
    }
    parser = configparser.ConfigParser()
    try:
        parser.read(mailer.CONFIG_FILE, encoding="utf-8")
        section = parser["settings"] if parser.has_section("settings") else {}
        for key in defaults:
            try:
                defaults[key] = int(section.get(key, defaults[key]))
            except (ValueError, TypeError):
                pass
    except (OSError, configparser.Error):
        pass
    env = {}
    try:
        for raw in mailer.ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, separator, value = line.partition("=")
            if separator:
                env[key.strip()] = value.strip().strip("\"'")
    except OSError:
        pass
    sender_email = env.get("sender_email", "").strip().lower()
    password = env.get("app_password", "").replace(" ", "").strip()
    return {
        "sender_email": sender_email,
        **defaults,
        "credential_configured": bool(mailer.EMAIL_RE.fullmatch(sender_email) and password),
    }


def read_template(path, default=""):
    return path.read_text(encoding="utf-8") if path.exists() else default


def normalize_delivery_status(value):
    return re.sub(r"[\s-]+", "_", str(value or "").strip().lower())


def is_conversation_eligible(row):
    return (
        row.get("status") == "sent"
        and bool(row.get("gmail_thread_id"))
        and normalize_delivery_status(row.get("delivery_status", ""))
        not in ("bounced", "delayed", "needs_review")
    )


def load_cached_gmail_thread(cfg, thread_id, root_message_id="", force=False):
    key = (str(cfg.get("email", "")).strip().lower(), str(thread_id))
    now = time.monotonic()
    if not force:
        with thread_cache_lock:
            cached = thread_cache.get(key)
            if cached:
                expires_at, messages = cached
                if expires_at > now:
                    thread_cache.move_to_end(key)
                    return copy.deepcopy(messages)
                del thread_cache[key]

    messages = mailer.load_gmail_thread(cfg, thread_id, root_message_id)
    if not isinstance(messages, list):
        raise ValueError("Gmail returned an invalid conversation response.")
    total_chars = sum(
        len(str(message.get("body", "")))
        for message in messages
        if isinstance(message, dict)
    )
    if total_chars <= THREAD_CACHE_MAX_CHARS:
        with thread_cache_lock:
            thread_cache[key] = (
                time.monotonic() + THREAD_CACHE_TTL_SECONDS,
                copy.deepcopy(messages),
            )
            thread_cache.move_to_end(key)
            while len(thread_cache) > THREAD_CACHE_MAX_ENTRIES:
                thread_cache.popitem(last=False)
    return messages


def invalidate_gmail_thread_cache(email, thread_id):
    key = (str(email).strip().lower(), str(thread_id))
    with thread_cache_lock:
        thread_cache.pop(key, None)


def gmail_connection_state(settings, monitor):
    checked_at = monitor.get("last_successful_at", "")
    if not settings["credential_configured"]:
        status = "not_configured"
    elif monitor["state"] == "checking" or (
        monitor["state"] == "starting" and not checked_at
    ):
        status = "checking"
    elif monitor["state"] == "error":
        status = "disconnected"
    elif checked_at:
        try:
            age = datetime.now() - datetime.fromisoformat(checked_at)
        except ValueError:
            status = "stale"
        else:
            status = (
                "connected"
                if timedelta(0) <= age <= timedelta(
                    seconds=monitor["interval_seconds"] * 2
                )
                else "stale"
            )
    else:
        status = "checking"
    return {
        "status": status,
        "checked_at": checked_at,
        "detail": monitor.get("error", "") if status == "disconnected" else "",
    }


def dashboard_state():
    fields, raw_contacts = read_raw_contacts()
    contacts = mailer.load_contacts()
    tracker = mailer.load_tracker_rows()
    mailer.seed_legacy_thread_replies(tracker)
    reply_state = mailer.load_thread_reply_state().get("conversations", {})
    suppressed = sorted(mailer.load_suppressed())
    suppressed_set = set(suppressed)
    archived_threads = read_archived_threads()
    with thread_cache_lock:
        thread_activity_snapshot = dict(thread_activity)
    for row in tracker:
        row["suppressed"] = row["email"] in suppressed_set
    settings = read_settings()
    _, sent_today = mailer.load_log()
    campaign_rows = [
        row for row in tracker if row.get("send_type") != "one_off"
    ]
    campaign_done = {
        row["email"] for row in campaign_rows
        if row["status"] in ("sent", "refused", "unknown")
    }
    contact_map = {contact["email"]: contact for contact in contacts}
    conversations = [
        {
            "email": row["email"],
            "name": contact_map.get(row["email"], {}).get("name") or "Unknown",
            "company": contact_map.get(row["email"], {}).get("company", ""),
            "subject": row["subject"] or "No subject",
            "timestamp": row["timestamp"],
            "thread_id": row["gmail_thread_id"],
            "delivery_status": normalize_delivery_status(row.get("delivery_status", "")),
            "followup_status": (
                "replied"
                if reply_state.get(row["email"], {}).get("latest_message_at")
                else row.get("followup_status", "")
            ),
            "last_reply_at": (
                reply_state.get(row["email"], {}).get("latest_message_at", "")
                or row.get("followup_timestamp", "")
            ),
            "last_message_preview": reply_state.get(row["email"], {}).get(
                "latest_message_preview", ""
            ),
            "unread_count": len(
                reply_state.get(row["email"], {}).get("unread_message_ids", [])
            ),
            "last_activity_at": max(
                (
                    row.get("followup_timestamp", ""),
                    reply_state.get(row["email"], {}).get("latest_message_at", ""),
                    row["timestamp"],
                    thread_activity_snapshot.get(row["email"], ""),
                ),
                key=activity_timestamp,
            ),
            "archived": row["email"] in archived_threads,
            "can_reply": (
                row["email"] not in suppressed_set
                and normalize_delivery_status(row.get("delivery_status", ""))
                not in ("bounced", "delayed", "needs_review")
            ),
        }
        for row in reversed(tracker)
        if is_conversation_eligible(row)
    ]
    conversations.sort(key=lambda item: activity_timestamp(item["last_activity_at"]), reverse=True)
    queued = [
        contact for contact in contacts
        if contact["email"] not in campaign_done
        and contact["email"] not in suppressed
    ]
    now = datetime.now()
    due_followups = []
    campaign_due_followups = []
    daily = {}
    weekly = {}
    statuses = {"sent": 0, "refused": 0, "error": 0, "unknown": 0}
    send_type_counts = {"campaign": 0, "one_off": 0}
    delivery_statuses = {}
    delivery_issues = []
    followup_statuses = {}
    for row in tracker:
        if row["status"] in statuses:
            statuses[row["status"]] += 1
        delivery_status = normalize_delivery_status(row.get("delivery_status", ""))
        row["delivery_status"] = delivery_status
        if delivery_status:
            delivery_statuses[delivery_status] = (
                delivery_statuses.get(delivery_status, 0) + 1
            )
        if delivery_status in ("bounced", "delayed", "needs_review"):
            delivery_issues.append(row)
        if row["followup_status"]:
            followup_statuses[row["followup_status"]] = (
                followup_statuses.get(row["followup_status"], 0) + 1
            )
        if row["status"] != "sent":
            continue
        send_type = row.get("send_type") or "campaign"
        if send_type in send_type_counts:
            send_type_counts[send_type] += 1
        try:
            sent_at = datetime.fromisoformat(row["timestamp"])
        except ValueError:
            continue
        day = sent_at.date().isoformat()
        week_start = (sent_at.date() - timedelta(days=sent_at.weekday())).isoformat()
        daily[day] = daily.get(day, 0) + 1
        weekly[week_start] = weekly.get(week_start, 0) + 1
        try:
            followup_count = int(row["followup_count"] or 0)
        except ValueError:
            followup_count = settings["max_followup_weeks"]
        if (
            row["followup_status"] not in ("", "followup_sent")
            or followup_count >= settings["max_followup_weeks"]
            or row["delivery_status"] in ("bounced", "delayed", "needs_review")
            or row.get("followup_opt_in", "").lower() == "no"
            or row["email"] in suppressed
            or not row["gmail_thread_id"]
        ):
            continue
        followup_interval = timedelta(days=settings["followup_interval_days"])
        next_due = sent_at + followup_interval * (followup_count + 1)
        if row["followup_timestamp"]:
            try:
                previous = datetime.fromisoformat(row["followup_timestamp"])
            except ValueError:
                continue
            next_due = max(next_due, previous + followup_interval)
        if now >= next_due:
            due_followups.append(row)
            if row.get("send_type") != "one_off":
                campaign_due_followups.append(row)

    notifications = []
    for row in tracker:
        email = row["email"]
        if row["status"] in ("refused", "error", "unknown"):
            send_title = {
                "refused": "Email rejected by server",
                "error": "Email send failed",
                "unknown": "Send outcome needs review",
            }[row["status"]]
            send_message = {
                "refused": f"Message rejected for {email}. Review the tracker.",
                "error": f"Send failed for {email}. Review the tracker.",
                "unknown": f"Check Gmail Sent before retrying for {email}.",
            }[row["status"]]
            notifications.append({
                "id": f"send:{email}:{row['status']}:{row['timestamp']}",
                "type": "send",
                "title": send_title,
                "message": send_message,
                "email": email,
                "created_at": row["timestamp"],
            })
        if row["followup_status"] == "replied":
            notifications.append({
                "id": f"reply:{email}:{row['followup_timestamp'] or row['timestamp']}",
                "type": "reply",
                "title": "Reply detected",
                "message": f"A reply was found in the saved Gmail thread for {email}.",
                "email": email,
                "created_at": row["followup_timestamp"] or row["timestamp"],
            })
        if row["delivery_status"] in ("bounced", "delayed", "needs_review"):
            try:
                delivery_detail = json.loads(row.get("delivery_detail") or "{}")
            except (TypeError, json.JSONDecodeError):
                delivery_detail = {}
            if not isinstance(delivery_detail, dict):
                delivery_detail = {}
            notice_identity = str(delivery_detail.get("message_id") or "").strip()
            if not notice_identity:
                stable_parts = (
                    row["delivery_status"],
                    delivery_detail.get("subject", ""),
                    delivery_detail.get("status_code", ""),
                    delivery_detail.get("detail", ""),
                )
                notice_identity = hashlib.sha256(
                    json.dumps(stable_parts, ensure_ascii=False).encode("utf-8")
                ).hexdigest()[:20]
            notifications.append({
                "id": (
                    f"delivery:{email}:{row['delivery_status']}:"
                    f"{notice_identity or row['timestamp']}"
                ),
                "type": "delivery",
                "title": f"Delivery {row['delivery_status'].replace('_', ' ')}",
                "message": f"Review delivery status for {email} in the tracker.",
                "email": email,
                "created_at": row["delivery_updated_at"] or row["timestamp"],
            })
    for row in due_followups:
        notifications.append({
            "id": f"followup:{row['email']}:{row['timestamp']}:{row['followup_count']}",
            "type": "followup",
            "title": "Follow-up due",
            "message": f"Check for a reply before following up with {row['email']}.",
            "email": row["email"],
            "created_at": row["timestamp"],
        })
    notifications.sort(key=lambda item: item["created_at"], reverse=True)
    read_notification_ids = load_read_notification_ids()
    for notification in notifications:
        notification["read"] = notification["id"] in read_notification_ids

    company_counts = {}
    role_counts = {}
    for contact in contacts:
        if contact["email"] in campaign_done:
            continue
        company = contact.get("company", "").strip() or "Not specified"
        role = contact.get("title", "").strip().split()
        role_group = " ".join(role[:2]) if role else "Not specified"
        company_counts[company] = company_counts.get(company, 0) + 1
        role_counts[role_group] = role_counts.get(role_group, 0) + 1
    last_success = next(
        (row for row in reversed(campaign_rows) if row["status"] == "sent"), {}
    )
    with jobs_lock:
        active = jobs.get(active_job_id) if active_job_id else None
        if active and active.get("profile_id") != active_profile_id:
            active = None
        job_snapshot = copy.deepcopy(active) if active else None
        active_jobs = [
            copy.deepcopy(job) for job in jobs.values()
            if job["state"] == "running" and job.get("profile_id") == active_profile_id
        ]
        monitor_snapshot = dict(delivery_monitor)
        reply_monitor_snapshot = dict(reply_monitor)
    with event_clients_lock:
        state_revision = event_revision
    resume_files = sorted(
        file.name for file in mailer.RESUME_DIR.glob("*")
        if file.suffix.lower() in mailer.RESUME_EXTS
    )
    selected_resume = ""
    if mailer.RESUME_SELECTION_FILE.exists():
        selected_resume = mailer.RESUME_SELECTION_FILE.read_text(
            encoding="utf-8"
        ).strip()
    if selected_resume not in resume_files:
        selected_resume = resume_files[0] if resume_files else ""
    return {
        "summary": {
            "total_contacts": len(raw_contacts),
            "valid_contacts": len(contacts),
            "already_emailed": len(campaign_done),
            "remaining": len(queued),
            "sent_today": sent_today,
            "daily_limit": settings["emails_per_day"],
            "next_contact": queued[0] if queued else None,
            "last_success": last_success,
            "due_followups": len(campaign_due_followups),
            "suppressed_count": len(suppressed),
            "delivery_issue_count": sum(
                row.get("send_type") != "one_off" for row in delivery_issues
            ),
            "unknown_send_count": sum(
                row["status"] == "unknown" for row in campaign_rows
            ),
            "send_progress_percent": round(
                (len(campaign_done) / len(raw_contacts) * 100)
                if raw_contacts else 0, 1
            ),
        },
        "analytics": {
            "send_statuses": statuses,
            "send_type_counts": send_type_counts,
            "delivery_statuses": delivery_statuses,
            "delivery_issues": delivery_issues[:50],
            "followup_statuses": followup_statuses,
            "daily_sends": sorted(daily.items()),
            "weekly_sends": sorted(weekly.items()),
            "due_followups": campaign_due_followups[:10],
            "top_companies": sorted(
                company_counts.items(), key=lambda item: (-item[1], item[0].lower())
            )[:8],
            "top_roles": sorted(
                role_counts.items(), key=lambda item: (-item[1], item[0].lower())
            )[:8],
            "reply_count": followup_statuses.get("replied", 0),
        },
        "contacts": raw_contacts,
        "tracker": tracker,
        "conversations": conversations,
        "settings": settings,
        "template": read_template(mailer.TEMPLATE_FILE),
        "followup_template": read_template(mailer.FOLLOWUP_TEMPLATE_FILE),
        "resume_files": resume_files,
        "selected_resume": selected_resume,
        "active_job": job_snapshot,
        "active_jobs": active_jobs,
        "revision": state_revision,
        "delivery_monitor": monitor_snapshot,
        "reply_monitor": reply_monitor_snapshot,
        "gmail_connection": gmail_connection_state(settings, monitor_snapshot),
        "notifications": notifications,
        "user": {
            "id": active_profile_id,
            "email": (active_profile or {}).get("email", ""),
            "display_name": (active_profile or {}).get("display_name", ""),
        },
        "setup": {
            "gmail_connected": settings["credential_configured"],
            "contacts_added": len(raw_contacts) > 0,
            "template_ready": bool(read_template(mailer.TEMPLATE_FILE).strip())
            and "[Write your outreach message here.]" not in read_template(mailer.TEMPLATE_FILE),
            "resume_added": bool(resume_files),
        },
    }


def write_env(sender_email, app_password):
    if not mailer.ENV_FILE.exists():
        lines = []
    else:
        lines = mailer.ENV_FILE.read_text(encoding="utf-8-sig").splitlines()
    values = {"sender_email": sender_email, "app_password": app_password}
    seen = set()
    output = []
    for line in lines:
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match.group(1) in values:
            key = match.group(1)
            output.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            output.append(line)
    for key, value in values.items():
        if key not in seen:
            output.append(f"{key}={value}")
    temporary = mailer.ENV_FILE.with_suffix(".env.tmp")
    temporary.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    temporary.replace(mailer.ENV_FILE)


def update_ini_settings(data):
    config = mailer.CONFIG_FILE.read_text(encoding="utf-8")
    values = {
        "emails_per_day": str(data["emails_per_day"]),
        "gap_min_seconds": str(data["gap_min_seconds"]),
        "gap_max_seconds": str(data["gap_max_seconds"]),
        "max_followup_weeks": str(data["max_followup_weeks"]),
        "followup_interval_days": str(data["followup_interval_days"]),
    }
    for key, value in values.items():
        pattern = rf"(?m)^(\s*{re.escape(key)}\s*=\s*).*$"
        config, count = re.subn(pattern, rf"\g<1>{value}", config, count=1)
        if count != 1:
            if key != "followup_interval_days" or count > 1:
                raise ValueError(f"Could not find {key} in config.ini.")
            config = f"{config.rstrip()}\n{key} = {value}\n"
    temporary = mailer.CONFIG_FILE.with_suffix(".ini.tmp")
    temporary.write_text(config, encoding="utf-8")
    temporary.replace(mailer.CONFIG_FILE)


def run_action(action, test_email=""):
    commands = {
        "preview": ["--dry-run"],
        "send": [],
        "followup_preview": ["--follow-ups", "--dry-run"],
        "followup_send": ["--follow-ups"],
        "sync_threads": ["--sync-threads"],
    }
    if action == "test":
        if not mailer.EMAIL_RE.fullmatch(test_email):
            raise ValueError("Enter a valid test recipient email address.")
        command = ["--test", test_email]
    elif action in commands:
        command = commands[action]
    else:
        raise ValueError("Unknown dashboard action.")

    if action in ("send", "followup_send"):
        command = [*command, "--dashboard-control"]
    return [sys.executable, "-u", str(SCRIPT_FILE), *command]


def run_job(job_id, command=None, operation=None):
    global active_job_id, active_delivery_job_id
    process = None
    try:
        if operation:
            operation_result = operation()
            if isinstance(operation_result, dict):
                output = operation_result.get("output", [])
                result = operation_result.get("result")
            else:
                output = operation_result
                result = None
            if not isinstance(output, list) or any(not isinstance(line, str) for line in output):
                raise RuntimeError("The background task returned an invalid result.")
            with jobs_lock:
                jobs[job_id]["output"].extend(output)
                if result is not None:
                    jobs[job_id]["result"] = result
                if jobs[job_id]["action"] == "reply_auto_check":
                    detected = re.search(
                        r"Replies detected\s*:\s*([\d,]+)",
                        "\n".join(output),
                        re.IGNORECASE,
                    )
                    jobs[job_id]["replies_detected"] = (
                        int(detected.group(1).replace(",", "")) if detected else 0
                    )
                jobs[job_id]["state"] = "completed"
                jobs[job_id]["return_code"] = 0
        else:
            with jobs_lock:
                profile_dir = jobs[job_id].get("profile_dir", str(BASE))
            process = subprocess.Popen(
                command,
                cwd=BASE,
                env={**os.environ, "COLD_MAILER_ACCOUNT_DIR": profile_dir},
                stdin=(
                    subprocess.PIPE
                    if jobs[job_id]["action"] in ("send", "followup_send")
                    else subprocess.DEVNULL
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            with jobs_lock:
                jobs[job_id]["pid"] = process.pid
                if jobs[job_id]["action"] in ("send", "followup_send"):
                    campaign_processes[job_id] = process
            with campaign_process_lock:
                with jobs_lock:
                    pause_state = jobs[job_id].get("pause_state", "running")
                queued_command = (
                    "PAUSE" if pause_state in ("pausing", "paused")
                    else "RESUME" if pause_state == "resuming" else ""
                )
                if queued_command:
                    try:
                        process.stdin.write(f"{queued_command}\n")
                        process.stdin.flush()
                    except (BrokenPipeError, OSError, AttributeError, ValueError):
                        pass
            publish_job(job_id)
            for line in process.stdout:
                cleaned = line.replace("\r", "").rstrip()
                if cleaned:
                    if cleaned in (
                        "__CAMPAIGN_PAUSED__",
                        "__CAMPAIGN_RESUMED__",
                        "__CAMPAIGN_RESUME_ACCEPTED__",
                    ):
                        with jobs_lock:
                            jobs[job_id]["pause_state"] = (
                                "paused" if cleaned == "__CAMPAIGN_PAUSED__"
                                else "running"
                            )
                            if cleaned == "__CAMPAIGN_PAUSED__":
                                jobs[job_id]["timer"] = None
                        publish_job(job_id, compact=True)
                        continue
                    timer = re.fullmatch(
                        r"\s*Next email in\s+(\d+):(\d{2})", cleaned
                    )
                    if timer:
                        minutes, seconds = map(int, timer.groups())
                        with jobs_lock:
                            jobs[job_id]["timer"] = {
                                "remaining_seconds": minutes * 60 + seconds,
                                "updated_at": datetime.now().timestamp(),
                            }
                        publish_job(job_id, compact=True)
                        continue
                    with jobs_lock:
                        tracks_progress = jobs[job_id]["action"] in ("send", "followup_send")
                        batch = re.search(r"Emails in this batch\s*:\s*([\d,]+)", cleaned, re.IGNORECASE) if jobs[job_id]["action"] == "send" else None
                        sent_today = re.search(r"Sent today\s*:\s*([\d,]+)\s*/\s*([\d,]+)", cleaned, re.IGNORECASE) if jobs[job_id]["action"] == "send" else None
                        if batch:
                            progress = jobs[job_id].get("progress") or {}
                            progress.update({
                                "current": 0,
                                "total": int(batch.group(1).replace(",", "")),
                                "sent": 0,
                                "failed": 0,
                                "unknown": 0,
                                "completed": 0,
                            })
                            jobs[job_id]["progress"] = progress
                        if sent_today:
                            progress = jobs[job_id].get("progress") or {}
                            jobs[job_id]["progress"] = progress
                            progress["daily_sent_before"] = int(sent_today.group(1).replace(",", ""))
                            progress["daily_limit"] = int(sent_today.group(2).replace(",", ""))
                        outcome = re.search(r"\[\s*(\d+)\s*/\s*(\d+)\s*\]\s*(SENT|REFUSED|ERROR|UNKNOWN|FAILED|CHECK)\b", cleaned, re.IGNORECASE) if tracks_progress else None
                        if outcome:
                            progress = jobs[job_id].get("progress") or {"current": 0, "total": int(outcome.group(2)), "sent": 0, "failed": 0, "unknown": 0, "completed": 0}
                            jobs[job_id]["progress"] = progress
                            progress["current"] = int(outcome.group(1))
                            progress["total"] = int(outcome.group(2))
                            category = outcome.group(3).lower()
                            progress["sent" if category == "sent" else "unknown" if category in ("unknown", "check") else "failed"] += 1
                            if category in ("sent", "refused", "unknown", "check"):
                                progress["completed"] = int(progress.get("completed", 0)) + 1
                        jobs[job_id]["output"].append(cleaned)
                        jobs[job_id]["output"] = jobs[job_id]["output"][-500:]
                    publish_job(job_id, compact=True)
            code = process.wait()
            with jobs_lock:
                jobs[job_id]["return_code"] = code
                jobs[job_id]["state"] = "completed" if code == 0 else "failed"
    except (OSError, ValueError, RuntimeError) as exc:
        with jobs_lock:
            jobs[job_id]["state"] = "failed"
            jobs[job_id]["output"].append(str(exc))
    except Exception as exc:
        with jobs_lock:
            jobs[job_id]["state"] = "failed"
            jobs[job_id]["output"].append(f"Dashboard task failed: {exc}")
    finally:
        if process is not None:
            with campaign_process_lock:
                process_stdin = getattr(process, "stdin", None)
                if process_stdin and not getattr(process_stdin, "closed", False):
                    try:
                        process_stdin.close()
                    except OSError:
                        pass
            with jobs_lock:
                campaign_processes.pop(job_id, None)
        reply_monitor_snapshot = None
        reply_state_changed = False
        reply_emails = []
        with jobs_lock:
            job = jobs[job_id]
            reply_emails = sorted({
                match.group(1).strip().lower()
                for line in job.get("output", [])
                if (match := re.fullmatch(r"Reply detected:\s*(\S+)", line.strip()))
                and mailer.EMAIL_RE.fullmatch(match.group(1).strip().lower())
            })
            reply_state_changed = bool(reply_emails)
            if job["action"] == "delivery_auto_check":
                delivery_monitor["state"] = (
                    "idle" if job["state"] == "completed" else "error"
                )
                delivery_monitor["last_completed_at"] = datetime.now().isoformat(
                    timespec="seconds"
                )
                if job["state"] == "completed":
                    delivery_monitor["last_successful_at"] = (
                        delivery_monitor["last_completed_at"]
                    )
                delivery_monitor["next_check_at"] = (
                    datetime.now() + timedelta(
                        seconds=DELIVERY_CHECK_INTERVAL_SECONDS
                    )
                ).isoformat(timespec="seconds")
                delivery_monitor["error"] = (
                    "" if job["state"] == "completed"
                    else "\n".join(job["output"][-3:])
                )
            if job["action"] == "reply_auto_check":
                now = datetime.now()
                reply_monitor.update({
                    "state": "idle" if job["state"] == "completed" else "error",
                    "last_completed_at": now.isoformat(timespec="seconds"),
                    "next_check_at": (
                        now + timedelta(seconds=REPLY_CHECK_INTERVAL_SECONDS)
                    ).isoformat(timespec="seconds"),
                    "error": "" if job["state"] == "completed"
                    else "\n".join(job["output"][-3:]),
                })
                if job["state"] == "completed":
                    reply_monitor["last_successful_at"] = reply_monitor["last_completed_at"]
                reply_monitor_snapshot = dict(reply_monitor)
                reply_state_changed = reply_state_changed or bool(
                    job.get("replies_detected", 0)
                )
            if active_job_id == job_id:
                active_job_id = None
            if active_delivery_job_id == job_id:
                active_delivery_job_id = None
            active_read_job_ids.discard(job_id)
        reply_job_finished.set()
        publish_job(job_id)
        if reply_monitor_snapshot:
            publish_event("monitor", {
                "name": "replies", "value": reply_monitor_snapshot,
            })
            if reply_state_changed:
                publish_event("state", {
                    "reason": "new-reply",
                    "job_id": job_id,
                    "emails": reply_emails,
                })
        else:
            publish_event("state", {
                "reason": "new-reply" if reply_state_changed else "job-finished",
                "job_id": job_id,
                "emails": reply_emails,
            })


def start_job(action, command=None, operation=None):
    with profile_lock:
        return _start_job_unlocked(action, command, operation)


def _start_job_unlocked(action, command=None, operation=None):
    global active_job_id, active_delivery_job_id
    if not active_profile_id:
        return None
    job_id = uuid.uuid4().hex
    with jobs_lock:
        # A follow-up preview checks Gmail and can write detected replies to
        # the tracker, so only the campaign dry-run is safe to overlap writers.
        read_only = action == "preview"
        if read_only:
            active_read_job_ids.add(job_id)
        elif action in ("delivery_auto_check", "reply_auto_check"):
            if active_delivery_job_id or active_job_id:
                return None
            active_delivery_job_id = job_id
        elif active_job_id or active_delivery_job_id:
            return None
        else:
            active_job_id = job_id
        jobs[job_id] = {
            "id": job_id,
            "action": action,
            "profile_id": active_profile_id,
            "profile_dir": str(mailer.PROFILE_DIR),
            "state": "running",
            "output": [],
            "timer": None,
            "return_code": None,
            "progress": None,
            "replies_detected": 0,
        }
        if action in ("send", "followup_send"):
            jobs[job_id]["pause_state"] = "running"
        while len(jobs) > 50:
            completed_id = next(
                (key for key, job in jobs.items()
                 if job["state"] != "running" and key not in (active_job_id, active_delivery_job_id)),
                None,
            )
            if completed_id is None:
                break
            del jobs[completed_id]
        if action == "delivery_auto_check":
            now = datetime.now()
            delivery_monitor.update({
                "state": "checking",
                "last_started_at": now.isoformat(timespec="seconds"),
                "next_check_at": (
                    now + timedelta(seconds=DELIVERY_CHECK_INTERVAL_SECONDS)
                ).isoformat(timespec="seconds"),
                "error": "",
            })
        elif action == "reply_auto_check":
            now = datetime.now()
            reply_monitor.update({
                "state": "checking",
                "last_started_at": now.isoformat(timespec="seconds"),
                "next_check_at": (
                    now + timedelta(seconds=REPLY_CHECK_INTERVAL_SECONDS)
                ).isoformat(timespec="seconds"),
                "error": "",
            })
    worker = threading.Thread(
        target=run_job, args=(job_id, command, operation), daemon=True
    )
    publish_job(job_id)
    if action == "reply_auto_check":
        publish_event("monitor", {
            "name": "replies", "value": dict(reply_monitor),
        })
    else:
        publish_event("state", {"reason": "job-started", "job_id": job_id})
    worker.start()
    return job_id


def delivery_monitor_loop():
    while not delivery_monitor_stop.is_set():
        if not active_profile_id:
            delivery_check_requested.wait(5)
            delivery_check_requested.clear()
            continue
        command = [
            sys.executable, "-u", str(SCRIPT_FILE), "--check-deliveries",
            "--lookback-days", str(DELIVERY_LOOKBACK_DAYS),
        ]
        with jobs_lock:
            last_successful = delivery_monitor["last_successful_at"]
        if last_successful:
            mailbox_since = (
                datetime.fromisoformat(last_successful).date()
                - timedelta(days=1)
            ).isoformat()
            command.extend(["--mailbox-since", mailbox_since])
        if start_job("delivery_auto_check", command):
            delivery_check_requested.wait(DELIVERY_CHECK_INTERVAL_SECONDS)
            delivery_check_requested.clear()
        else:
            delivery_check_requested.wait(15)
            delivery_check_requested.clear()


def reply_monitor_loop():
    while not reply_monitor_stop.is_set():
        if not active_profile_id:
            reply_check_requested.wait(5)
            reply_check_requested.clear()
            continue
        try:
            configured = read_settings()["credential_configured"]
        except (OSError, ValueError, configparser.Error) as exc:
            with jobs_lock:
                reply_monitor.update({
                    "state": "error",
                    "error": str(exc),
                    "next_check_at": (
                        datetime.now() + timedelta(seconds=REPLY_CHECK_INTERVAL_SECONDS)
                    ).isoformat(timespec="seconds"),
                })
                snapshot = dict(reply_monitor)
            publish_event("monitor", {"name": "replies", "value": snapshot})
            reply_check_requested.wait(REPLY_CHECK_INTERVAL_SECONDS)
            reply_check_requested.clear()
            continue
        if not configured:
            with jobs_lock:
                reply_monitor.update({
                    "state": "not_configured",
                    "error": "",
                    "next_check_at": (
                        datetime.now() + timedelta(seconds=REPLY_CHECK_INTERVAL_SECONDS)
                    ).isoformat(timespec="seconds"),
                })
                snapshot = dict(reply_monitor)
            publish_event("monitor", {"name": "replies", "value": snapshot})
            reply_check_requested.wait(REPLY_CHECK_INTERVAL_SECONDS)
            reply_check_requested.clear()
            continue
        operation = lambda: mailer.check_new_thread_replies(mailer.load_config())
        requested = reply_check_requested.is_set()
        if requested:
            reply_check_requested.clear()
        if start_job("reply_auto_check", operation=operation):
            reply_check_requested.wait(REPLY_CHECK_INTERVAL_SECONDS)
        else:
            if requested:
                # Keep the mailbox event queued while another job owns the
                # tracker, then retry as soon as that job releases its slot.
                reply_check_requested.set()
                with jobs_lock:
                    queued_changed = reply_monitor.get("state") != "queued"
                    reply_monitor.update({"state": "queued", "error": ""})
                    queued_snapshot = dict(reply_monitor)
                if queued_changed:
                    publish_event("monitor", {
                        "name": "replies", "value": queued_snapshot,
                    })
            reply_job_finished.wait(10)
            reply_job_finished.clear()


def set_reply_watch_status(state, error=""):
    with jobs_lock:
        reply_monitor.update({"push_state": state, "push_error": error})
        snapshot = dict(reply_monitor)
    publish_event("monitor", {"name": "replies", "value": snapshot})


def gmail_idle_watch_loop():
    while not reply_monitor_stop.is_set():
        try:
            profile_changed_event.clear()
            if not active_profile_id:
                set_reply_watch_status("not_configured")
                profile_changed_event.wait(5)
                continue
            if not read_settings()["credential_configured"]:
                set_reply_watch_status("not_configured")
                profile_changed_event.wait(REPLY_CHECK_INTERVAL_SECONDS)
                continue
            cfg = mailer.load_config()

            def connected():
                set_reply_watch_status("live")

            set_reply_watch_status("connecting")
            changed = mailer.wait_for_gmail_mailbox_update(
                cfg,
                reply_monitor_stop,
                duration=20 * 60,
                on_connected=connected,
                interrupt_event=profile_changed_event,
            )
            if changed:
                with jobs_lock:
                    reply_monitor["push_last_event_at"] = datetime.now().isoformat(
                        timespec="seconds"
                    )
                    snapshot = dict(reply_monitor)
                publish_event("monitor", {"name": "replies", "value": snapshot})
                reply_check_requested.set()
        except Exception as exc:
            if reply_monitor_stop.is_set():
                break
            set_reply_watch_status("reconnecting", str(exc))
            reply_monitor_stop.wait(REPLY_WATCH_RECONNECT_SECONDS)



class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # Browsers routinely close keep-alive requests while this local server
        # is waiting for the next request. Treat those socket cancellations as
        # disconnects; retain the standard traceback for other failures.
        error = sys.exc_info()[1]
        if isinstance(
            error,
            (BrokenPipeError, ConnectionAbortedError, ConnectionResetError),
        ):
            return
        super().handle_error(request, client_address)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "ColdMailerDashboard/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def send_json(self, status, data, cookie=None, clear_cookie=False):
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header(
                "Set-Cookie",
                f"{AUTH_COOKIE_NAME}={cookie}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_LIFETIME_SECONDS}",
            )
        elif clear_cookie:
            self.send_header(
                "Set-Cookie",
                f"{AUTH_COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0",
            )
        self.end_headers()
        self.wfile.write(payload)
        if self.command == "POST" and status < 300:
            path = urlparse(self.path).path
            if path in ("/api/one-off/drafts", "/api/one-off/draft"):
                publish_event("drafts", {"reason": "draft-update"})
            elif path not in (
                "/api/one-off/preview", "/api/action",
                "/api/one-off/send", "/api/tracker/reply",
                "/api/contacts/import/preview", "/api/contacts/import/table",
                "/api/contacts/import/inspect", "/api/contacts/import/discard",
            ):
                publish_event("state", {"reason": "api-update"})

    def handle_event_stream(self, account_id, session_token):
        client = queue.Queue(maxsize=40)
        with event_clients_lock:
            event_clients.add(client)
            revision = event_revision
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            ready = json.dumps({"revision": revision})
            self.wfile.write(f"event: ready\ndata: {ready}\n\n".encode("utf-8"))
            self.wfile.flush()
            while True:
                session = auth_store.resolve_session(session_token)
                if (
                    session is None
                    or session["user"]["id"] != account_id
                    or active_profile_id != account_id
                ):
                    break
                try:
                    event = client.get(timeout=20)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                data = json.dumps(event, ensure_ascii=False)
                self.wfile.write(
                    f"event: {event['type']}\ndata: {data}\n\n".encode("utf-8")
                )
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            with event_clients_lock:
                event_clients.discard(client)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/api/events", "/api/tracker/thread"):
            return self._do_GET()
        with profile_lock:
            return self._do_GET()

    def check_local_request(self, require_origin=False):
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            self.send_json(403, {"error": "Dashboard accepts local connections only."})
            return False
        host = self.headers.get("Host", "")
        if not re.fullmatch(r"(localhost|127\.0\.0\.1|\[::1\]):\d+", host):
            self.send_json(403, {"error": "Invalid dashboard host."})
            return False
        origin = self.headers.get("Origin")
        if require_origin and not origin:
            self.send_json(403, {"error": "This request must come from the local dashboard."})
            return False
        if require_origin and origin:
            parsed = urlparse(origin)
            if (
                parsed.scheme != "http"
                or parsed.hostname not in ("localhost", "127.0.0.1", "::1")
                or parsed.port != self.server.server_port
            ):
                self.send_json(403, {"error": "Cross-origin requests are not allowed."})
                return False
        return True

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid request length.") from None
        if length < 1 or length > MAX_JSON_BYTES:
            raise ValueError("Request body must be between 1 byte and 12 MB.")
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("Request body must be valid JSON.") from None

    def session_token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            morsel = cookie.get(AUTH_COOKIE_NAME)
            return morsel.value if morsel else ""
        except (ValueError, TypeError):
            return ""

    def require_authenticated(self, require_csrf=False):
        token = self.session_token()
        session = auth_store.resolve_session(token)
        if session is None:
            self.send_json(401, {"error": "Sign in to continue."})
            return False
        user = session["user"]
        with profile_lock:
            if not active_profile_id:
                activate_account(user, copy_legacy=(auth_store.user_count() == 1))
            elif active_profile_id != user["id"]:
                self.send_json(401, {"error": "Your local account session has ended. Sign in again."})
                return False
        if require_csrf:
            supplied = self.headers.get("X-CSRF-Token", "")
            if not supplied or not hmac.compare_digest(supplied, session["csrf_token"]):
                self.send_json(403, {"error": "Refresh this page before making changes."})
                return False
        self.auth_user = user
        self.auth_session = session
        self.auth_token = token
        return True

    def auth_status(self):
        token = self.session_token()
        session = auth_store.resolve_session(token)
        if session:
            user = session["user"]
            if not active_profile_id:
                activate_account(user, copy_legacy=(auth_store.user_count() == 1))
            if active_profile_id == user["id"]:
                self.send_json(200, {
                    "authenticated": True,
                    "user": user,
                    "csrf_token": session["csrf_token"],
                    "expires_at": session["expires_at"],
                })
                return
        self.send_json(200, {
            "authenticated": False,
            "setup_required": auth_store.user_count() == 0,
        })

    def active_mail_operation(self):
        with jobs_lock:
            return bool(active_job_id or active_delivery_job_id or active_read_job_ids)

    def auth_attempt_allowed(self, email):
        key = f"{self.client_address[0]}:{email.casefold()}"
        now = time.monotonic()
        with auth_failures_lock:
            recent = [stamp for stamp in auth_failures.get(key, [])
                      if now - stamp < AUTH_FAILURE_WINDOW_SECONDS]
            auth_failures[key] = recent
            if len(recent) >= AUTH_FAILURE_LIMIT:
                return False, AUTH_FAILURE_WINDOW_SECONDS - (now - recent[0])
        return True, 0

    def record_auth_failure(self, email, failed=True):
        key = f"{self.client_address[0]}:{email.casefold()}"
        with auth_failures_lock:
            if failed:
                auth_failures.setdefault(key, []).append(time.monotonic())
            else:
                auth_failures.pop(key, None)

    def handle_auth_register(self, data):
        first_account = auth_store.user_count() == 0
        if not first_account:
            self.send_json(409, {
                "error": "This local installation already has an account. Sign in instead."
            })
            return
        if str(data.get("password", "")) != str(data.get("password_confirmation", "")):
            raise ValueError("Passwords do not match.")
        user = auth_store.register(
            data.get("email"), data.get("display_name"), data.get("password")
        )
        account_root = account_directory(user["id"])
        try:
            create_account_data(user["id"], copy_legacy=first_account)
        except Exception:
            auth_store.delete_user(user["id"])
            if account_root.exists() and account_root.parent == PROFILE_ROOT.resolve():
                shutil.rmtree(account_root, ignore_errors=True)
            raise
        with jobs_lock:
            busy = bool(active_job_id or active_delivery_job_id or active_read_job_ids)
        if busy and active_profile_id != user["id"]:
            auth_store.delete_user(user["id"])
            shutil.rmtree(account_root, ignore_errors=True)
            self.send_json(409, {"error": "Wait for the current mail task to finish, then create your account."})
            return
        auth_store.revoke_all_sessions()
        session_token, csrf_token = auth_store.create_session(user["id"])
        activate_account(user)
        self.send_json(201, {
            "authenticated": True, "user": user, "csrf_token": csrf_token,
        }, cookie=session_token)

    def handle_auth_login(self, data):
        email = str(data.get("email", "")).strip().lower()
        allowed, retry_after = self.auth_attempt_allowed(email)
        if not allowed:
            self.send_json(429, {
                "error": f"Too many sign-in attempts. Try again in {max(1, int(retry_after))} seconds."
            })
            return
        user = auth_store.authenticate(email, data.get("password", ""))
        if user is None:
            self.record_auth_failure(email)
            self.send_json(401, {"error": "Email or password is incorrect."})
            return
        if active_profile_id and active_profile_id != user["id"] and self.active_mail_operation():
            self.send_json(409, {"error": "Wait for the current mail task to finish before switching accounts."})
            return
        self.record_auth_failure(email, failed=False)
        session_token, csrf_token = auth_store.create_session(user["id"])
        activate_account(user)
        self.send_json(200, {
            "authenticated": True, "user": user, "csrf_token": csrf_token,
        }, cookie=session_token)

    def handle_auth_logout(self):
        if self.active_mail_operation():
            self.send_json(409, {"error": "Wait for the current mail task to finish before signing out."})
            return
        auth_store.revoke_all_sessions()
        deactivate_account()
        self.send_json(200, {"ok": True}, clear_cookie=True)

    def _do_GET(self):
        if not self.check_local_request():
            return
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        if path == "/api/auth/status":
            self.auth_status()
            return
        if path == "/api/events":
            if self.require_authenticated():
                self.handle_event_stream(self.auth_user["id"], self.auth_token)
            return
        if path != "/" and not self.require_authenticated():
            return
        if path == "/":
            try:
                payload = HTML_FILE.read_bytes()
            except OSError as exc:
                self.send_json(500, {"error": f"Could not load dashboard page: {exc}"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
        elif path == "/api/state":
            try:
                self.send_json(200, dashboard_state())
            except (OSError, ValueError, RuntimeError, SystemExit) as exc:
                self.send_json(500, {"error": str(exc)})
            except Exception as exc:
                print(f"Dashboard state error: {type(exc).__name__}: {exc}", file=sys.stderr)
                self.send_json(500, {"error": "Could not load account data. Check the dashboard terminal for details."})
        elif path == "/api/contacts/export":
            try:
                fields, rows = read_raw_contacts()
                payload = contact_export_bytes(fields, rows)
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="contacts-{datetime.now():%Y%m%d}.csv"',
                )
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
            except (OSError, csv.Error, profile_store.ProfileStoreError) as exc:
                self.send_json(500, {"error": f"Could not export contacts: {exc}"})
        elif path == "/api/one-off/drafts":
            try:
                self.send_json(200, {
                    "drafts": [{
                        "draft_id": draft["draft_id"],
                        "email": draft["email"],
                        "subject": draft["subject"],
                        "updated_at": draft["updated_at"],
                    } for draft in reversed(read_one_off_drafts())]
                })
            except (OSError, ValueError, json.JSONDecodeError, profile_store.ProfileStoreError) as exc:
                self.send_json(500, {"error": f"Could not load saved drafts: {exc}"})
        elif path == "/api/one-off/draft":
            try:
                draft_id = parse_qs(parsed_url.query).get("id", [""])[0]
                if draft_id:
                    draft = next(
                        (item for item in read_one_off_drafts()
                         if item["draft_id"] == draft_id),
                        None,
                    )
                else:
                    draft = read_one_off_draft()
                if draft_id and draft is None:
                    self.send_json(404, {"error": "Saved draft no longer exists."})
                else:
                    self.send_json(200, {"draft": draft})
            except (OSError, ValueError, json.JSONDecodeError, profile_store.ProfileStoreError) as exc:
                self.send_json(500, {"error": f"Could not load one-off draft: {exc}"})
        elif path == "/api/tracker/thread":
            try:
                query = parse_qs(parsed_url.query)
                email = query.get("email", [""])[0].strip().lower()
                force_refresh = query.get("refresh", [""])[0] == "1"
                self.handle_tracker_thread(email, force_refresh)
            except (
                imaplib.IMAP4.error, OSError, ValueError, RuntimeError, SystemExit
            ) as exc:
                self.send_json(502, {"error": f"Could not load Gmail conversation: {exc}"})
            except Exception as exc:
                print(
                    f"Gmail conversation error: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                self.send_json(502, {
                    "error": "Could not load Gmail conversation. Check the dashboard terminal for details."
                })
        elif path == "/api/conversations":
            try:
                state = dashboard_state()
                self.send_json(200, {"conversations": state["conversations"]})
            except (OSError, ValueError, RuntimeError, SystemExit) as exc:
                self.send_json(500, {"error": f"Could not load conversations: {exc}"})
        elif path.startswith("/api/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            with jobs_lock:
                job = jobs.get(job_id)
                if job and job.get("profile_id") == active_profile_id:
                    self.send_json(200, dict(job))
                else:
                    self.send_json(404, {
                        "error": "Job not found."
                    })
        else:
            self.send_json(404, {"error": "Not found."})

    def do_POST(self):
        with profile_lock:
            return self._do_POST()

    def _do_POST(self):
        if not self.check_local_request(require_origin=True):
            return
        try:
            data = self.read_json()
            if not isinstance(data, dict):
                raise ValueError("Request body must be a JSON object.")
            path = urlparse(self.path).path
            if path == "/api/auth/register":
                self.handle_auth_register(data)
                return
            if path == "/api/auth/login":
                self.handle_auth_login(data)
                return
            if path == "/api/auth/logout":
                if self.require_authenticated(require_csrf=True):
                    self.handle_auth_logout()
                return
            if not self.require_authenticated(require_csrf=True):
                return
            if path == "/api/auth/password":
                current = str(data.get("current_password", ""))
                replacement = str(data.get("new_password", ""))
                if replacement != str(data.get("password_confirmation", "")):
                    raise ValueError("New passwords do not match.")
                if not auth_store.change_password(self.auth_user["id"], current, replacement):
                    self.send_json(400, {"error": "Current password is incorrect."})
                    return
                session_token, csrf_token = auth_store.create_session(self.auth_user["id"])
                self.send_json(200, {"ok": True, "csrf_token": csrf_token}, cookie=session_token)
            elif path == "/api/action":
                self.handle_action(data)
            elif path.startswith("/api/jobs/"):
                parts = path.strip("/").split("/")
                if len(parts) == 4 and parts[3] in ("pause", "resume"):
                    self.handle_campaign_control(parts[2], parts[3])
                else:
                    self.send_json(404, {"error": "Not found."})
            elif path == "/api/conversations/archive":
                email = str(data.get("email", "")).strip().lower()
                if not mailer.EMAIL_RE.fullmatch(email):
                    raise ValueError("Choose a valid conversation to archive.")
                archived = read_archived_threads()
                if data.get("archived") is True:
                    archived.add(email)
                elif data.get("archived") is False:
                    archived.discard(email)
                else:
                    raise ValueError("Archived must be true or false.")
                save_archived_threads(archived)
                self.send_json(200, {"ok": True, "archived": data["archived"]})
            elif path == "/api/conversations/read":
                email = str(data.get("email", "")).strip().lower()
                message_ids = data.get("message_ids")
                if not mailer.EMAIL_RE.fullmatch(email):
                    raise ValueError("Choose a valid conversation to mark as read.")
                if not isinstance(message_ids, list) or len(message_ids) > 500:
                    raise ValueError("Provide up to 500 visible message IDs to mark as read.")
                if any(
                    not isinstance(value, str) or not value or len(value) > 512
                    for value in message_ids
                ):
                    raise ValueError("Message IDs must be non-empty strings under 512 characters.")
                result = mailer.mark_thread_replies_read(email, message_ids)
                self.send_json(200, {"ok": True, **result})
            elif path == "/api/notifications/read":
                ids = data.get("ids")
                if not isinstance(ids, list) or len(ids) > 5000:
                    raise ValueError("Provide up to 5,000 notification IDs to mark as read.")
                if any(not isinstance(value, str) or not value or len(value) > 512 for value in ids):
                    raise ValueError("Notification IDs must be non-empty strings under 512 characters.")
                mark_notifications_read(ids)
                self.send_json(200, {"ok": True, "count": len(set(ids))})
                publish_event("state", {"reason": "notifications-read"})
            elif path == "/api/one-off/drafts":
                save_one_off_draft(data)
                self.send_json(200, {"ok": True})
            elif path == "/api/one-off/preview":
                self.handle_one_off_preview(data)
            elif path == "/api/one-off/send":
                self.handle_one_off_send(data)
            elif path == "/api/one-off/draft":
                save_one_off_draft(data)
                self.send_json(200, {"ok": True})
            elif path == "/api/tracker/reply":
                self.handle_tracker_reply(data)
            elif path == "/api/followup/preference":
                self.handle_followup_preference(data)
            elif path == "/api/followup/preferences":
                self.handle_bulk_followup_preferences(data)
            elif path == "/api/contact/save":
                self.handle_contact_save(data)
            elif path == "/api/contacts/import/preview":
                self.handle_contact_import_preview(data)
            elif path == "/api/contacts/import/table":
                self.handle_contact_import_table(data)
            elif path == "/api/contacts/import/inspect":
                self.handle_contact_import_inspect(data)
            elif path == "/api/contacts/import/commit":
                self.handle_contact_import_commit(data)
            elif path == "/api/contacts/import/discard":
                discard_contact_import(str(data.get("token", "")))
                self.send_json(200, {"ok": True})
            elif path == "/api/contact/delete":
                self.handle_contact_delete(data)
            elif path == "/api/contacts/delete":
                self.handle_bulk_contact_delete(data)
            elif path == "/api/tracker/delete":
                self.handle_bulk_tracker_delete(data)
            elif path == "/api/suppression":
                self.handle_suppression(data)
            elif path == "/api/settings":
                self.handle_settings(data)
            elif path == "/api/templates":
                self.handle_templates(data)
            elif path == "/api/credentials":
                self.handle_credentials(data)
            elif path == "/api/resume":
                self.handle_resume(data)
            elif path == "/api/resume/select":
                self.handle_resume_select(data)
            else:
                self.send_json(404, {"error": "Not found."})
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
        except (OSError, csv.Error, configparser.Error, RuntimeError) as exc:
            self.send_json(500, {"error": f"Could not save dashboard change: {exc}"})
        except Exception as exc:
            print(f"Dashboard request error ({urlparse(self.path).path}): {type(exc).__name__}: {exc}", file=sys.stderr)
            self.send_json(500, {"error": "The request could not be completed. Check the dashboard terminal for details."})


    def handle_action(self, data):
        global active_job_id
        action = str(data.get("action", ""))
        needs_confirmation = action in (
            "send", "followup_send", "test"
        )
        if needs_confirmation and data.get("confirmation") != "CONFIRM":
            self.send_json(400, {"error": "Confirmation is required before sending email."})
            return
        command = run_action(action, str(data.get("email", "")).strip())
        job_id = start_job(action, command)
        if job_id is None:
            self.send_json(409, {"error": "Another mailer task is already running."})
            return
        self.send_json(202, {"job_id": job_id})

    def handle_campaign_control(self, job_id, command):
        process = None
        current = "running"
        desired = "running"
        control_error = None
        with campaign_process_lock:
            with jobs_lock:
                job = jobs.get(job_id)
                if job is None or job.get("profile_id") != active_profile_id:
                    self.send_json(404, {"error": "Campaign job not found."})
                    return
                if (
                    job.get("action") not in ("send", "followup_send")
                    or job.get("state") != "running"
                ):
                    self.send_json(409, {"error": "This campaign is no longer running."})
                    return
                current = job.get("pause_state", "running")
                if command == "pause" and current in ("pausing", "paused"):
                    self.send_json(200, {"job": dict(job)})
                    return
                if command == "resume" and current in ("running", "resuming"):
                    self.send_json(200, {"job": dict(job)})
                    return
                desired = "pausing" if command == "pause" else "resuming"
                job["pause_state"] = desired
                process = campaign_processes.get(job_id)
            if process is not None:
                try:
                    if process.poll() is not None or process.stdin is None:
                        raise RuntimeError("The campaign process has already stopped.")
                    process.stdin.write(
                        "PAUSE\n" if command == "pause" else "RESUME\n"
                    )
                    process.stdin.flush()
                except (BrokenPipeError, OSError, RuntimeError, ValueError) as exc:
                    control_error = exc
                    with jobs_lock:
                        latest = jobs.get(job_id)
                        if latest and latest.get("state") == "running":
                            latest["pause_state"] = current
        if control_error is not None:
            publish_job(job_id, compact=True)
            self.send_json(409, {"error": str(control_error)})
            return
        publish_job(job_id, compact=True)
        with jobs_lock:
            snapshot = dict(jobs.get(job_id, {}))
        self.send_json(202, {"job": snapshot})

    def handle_one_off_preview(self, data):
        contact, subject, body, html_body, followup_opt_in = validate_one_off(data)
        attachments = validate_one_off_attachments(data.get("attachments", []))
        rendered_subject = mailer.personalise(subject, contact)
        rendered_body = mailer.build_message_body(body, contact)
        rendered_html_body = (
            mailer.build_html_message_body(html_body, contact) if html_body else ""
        )
        if (
            not rendered_subject
            or len(rendered_subject) > 998
            or "\r" in rendered_subject
            or "\n" in rendered_subject
        ):
            raise ValueError(
                "The personalized subject must be one line and no longer than 998 characters."
            )
        tracked = next(
            (row for row in mailer.load_tracker_rows()
             if row["email"] == contact["email"]),
            None,
        )
        suppressed = contact["email"] in mailer.load_suppressed()
        reason = ""
        if suppressed:
            reason = "This address is suppressed and cannot be sent to."
        elif tracked:
            reason = (
                "This address already has tracker history. One-off sends are "
                "limited to untracked recipients to preserve the existing record."
            )
        try:
            attachment = mailer.find_resume()[0]
        except SystemExit as exc:
            attachment = ""
            reason = reason or str(exc)
        self.send_json(200, {
            "contact": contact,
            "subject": rendered_subject,
            "body": rendered_body,
            "html_body": rendered_html_body,
            "followup_opt_in": followup_opt_in,
            "attachment": attachment,
            "attachments": [item[0] for item in attachments],
            "sendable": not suppressed and tracked is None and bool(attachment),
            "reason": reason,
        })

    def handle_one_off_send(self, data):
        if data.get("confirmation") != "CONFIRM":
            self.send_json(400, {
                "error": "Confirmation is required before sending this email."
            })
            return
        draft_id = str(data.get("draft_id", "")).strip()
        if draft_id and not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", draft_id):
            raise ValueError("Draft ID is invalid.")
        contact, subject, body, html_body, followup_opt_in = validate_one_off(data)
        attachments = validate_one_off_attachments(data.get("attachments", []))
        email = contact["email"]
        if email in mailer.load_suppressed():
            raise ValueError("This address is suppressed and cannot be sent to.")
        if any(row["email"] == email for row in mailer.load_tracker_rows()):
            raise ValueError(
                "This address already has tracker history. One-off sends are "
                "limited to untracked recipients to preserve the existing record."
            )
        try:
            cfg = mailer.load_config()
        except SystemExit as exc:
            raise ValueError(str(exc)) from None
        def operation():
            save_one_off_contact(contact)
            try:
                result = mailer.send_one_off(
                    cfg, contact, subject, body, followup_opt_in, html_body,
                    attachments,
                )
            except (Exception, SystemExit) as exc:
                if not any(
                    row["email"] == email for row in mailer.load_tracker_rows()
                ):
                    mailer.write_log(
                        email, "error", str(exc)[:200], "one_off",
                        "yes" if followup_opt_in else "no",
                        json.dumps({
                            "subject": mailer.personalise(subject, contact),
                            "body": mailer.personalise(body, contact),
                            "template_name": "custom",
                        }),
                    )
                if isinstance(exc, SystemExit):
                    raise RuntimeError(str(exc)) from None
                raise
            status = result["status"].upper()
            draft_cleanup_warning = ""
            if status == "SENT" and draft_id:
                try:
                    save_one_off_draft({
                        "action": "clear", "draft_id": draft_id
                    })
                except (OSError, ValueError, json.JSONDecodeError, profile_store.ProfileStoreError) as exc:
                    draft_cleanup_warning = (
                        f"Message was sent, but the saved draft could not be "
                        f"removed: {exc}"
                    )
            lines = [f"One-off send {status}: {email}"]
            lines.append(f"Subject: {result['subject']}")
            if draft_cleanup_warning:
                lines.append(draft_cleanup_warning)
            if result.get("thread_id"):
                lines.append(f"Gmail thread: {result['thread_id']}")
            if result.get("thread_warning"):
                lines.append(
                    "Message was sent, but Gmail thread lookup failed: "
                    f"{result['thread_warning']}"
                )
            if attachments:
                lines.append(
                    "Additional attachments: "
                    + ", ".join(item[0] for item in attachments)
                )
            if followup_opt_in:
                lines.append(
                    "Automated follow-up: opted in (timing is configured in Settings; "
                    "replies are checked before sending)."
                )
            else:
                lines.append("Automated follow-up: not enabled.")
            if status in ("REFUSED", "ERROR", "UNKNOWN"):
                lines.append("The attempt is recorded in the tracker; review it before retrying.")
            return lines

        job_id = start_job("one_off_send", operation=operation)
        if job_id is None:
            self.send_json(409, {"error": "Another mailer task is already running."})
            return
        self.send_json(202, {"job_id": job_id})

    def handle_tracker_thread(self, email, force_refresh=False):
        if not mailer.EMAIL_RE.fullmatch(email):
            self.send_json(400, {"error": "Choose a valid tracked email address."})
            return
        # Capture account-bound data under the profile lock, then release it
        # before Gmail I/O so unrelated dashboard requests remain responsive.
        with profile_lock:
            request_profile_id = active_profile_id
            row = next(
                (entry for entry in mailer.load_tracker_rows()
                 if entry["email"] == email),
                None,
            )
            cfg = mailer.load_config()
            suppressed = email in mailer.load_suppressed()
        if row is None or row["status"] != "sent":
            self.send_json(
                404, {"error": "Conversation view is available only for sent messages."}
            )
            return
        thread_id = row["gmail_thread_id"]
        try:
            details = json.loads(row["detail"] or "{}")
        except json.JSONDecodeError:
            details = {}
        if not isinstance(details, dict):
            details = {}
        if not thread_id:
            message_id = row["gmail_message_id"] or details.get("message_id", "")
            if not message_id:
                self.send_json(
                    409,
                    {"error": "No Gmail message ID is saved. Sync Gmail threads and retry."},
                )
                return
            threads = mailer.find_gmail_thread_ids(cfg, [(email, {
                "message_id": message_id,
                "subject": row["subject"],
                "timestamp": row["timestamp"],
            })])
            if email not in threads:
                self.send_json(
                    404,
                    {"error": "Gmail has not exposed a conversation for this message yet."},
                )
                return
            thread_id = threads[email]["thread_id"]
            with profile_lock:
                if active_profile_id == request_profile_id:
                    mailer.update_message_details(email, {
                        "message_id": threads[email]["message_id"],
                        "subject": threads[email]["subject"],
                        "gmail_thread_id": thread_id,
                    })
        root_message_id = (
            row["gmail_message_id"] or details.get("message_id", "")
        )
        messages = load_cached_gmail_thread(
            cfg, thread_id, root_message_id, force=force_refresh
        )
        latest_activity = str(messages[-1].get("date", "")) if messages else ""
        baseline_activity = max(
            (row.get("timestamp", ""), row.get("followup_timestamp", "")),
            key=activity_timestamp,
        )
        activity_changed = False
        with profile_lock:
            if request_profile_id == active_profile_id:
                with thread_cache_lock:
                    previous_activity = thread_activity.get(email, "")
                    if activity_timestamp(latest_activity) > max(
                        activity_timestamp(previous_activity),
                        activity_timestamp(baseline_activity),
                    ):
                        thread_activity[email] = latest_activity
                        thread_activity.move_to_end(email)
                        while len(thread_activity) > 5000:
                            thread_activity.popitem(last=False)
                        activity_changed = True
        current_session = auth_store.resolve_session(getattr(self, "auth_token", ""))
        if (
            request_profile_id != active_profile_id
            or current_session is None
            or current_session["user"]["id"] != request_profile_id
        ):
            self.send_json(401, {"error": "Your local account session has ended."})
            return
        self.send_json(200, {
            "email": email,
            "thread_id": thread_id,
            "subject": row["subject"],
            "can_reply": (
                not suppressed
                and normalize_delivery_status(row.get("delivery_status", ""))
                not in ("bounced", "delayed", "needs_review")
            ),
            "messages": messages,
        })
        if activity_changed:
            publish_event("state", {"reason": "thread-activity", "email": email})

    def handle_tracker_reply(self, data):
        if data.get("confirmation") != "CONFIRM":
            self.send_json(400, {"error": "Confirmation is required before replying."})
            return
        email = str(data.get("email", "")).strip().lower()
        thread_id = str(data.get("thread_id", "")).strip()
        parent_message_id = str(data.get("parent_message_id", "")).strip()
        body = str(data.get("body", "")).strip()
        if not mailer.EMAIL_RE.fullmatch(email):
            raise ValueError("Choose a valid tracked email address.")
        if not re.fullmatch(r"\d+", thread_id):
            raise ValueError("This Gmail conversation has an invalid thread ID.")
        if (
            not parent_message_id or len(parent_message_id) > 998
            or any(ord(char) < 32 or ord(char) == 127 for char in parent_message_id)
        ):
            raise ValueError("Choose a valid message to reply to.")
        if not body or len(body) > 50000:
            raise ValueError("Reply must contain 1 to 50,000 characters.")
        if any(ord(char) < 32 and char not in "\r\n\t" for char in body):
            raise ValueError("Reply contains unsupported control characters.")
        if email in mailer.load_suppressed():
            raise ValueError("This address is suppressed and cannot be replied to.")

        def operation():
            try:
                cfg = mailer.load_config()
                result = mailer.send_thread_reply(
                    cfg, email, thread_id, parent_message_id, body
                )
            except SystemExit as exc:
                raise RuntimeError(str(exc)) from None
            invalidate_gmail_thread_cache(email, thread_id)
            return [
                f"Reply sent to {email}.",
                f"Subject: {result['subject']}",
                "The message was added to the existing Gmail conversation.",
            ]

        job_id = start_job("thread_reply", operation=operation)
        if job_id is None:
            self.send_json(409, {"error": "Another mailer task is already running."})
            return
        self.send_json(202, {"job_id": job_id})

    def update_followup_preferences(self, emails, enabled):
        if not emails or len(emails) > 10000:
            raise ValueError("Select between 1 and 10,000 tracker rows.")
        if not isinstance(enabled, bool):
            raise ValueError("Follow-up enabled must be true or false.")
        normalized = [str(email).strip().lower() for email in emails]
        if len(set(normalized)) != len(normalized):
            raise ValueError("Selected tracker rows contain duplicate addresses.")
        if any(not mailer.EMAIL_RE.fullmatch(email) for email in normalized):
            raise ValueError("Selection contains an invalid email address.")
        with jobs_lock:
            if active_job_id or active_delivery_job_id:
                self.send_json(
                    409,
                    {"error": "A mailer task is active. Change preferences after it finishes."},
                )
                return False
            rows = mailer.load_tracker_rows()
            row_map = {row["email"]: row for row in rows}
            suppressed = mailer.load_suppressed()
            max_followups = mailer.load_config()["max_followup_weeks"]
            selected = []
            for email in normalized:
                row = row_map.get(email)
                if row is None or row["status"] != "sent":
                    raise ValueError(
                        f"Follow-up preferences are available only for sent messages ({email})."
                    )
                if enabled:
                    if row["followup_status"] in (
                        "replied", "followup_unknown", "followup_error"
                    ):
                        raise ValueError(
                            f"Follow-up preference is locked for {email} due to its current outcome."
                        )
                    try:
                        followup_count = int(row["followup_count"] or 0)
                    except ValueError:
                        raise ValueError(
                            f"Invalid follow-up count in tracker for {email}."
                        ) from None
                    if followup_count >= max_followups:
                        raise ValueError(
                            f"{email} has reached the configured follow-up limit."
                        )
                    if (
                        row["delivery_status"] in ("bounced", "delayed", "needs_review")
                        or email in suppressed
                    ):
                        raise ValueError(
                            f"{email} has a delivery issue or is blocked; "
                            "follow-ups cannot be enabled."
                        )
                selected.append(row)
            for row in selected:
                row["followup_opt_in"] = "yes" if enabled else "no"
            mailer.save_tracker_rows(rows)
        return True

    def handle_followup_preference(self, data):
        email = str(data.get("email", "")).strip().lower()
        enabled = data.get("enabled")
        if not mailer.EMAIL_RE.fullmatch(email):
            raise ValueError("Choose a valid tracked address and follow-up preference.")
        if not self.update_followup_preferences([email], enabled):
            return
        self.send_json(200, {
            "ok": True,
            "email": email,
            "followup_opt_in": "yes" if enabled else "no",
        })

    def handle_bulk_followup_preferences(self, data):
        emails = data.get("emails")
        enabled = data.get("enabled")
        if not isinstance(emails, list):
            raise ValueError("Select tracker rows to update.")
        if not self.update_followup_preferences(emails, enabled):
            return
        self.send_json(200, {
            "ok": True,
            "updated": len(emails),
            "followup_opt_in": "yes" if enabled else "no",
        })

    def handle_contact_import_preview(self, data):
        token = ""
        try:
            token, _ = stage_contact_import(
                data.get("filename", ""), data.get("content", ""),
            )
            view = preview_contact_import(token)
            self.send_json(200, view)
        except Exception:
            if token:
                discard_contact_import(token)
            raise

    def handle_contact_import_inspect(self, data):
        result = inspect_contact_import(
            str(data.get("token", "")),
            data.get("sheet", ""),
            data.get("has_header", True),
            data.get("mapping"),
        )
        result.pop("_contacts", None)
        result.pop("_fields", None)
        result.pop("_existing_rows", None)
        self.send_json(200, result)

    def handle_contact_import_table(self, data):
        self.send_json(200, preview_contact_import(
            str(data.get("token", "")),
            data.get("sheet", ""),
            data.get("has_header", True),
        ))

    def handle_contact_import_commit(self, data):
        if data.get("confirm") is not True:
            raise ValueError("Review the preview and confirm the contact import.")
        result = inspect_contact_import(
            str(data.get("token", "")),
            data.get("sheet", ""),
            data.get("has_header", True),
            data.get("mapping"),
        )
        try:
            expected_ready = int(data.get("expected_ready"))
        except (TypeError, ValueError):
            raise ValueError("Review the import preview again before importing.") from None
        if expected_ready != result["ready"]:
            self.send_json(409, {
                "error": "Your contact list changed while this preview was open. Review the refreshed counts, then import again.",
                "ready": result["ready"],
                "duplicates": result["duplicates"],
                "blocked": result["blocked"],
            })
            return
        with tracker_write_guard(self, "import contacts") as allowed:
            if not allowed:
                return
            fields, rows = read_raw_contacts()
            existing = {str(row.get("email", "")).strip().casefold() for row in rows}
            suppressed = {email.casefold() for email in mailer.load_suppressed()}
            added = []
            for contact in result["_contacts"]:
                email = contact["email"].casefold()
                if email in existing or email in suppressed:
                    continue
                added.append(contact)
                existing.add(email)
            if added:
                for key in CONTACT_FIELDS:
                    if key not in fields:
                        fields.append(key)
                save_raw_contacts(fields, [*rows, *added])
        discard_contact_import(str(data.get("token", "")))
        self.send_json(200, {
            "ok": True,
            "imported": len(added),
            "duplicates": result["duplicates"],
            "invalid": result["invalid"],
            "blocked": result["blocked"],
            "skipped_changed": max(0, result["ready"] - len(added)),
        })

    def handle_contact_save(self, data):
        contact = {
            key: str(data.get(key, "")).strip()
            for key in CONTACT_FIELDS
        }
        contact["email"] = contact["email"].lower()
        if not contact["name"]:
            raise ValueError("Contact name is required.")
        if not mailer.EMAIL_RE.fullmatch(contact["email"]):
            raise ValueError("Enter a valid email address.")
        with tracker_write_guard(self, "change contacts") as allowed:
            if not allowed:
                return
            if contact["email"] in mailer.load_suppressed():
                raise ValueError("This address is blocked and cannot be added to contacts.")
            fields, rows = read_raw_contacts()
            old_email = str(data.get("old_email", "")).strip().lower()
            existing = next((row for row in rows if row["email"].lower() == old_email), None)
            if old_email and existing is None:
                raise ValueError("That contact no longer exists; refresh and try again.")
            if any(row["email"].lower() == contact["email"] and row is not existing for row in rows):
                raise ValueError("A contact with that email address already exists.")
            tracker = {row["email"]: row for row in mailer.load_tracker_rows()}
            if (
                existing
                and old_email != contact["email"]
                and tracker.get(old_email, {}).get("status") == "sent"
                and tracker.get(old_email, {}).get("delivery_status") != "bounced"
            ):
                raise ValueError("Email address cannot be changed after a successful send.")
            if existing:
                existing.update(contact)
            else:
                for key in CONTACT_FIELDS:
                    if key not in fields:
                        fields.append(key)
                rows.append(contact)
            save_raw_contacts(fields, rows)
            self.send_json(200, {"ok": True})

    def handle_contact_delete(self, data):
        email = str(data.get("email", "")).strip().lower()
        with tracker_write_guard(self, "change contacts") as allowed:
            if not allowed:
                return
            fields, rows = read_raw_contacts()
            filtered = [row for row in rows if row["email"].lower() != email]
            if len(filtered) == len(rows):
                raise ValueError("Contact not found.")
            save_raw_contacts(fields, filtered)
            self.send_json(200, {"ok": True})

    def handle_bulk_contact_delete(self, data):
        emails = data.get("emails")
        if not isinstance(emails, list) or not emails or len(emails) > 10000:
            raise ValueError("Select between 1 and 10,000 contacts to remove.")
        normalized = [str(email).strip().lower() for email in emails]
        if len(set(normalized)) != len(normalized):
            raise ValueError("Selected contacts contain duplicate addresses.")
        if any(not mailer.EMAIL_RE.fullmatch(email) for email in normalized):
            raise ValueError("Selection contains an invalid email address.")
        with jobs_lock:
            if active_job_id or active_delivery_job_id:
                self.send_json(
                    409,
                    {"error": "A mailer task is active. Remove contacts after it finishes."},
                )
                return
            fields, rows = read_raw_contacts()
            existing = {row["email"].strip().lower() for row in rows}
            missing = set(normalized) - existing
            if missing:
                raise ValueError(
                    f"Contact not found: {sorted(missing)[0]}. Refresh and try again."
                )
            selected = set(normalized)
            save_raw_contacts(
                fields,
                [row for row in rows if row["email"].strip().lower() not in selected],
            )
        self.send_json(200, {"ok": True, "deleted": len(normalized)})

    def handle_bulk_tracker_delete(self, data):
        emails = data.get("emails")
        if not isinstance(emails, list) or not emails or len(emails) > 10000:
            raise ValueError("Select between 1 and 10,000 tracker records to delete.")
        normalized = [str(email).strip().lower() for email in emails]
        if len(set(normalized)) != len(normalized):
            raise ValueError("Selected tracker records contain duplicate addresses.")
        if any(not mailer.EMAIL_RE.fullmatch(email) for email in normalized):
            raise ValueError("Selection contains an invalid email address.")
        with jobs_lock:
            if active_job_id or active_delivery_job_id:
                self.send_json(
                    409,
                    {"error": "A mailer task is active. Delete tracker records after it finishes."},
                )
                return
            rows = mailer.load_tracker_rows()
            existing = {row["email"].strip().lower() for row in rows}
            missing = set(normalized) - existing
            if missing:
                raise ValueError(
                    f"Tracker record not found: {sorted(missing)[0]}. Refresh and try again."
                )
            selected = set(normalized)
            mailer.save_tracker_rows(
                [row for row in rows if row["email"].strip().lower() not in selected]
            )
        self.send_json(200, {"ok": True, "deleted": len(normalized)})

    def handle_suppression(self, data):
        email = str(data.get("email", "")).strip().lower()
        if not mailer.EMAIL_RE.fullmatch(email):
            raise ValueError("Enter a valid email address.")
        if data.get("suppressed", True) is not True:
            raise ValueError("Blocked addresses cannot be unblocked from the dashboard.")
        with tracker_write_guard(self, "change suppression records") as allowed:
            if not allowed:
                return
            removed = mailer.suppress_addresses({email})
            self.send_json(200, {"ok": True, "contacts_removed": removed})

    def handle_settings(self, data):
        try:
            per_day = int(data.get("emails_per_day"))
            gap_min = int(data.get("gap_min_seconds"))
            gap_max = int(data.get("gap_max_seconds"))
            max_followup_weeks = int(data.get("max_followup_weeks"))
            followup_interval_days = int(data.get("followup_interval_days"))
        except (TypeError, ValueError):
            raise ValueError("Daily limit and delay settings must be whole numbers.") from None
        if not 1 <= per_day <= 100:
            raise ValueError("Daily email limit must be between 1 and 100.")
        if not 0 <= gap_min <= gap_max <= 3600:
            raise ValueError("Delays must satisfy 0 <= minimum <= maximum <= 3600.")
        if not 1 <= max_followup_weeks <= 3:
            raise ValueError("Maximum follow-ups per recipient must be between 1 and 3.")
        if not 1 <= followup_interval_days <= 90:
            raise ValueError("Follow-up interval must be between 1 and 90 days.")
        update_ini_settings({
            "emails_per_day": per_day,
            "gap_min_seconds": gap_min,
            "gap_max_seconds": gap_max,
            "max_followup_weeks": max_followup_weeks,
            "followup_interval_days": followup_interval_days,
        })
        self.send_json(200, {"ok": True})

    def handle_templates(self, data):
        initial = str(data.get("template", ""))
        followup = str(data.get("followup_template", ""))
        for label, content in (("Initial template", initial), ("Follow-up template", followup)):
            head, separator, _ = content.replace("\r\n", "\n").partition("\n\n")
            if not separator or not re.search(r"(?im)^subject:\s*.+$", head):
                raise ValueError(f"{label} needs a Subject line followed by a blank line.")
        for target, content in (
            (mailer.TEMPLATE_FILE, initial),
            (mailer.FOLLOWUP_TEMPLATE_FILE, followup),
        ):
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_text(content.replace("\r\n", "\n").rstrip() + "\n", encoding="utf-8")
            temporary.replace(target)
        self.send_json(200, {"ok": True})

    def handle_credentials(self, data):
        sender_email = str(data.get("sender_email", "")).strip()
        app_password = re.sub(r"\s+", "", str(data.get("app_password", "")))
        if not mailer.EMAIL_RE.fullmatch(sender_email):
            raise ValueError("Enter a valid sender email address.")
        existing = mailer.load_env_file()
        password_to_save = app_password or existing["app_password"]
        if not password_to_save:
            raise ValueError("Enter a Google App Password.")
        if app_password and len(app_password) != 16:
            raise ValueError("Google creates a 16-character App Password. Copy all 16 characters; spaces are okay.")
        request_profile_id = active_profile_id

        def operation():
            try:
                server = mailer.connect(
                    {"email": sender_email, "password": password_to_save},
                    timeout=20,
                )
                try:
                    server.quit()
                except (OSError, smtplib.SMTPException):
                    pass
            except smtplib.SMTPAuthenticationError:
                raise RuntimeError(
                    "Google did not accept this sign-in. Check the Gmail address, turn on 2-Step Verification, then try a fresh App Password. Some work or school accounts block App Passwords."
                ) from None
            except (TimeoutError, OSError, smtplib.SMTPException):
                raise RuntimeError(
                    "Could not reach Gmail's sending service. Check your internet connection and try again."
                ) from None

            # A successful SMTP sign-in is enough to preserve the working
            # sender credentials. IMAP is checked separately because Gmail
            # may have inbox access disabled while SMTP sending is allowed.
            with profile_lock:
                if active_profile_id != request_profile_id:
                    raise RuntimeError("The active local account changed. Sign in and try again.")
                write_env(sender_email, password_to_save)

            imap_ready = False
            mailbox = None
            try:
                mailbox = imaplib.IMAP4_SSL(
                    "imap.gmail.com", 993,
                    ssl_context=mailer.ssl.create_default_context(), timeout=20,
                )
                mailbox.login(sender_email, password_to_save)
                status, _ = mailbox.select("INBOX", readonly=True)
                imap_ready = status == "OK"
            except (imaplib.IMAP4.error, OSError, TimeoutError):
                imap_ready = False
            finally:
                if mailbox is not None:
                    try:
                        mailbox.logout()
                    except (imaplib.IMAP4.error, OSError):
                        pass
            profile_changed_event.set()
            reply_check_requested.set()
            lines = ["Gmail sending is connected. App Password saved on this computer."]
            lines.append(
                "Inbox monitoring is connected."
                if imap_ready else
                "Inbox monitoring could not be verified. Sending is ready; turn on IMAP in Gmail to sync replies."
            )
            return {
                "output": lines,
                "result": {
                    "sender_email": sender_email,
                    "smtp_connected": True,
                    "imap_connected": imap_ready,
                },
            }

        job_id = start_job("gmail_connection_test", operation=operation)
        if job_id is None:
            self.send_json(409, {"error": "Finish the current mail operation before connecting Gmail."})
            return
        self.send_json(202, {"job_id": job_id})

    def handle_resume(self, data):
        filename = Path(str(data.get("filename", ""))).name
        suffix = Path(filename).suffix.lower()
        if suffix not in mailer.RESUME_EXTS:
            raise ValueError("Resume must be a PDF, DOC, or DOCX file.")
        try:
            content = base64.b64decode(data.get("content", ""), validate=True)
        except (ValueError, TypeError):
            raise ValueError("Resume file data is invalid.") from None
        if not content or len(content) > MAX_RESUME_BYTES:
            raise ValueError("Resume must be between 1 byte and 8 MB.")
        if suffix == ".pdf" and not content.startswith(b"%PDF-"):
            raise ValueError("File does not appear to be a valid PDF.")
        if suffix == ".docx" and not content.startswith(b"PK"):
            raise ValueError("File does not appear to be a valid Word document.")
        if suffix == ".doc" and not content.startswith(b"\xd0\xcf\x11\xe0"):
            raise ValueError("File does not appear to be a valid DOC document.")
        mailer.RESUME_DIR.mkdir(exist_ok=True)
        destination = mailer.RESUME_DIR / filename
        destination.write_bytes(content)
        mailer.RESUME_SELECTION_FILE.write_text(filename, encoding="utf-8")
        self.send_json(200, {"ok": True, "filename": filename})

    def handle_resume_select(self, data):
        filename = str(data.get("filename", ""))
        path = mailer.RESUME_DIR / Path(filename).name
        if path.name != filename or not path.is_file() or path.suffix.lower() not in mailer.RESUME_EXTS:
            raise ValueError("Choose an existing PDF, DOC, or DOCX resume.")
        mailer.RESUME_SELECTION_FILE.write_text(path.name, encoding="utf-8")
        self.send_json(200, {"ok": True})


def main():
    parser = argparse.ArgumentParser(description="Local cold email dashboard")
    parser.add_argument("--port", type=int, default=8765, help="Local web port")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    try:
        initialize_auth()
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(f"Could not initialize local authentication: {exc}")
    server = DashboardServer(("127.0.0.1", args.port), DashboardHandler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Dashboard running locally at {url}")
    print(
        "Automatic delivery checks: All Mail (Inbox and archived) and Spam, last "
        f"{DELIVERY_LOOKBACK_DAYS} days, every "
        f"{DELIVERY_CHECK_INTERVAL_SECONDS // 60} minutes."
    )
    print(
        "Live reply watch: Gmail IMAP IDLE on All Mail, with incremental "
        f"All Mail and Spam safety scans every {REPLY_CHECK_INTERVAL_SECONDS // 60} minutes."
    )
    print("Press Ctrl+C here to stop it.")
    if not args.no_browser:
        webbrowser.open(url)
    monitor = threading.Thread(target=delivery_monitor_loop, daemon=True)
    monitor.start()
    replies = threading.Thread(target=reply_monitor_loop, daemon=True)
    replies.start()
    reply_watch = threading.Thread(target=gmail_idle_watch_loop, daemon=True)
    reply_watch.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        delivery_monitor_stop.set()
        delivery_check_requested.set()
        reply_monitor_stop.set()
        reply_check_requested.set()
        reply_job_finished.set()
        server.server_close()


if __name__ == "__main__":
    main()
