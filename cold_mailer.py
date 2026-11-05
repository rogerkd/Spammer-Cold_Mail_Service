#!/usr/bin/env python3
"""
cold_mailer.py - careful, low-volume outreach sender for Gmail.

Just run it once per day:

    python cold_mailer.py

It reads the next N people from the signed-in profile's SQLite database who
haven't been emailed yet (N = emails_per_day), attaches your resume from
data/resume/, and sends with a random gap between emails. Progress is saved
transactionally in the profile database, so tomorrow's run continues where
today's stopped.

Other commands:
    python cold_mailer.py --dry-run             preview, sends nothing
    python cold_mailer.py --test you@gmail.com  send ONE test email to yourself
    python cold_mailer.py --suppress a@b.com    never email this address again
    python cold_mailer.py --status              show progress
    python cold_mailer.py --follow-ups --dry-run preview eligible follow-ups
"""

import argparse
import configparser
import csv
import html
from html.parser import HTMLParser
import imaplib
import json
import math
import mimetypes
import os
import random
import re
import shutil
import socket
import smtplib
import ssl
import sys
import threading
import time
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import (
    formataddr, formatdate, getaddresses, make_msgid, parsedate_to_datetime,
)
from email import policy
from pathlib import Path

import profile_store

BASE = Path(__file__).resolve().parent
PROFILE_DIR = BASE
DATA_DIR = BASE / "data"
TEMPLATES_DIR = BASE / "templates"
CONFIG_FILE = BASE / "config.ini"
ENV_FILE = BASE / ".env"
CONTACTS_FILE = DATA_DIR / "contacts.csv"
TEMPLATE_FILE = TEMPLATES_DIR / "template.txt"
FOLLOWUP_TEMPLATE_FILE = TEMPLATES_DIR / "followup_template.txt"
RESUME_DIR = DATA_DIR / "resume"
TRACKER_FILE = DATA_DIR / "tracker.csv"
LEGACY_LOG_FILE = BASE / "sent_log.csv"
SUPPRESS_FILE = DATA_DIR / "unsubscribed.txt"
RESUME_SELECTION_FILE = RESUME_DIR / "selected_resume.txt"
GMAIL_REPLY_CURSOR_FILE = DATA_DIR / "gmail_reply_cursor.json"
THREAD_REPLY_STATE_FILE = DATA_DIR / "thread_reply_state.json"
GMAIL_REPLY_RECONCILE_INTERVAL_SECONDS = 15 * 60
thread_reply_state_lock = threading.RLock()
campaign_pause_requested = threading.Event()


def configure_profile_directory(profile_dir=None):
    """Point this process at one authenticated local account's data folder."""
    global PROFILE_DIR, DATA_DIR, TEMPLATES_DIR, CONFIG_FILE, ENV_FILE
    global CONTACTS_FILE, TEMPLATE_FILE, FOLLOWUP_TEMPLATE_FILE, RESUME_DIR
    global TRACKER_FILE, LEGACY_LOG_FILE, SUPPRESS_FILE, RESUME_SELECTION_FILE
    global GMAIL_REPLY_CURSOR_FILE, THREAD_REPLY_STATE_FILE
    PROFILE_DIR = Path(profile_dir).resolve() if profile_dir else BASE
    DATA_DIR = PROFILE_DIR / "data"
    TEMPLATES_DIR = PROFILE_DIR / "templates"
    CONFIG_FILE = PROFILE_DIR / "config.ini"
    ENV_FILE = PROFILE_DIR / ".env"
    CONTACTS_FILE = DATA_DIR / "contacts.csv"
    TEMPLATE_FILE = TEMPLATES_DIR / "template.txt"
    FOLLOWUP_TEMPLATE_FILE = TEMPLATES_DIR / "followup_template.txt"
    RESUME_DIR = DATA_DIR / "resume"
    TRACKER_FILE = DATA_DIR / "tracker.csv"
    LEGACY_LOG_FILE = PROFILE_DIR / "sent_log.csv"
    SUPPRESS_FILE = DATA_DIR / "unsubscribed.txt"
    RESUME_SELECTION_FILE = RESUME_DIR / "selected_resume.txt"
    GMAIL_REPLY_CURSOR_FILE = DATA_DIR / "gmail_reply_cursor.json"
    THREAD_REPLY_STATE_FILE = DATA_DIR / "thread_reply_state.json"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    profile_store.get_store(DATA_DIR).ensure_legacy_imported(
        CONTACTS_FILE, TRACKER_FILE, SUPPRESS_FILE, LEGACY_LOG_FILE
    )


def _store_for(path):
    """Get this profile's store and perform its one-time legacy CSV import."""
    data_dir = Path(path).parent
    store = profile_store.get_store(data_dir)
    legacy_log = LEGACY_LOG_FILE
    try:
        valid_legacy_root = Path(legacy_log).resolve().parent in {
            data_dir.resolve(), data_dir.resolve().parent,
        }
    except OSError:
        valid_legacy_root = False
    store.ensure_legacy_imported(
        data_dir / "contacts.csv",
        data_dir / "tracker.csv",
        data_dir / "unsubscribed.txt",
        legacy_log if valid_legacy_root else None,
    )
    return store


def load_raw_contacts():
    return _store_for(CONTACTS_FILE).load_contacts()


def save_raw_contacts(fields, rows):
    _store_for(CONTACTS_FILE).save_contacts(fields, rows)


if os.environ.get("COLD_MAILER_ACCOUNT_DIR"):
    configure_profile_directory(os.environ["COLD_MAILER_ACCOUNT_DIR"])

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
# generic mailboxes are often spam traps - skipped automatically
ROLE_PREFIXES = {"info", "admin", "support", "sales", "contact", "hello",
                 "noreply", "no-reply", "qa", "hr", "jobs", "careers", "payable"}
RESUME_EXTS = {".pdf", ".docx", ".doc"}
TRACKER_FIELDS = [
    "email", "status", "timestamp", "detail",
    "gmail_message_id", "gmail_thread_id", "subject",
    "followup_status", "followup_timestamp", "followup_detail",
    "followup_count", "followup_last_message_id",
    "delivery_status", "delivery_updated_at", "delivery_detail",
    "send_type", "followup_opt_in", "message_snapshot",
]


def print_heading(title):
    width = min(max(len(title) + 4, 38), shutil.get_terminal_size((80, 20)).columns)
    print(f"\n{title}\n{'=' * width}")


def print_summary(items):
    label_width = max((len(label) for label, _ in items), default=0)
    for label, value in items:
        print(f"  {label:<{label_width}} : {value}")


def print_contact_row(number, contact, subject=None):
    company = contact.get("company", "").strip() or "-"
    print(f"  {number:>2}. {contact.get('name') or contact['email']} <{contact['email']}>")
    if company:
        print(f"      {company}")
    if subject:
        print(f"      Subject: {subject}")


# ------------------------------------------------------------ config
def load_env_file():
    if not ENV_FILE.exists():
        sys.exit(f"{ENV_FILE.name} not found. Add sender_email and app_password.")
    values = {}
    for line_number, raw_line in enumerate(
            ENV_FILE.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
            sys.exit(f"Invalid .env entry on line {line_number}; expected KEY=value.")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key.strip()] = value
    missing = [key for key in ("sender_email", "app_password") if not values.get(key)]
    if missing:
        sys.exit(f"Missing required .env value(s): {', '.join(missing)}.")
    return values


def load_config():
    if not CONFIG_FILE.exists():
        sys.exit("config.ini not found.")
    cp = configparser.ConfigParser()
    cp.read(CONFIG_FILE, encoding="utf-8")
    env = load_env_file()
    try:
        s = cp["settings"]
        cfg = {
            "email": env["sender_email"].strip(),
            "password": env["app_password"].replace(" ", "").strip(),
            "per_day": int(s["emails_per_day"]),
            "gap_min": int(s["gap_min_seconds"]),
            "gap_max": int(s["gap_max_seconds"]),
            "max_followup_weeks": int(s.get("max_followup_weeks", "1")),
            "followup_interval_days": int(s.get("followup_interval_days", "7")),
        }
    except (KeyError, ValueError) as e:
        sys.exit(f"Problem in config.ini: {e}")
    if not 1 <= cfg["max_followup_weeks"] <= 3:
        sys.exit("Problem in config.ini: maximum follow-ups must be between 1 and 3.")
    if not 1 <= cfg["followup_interval_days"] <= 90:
        sys.exit("Problem in config.ini: followup_interval_days must be between 1 and 90.")
    if cfg["gap_min"] > cfg["gap_max"]:
        cfg["gap_min"], cfg["gap_max"] = cfg["gap_max"], cfg["gap_min"]
    return cfg


def check_credentials(cfg):
    if not EMAIL_RE.match(cfg["email"]) or cfg["email"].startswith("yourname@"):
        sys.exit("Set a valid Gmail address in .env (sender_email).")
    if not cfg["password"] or cfg["password"].lower().startswith("xxxx"):
        sys.exit("Set your Google App Password in .env (app_password).")
    if cfg["per_day"] > 100:
        print("Warning: more than ~100/day to cold contacts risks getting your "
              "account flagged. Consider lowering emails_per_day.")


# ----------------------------------------------------------- data files
def load_suppressed():
    return _store_for(SUPPRESS_FILE).load_suppressed()


def remove_contacts(emails):
    emails = {email.strip().lower() for email in emails if email.strip()}
    return _store_for(CONTACTS_FILE).delete_contacts(emails)


def save_suppressed(emails):
    _store_for(SUPPRESS_FILE).save_suppressed(emails)


def suppress_addresses(emails):
    addresses = {email.strip().lower() for email in emails if email.strip()}
    if not addresses:
        return 0
    return _store_for(SUPPRESS_FILE).suppress_addresses(addresses)


def add_suppressed(addr):
    removed = suppress_addresses({addr})
    print_heading("Address blocked")
    print_summary([
        ("Address", addr.strip().lower()),
        ("Status", "Blocked from future sends and removed from contacts"),
        ("Contact rows removed", f"{removed:,}"),
    ])


def load_log():
    done, today_count = set(), 0
    today = datetime.now().date().isoformat()
    for row in load_tracker_rows():
        email = row["email"]
        if row["status"] in ("sent", "refused", "unknown"):
            done.add(email)
        if (
            row["status"] == "sent"
            and row["send_type"] != "one_off"
            and row["timestamp"].startswith(today)
        ):
            today_count += 1
    return done, today_count


def save_tracker_rows(rows):
    _store_for(TRACKER_FILE).save_tracker_rows(rows)





def write_log(email, status, detail="", send_type="campaign",
              followup_opt_in="", message_snapshot="", event_at=None):
    email = email.strip().lower()
    store = _store_for(TRACKER_FILE)
    stored_row = store.get_tracker_row(email)
    row = dict.fromkeys(TRACKER_FIELDS, "")
    if stored_row:
        row.update(stored_row)
    else:
        row["email"] = email

    timestamp = (
        str(event_at).strip()
        if status == "replied" and event_at
        else datetime.now().isoformat(timespec="seconds")
    )
    if status in ("sent", "refused", "error", "unknown"):
        row.update(status=status, timestamp=timestamp, detail=detail)
        row["send_type"] = send_type
        if send_type == "one_off":
            row["followup_opt_in"] = followup_opt_in
            row["message_snapshot"] = message_snapshot
        if status in ("sent", "unknown"):
            row["delivery_status"] = "awaiting_check"
            row["delivery_updated_at"] = timestamp
            if status == "sent":
                row["delivery_detail"] = ""
            try:
                details = json.loads(detail or "{}")
            except json.JSONDecodeError:
                details = {}
            if isinstance(details, dict):
                row["gmail_message_id"] = details.get("message_id", row["gmail_message_id"])
                row["gmail_thread_id"] = details.get(
                    "gmail_thread_id", row["gmail_thread_id"]
                )
                row["subject"] = details.get("subject", row["subject"])
    elif status in ("followup_sent", "followup_unknown", "followup_error", "replied"):
        if (
            status == "replied"
            and row.get("followup_status") == "replied"
            and (
                row.get("followup_detail") == detail
                or _reply_activity_timestamp(row.get("followup_timestamp", ""))
                > _reply_activity_timestamp(timestamp)
            )
        ):
            return
        row.update(
            followup_status=status,
            followup_timestamp=timestamp,
            followup_detail=detail,
        )
        if status == "followup_sent":
            row["followup_count"] = str(
                (int(row["followup_count"] or 0)) + 1
            )
            try:
                details = json.loads(detail or "{}")
            except json.JSONDecodeError:
                details = {}
            if isinstance(details, dict):
                row["followup_last_message_id"] = details.get(
                    "message_id", row["followup_last_message_id"]
                )
    else:
        raise ValueError(f"Unknown tracker status: {status}")
    store.save_tracker_row(row)


def load_last_success():
    last_email, last_time = "", ""
    for row in load_tracker_rows():
        if row.get("status") == "sent" and row.get("email"):
            last_email = row["email"].strip()
            last_time = row.get("timestamp", "").strip()
    return last_email, last_time


def build_tracker_state(contacts, done, suppressed):
    queue = [c for c in contacts if c["email"] not in done and c["email"] not in suppressed]
    next_contact = queue[0] if queue else {}
    last_email, last_time = load_last_success()
    return {
        "total_contacts": len(contacts),
        "done": len(done),
        "left": len(queue),
        "next_email": next_contact.get("email", ""),
        "next_name": next_contact.get("name", ""),
        "next_company": next_contact.get("company", ""),
        "last_email": last_email,
        "last_time": last_time,
    }


def load_tracker_rows():
    rows = _store_for(TRACKER_FILE).load_tracker_rows()

    canonical = []
    by_email = {}
    changed = False
    for old in rows:
        email = (old.get("email") or "").strip().lower()
        if not email or old.get("status") == "tracker":
            continue
        row = by_email.get(email)
        if row is None:
            row = dict.fromkeys(TRACKER_FIELDS, "")
            row["email"] = email
            by_email[email] = row
            canonical.append(row)
        status = old.get("status", "")
        timestamp = old.get("timestamp", "")
        detail = old.get("detail", "")
        for field in ("followup_status", "followup_timestamp", "followup_detail"):
            if old.get(field):
                row[field] = old[field]
        for field in ("delivery_status", "delivery_updated_at", "delivery_detail"):
            if old.get(field):
                row[field] = old[field]
        for field in ("gmail_message_id", "gmail_thread_id", "subject"):
            if old.get(field):
                row[field] = old[field]
        for field in ("send_type", "followup_opt_in", "message_snapshot"):
            if old.get(field):
                row[field] = old[field]
        try:
            row["followup_count"] = str(max(
                int(row["followup_count"] or 0),
                int(old.get("followup_count") or 0),
            ))
        except ValueError:
            row["followup_count"] = "0"
        if old.get("followup_last_message_id"):
            row["followup_last_message_id"] = old["followup_last_message_id"]
        try:
            details = json.loads(old.get("detail") or "{}")
        except json.JSONDecodeError:
            details = {}
        if isinstance(details, dict):
            row["gmail_message_id"] = row["gmail_message_id"] or details.get("message_id", "")
            row["gmail_thread_id"] = row["gmail_thread_id"] or details.get("gmail_thread_id", "")
            row["subject"] = row["subject"] or details.get("subject", "")
        if status in ("sent", "refused", "error", "unknown"):
            if status == "sent":
                if row["status"] != "sent" or (
                        timestamp and timestamp < row["timestamp"]):
                    row.update(status=status, timestamp=timestamp, detail=detail)
            elif status == "unknown":
                if row["status"] != "sent":
                    row.update(status=status, timestamp=timestamp, detail=detail)
            elif row["status"] != "sent":
                row.update(status=status, timestamp=timestamp, detail=detail)
        elif status in ("followup_sent", "followup_unknown", "followup_error", "replied"):
            row.update(
                followup_status=status,
                followup_timestamp=timestamp,
                followup_detail=detail,
            )
            if status == "followup_sent":
                row["followup_count"] = str(max(
                    int(row["followup_count"] or 0), 1
                ))
            try:
                followup_details = json.loads(detail or "{}")
            except json.JSONDecodeError:
                followup_details = {}
            if status == "followup_sent" and isinstance(followup_details, dict):
                row["followup_last_message_id"] = (
                    followup_details.get("message_id")
                    or row["followup_last_message_id"]
                )
        if row["status"] == "sent" and not row["delivery_status"]:
            row["delivery_status"] = "awaiting_check"
            row["delivery_updated_at"] = timestamp
        if (
            row["delivery_status"] in ("bounced", "delayed", "needs_review")
            and row["followup_opt_in"].lower() != "no"
        ):
            row["followup_opt_in"] = "no"
            changed = True
    if len(canonical) != len(rows) or changed:
        save_tracker_rows(canonical)
    return canonical


def update_message_details(email, updates):
    rows = load_tracker_rows()
    row = next((entry for entry in rows if entry["email"] == email.lower()), None)
    if row is None:
        raise ValueError(f"No tracker row exists for {email}.")
    try:
        details = json.loads(row["detail"] or "{}")
    except json.JSONDecodeError:
        details = {}
    if not isinstance(details, dict):
        details = {}
    details.update(updates)
    if "message_id" in updates:
        row["gmail_message_id"] = updates["message_id"]
    if "gmail_thread_id" in updates:
        row["gmail_thread_id"] = updates["gmail_thread_id"]
    if "subject" in updates:
        row["subject"] = updates["subject"]
    row["detail"] = json.dumps(details)
    save_tracker_rows(rows)


def load_followup_states():
    states = {}
    for row in load_tracker_rows():
        email = row["email"]
        if not email:
            continue
        if (
            row["status"] == "sent"
            and row["delivery_status"] not in ("bounced", "delayed", "needs_review")
            and row["followup_opt_in"].lower() != "no"
        ):
            try:
                details = json.loads(row["detail"] or "{}")
            except json.JSONDecodeError:
                details = {}
            if not isinstance(details, dict):
                details = {}
            states[email] = {
                "status": row["followup_status"] or "sent",
                "timestamp": row["timestamp"],
                "message_id": row["gmail_message_id"] or details.get("message_id", ""),
                "last_message_id": row["followup_last_message_id"],
                "subject": row["subject"] or details.get("subject", ""),
                "thread_id": row["gmail_thread_id"] or details.get("gmail_thread_id", ""),
                "followup_count": int(row["followup_count"] or 0),
                "followup_timestamp": row["followup_timestamp"],
            }
    return states


def send_one_off(
        cfg, contact, subject, body, followup_opt_in, html_body="",
        attachments=()):
    """Send and track one customized initial message to an untracked contact."""
    email = contact["email"]
    if email in load_suppressed():
        raise ValueError("This address is suppressed and cannot be sent to.")
    if any(row["email"] == email for row in load_tracker_rows()):
        raise ValueError(
            "This address already has tracker history; a one-off send would "
            "overwrite its existing one-row-per-email record."
        )
    rendered_subject = personalise(subject, contact).strip()
    rendered_body = personalise(body, contact).strip()
    safe_html_body, _ = sanitize_email_html(html_body) if html_body else ("", "")
    if not rendered_subject or not rendered_body or "\r" in rendered_subject or "\n" in rendered_subject:
        raise ValueError("Subject and message body are required; subject must be one line.")
    try:
        check_credentials(cfg)
        template_name, _, _ = load_template()
        sender_name = template_name or cfg["email"].split("@")[0]
        resume = find_resume()
        msg = build_message(
            contact, cfg["email"], sender_name,
            rendered_subject, rendered_body, resume,
            html_body=safe_html_body or None, attachments=attachments,
        )
    except SystemExit as exc:
        raise RuntimeError(str(exc)) from None
    snapshot = json.dumps({
        "subject": rendered_subject,
        "body": msg.get_body(preferencelist=("plain",)).get_content().strip(),
        "html_body": (
            msg.get_body(preferencelist=("html",)).get_content()
            if msg.get_body(preferencelist=("html",)) else ""
        ),
        "attachment": resume[0],
        "additional_attachments": [item[0] for item in attachments],
        "template_name": "custom",
    })
    try:
        server = connect(cfg)
    except smtplib.SMTPAuthenticationError:
        raise RuntimeError(
            "Gmail login failed. Use a Google App Password, not your normal password."
        ) from None
    except (OSError, smtplib.SMTPException) as exc:
        write_log(
            email, "error", str(exc)[:200], "one_off",
            "yes" if followup_opt_in else "no", snapshot,
        )
        return {"status": "error", "message_id": "", "subject": rendered_subject}
    try:
        try:
            server.send_message(msg)
        except smtplib.SMTPRecipientsRefused as exc:
            write_log(
                email, "refused", str(exc)[:200], "one_off",
                "yes" if followup_opt_in else "no", snapshot,
            )
            return {"status": "refused", "message_id": "", "subject": rendered_subject}
        except (smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as exc:
            write_log(
                email, "error", str(exc)[:200], "one_off",
                "yes" if followup_opt_in else "no", snapshot,
            )
            return {"status": "error", "message_id": "", "subject": rendered_subject}
        except (smtplib.SMTPServerDisconnected, TimeoutError, OSError) as exc:
            write_log(email, "unknown", json.dumps({
                "message_id": msg["Message-ID"],
                "subject": rendered_subject,
                "reason": str(exc)[:200],
            }), "one_off", "yes" if followup_opt_in else "no", snapshot)
            return {
                "status": "unknown", "message_id": msg["Message-ID"],
                "subject": rendered_subject,
            }
    finally:
        try:
            server.quit()
        except (OSError, smtplib.SMTPException):
            pass

    write_log(email, "sent", json.dumps({
        "message_id": msg["Message-ID"],
        "subject": rendered_subject,
    }), "one_off", "yes" if followup_opt_in else "no", snapshot)
    try:
        threads = find_gmail_thread_ids(cfg, [(email, {
            "message_id": msg["Message-ID"],
            "subject": rendered_subject,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        })])
        if email in threads:
            update_message_details(email, {
                "message_id": threads[email]["message_id"],
                "subject": threads[email]["subject"],
                "gmail_thread_id": threads[email]["thread_id"],
            })
    except (imaplib.IMAP4.error, OSError, RuntimeError, ValueError) as exc:
        return {
            "status": "sent",
            "message_id": msg["Message-ID"],
            "subject": rendered_subject,
            "thread_warning": str(exc),
        }
    return {
        "status": "sent", "message_id": msg["Message-ID"],
        "subject": rendered_subject,
        "thread_id": threads.get(email, {}).get("thread_id", ""),
    }


def find_gmail_thread_ids(cfg, candidates):
    threads = {}
    if not candidates:
        return threads

    mailbox = imaplib.IMAP4_SSL(
        "imap.gmail.com", 993, ssl_context=ssl.create_default_context(),
        timeout=20,
    )
    try:
        mailbox.login(cfg["email"], cfg["password"])
        status, _ = mailbox.select('"[Gmail]/Sent Mail"', readonly=True)
        if status != "OK":
            raise RuntimeError(
                "Could not open Gmail's Sent Mail folder via IMAP; "
                "check IMAP access is enabled."
            )
        for email, record in candidates:
            if record["message_id"]:
                status, data = mailbox.uid(
                    "SEARCH", None, "HEADER", "Message-ID", f'"{record["message_id"]}"'
                )
            else:
                sent_at = datetime.fromisoformat(record["timestamp"])
                status, data = mailbox.uid(
                    "SEARCH", None, "TO", email, "SINCE",
                    sent_at.strftime("%d-%b-%Y"), "SUBJECT", f'"{record["subject"]}"'
                )
            if status != "OK":
                raise RuntimeError(f"Could not search Sent Mail for {email}.")
            message_uids = data[0].split() if data and data[0] else []
            matches = []
            for uid in message_uids:
                status, fetched = mailbox.uid(
                    "FETCH", uid,
                    "(BODY.PEEK[HEADER.FIELDS (TO SUBJECT DATE MESSAGE-ID)] X-GM-THRID)"
                )
                if status != "OK":
                    raise RuntimeError(f"Could not read Gmail thread ID for {email}.")
                for item in fetched:
                    if not isinstance(item, tuple):
                        continue
                    thread_match = re.search(
                        rb"X-GM-THRID\s+(\d+)", item[0], re.IGNORECASE
                    )
                    if not thread_match:
                        continue
                    headers = BytesParser(policy=policy.default).parsebytes(item[1])
                    message_id = str(headers.get("Message-ID", "")).strip()
                    subject = str(headers.get("Subject", "")).strip()
                    if not record["message_id"]:
                        recipients = {
                            address.lower()
                            for _, address in getaddresses(headers.get_all("To", []))
                        }
                        if email not in recipients or subject != record["subject"]:
                            continue
                        try:
                            message_time = datetime.fromtimestamp(
                                parsedate_to_datetime(str(headers["Date"])).timestamp()
                            )
                        except (TypeError, ValueError, OverflowError):
                            continue
                        difference = abs((message_time - sent_at).total_seconds())
                        if difference > 15 * 60:
                            continue
                        matches.append((
                            difference,
                            message_id,
                            subject,
                            thread_match.group(1).decode("ascii"),
                        ))
                    else:
                        matches.append((
                            0,
                            message_id or record["message_id"],
                            subject or record["subject"],
                            thread_match.group(1).decode("ascii"),
                        ))
            if len(matches) == 1:
                _, message_id, subject, thread_id = matches[0]
                threads[email] = {
                    "message_id": message_id,
                    "subject": subject,
                    "thread_id": thread_id,
                }
            elif len(matches) > 1 and not record["message_id"]:
                print(f"Ambiguous Sent Mail match for {email}; leaving its thread ID unset.")
    finally:
        try:
            mailbox.logout()
        except imaplib.IMAP4.error:
            pass
    return threads


def gmail_conversation_folders(mailbox):
    status, folders = mailbox.list()
    if status != "OK":
        raise RuntimeError("Could not list Gmail folders over IMAP.")
    names = []
    for folder in folders or []:
        if not isinstance(folder, bytes):
            continue
        match = re.search(rb'"((?:[^"\\]|\\.)*)"\s*$', folder)
        if match:
            names.append(match.group(1).decode("ascii", errors="replace"))
    preferred = ("[Gmail]/All Mail", "[Gmail]/Sent Mail", "INBOX")
    selected = [
        next((name for name in names if name.casefold() == wanted.casefold()), "")
        for wanted in preferred
    ]
    return list(dict.fromkeys(name for name in selected if name))


def wait_for_gmail_mailbox_update(
    cfg, stop_event, duration=20 * 60, on_connected=None, interrupt_event=None,
):
    """Wait for Gmail IMAP IDLE to report a mailbox change.

    Returns True when the selected mailbox changes and False when the watch
    duration expires or ``stop_event`` is set. Gmail's All Mail folder covers
    Inbox and archived mail; the dashboard's periodic scan still checks Spam.
    This small wire-level IDLE client keeps working on Python versions before
    ``imaplib.IMAP4.idle`` was added.
    """
    context = ssl.create_default_context()
    raw_socket = socket.create_connection(("imap.gmail.com", 993), timeout=20)
    connection = context.wrap_socket(raw_socket, server_hostname="imap.gmail.com")
    connection.settimeout(20)
    buffered = bytearray()
    tag_number = 0

    def read_line():
        while b"\r\n" not in buffered:
            chunk = connection.recv(4096)
            if not chunk:
                raise ConnectionError("Gmail closed the IMAP connection.")
            buffered.extend(chunk)
        end = buffered.index(b"\r\n")
        line = bytes(buffered[:end])
        del buffered[:end + 2]
        return line

    def send_command(command, expect_continuation=False):
        nonlocal tag_number
        tag_number += 1
        tag = f"A{tag_number:03d}"
        connection.sendall(f"{tag} {command}\r\n".encode("utf-8"))
        while True:
            line = read_line()
            if expect_continuation and line.startswith(b"+"):
                return tag
            if line.upper().startswith(tag.encode("ascii") + b" "):
                if not line.upper().startswith(tag.encode("ascii") + b" OK"):
                    raise RuntimeError("Gmail rejected an IMAP mailbox watch command.")
                return tag

    def quote_imap_string(value):
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'

    try:
        if not read_line().upper().startswith(b"* OK"):
            raise RuntimeError("Gmail did not accept the IMAP connection.")
        send_command(
            "LOGIN " + quote_imap_string(cfg["email"]) + " "
            + quote_imap_string(cfg["password"])
        )
        try:
            send_command('EXAMINE "[Gmail]/All Mail"')
        except RuntimeError:
            # All Mail is Gmail's preferred watch folder. Inbox keeps the live
            # watch useful if the account has hidden or renamed that label.
            send_command('EXAMINE "INBOX"')

        idle_tag = send_command("IDLE", expect_continuation=True)
        if on_connected:
            on_connected()
        deadline = time.monotonic() + max(1, duration)
        connection.settimeout(1)
        changed = False
        while (
            not stop_event.is_set()
            and not (interrupt_event and interrupt_event.is_set())
            and time.monotonic() < deadline
        ):
            try:
                chunk = connection.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("Gmail closed the live mailbox watch.")
            buffered.extend(chunk)
            while b"\r\n" in buffered:
                line = read_line()
                if re.search(rb"\b\d+\s+(?:EXISTS|RECENT)\b", line, re.I):
                    changed = True
                    break
                if line.upper().startswith(b"* BYE"):
                    raise ConnectionError("Gmail ended the live mailbox watch.")
            if changed:
                break

        connection.settimeout(20)
        connection.sendall(b"DONE\r\n")
        while True:
            line = read_line()
            if line.upper().startswith(idle_tag.encode("ascii") + b" "):
                if not line.upper().startswith(idle_tag.encode("ascii") + b" OK"):
                    raise RuntimeError("Gmail ended the IMAP mailbox watch unexpectedly.")
                break
        return changed
    finally:
        try:
            connection.settimeout(2)
            connection.sendall(b"A999 LOGOUT\r\n")
            read_line()
        except (OSError, RuntimeError, ConnectionError):
            pass
        try:
            connection.close()
        except OSError:
            pass


def split_quoted_email_body(body):
    """Separate a reply's new text from the quoted history below it."""
    normalized = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    candidates = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if re.match(r"^On .{4,240}\bwrote:\s*$", stripped, re.I):
            candidates.append(index)
        elif re.match(
            r"^(?:[-_ ]{2,})?(?:Original Message|Forwarded message)(?:[-_ ]{2,})?$",
            stripped,
            re.I,
        ):
            candidates.append(index)
        elif re.match(r"^Begin forwarded message:\s*$", stripped, re.I):
            candidates.append(index)
        elif re.match(r"^From:\s*.+", stripped, re.I):
            following = "\n".join(lines[index + 1:index + 5])
            if re.search(r"^(?:Date|Sent|To|Subject):\s*.+$", following, re.I | re.M):
                candidates.append(index)

        if re.match(r"^\s*>+\s?.*", line):
            candidates.append(index)

    if not candidates:
        return normalized.strip(), ""
    cut = min(candidates)
    visible = "\n".join(lines[:cut]).strip()
    quoted = "\n".join(lines[cut:]).strip()
    if not quoted:
        return normalized.strip(), ""
    return visible, quoted


def load_gmail_thread(cfg, thread_id, root_message_id=""):
    if not re.fullmatch(r"\d+", str(thread_id)):
        raise ValueError("This tracker record has an invalid Gmail thread ID.")
    mailbox = imaplib.IMAP4_SSL(
        "imap.gmail.com", 993, ssl_context=ssl.create_default_context(),
        timeout=20,
    )
    try:
        mailbox.login(cfg["email"], cfg["password"])
        folders = gmail_conversation_folders(mailbox)
        if not folders:
            raise RuntimeError(
                "Gmail did not expose All Mail, Sent Mail, or Inbox over IMAP."
            )
        message_uids = []
        selected_folder = ""
        search_errors = []
        for folder in folders:
            status, _ = mailbox.select(f'"{folder}"', readonly=True)
            if status != "OK":
                search_errors.append(folder)
                continue
            status, data = mailbox.uid(
                "SEARCH", None, "X-GM-THRID", str(thread_id)
            )
            if status != "OK":
                search_errors.append(folder)
                continue
            message_uids = data[0].split() if data and data[0] else []
            if message_uids:
                selected_folder = folder
                break
        if not message_uids and root_message_id:
            for folder in folders:
                status, _ = mailbox.select(f'"{folder}"', readonly=True)
                if status != "OK":
                    continue
                status, data = mailbox.uid(
                    "SEARCH", None, "HEADER", "Message-ID",
                    f'"{root_message_id}"',
                )
                if status != "OK":
                    continue
                root_uids = data[0].split() if data and data[0] else []
                if not root_uids:
                    continue
                status, fetched = mailbox.uid(
                    "FETCH", root_uids[0], "(X-GM-THRID)"
                )
                if status != "OK":
                    continue
                thread_match = re.search(
                    rb"X-GM-THRID\s+(\d+)",
                    b" ".join(
                        item[0] for item in fetched if isinstance(item, tuple)
                    ),
                    re.IGNORECASE,
                )
                if not thread_match:
                    selected_folder = folder
                    message_uids = root_uids
                    break
                recovered_thread_id = thread_match.group(1).decode("ascii")
                for thread_folder in folders:
                    status, _ = mailbox.select(
                        f'"{thread_folder}"', readonly=True
                    )
                    if status != "OK":
                        continue
                    status, data = mailbox.uid(
                        "SEARCH", None, "X-GM-THRID", recovered_thread_id
                    )
                    if status == "OK" and data and data[0]:
                        selected_folder = thread_folder
                        message_uids = data[0].split()
                        break
                if message_uids:
                    break
        if not message_uids and search_errors:
            raise RuntimeError(
                "Gmail could not search this conversation in its available folders."
            )
        if not message_uids:
            raise ValueError("No messages were found in this Gmail conversation.")
        if len(message_uids) > 100:
            message_uids = message_uids[-100:]
        parsed_messages = []
        status, fetched = mailbox.uid(
            "FETCH", b",".join(message_uids),
            "(UID X-GM-MSGID BODY.PEEK[]<0.65536>)",
        )
        if status != "OK":
            raise RuntimeError("Could not read messages in this conversation.")
        raw_messages = [
            (item[0], item[1]) for item in fetched
            if isinstance(item, tuple) and len(item) > 1
            and isinstance(item[0], bytes) and isinstance(item[1], bytes)
        ]
        for metadata, raw_message in raw_messages:
            message = BytesParser(policy=policy.default).parsebytes(raw_message)
            message_id = str(message.get("Message-ID", "")).strip()
            gmail_message_match = re.search(
                rb"X-GM-MSGID\s+(\d+)", metadata, re.IGNORECASE
            )
            gmail_message_id = (
                gmail_message_match.group(1).decode("ascii")
                if gmail_message_match else ""
            )
            read_id = message_id or (
                f"gmail:{gmail_message_id}" if gmail_message_id else ""
            )
            if not read_id:
                continue
            body_part = message.get_body(preferencelist=("plain", "html"))
            body = ""
            if body_part is not None:
                try:
                    body = body_part.get_content()
                except (LookupError, UnicodeDecodeError):
                    body = ""
                if not isinstance(body, str):
                    body = ""
                if body_part.get_content_type() == "text/html":
                    _, body = sanitize_email_html(body)
            body = body[:25000]
            visible_body, quoted_body = split_quoted_email_body(body)
            try:
                sent_at = parsedate_to_datetime(str(message.get("Date", "")))
                sent_timestamp = sent_at.timestamp()
                sent_date = sent_at.isoformat()
            except (TypeError, ValueError, OverflowError):
                sent_timestamp = 0
                sent_date = ""
            references = re.findall(
                r"<[^<>]+>", str(message.get("References", ""))
            )
            parsed_messages.append({
                "message_id": message_id,
                "read_id": read_id,
                "gmail_message_id": gmail_message_id,
                "in_reply_to": str(message.get("In-Reply-To", "")).strip(),
                "references": references[-20:],
                "from": str(message.get("From", "")),
                "to": str(message.get("To", "")),
                "date": sent_date,
                "timestamp": sent_timestamp,
                "subject": str(message.get("Subject", "")),
                "body": visible_body,
                "quoted_body": quoted_body,
                "truncated": len(raw_message) >= 65536 or len(body) >= 25000,
            })
        if not parsed_messages:
            raise ValueError("Gmail returned no readable messages for this conversation.")
        parsed_messages.sort(key=lambda item: item["timestamp"])
        known_ids = {message["message_id"] for message in parsed_messages}
        for message in parsed_messages:
            parent_id = message["in_reply_to"]
            if parent_id not in known_ids:
                parent_id = next(
                    (reference for reference in reversed(message["references"])
                     if reference in known_ids),
                    "",
                )
            message["parent_message_id"] = (
                parent_id if parent_id != message["message_id"] else ""
            )
            message.pop("timestamp")
        return parsed_messages
    finally:
        try:
            mailbox.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def send_thread_reply(cfg, email, thread_id, parent_message_id, body):
    email = email.strip().lower()
    if email in load_suppressed():
        raise ValueError("This address is suppressed and cannot be replied to.")
    row = next(
        (entry for entry in load_tracker_rows() if entry["email"] == email),
        None,
    )
    if row is None or row["status"] != "sent":
        raise ValueError("Replies are available only for successfully sent messages.")
    delivery_status = re.sub(
        r"[\s-]+", "_", row["delivery_status"].strip().lower()
    )
    if delivery_status in ("bounced", "delayed", "needs_review"):
        raise ValueError("Replies are disabled because delivery has a reported issue.")
    if not row["gmail_thread_id"] or row["gmail_thread_id"] != str(thread_id):
        raise ValueError("This Gmail conversation no longer matches the tracker.")
    body = body.strip()
    if not body or len(body) > 50000:
        raise ValueError("Reply must contain 1 to 50,000 characters.")
    if any(ord(char) < 32 and char not in "\r\n\t" for char in body):
        raise ValueError("Reply contains unsupported control characters.")
    messages = load_gmail_thread(
        cfg, thread_id, row["gmail_message_id"]
    )
    parent = next(
        (message for message in messages
         if message["message_id"] == parent_message_id),
        None,
    )
    if parent is None:
        raise ValueError(
            "The selected reply is no longer in this conversation. Refresh and retry."
        )
    subject = row["subject"] or parent["subject"]
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    sender_name, _, _ = load_template()
    sender_name = sender_name or cfg["email"].split("@")[0]
    reply = EmailMessage()
    reply["Subject"] = subject
    reply["From"] = formataddr((sender_name, cfg["email"]))
    reply["To"] = formataddr(("", email))
    reply["Reply-To"] = cfg["email"]
    reply["Date"] = formatdate(localtime=True)
    reply["Message-ID"] = make_msgid(domain=cfg["email"].split("@")[1])
    reply["In-Reply-To"] = parent["message_id"]
    references = parent["references"] + [parent["message_id"]]
    reply["References"] = " ".join(references[-20:])
    reply.set_content(body)
    check_credentials(cfg)
    try:
        with connect(cfg) as server:
            server.send_message(reply)
    except smtplib.SMTPAuthenticationError:
        raise RuntimeError(
            "Gmail login failed. Use a Google App Password, not your normal password."
        ) from None
    except (smtplib.SMTPServerDisconnected, TimeoutError, OSError) as exc:
        raise RuntimeError(
            "Gmail disconnected during the reply. Its send outcome is uncertain; "
            "check Sent Mail before retrying."
        ) from exc
    except smtplib.SMTPException as exc:
        raise RuntimeError(f"Gmail could not send the reply: {exc}") from exc
    return {
        "message_id": reply["Message-ID"],
        "subject": subject,
    }


def find_replies(cfg, candidates):
    replies = set()
    if not candidates:
        return replies
    mailbox = imaplib.IMAP4_SSL(
        "imap.gmail.com", 993, ssl_context=ssl.create_default_context()
    )
    try:
        mailbox.login(cfg["email"], cfg["password"])
        folders = gmail_monitor_folders(mailbox)
        for folder in folders:
            status, _ = mailbox.select(f'"{folder}"', readonly=True)
            if status != "OK":
                raise RuntimeError(f"Could not open Gmail's {folder} folder over IMAP.")
            for email, record in candidates:
                status, data = mailbox.uid(
                    "SEARCH", None, "X-GM-THRID", record["thread_id"]
                )
                if status != "OK":
                    raise RuntimeError(f"Could not search the Gmail thread for {email}.")
                message_uids = data[0].split() if data and data[0] else []
                for uid in message_uids:
                    status, fetched = mailbox.uid(
                        "FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM)])"
                    )
                    if status != "OK":
                        raise RuntimeError(
                            f"Could not read a thread message from {email}."
                        )
                    for item in fetched:
                        if not isinstance(item, tuple):
                            continue
                        message = BytesParser(policy=policy.default).parsebytes(item[1])
                        senders = {
                            address.lower()
                            for _, address in getaddresses(message.get_all("From", []))
                        }
                        if email in senders:
                            replies.add(email)
                            break
                    if email in replies:
                        break
                if len(replies) == len(candidates):
                    break
    finally:
        try:
            mailbox.logout()
        except imaplib.IMAP4.error:
            pass
    return replies


def gmail_monitor_folders(mailbox):
    status, folders = mailbox.list()
    if status != "OK":
        raise RuntimeError("Could not list Gmail folders over IMAP.")
    names = []
    for folder in folders or []:
        if not isinstance(folder, bytes):
            continue
        match = re.search(rb'"((?:[^"\\]|\\.)*)"\s*$', folder)
        if match:
            names.append(match.group(1).decode("ascii", errors="replace"))
    inbox = next((name for name in names if name.casefold() == "inbox"), None)
    spam = next(
        (name for name in names if name.casefold().endswith("/spam")
         or name.casefold() == "spam"),
        None,
    )
    if not inbox:
        raise RuntimeError("Gmail did not expose an Inbox folder over IMAP.")
    if not spam:
        raise RuntimeError(
            "Gmail did not expose its Spam folder over IMAP. "
            "Enable IMAP and confirm the Spam label is visible."
        )
    return [inbox, spam]


def gmail_delivery_folders(mailbox):
    """Search archived mail and Spam; Gmail All Mail already includes Inbox."""
    status, folders = mailbox.list()
    if status != "OK":
        raise RuntimeError("Could not list Gmail folders over IMAP.")
    names = []
    for folder in folders or []:
        if not isinstance(folder, bytes):
            continue
        match = re.search(rb'"((?:[^"\\]|\\.)*)"\s*$', folder)
        if match:
            names.append(match.group(1).decode("ascii", errors="replace"))
    all_mail = next((name for name in names if name.casefold().endswith("/all mail") or name.casefold() == "all mail"), None)
    spam = next((name for name in names if name.casefold().endswith("/spam") or name.casefold() == "spam"), None)
    if not all_mail:
        raise RuntimeError("Gmail did not expose All Mail over IMAP; archived delivery notices cannot be checked.")
    if not spam:
        raise RuntimeError("Gmail did not expose Spam over IMAP; delivery notice coverage is incomplete.")
    return [all_mail, spam]


def _empty_thread_reply_state():
    return {"version": 1, "conversations": {}}


def _read_thread_reply_state_unlocked():
    try:
        value = _store_for(THREAD_REPLY_STATE_FILE).load_json_state(
            THREAD_REPLY_STATE_FILE, _empty_thread_reply_state()
        )
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, profile_store.ProfileStoreError) as exc:
        raise RuntimeError(f"Could not read saved conversation unread state: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("conversations"), dict):
        raise RuntimeError("Saved conversation unread state has an invalid format.")
    for conversation in value["conversations"].values():
        if not isinstance(conversation, dict) or any(
            not isinstance(conversation.get(field, []), list)
            for field in ("seen_message_ids", "unread_message_ids", "read_message_ids")
        ):
            raise RuntimeError("Saved conversation unread state has an invalid format.")
    value["version"] = 1
    return value


def _write_thread_reply_state_unlocked(value):
    try:
        _store_for(THREAD_REPLY_STATE_FILE).save_json_state(THREAD_REPLY_STATE_FILE, value)
    except profile_store.ProfileStoreError as exc:
        raise RuntimeError(f"Could not save conversation unread state: {exc}") from exc


def load_thread_reply_state():
    """Return persistent per-message unread state for the conversation inbox."""
    with thread_reply_state_lock:
        return _read_thread_reply_state_unlocked()


def _reply_activity_timestamp(value):
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0


def record_thread_reply_events(events):
    """Persist newly detected replies once and return only newly unread events."""
    added = []
    with thread_reply_state_lock:
        state = _read_thread_reply_state_unlocked()
        conversations = state["conversations"]
        changed = False
        seen_sets = {}
        read_sets = {}
        for event in events:
            email = str(event.get("email", "")).strip().lower()
            message_id = str(event.get("message_id", "")).strip()
            if not EMAIL_RE.fullmatch(email) or not message_id or len(message_id) > 512:
                continue
            conversation = conversations.setdefault(email, {})
            seen = conversation.setdefault("seen_message_ids", [])
            unread = conversation.setdefault("unread_message_ids", [])
            read = conversation.setdefault("read_message_ids", [])
            if (
                not isinstance(seen, list)
                or not isinstance(unread, list)
                or not isinstance(read, list)
            ):
                raise RuntimeError("Saved conversation unread state has an invalid format.")
            seen_ids = seen_sets.get(email)
            if seen_ids is None:
                seen_ids = set(str(value) for value in seen)
                seen_sets[email] = seen_ids
            if message_id in seen_ids:
                if (
                    event.get("reconcile_existing")
                    and conversation.get("latest_message_id") == message_id
                ):
                    received_at = str(event.get("received_at", "")).strip()
                    subject = str(event.get("subject", "")).strip()[:500]
                    if received_at:
                        if conversation.get("latest_message_at") != received_at:
                            conversation["latest_message_at"] = received_at
                            changed = True
                    if subject:
                        if conversation.get("latest_message_preview") != subject:
                            conversation["latest_message_preview"] = subject
                            changed = True
                continue

            # Replace an old one-bit reply marker with the actual message IDs as
            # the mailbox is reconciled. This preserves unread state without
            # double-counting the same legacy reply.
            legacy_id = str(conversation.get("legacy_unresolved_id", ""))
            replaced_legacy = bool(legacy_id)
            if legacy_id:
                unread[:] = [value for value in unread if str(value) != legacy_id]
                if legacy_id not in seen_ids:
                    seen.append(legacy_id)
                    seen_ids.add(legacy_id)
                conversation.pop("legacy_unresolved_id", None)

            received_at = str(event.get("received_at", "")).strip()
            if not received_at:
                received_at = datetime.now().astimezone().isoformat(timespec="seconds")
            subject = str(event.get("subject", "")).strip()[:500]
            read_ids = read_sets.get(email)
            if read_ids is None:
                read_ids = set(str(value) for value in read)
                read_sets[email] = read_ids
            already_read = message_id in read_ids
            seen.append(message_id)
            seen_ids.add(message_id)
            if already_read:
                conversation["read_message_ids"] = [
                    value for value in read if str(value) != message_id
                ]
                read_ids.discard(message_id)
            else:
                unread.append(message_id)
            if replaced_legacy or _reply_activity_timestamp(received_at) >= _reply_activity_timestamp(
                conversation.get("latest_message_at", "")
            ):
                conversation["latest_message_at"] = received_at
                conversation["latest_message_id"] = message_id
                conversation["latest_message_preview"] = subject or "New reply"
            if event.get("legacy_unresolved"):
                conversation["legacy_unresolved_id"] = message_id
            added.append({
                "email": email,
                "message_id": message_id,
                "received_at": received_at,
                "subject": subject,
            })
            changed = True
        if changed:
            _write_thread_reply_state_unlocked(state)
    return added


def seed_legacy_thread_replies(rows):
    """Migrate the previous one-reply tracker flag to persistent unread state."""
    events = []
    for row in rows:
        if row.get("followup_status") != "replied":
            continue
        email = str(row.get("email", "")).strip().lower()
        if not EMAIL_RE.fullmatch(email):
            continue
        detail = str(row.get("followup_detail", "")).strip()
        match = re.search(
            r"reply detected in saved Gmail thread \((.+)\)\s*$",
            detail,
            re.IGNORECASE,
        )
        message_id = match.group(1).strip() if match else ""
        unresolved = not message_id
        if unresolved:
            message_id = f"legacy:{email}:{row.get('followup_timestamp', '')}"
        events.append({
            "email": email,
            "message_id": message_id,
            "received_at": row.get("followup_timestamp", ""),
            "subject": row.get("subject", "") or "New reply",
            "legacy_unresolved": unresolved,
        })
    record_thread_reply_events(events)


def mark_thread_replies_read(email, message_ids):
    """Mark only messages actually present in the opened conversation as read."""
    email = str(email or "").strip().lower()
    if not EMAIL_RE.fullmatch(email):
        raise ValueError("Choose a valid conversation to mark as read.")
    read_ids = {str(value).strip() for value in message_ids if str(value).strip()}
    with thread_reply_state_lock:
        state = _read_thread_reply_state_unlocked()
        conversation = state["conversations"].setdefault(email, {})
        unread = conversation.get("unread_message_ids", [])
        seen = conversation.get("seen_message_ids", [])
        read = conversation.get("read_message_ids", [])
        if (
            not isinstance(unread, list)
            or not isinstance(seen, list)
            or not isinstance(read, list)
        ):
            raise RuntimeError("Saved conversation unread state has an invalid format.")
        before = len(unread)
        conversation["unread_message_ids"] = [
            value for value in unread if str(value) not in read_ids
        ]
        changed = len(conversation["unread_message_ids"]) != before
        seen_ids = set(str(value) for value in seen)
        read_ids_existing = set(str(value) for value in read)
        for message_id in read_ids:
            if message_id not in seen_ids and message_id not in read_ids_existing:
                read.append(message_id)
                read_ids_existing.add(message_id)
                changed = True
        legacy_id = str(conversation.get("legacy_unresolved_id", ""))
        if legacy_id and read_ids:
            changed = True
            old_unread_count = len(conversation["unread_message_ids"])
            conversation["unread_message_ids"] = [
                value for value in conversation["unread_message_ids"]
                if str(value) != legacy_id
            ]
            conversation.pop("legacy_unresolved_id", None)
            changed = changed or len(conversation["unread_message_ids"]) != old_unread_count
        remaining = len(conversation["unread_message_ids"])
        marked = before - remaining
        if changed:
            _write_thread_reply_state_unlocked(state)
        return {"marked_read": marked, "unread_count": remaining}


def check_new_thread_replies(cfg):
    """Check new Gmail UIDs and reconcile tracked threads for missed replies."""
    check_credentials(cfg)
    tracker = load_tracker_rows()
    seed_legacy_thread_replies(tracker)
    by_thread = {}
    rows_by_sender = {}
    for row in tracker:
        thread_id = str(row.get("gmail_thread_id", "")).strip()
        if (
            row.get("status") == "sent"
            and thread_id
            and row.get("delivery_status") not in ("bounced", "delayed", "needs_review")
        ):
            email = row["email"].strip().lower()
            by_thread.setdefault(thread_id, {})[email] = row
            rows_by_sender.setdefault(email, []).append(row)

    try:
        cursor_state = _store_for(GMAIL_REPLY_CURSOR_FILE).load_json_state(
            GMAIL_REPLY_CURSOR_FILE, {}
        )
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, profile_store.ProfileStoreError) as exc:
        raise RuntimeError(f"Could not read the Gmail reply cursor: {exc}") from exc
    if not isinstance(cursor_state, dict):
        raise RuntimeError("Gmail reply cursor has an invalid format.")
    stored_folders = cursor_state.get("folders", {})
    if not isinstance(stored_folders, dict):
        stored_folders = {}
    stored_message_ids = cursor_state.get("processed_message_ids", [])
    if not isinstance(stored_message_ids, list):
        stored_message_ids = []
    processed_ids = [str(value) for value in stored_message_ids if value]
    processed_id_set = set(processed_ids)
    try:
        last_reconciled = datetime.fromisoformat(
            str(cursor_state.get("last_reconciled_at", ""))
        )
        reconciliation_due = (
            datetime.now().timestamp() - last_reconciled.timestamp()
            >= GMAIL_REPLY_RECONCILE_INTERVAL_SECONDS
        )
    except (TypeError, ValueError, OverflowError):
        reconciliation_due = True
    reply_state = load_thread_reply_state()
    known_reply_ids = {
        email: set(str(value) for value in conversation.get("seen_message_ids", []))
        for email, conversation in reply_state["conversations"].items()
        if isinstance(conversation, dict)
    }
    reconciliation_groups = []
    sender_items = sorted(rows_by_sender.items())
    for offset in range(0, len(sender_items), 25):
        group = sender_items[offset:offset + 25]
        sent_dates = []
        for _, sender_rows in group:
            for row in sender_rows:
                try:
                    sent_dates.append(datetime.fromisoformat(row.get("timestamp", "")).date())
                except (TypeError, ValueError):
                    continue
        earliest = min(sent_dates) if sent_dates else (datetime.now() - timedelta(days=30)).date()
        reconciliation_groups.append((
            [email for email, _ in group],
            (earliest - timedelta(days=1)).strftime("%Y/%m/%d"),
        ))

    mailbox = imaplib.IMAP4_SSL(
        "imap.gmail.com", 993, ssl_context=ssl.create_default_context(),
        timeout=30,
    )
    next_cursors = dict(stored_folders)
    scanned = 0
    found_events = {}
    seen_message_ids = set()
    checked_folders = []

    def inspect_message(folder, uid, uid_validity):
        nonlocal scanned
        status, fetched = mailbox.uid(
            "FETCH", uid,
            "(X-GM-THRID X-GM-MSGID INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM DATE MESSAGE-ID SUBJECT)])",
        )
        if status != "OK":
            raise RuntimeError(f"Could not inspect a message in {folder}.")
        metadata = b" ".join(
            item[0] for item in fetched
            if isinstance(item, tuple) and isinstance(item[0], bytes)
        )
        thread_match = re.search(rb"X-GM-THRID\s+(\d+)", metadata, re.IGNORECASE)
        gmail_message_match = re.search(rb"X-GM-MSGID\s+(\d+)", metadata, re.IGNORECASE)
        header_bytes = next(
            (item[1] for item in fetched
             if isinstance(item, tuple) and isinstance(item[1], bytes)),
            b"",
        )
        if not thread_match or not header_bytes:
            return
        scanned += 1
        thread_id = thread_match.group(1).decode("ascii")
        candidate_rows = by_thread.get(thread_id)
        if not candidate_rows:
            return
        message = BytesParser(policy=policy.default).parsebytes(header_bytes)
        message_id = str(message.get("Message-ID", "")).strip()
        gmail_message_id = (
            gmail_message_match.group(1).decode("ascii")
            if gmail_message_match else ""
        )
        uid_text = uid.decode("ascii", errors="ignore") if isinstance(uid, bytes) else str(uid)
        identity = message_id or (
            f"gmail:{gmail_message_id}" if gmail_message_id
            else f"{folder}:{uid_validity}:{uid_text}"
        )
        if identity in seen_message_ids:
            return
        seen_message_ids.add(identity)
        senders = {
            address.lower()
            for _, address in getaddresses(message.get_all("From", []))
        }
        email = next((address for address in senders if address in candidate_rows), "")
        if not email:
            return
        try:
            sent_at = datetime.fromisoformat(candidate_rows[email].get("timestamp", ""))
        except (TypeError, ValueError):
            sent_at = None
        try:
            message_at = parsedate_to_datetime(str(message.get("Date", "")))
        except (TypeError, ValueError, OverflowError):
            message_at = None
        internal_date_match = re.search(
            rb'INTERNALDATE\s+"([^"]+)"', metadata, re.IGNORECASE
        )
        try:
            received_at_value = (
                parsedate_to_datetime(internal_date_match.group(1).decode("ascii"))
                if internal_date_match else None
            )
        except (TypeError, ValueError, OverflowError, UnicodeDecodeError):
            received_at_value = None
        reply_at = received_at_value or message_at
        if (
            reply_at and sent_at
            and reply_at.timestamp() < sent_at.timestamp() - 300
        ):
            return
        if reply_at:
            received_at = reply_at.astimezone().isoformat(timespec="seconds")
        else:
            received_at = datetime.now().astimezone().isoformat(timespec="seconds")
        found_events.setdefault((email, identity), {
            "email": email,
            "message_id": identity,
            "received_at": received_at,
            "subject": str(message.get("Subject", "")).strip(),
            "reconcile_existing": True,
        })
        if identity not in processed_id_set:
            processed_ids.append(identity)
            processed_id_set.add(identity)

    try:
        mailbox.login(cfg["email"], cfg["password"])
        folders = gmail_delivery_folders(mailbox)
        for folder in folders:
            status, status_data = mailbox.status(
                f'"{folder}"', "(UIDVALIDITY UIDNEXT)"
            )
            if status != "OK":
                raise RuntimeError(f"Could not read Gmail UID state for {folder}.")
            status_text = b" ".join(
                item for item in (status_data or []) if isinstance(item, bytes)
            )
            validity_match = re.search(rb"UIDVALIDITY\s+(\d+)", status_text)
            next_match = re.search(rb"UIDNEXT\s+(\d+)", status_text)
            if not validity_match or not next_match:
                raise RuntimeError(f"Gmail did not return UID state for {folder}.")
            uid_validity = validity_match.group(1).decode("ascii")
            uid_next = int(next_match.group(1))
            previous = stored_folders.get(folder, {})
            try:
                previous_uid = int(previous.get("last_uid", 0))
            except (AttributeError, TypeError, ValueError):
                previous_uid = 0
            if not isinstance(previous, dict) or previous.get("uid_validity") != uid_validity:
                # The targeted Gmail thread reconciliation below covers existing
                # conversations; start the UID cursor at the current edge instead
                # of walking every unrelated mailbox message on first setup.
                previous_uid = max(uid_next - 1, 0)
                reconciliation_due = True

            status, _ = mailbox.select(f'"{folder}"', readonly=True)
            if status != "OK":
                raise RuntimeError(f"Could not open Gmail's {folder} folder over IMAP.")
            upper_uid = max(uid_next - 1, 0)
            if previous_uid < upper_uid:
                status, search_data = mailbox.uid(
                    "SEARCH", None, "UID", f"{previous_uid + 1}:{upper_uid}"
                )
                if status != "OK":
                    raise RuntimeError(f"Could not search new messages in {folder}.")
                candidate_uids = search_data[0].split() if search_data and search_data[0] else []
            else:
                candidate_uids = []

            scan_uids = set(candidate_uids)
            if reconciliation_due and reconciliation_groups:
                for emails, after_date in reconciliation_groups:
                    from_filter = "{" + " ".join(
                        f"from:{email}" for email in emails
                    ) + "}"
                    query = f"after:{after_date} {from_filter}"
                    status, search_data = mailbox.uid(
                        "SEARCH", None, "X-GM-RAW", f'"{query}"'
                    )
                    if status != "OK":
                        raise RuntimeError(
                            f"Could not reconcile tracked Gmail threads in {folder}."
                        )
                    if search_data and search_data[0]:
                        scan_uids.update(search_data[0].split())

            for uid in sorted(scan_uids, key=lambda value: int(value)):
                inspect_message(folder, uid, uid_validity)
            checked_folders.append(folder)
            next_cursors[folder] = {
                "uid_validity": uid_validity,
                "last_uid": upper_uid,
            }
    finally:
        try:
            mailbox.logout()
        except (imaplib.IMAP4.error, OSError):
            pass

    new_events = [
        event for event in found_events.values()
        if event["message_id"] not in known_reply_ids.get(event["email"], set())
    ]
    latest_event_by_email = {}
    for event in new_events:
        previous_event = latest_event_by_email.get(event["email"])
        if (
            previous_event is None
            or _reply_activity_timestamp(event["received_at"])
            >= _reply_activity_timestamp(previous_event["received_at"])
        ):
            latest_event_by_email[event["email"]] = event
    for email, event in latest_event_by_email.items():
        write_log(
            email, "replied",
            f"reply detected in saved Gmail thread ({event['message_id']})",
            event_at=event["received_at"],
        )
    added_events = record_thread_reply_events(found_events.values())

    next_cursor_state = {
        "folders": next_cursors,
        "processed_message_ids": processed_ids[-5000:],
    }
    if reconciliation_due:
        next_cursor_state["last_reconciled_at"] = datetime.now().astimezone().isoformat(
            timespec="seconds"
        )
    elif cursor_state.get("last_reconciled_at"):
        next_cursor_state["last_reconciled_at"] = cursor_state["last_reconciled_at"]
    try:
        _store_for(GMAIL_REPLY_CURSOR_FILE).save_json_state(
            GMAIL_REPLY_CURSOR_FILE, next_cursor_state
        )
    except profile_store.ProfileStoreError as exc:
        raise RuntimeError(f"Could not save the Gmail reply cursor: {exc}") from exc

    updated = {event["email"] for event in added_events}
    return [
        "Gmail reply check with tracked-thread reconciliation",
        f"Folders checked: {', '.join(checked_folders)}",
        f"Messages inspected: {scanned:,}",
        f"New reply messages: {len(added_events):,}",
        f"Replies detected: {len(added_events):,}",
        *[f"Reply detected: {email}" for email in sorted(updated)],
    ]


def delivery_notification(message):
    sender_addresses = {
        address.lower()
        for _, address in getaddresses(message.get_all("From", []))
    }
    sender_text = " ".join(sender_addresses)
    subject = str(message.get("Subject", ""))
    if not (
        re.search(r"(?:mailer-daemon|postmaster)", sender_text, re.IGNORECASE)
        and re.search(
            r"(?:delivery status notification|undelivered|delivery failure|"
            r"failure notice|returned mail|mail delivery failed)",
            subject,
            re.IGNORECASE,
        )
    ):
        return None

    recipient_headers = []
    body_parts = []
    status_codes = []
    for part in message.walk():
        for header in ("Final-Recipient", "Original-Recipient", "X-Failed-Recipients"):
            recipient_headers.extend(part.get_all(header, []))
        status_codes.extend(part.get_all("Status", []))
        if part.get_content_maintype() == "text":
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or "utf-8"
                body_parts.append(payload.decode(charset, errors="replace"))

    recipients = set()
    for value in recipient_headers:
        recipient_value = str(value).rsplit(";", 1)[-1].strip(" <>")
        for _, address in getaddresses([recipient_value]):
            address = address.lower()
            if EMAIL_RE.fullmatch(address):
                recipients.add(address)

    body = "\n".join(body_parts)
    body = re.sub(r"<[^>]*>", " ", body)
    if not recipients:
        delivered_match = re.search(
            r"(?:wasn(?:'|&#39;|&#x27;)t delivered to|was not delivered to|"
            r"delivery to|failed to deliver to)\s*<?"
            r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})",
            body,
            re.IGNORECASE,
        )
        if delivered_match:
            recipients.add(delivered_match.group(1).lower())

    if not recipients:
        return None

    status_text = " ".join(str(value) for value in status_codes)
    if not status_text:
        status_match = re.search(r"\b([245]\.\d{1,3}\.\d{1,3})\b", body)
        status_text = status_match.group(1) if status_match else ""
    searchable = f"{subject}\n{body}".lower()
    hard_bounce = bool(
        re.search(r"\b5\.1\.1\b", status_text)
        or re.search(
            r"(?:address not found|address couldn['’]?t be found|"
            r"user unknown|unknown user|mailbox does not exist)",
            searchable,
        )
    )
    if hard_bounce:
        outcome = "bounced"
    elif re.search(r"\b4\.\d{1,3}\.\d{1,3}\b", status_text):
        outcome = "delayed"
    else:
        outcome = "needs_review"

    return {
        "recipients": recipients,
        "status": outcome,
        "status_code": status_text,
        "subject": subject,
        "message_id": str(message.get("Message-ID", "")),
        "detail": re.sub(r"\s+", " ", body).strip()[:500],
    }


def update_delivery_status(email, status, detail):
    rows = load_tracker_rows()
    row = next((entry for entry in rows if entry["email"] == email.lower()), None)
    if row is None:
        raise ValueError(f"No tracker row exists for {email}.")
    if row["delivery_status"] == "bounced" and status != "bounced":
        return False
    try:
        previous_detail = json.loads(row.get("delivery_detail") or "{}")
    except (TypeError, json.JSONDecodeError):
        previous_detail = {}
    if not isinstance(previous_detail, dict):
        previous_detail = {}
    previous_notice_id = str(previous_detail.get("message_id") or "").strip()
    current_notice_id = str(detail.get("message_id") or "").strip()
    if row.get("delivery_status") == status:
        if previous_notice_id and current_notice_id:
            same_notice = previous_notice_id == current_notice_id
        else:
            stable_keys = ("subject", "status_code", "detail")
            same_notice = all(
                str(previous_detail.get(key, "")) == str(detail.get(key, ""))
                for key in stable_keys
            )
        if same_notice:
            return False
    row["delivery_status"] = status
    row["delivery_updated_at"] = datetime.now().isoformat(timespec="seconds")
    row["delivery_detail"] = json.dumps(detail)
    if status in ("bounced", "delayed", "needs_review"):
        row["followup_opt_in"] = "no"
    save_tracker_rows(rows)
    return True


def check_delivery_failures(cfg, dry_run=False, lookback_days=30,
                            mailbox_since=None):
    cutoff = datetime.now().astimezone() - timedelta(days=lookback_days)
    sent_rows = []
    sent_times = {}
    for row in load_tracker_rows():
        if row["status"] not in ("sent", "unknown"):
            continue
        if row["delivery_status"] == "bounced":
            continue
        sent_at = datetime.fromisoformat(row["timestamp"]).astimezone()
        if sent_at >= cutoff:
            sent_rows.append(row)
            sent_times[row["email"]] = sent_at
    if not sent_rows:
        check_credentials(cfg)
        mailbox = imaplib.IMAP4_SSL(
            "imap.gmail.com", 993, ssl_context=ssl.create_default_context(),
            timeout=20,
        )
        try:
            mailbox.login(cfg["email"], cfg["password"])
        finally:
            try:
                mailbox.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
        print_heading("Delivery failure check")
        print("  Gmail IMAP connection verified; no sent messages need a delivery-failure check.")
        return
    check_credentials(cfg)
    lookup = {row["email"]: row for row in sent_rows}
    earliest = min(sent_times.values())
    earliest_mail_date = earliest.date() - timedelta(days=1)
    if mailbox_since:
        requested_mail_date = datetime.strptime(mailbox_since, "%Y-%m-%d").date()
        since_date = max(earliest_mail_date, requested_mail_date)
    else:
        since_date = earliest_mail_date
    since = since_date.strftime("%d-%b-%Y")
    notifications = {}
    scanned = 0
    seen_notice_ids = set()
    mailbox = imaplib.IMAP4_SSL(
        "imap.gmail.com", 993, ssl_context=ssl.create_default_context(),
        timeout=20,
    )
    try:
        mailbox.login(cfg["email"], cfg["password"])
        folders = gmail_delivery_folders(mailbox)
        for folder in folders:
            status, _ = mailbox.select(f'"{folder}"', readonly=True)
            if status != "OK":
                raise RuntimeError(f"Could not open Gmail's {folder} folder over IMAP.")
            status, data = mailbox.uid("SEARCH", None, "SINCE", since)
            if status != "OK":
                raise RuntimeError(f"Could not search Gmail's {folder} folder.")
            for uid in (data[0].split() if data and data[0] else []):
                status, fetched = mailbox.uid(
                    "FETCH", uid,
                    "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)])",
                )
                if status != "OK":
                    raise RuntimeError(
                        f"Could not inspect a message in Gmail's {folder} folder."
                    )
                headers = [
                    BytesParser(policy=policy.default).parsebytes(item[1])
                    for item in fetched if isinstance(item, tuple)
                ]
                header = next(iter(headers), None)
                if header is None:
                    continue
                sender = " ".join(header.get_all("From", []))
                subject = str(header.get("Subject", ""))
                if not (
                    re.search(r"(?:mailer-daemon|postmaster)", sender, re.IGNORECASE)
                    and re.search(
                        r"(?:delivery status notification|undelivered|"
                        r"delivery failure|failure notice|returned mail|"
                        r"mail delivery failed)",
                        subject,
                        re.IGNORECASE,
                    )
                ):
                    continue
                status, fetched = mailbox.uid("FETCH", uid, "(BODY.PEEK[])")
                if status != "OK":
                    raise RuntimeError(
                        f"Could not read a delivery notice in Gmail's {folder} folder."
                    )
                scanned += 1
                for item in fetched:
                    if not isinstance(item, tuple):
                        continue
                    message = BytesParser(policy=policy.default).parsebytes(item[1])
                    notice_id = str(message.get("Message-ID", "")).strip()
                    if notice_id and notice_id in seen_notice_ids:
                        continue
                    if notice_id:
                        seen_notice_ids.add(notice_id)
                    notice = delivery_notification(message)
                    if not notice:
                        continue
                    notification_date = parsedate_to_datetime(
                        str(message.get("Date", ""))
                    )
                    for email in notice["recipients"] & lookup.keys():
                        if notification_date.astimezone() < (
                            sent_times[email] - timedelta(minutes=5)
                        ):
                            continue
                        previous = notifications.get(email)
                        if previous and previous["status"] == "bounced":
                            continue
                        notice["folder"] = folder
                        notifications[email] = notice
    finally:
        try:
            mailbox.logout()
        except (imaplib.IMAP4.error, OSError):
            pass

    print_heading("Delivery failure check")
    print_summary([
        ("Folders checked", ", ".join(folders)),
        ("Delivery notices inspected", f"{scanned:,}"),
        ("Recipients matched", f"{len(notifications):,}"),
    ])
    if dry_run:
        for email, notice in sorted(notifications.items()):
            print(f"  {notice['status'].upper():<12} {email} ({notice['folder']})")
        print("\n  Dry run: tracker and blocked-address records were not changed.")
        return

    updated = 0
    bounced_addresses = set()
    for email, notice in notifications.items():
        detail = {
            "folder": notice["folder"],
            "subject": notice["subject"],
            "message_id": notice["message_id"],
            "status_code": notice["status_code"],
            "detail": notice["detail"],
        }
        if update_delivery_status(email, notice["status"], detail):
            updated += 1
        if notice["status"] == "bounced":
            bounced_addresses.add(email)
        print(f"  {notice['status'].upper():<12} {email} ({notice['folder']})")
    removed = suppress_addresses(bounced_addresses)
    print(
        f"\n  Tracker updated: {updated:,} · hard bounces blocked: "
        f"{len(bounced_addresses):,} · contacts removed: {removed:,}."
    )


def build_followup_message(
        contact, sender, sender_name, subject, message_id, subject_t, body_t):
    msg = EmailMessage()
    values = {
        "subject": subject,
        "name": contact.get("name", "") or sender_name,
        "first_name": contact_first_name(contact),
        "company": contact.get("company", "your company").strip(" ,") or "your company",
        "title": contact.get("title", ""),
    }
    rendered_subject = subject_t.format_map(SafeDict(values))
    msg["Subject"] = (
        rendered_subject if rendered_subject.lower().startswith("re:")
        else f"Re: {rendered_subject}"
    )
    msg["From"] = formataddr((sender_name, sender))
    msg["To"] = formataddr((contact.get("name", ""), contact["email"]))
    msg["Reply-To"] = sender
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[1])
    msg["In-Reply-To"] = message_id
    msg["References"] = message_id
    msg["List-Unsubscribe"] = f"<mailto:{sender}?subject=unsubscribe>"
    msg.set_content(body_t.format_map(SafeDict(values)))
    return msg


def sync_gmail_threads(cfg, contacts):
    check_credentials(cfg)
    contact_by_email = {contact["email"]: contact for contact in contacts}
    _, subject_template, _ = load_template()
    candidates = []
    for row in load_tracker_rows():
        if row["status"] not in ("sent", "unknown") or row["gmail_thread_id"]:
            continue
        email = row["email"]
        if row["status"] == "unknown" and not row["gmail_message_id"]:
            continue
        contact = contact_by_email.get(email)
        if not contact and not row["gmail_message_id"]:
            continue
        candidates.append((email, {
            "message_id": row["gmail_message_id"],
            "subject": row["subject"] or (
                personalise(subject_template, contact) if contact else ""
            ),
            "timestamp": row["timestamp"],
        }))

    if not candidates:
        print_heading("Gmail thread sync")
        print("  No sent messages need thread sync; no unknown send can be reconciled.")
        return

    try:
        threads = find_gmail_thread_ids(cfg, candidates)
    except (imaplib.IMAP4.error, OSError, RuntimeError) as exc:
        sys.exit(f"Could not sync Gmail thread IDs over IMAP: {exc}")

    for email, thread in threads.items():
        update_message_details(email, {
            "message_id": thread["message_id"],
            "subject": thread["subject"],
            "gmail_thread_id": thread["thread_id"],
        })
    rows = load_tracker_rows()
    confirmed_unknown = 0
    for row in rows:
        if row["email"] in threads and row["status"] == "unknown":
            row["status"] = "sent"
            row["delivery_status"] = "awaiting_check"
            row["delivery_updated_at"] = datetime.now().isoformat(timespec="seconds")
            confirmed_unknown += 1
    if confirmed_unknown:
        save_tracker_rows(rows)

    print_heading("Gmail thread sync")
    print_summary([
        ("Checked", f"{len(candidates):,}"),
        ("Thread IDs saved", f"{len(threads):,}"),
        ("Unknown sends confirmed in Sent", f"{confirmed_unknown:,}"),
        ("Not matched", f"{len(candidates) - len(threads):,}"),
    ])
    if len(threads) < len(candidates):
        print("  Unmatched rows were left unchanged; ambiguous matches are not guessed.")


def send_followups(
    cfg, contacts, sender_name, dry_run=False, dashboard_control=False,
):
    contact_by_email = {contact["email"]: contact for contact in contacts}
    subject_template, body_template = load_followup_template()
    _, original_subject_template, _ = load_template()
    suppressed = load_suppressed()
    states = load_followup_states()
    now = datetime.now()
    due_candidates = []
    for email, record in states.items():
        if record["status"] not in ("sent", "followup_sent") or email in suppressed:
            continue
        if record["followup_count"] >= cfg["max_followup_weeks"]:
            continue
        try:
            sent_at = datetime.fromisoformat(record["timestamp"])
        except ValueError:
            print(f"Skipping follow-up for {email}: invalid sent timestamp.")
            continue
        interval = timedelta(days=cfg["followup_interval_days"])
        next_due = sent_at + interval * (record["followup_count"] + 1)
        if record["followup_timestamp"]:
            try:
                previous_followup = datetime.fromisoformat(
                    record["followup_timestamp"]
                )
            except ValueError:
                print(f"Skipping follow-up for {email}: invalid follow-up timestamp.")
                continue
            next_due = max(next_due, previous_followup + interval)
        if now < next_due:
            continue
        if email in contact_by_email:
            if not record["subject"]:
                record["subject"] = personalise(
                    original_subject_template, contact_by_email[email]
                )
            due_candidates.append((email, record))

    missing_thread_ids = [
        (email, record) for email, record in due_candidates if not record["thread_id"]
    ]
    if missing_thread_ids:
        check_credentials(cfg)
        try:
            resolved = find_gmail_thread_ids(cfg, missing_thread_ids)
        except (imaplib.IMAP4.error, OSError, RuntimeError) as exc:
            sys.exit(f"Could not look up Gmail thread IDs over IMAP: {exc}")
        for email, thread in resolved.items():
            update_message_details(email, {
                "message_id": thread["message_id"],
                "subject": thread["subject"],
                "gmail_thread_id": thread["thread_id"],
            })
            states[email].update({
                "message_id": thread["message_id"],
                "subject": thread["subject"],
                "thread_id": thread["thread_id"],
            })

    due = [(email, record) for email, record in due_candidates if record["thread_id"]]
    missing_thread_ids_count = len(due_candidates) - len(due)
    if missing_thread_ids_count:
        print(f"Note: {missing_thread_ids_count} message(s) have no discoverable Gmail "
              "thread ID and were skipped to avoid an unthreaded follow-up.")
    if not due:
        print_heading("Follow-up check")
        print("  No follow-ups are due.")
        print(
            "  A follow-up becomes eligible after "
            f"{cfg['followup_interval_days']} days, if no reply was detected."
        )
        return

    sent_today = sum(
        1 for row in load_tracker_rows()
        if row["followup_status"] == "followup_sent"
        and row["followup_timestamp"].startswith(now.date().isoformat())
    )
    slots = max(cfg["per_day"] - sent_today, 0)
    check_credentials(cfg)
    try:
        reply_check = check_new_thread_replies(cfg)
        due_emails = {email for email, _ in due}
        replied = {
            line.split(":", 1)[1].strip().lower()
            for line in reply_check
            if line.startswith("Reply detected:")
            and line.split(":", 1)[1].strip().lower() in due_emails
        }
    except (imaplib.IMAP4.error, OSError, RuntimeError) as exc:
        sys.exit(f"Could not check Gmail replies over IMAP: {exc}")

    print_heading("Follow-up check")
    print_summary([
        ("Eligible to check", f"{len(due):,}"),
        ("Replies detected", f"{len(replied):,}"),
        ("Daily follow-up slots", f"{slots:,}"),
    ])

    for email in sorted(replied):
        states[email]["status"] = "replied"
    for line in reply_check:
        if line.startswith("Reply detected:"):
            print(line)

    pending = [(email, record) for email, record in due if email not in replied]
    print(f"\nNo reply detected: {len(pending):,}")
    if dry_run:
        for i, (email, record) in enumerate(pending[:slots], 1):
            print_contact_row(i, contact_by_email[email], f"Re: {record['subject']}")
        if len(pending) > slots:
            print(f"\n  {len(pending) - slots:,} follow-up(s) deferred by today's limit.")
        print("\n  Dry run: no email was sent.")
        return
    if slots == 0:
        print("  Daily limit reached; eligible follow-ups remain queued.")
        return

    server = None
    sent_now = 0
    followup_batch = pending[:slots]
    try:
        server = connect(cfg)
        for index, (email, record) in enumerate(followup_batch, 1):
            if dashboard_control:
                wait_if_campaign_paused()
            contact = contact_by_email[email]
            message = build_followup_message(
                contact, cfg["email"], sender_name, record["subject"],
                record["last_message_id"] or record["message_id"],
                subject_template, body_template
            )
            if dashboard_control:
                wait_if_campaign_paused()
            try:
                server.send_message(message)
            except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused,
                    smtplib.SMTPDataError) as exc:
                write_log(email, "followup_error", str(exc)[:200])
                print(f"  [{index:>2}/{len(followup_batch)}] FAILED  {email} - {exc}")
                continue
            except (smtplib.SMTPServerDisconnected, OSError) as exc:
                write_log(email, "followup_unknown", str(exc)[:200])
                print(f"  [{index:>2}/{len(followup_batch)}] CHECK   {email} - delivery uncertain; check Gmail Sent before retrying.")
                break
            write_log(email, "followup_sent", json.dumps({
                "message_id": message["Message-ID"],
                "in_reply_to": record["last_message_id"] or record["message_id"],
            }))
            sent_now += 1
            print(f"  [{index:>2}/{len(followup_batch)}] SENT    {email}")
            if dashboard_control:
                wait_if_campaign_paused()
    except smtplib.SMTPAuthenticationError:
        sys.exit("Gmail login failed. Use a valid Google App Password.")
    finally:
        if server:
            try:
                server.quit()
            except (smtplib.SMTPException, OSError):
                pass
    print(f"\nFollow-ups sent: {sent_now:,}")


def load_contacts():
    seen, out = set(), []
    _, raw_rows = load_raw_contacts()
    for source in raw_rows:
        row = {
            str(key).strip().lower(): (value or "").strip()
            for key, value in source.items() if key
        }
        addr = row.get("email", "").lower()
        if not EMAIL_RE.match(addr) or addr in seen:
            continue
        if addr.split("@")[0] in ROLE_PREFIXES:
            continue
        seen.add(addr)
        row["email"] = addr
        out.append(row)
    return out


def load_template():
    if not TEMPLATE_FILE.exists():
        sys.exit("template.txt not found.")
    text = TEMPLATE_FILE.read_text(encoding="utf-8").replace("\r\n", "\n")
    head, _, body = text.partition("\n\n")
    headers = {}
    for line in head.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    if "subject" not in headers:
        sys.exit("template.txt must start with a 'Subject: ...' line, then a blank line.")
    return headers.get("name", ""), headers["subject"], body.strip()


def load_followup_template():
    if not FOLLOWUP_TEMPLATE_FILE.exists():
        return (
            "Re: {subject}",
            "Hi {first_name},\n\n"
            "I wanted to politely follow up on my note about opportunities at {company}. "
            "If there may be a suitable opportunity, or someone else I should contact, "
            "I'd be grateful for your guidance. If now isn't a good time, no worries.\n\n"
            "Best regards,\n{name}\n\n"
            "--\nIf you'd rather not hear from me, just reply \"unsubscribe\" "
            "and I won't email you again.",
        )
    text = FOLLOWUP_TEMPLATE_FILE.read_text(encoding="utf-8").replace("\r\n", "\n")
    head, separator, body = text.partition("\n\n")
    if not separator or not head.lower().startswith("subject:"):
        sys.exit("followup_template.txt must start with 'Subject: ...', then a blank line.")
    subject = head.split(":", 1)[1].strip()
    if not subject:
        sys.exit("followup_template.txt has an empty Subject.")
    return subject, body.strip()


def find_resume():
    files = sorted(p for p in RESUME_DIR.glob("*") if p.suffix.lower() in RESUME_EXTS)
    if not files:
        sys.exit("No resume found. Put your resume (.pdf, .doc, or .docx) in data/resume/.")
    if RESUME_SELECTION_FILE.exists():
        selected = RESUME_DIR / RESUME_SELECTION_FILE.read_text(encoding="utf-8").strip()
        if selected in files:
            files = [selected]
    if len(files) > 1:
        print(f"Note: several files in data/resume/. Using '{files[0].name}'.")
    path = files[0]
    ctype, _ = mimetypes.guess_type(path.name)
    maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
    return path.name, path.read_bytes(), maintype, subtype


# -------------------------------------------------------------- message
class SafeDict(dict):
    def __missing__(self, key):
        return ""


def contact_first_name(contact):
    name = contact.get("name", "").strip()
    if not name or name.casefold() == "unknown":
        return "there"
    return name.split()[0].title()


def personalise(text, contact):
    name = contact.get("name", "")
    first = contact_first_name(contact)
    return text.format_map(SafeDict(
        name=name, first_name=first,
        company=contact.get("company", "your company").strip(" ,") or "your company",
        title=contact.get("title", ""),
    ))


class EmailHTMLSanitizer(HTMLParser):
    allowed_tags = {
        "b", "blockquote", "br", "div", "em", "i", "li", "ol", "p", "span", "strong", "u", "ul",
    }
    block_tags = {"blockquote", "div", "li", "p"}
    allowed_fonts = {
        "Arial, sans-serif", "Georgia, serif", "Verdana, sans-serif",
        "Tahoma, sans-serif", "'Times New Roman', serif",
        '"Times New Roman", serif', "'Courier New', monospace",
        '"Courier New", monospace',
        "Calibri, sans-serif", "Cambria, serif", "Helvetica, sans-serif",
        "'Trebuchet MS', sans-serif", "'Garamond', serif",
        "'Palatino Linotype', serif",
    }
    allowed_sizes = {"10px", "12px", "13px", "14px", "16px", "18px",
                     "20px", "24px", "26px", "32px"}
    blocked_content_tags = {"iframe", "object", "script", "style", "svg", "template"}
    void_tags = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output = []
        self.plain = []
        self.stack = []
        self.suppressed = []
        self.quote_depth = 0

    def _safe_style(self, raw_style):
        styles = []
        for declaration in raw_style.split(";"):
            name, separator, value = declaration.partition(":")
            if not separator:
                continue
            name, value = name.strip().lower(), value.strip()
            if name == "font-family" and value in self.allowed_fonts:
                styles.append(f"font-family:{value}")
            elif name == "font-size" and value in self.allowed_sizes:
                styles.append(f"font-size:{value}")
            elif name == "white-space" and value == "pre-wrap":
                styles.append("white-space:pre-wrap")
            elif name == "color" and re.fullmatch(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?", value):
                styles.append(f"color:{value.lower()}")
        return ";".join(styles)

    def handle_starttag(self, tag, attrs):
        if self.suppressed:
            if tag not in self.void_tags:
                self.suppressed.append(tag)
            return
        if tag in self.blocked_content_tags:
            self.suppressed.append(tag)
            return
        if tag not in self.allowed_tags:
            return
        if tag == "br":
            self.output.append("<br>")
            self.plain.append("\n")
            return
        safe_attrs = ""
        if tag == "div":
            style = self._safe_style(dict(attrs).get("style", ""))
            if style:
                safe_attrs = f' style="{html.escape(style, quote=True)}"'
        self.output.append(f"<{tag}{safe_attrs}>")
        self.stack.append(tag)
        if tag in self.block_tags and self.plain and not self.plain[-1].endswith("\n"):
            self.plain.append("\n")
        if tag == "blockquote":
            self.quote_depth += 1

    def handle_endtag(self, tag):
        if self.suppressed:
            if tag in self.suppressed:
                while self.suppressed:
                    if self.suppressed.pop() == tag:
                        break
            return
        if tag not in self.stack:
            return
        while self.stack:
            current = self.stack.pop()
            self.output.append(f"</{current}>")
            if current == "blockquote":
                self.quote_depth = max(0, self.quote_depth - 1)
            if current == tag:
                break
        if tag in self.block_tags and (not self.plain or not self.plain[-1].endswith("\n")):
            self.plain.append("\n")

    def handle_data(self, data):
        if self.suppressed:
            return
        self.output.append(html.escape(data))
        if not self.quote_depth:
            self.plain.append(data)
            return
        for part in re.split(r"(\r\n|\r|\n)", data):
            if not part:
                continue
            if part in ("\r", "\n", "\r\n"):
                self.plain.append("\n")
                continue
            if not self.plain or self.plain[-1].endswith(("\n", "\r")):
                self.plain.append(">" * self.quote_depth + " ")
            self.plain.append(part)

    def close(self):
        super().close()
        while self.stack:
            self.output.append(f"</{self.stack.pop()}>")


def sanitize_email_html(value):
    if len(value) > 100000:
        raise ValueError("Formatted message markup must be 100,000 characters or fewer.")
    parser = EmailHTMLSanitizer()
    parser.feed(value)
    parser.close()
    return "".join(parser.output), "".join(parser.plain).strip()


def personalise_html(text, contact):
    name = contact.get("name", "")
    first = contact_first_name(contact)
    values = {
        "name": name,
        "first_name": first,
        "company": contact.get("company", "your company").strip(" ,") or "your company",
        "title": contact.get("title", ""),
    }
    return text.format_map(SafeDict({
        key: html.escape(value, quote=True) for key, value in values.items()
    }))


def build_message_body(body_template, contact):
    body = personalise(body_template, contact)
    return (
        body + "\n\n--\nIf you'd rather not hear from me, just reply "
        "\"unsubscribe\" and I won't email you again."
    )


def build_html_message_body(body_template, contact):
    return (
        personalise_html(body_template, contact)
        + "<br><br><p><small>--<br>If you'd rather not hear from me, just reply "
        + "&quot;unsubscribe&quot; and I won't email you again.</small></p>"
    )


def build_message(contact, sender, sender_name, subject_t, body_t, resume,
                  html_body=None, attachments=()):
    msg = EmailMessage()
    msg["Subject"] = personalise(subject_t, contact)
    msg["From"] = formataddr((sender_name, sender))
    msg["To"] = formataddr((contact.get("name", ""), contact["email"]))
    msg["Reply-To"] = sender
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[1])
    msg["List-Unsubscribe"] = f"<mailto:{sender}?subject=unsubscribe>"
    body = build_message_body(body_t, contact)
    msg.set_content(body)
    if html_body:
        msg.add_alternative(build_html_message_body(html_body, contact), subtype="html")
    fname, data, maintype, subtype = resume
    msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=fname)
    for filename, content, attachment_type, attachment_subtype in attachments:
        msg.add_attachment(
            content, maintype=attachment_type, subtype=attachment_subtype,
            filename=filename,
        )
    return msg


# ----------------------------------------------------------------- smtp
def connect(cfg, timeout=60):
    ctx = ssl.create_default_context()
    server = smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=timeout)
    server.login(cfg["email"], cfg["password"])
    return server


def alive(server):
    try:
        return server is not None and server.noop()[0] == 250
    except Exception:
        return False


def read_campaign_controls():
    """Read dashboard pause/resume commands from the child process stdin."""
    for line in sys.stdin:
        command = line.strip().upper()
        if command == "PAUSE":
            campaign_pause_requested.set()
        elif command == "RESUME":
            campaign_pause_requested.clear()
            print("\n__CAMPAIGN_RESUME_ACCEPTED__", flush=True)


def wait_if_campaign_paused():
    if not campaign_pause_requested.is_set():
        return 0.0
    paused_at = time.monotonic()
    print("\n__CAMPAIGN_PAUSED__", flush=True)
    while campaign_pause_requested.is_set():
        time.sleep(0.25)
    paused_for = time.monotonic() - paused_at
    print("__CAMPAIGN_RESUMED__", flush=True)
    return paused_for


def wait(seconds):
    end = time.monotonic() + seconds
    while True:
        end += wait_if_campaign_paused()
        left = max(0, math.ceil(end - time.monotonic()))
        print(
            f"\r  Next email in {left // 60:02d}:{left % 60:02d} ",
            end="", flush=True,
        )
        if left == 0:
            break
        time.sleep(min(1, max(0, end - time.monotonic())))
    print("\r" + " " * 40 + "\r", end="")


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Careful daily cold-email sender")
    ap.add_argument("--account-dir", help=argparse.SUPPRESS)
    ap.add_argument("--dry-run", action="store_true", help="preview, send nothing")
    ap.add_argument("--test", metavar="EMAIL", help="send one test email to this address")
    ap.add_argument("--suppress", metavar="EMAIL", help="never email this address")
    ap.add_argument("--status", action="store_true", help="show progress")
    ap.add_argument("--follow-ups", action="store_true",
                    help="check for replies and send due one-week follow-ups")
    ap.add_argument("--sync-threads", action="store_true",
                    help="look up and save Gmail thread IDs; sends nothing")
    ap.add_argument("--check-deliveries", action="store_true",
                    help="check recent sent messages for delivery failure notices")
    ap.add_argument("--lookback-days", type=int, default=30,
                    help="delivery-check window in days (default: 30; maximum: 90)")
    ap.add_argument("--mailbox-since", help=argparse.SUPPRESS)
    ap.add_argument("--dashboard-control", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.account_dir:
        configure_profile_directory(args.account_dir)

    if args.dashboard_control:
        threading.Thread(target=read_campaign_controls, daemon=True).start()

    if args.suppress:
        add_suppressed(args.suppress)
        return

    cfg = load_config()
    name, subject_t, body_t = load_template()
    sender_name = name or cfg["email"].split("@")[0]
    contacts = load_contacts()
    done, sent_today = load_log()
    suppressed = load_suppressed()
    queue = [c for c in contacts if c["email"] not in done and c["email"] not in suppressed]
    remaining_today = max(cfg["per_day"] - sent_today, 0)

    tracker = build_tracker_state(contacts, done, suppressed)
    if args.status:
        days = -(-len(queue) // max(cfg["per_day"], 1))
        next_contact = tracker["next_email"] or "None"
        last_email = tracker["last_email"] or "None"
        last_time = tracker["last_time"] or "n/a"
        print_heading("Outreach status")
        print_summary([
            ("Total contacts", f"{len(contacts):,}"),
            ("Already emailed", f"{len(done):,}"),
            ("Contacts remaining", f"{len(queue):,}"),
            ("Sent today", f"{sent_today:,} / {cfg['per_day']:,}"),
            ("Estimated time left", f"about {days:,} days at {cfg['per_day']:,}/day"),
            ("Next recipient", next_contact),
            ("Last successful send", f"{last_email} ({last_time})"),
        ])
        return

    if args.follow_ups:
        send_followups(
            cfg, contacts, sender_name, dry_run=args.dry_run,
            dashboard_control=args.dashboard_control,
        )
        return

    if args.check_deliveries:
        if not 1 <= args.lookback_days <= 90:
            ap.error("--lookback-days must be between 1 and 90")
        if args.mailbox_since:
            try:
                datetime.strptime(args.mailbox_since, "%Y-%m-%d")
            except ValueError:
                ap.error("--mailbox-since must be an ISO date (YYYY-MM-DD)")
        try:
            check_delivery_failures(
                cfg, dry_run=args.dry_run, lookback_days=args.lookback_days,
                mailbox_since=args.mailbox_since,
            )
        except (imaplib.IMAP4.error, OSError, RuntimeError, ValueError) as exc:
            sys.exit(f"Could not check Gmail delivery failures over IMAP: {exc}")
        return

    if args.sync_threads:
        sync_gmail_threads(cfg, contacts)
        return

    resume = find_resume()

    if args.test:
        check_credentials(cfg)
        sample = dict(contacts[0]) if contacts else {"name": "Test User", "company": "Test Co"}
        sample["email"] = args.test
        print_heading("Sending test email")
        print_summary([
            ("Recipient", args.test),
            ("Attachment", resume[0]),
            ("Status", "Sending"),
        ])
        with connect(cfg) as s:
            s.send_message(build_message(sample, cfg["email"], sender_name,
                                         subject_t, body_t, resume))
        print("\n  SUCCESS - test email sent.")
        print("  Check Inbox or Spam and confirm the attachment.")
        return

    batch = queue[:remaining_today]
    print_heading("Today's outreach")
    print_summary([
        ("Contacts remaining", f"{len(queue):,}"),
        ("Sent today", f"{sent_today:,} / {cfg['per_day']:,}"),
        ("Emails in this batch", f"{len(batch):,}"),
        ("Resume attachment", resume[0]),
        ("Next recipient", queue[0]["email"] if queue else "None"),
    ])

    if args.dry_run:
        if batch:
            print("\nPreview")
        for i, c in enumerate(batch[:3], 1):
            m = build_message(c, cfg["email"], sender_name, subject_t, body_t, resume)
            print_contact_row(i, c, m["Subject"])
        if len(batch) > 3:
            print(f"  ... and {len(batch) - 3:,} more")
        print("\n  Dry run: no email was sent.")
        return

    check_credentials(cfg)
    if not batch:
        print("\n  Nothing to send - daily limit reached or all contacts are done.")
        return

    print("\nSending")
    server, bounces, sent_now = None, 0, 0
    sent_message_records = []
    try:
        for i, c in enumerate(batch, 1):
            wait_if_campaign_paused()
            if not alive(server):
                server = connect(cfg)
            msg = build_message(c, cfg["email"], sender_name, subject_t, body_t, resume)
            wait_if_campaign_paused()
            try:
                server.send_message(msg)
                write_log(c["email"], "sent", json.dumps({
                    "message_id": msg["Message-ID"],
                    "subject": msg["Subject"],
                }))
                sent_message_records.append((c["email"], {
                    "message_id": msg["Message-ID"],
                    "subject": msg["Subject"],
                }))
                done.add(c["email"].lower())
                bounces, sent_now = 0, sent_now + 1
                print(f"  [{i:>2}/{len(batch)}] SENT    {c['email']}")
            except smtplib.SMTPRecipientsRefused as e:
                write_log(c["email"], "refused", str(e)[:200])
                bounces += 1
                print(f"  [{i:>2}/{len(batch)}] REFUSED {c['email']}")
                if bounces >= 3:
                    print("  Stopping after 3 consecutive recipient refusals.")
                    break
            except (smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as e:
                write_log(c["email"], "error", str(e)[:200])
                print(f"  Gmail rejected the message ({e.smtp_code}); stopping this batch.")
                break
            except (smtplib.SMTPServerDisconnected, TimeoutError, OSError) as exc:
                write_log(c["email"], "unknown", json.dumps({
                    "message_id": msg["Message-ID"],
                    "subject": msg["Subject"],
                    "reason": str(exc)[:200],
                }))
                server = None
                print(f"  [{i:>2}/{len(batch)}] UNKNOWN {c['email']} - "
                      "connection failed during send; held from retry. "
                      "Check Gmail Sent and run --sync-threads.")
                break
            wait_if_campaign_paused()
            if i < len(batch):
                wait(random.uniform(cfg["gap_min"], cfg["gap_max"]))
        if sent_message_records:
            try:
                thread_ids = find_gmail_thread_ids(cfg, sent_message_records)
                for email, thread in thread_ids.items():
                    update_message_details(email, {
                        "message_id": thread["message_id"],
                        "subject": thread["subject"],
                        "gmail_thread_id": thread["thread_id"],
                    })
                missing = len(sent_message_records) - len(thread_ids)
                if missing:
                    print(f"\n  Note: Gmail has not exposed a thread ID for "
                          f"{missing} sent message(s) yet. Follow-up mode will retry lookup.")
            except (imaplib.IMAP4.error, OSError, RuntimeError) as exc:
                print(f"\n  Note: emails were sent, but Gmail thread IDs could not be "
                      f"saved ({exc}). Follow-up mode will retry lookup.")
    except smtplib.SMTPAuthenticationError:
        sys.exit("Gmail login failed. Use a Google App Password, not your normal password.")
    except KeyboardInterrupt:
        print("\nStopped. Progress is saved; run again to continue.")
    finally:
        if server:
            try:
                server.quit()
            except Exception:
                pass
    print_heading("Send run complete")
    print_summary([
        ("Sent this run", f"{sent_now:,}"),
        ("Sent today", f"{sent_today + sent_now:,} / {cfg['per_day']:,}"),
        ("Next recipient", build_tracker_state(
            contacts, done, suppressed
        )["next_email"] or "None"),
    ])


if __name__ == "__main__":
    main()
