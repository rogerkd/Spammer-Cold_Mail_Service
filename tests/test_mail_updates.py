import base64
import imaplib
import tempfile
import time
from types import SimpleNamespace
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

import cold_mailer
import dashboard


def raw_message(message_id, body, parent="", references="", sender="Sender"):
    message = EmailMessage()
    message["From"] = f"{sender} <sender@example.com>"
    message["To"] = "recipient@example.com"
    message["Subject"] = "Opportunity"
    message["Date"] = "Mon, 02 Nov 2026 10:00:00 +0000"
    message["Message-ID"] = message_id
    if parent:
        message["In-Reply-To"] = parent
    if references:
        message["References"] = references
    message.set_content(body)
    return message.as_bytes()


class FakeMailbox:
    def __init__(self, messages):
        self.messages = messages
        self.fetch_count = 0
        self.selected = ""

    def login(self, *_):
        return "OK", []

    def list(self):
        return "OK", [
            b'(\\All) "/" "[Gmail]/All Mail"',
            b'(\\Sent) "/" "[Gmail]/Sent Mail"',
            b'() "/" "INBOX"',
        ]

    def select(self, *_args, **_kwargs):
        self.selected = str(_args[0]).strip('"')
        return "OK", []

    def uid(self, command, *_args):
        if command == "SEARCH":
            return "OK", [b" ".join(
                str(index).encode("ascii")
                for index in range(1, len(self.messages) + 1)
            )]
        self.fetch_count += 1
        uids = [int(uid) for uid in _args[0].split(b",")]
        return "OK", [(b"FETCH", self.messages[uid - 1]) for uid in uids]

    def logout(self):
        return "BYE", []


class ThreadFallbackMailbox(FakeMailbox):
    def uid(self, command, *args):
        if command == "SEARCH":
            if "HEADER" in args:
                return "OK", [b"1"]
            if "X-GM-THRID" in args:
                return "OK", [b"" if str(args[-1]) == "99999" else b"1"]
        if command == "FETCH" and "X-GM-THRID" in args[-1]:
            return "OK", [(b"1", b"1 (X-GM-THRID 12345)")]
        return super().uid(command, *args)


class FakeSMTP:
    def __init__(self):
        self.message = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def send_message(self, message):
        self.message = message


class MailExperienceTests(unittest.TestCase):
    def test_preview_job_with_empty_progress_finishes_without_error(self):
        job_id = "preview-job"
        job = {
            "id": job_id, "action": "preview", "state": "running",
            "output": [], "progress": None,
        }
        process = SimpleNamespace(
            pid=123,
            stdout=[
                "Contacts remaining: 3,457\n",
                "Sent today: 0 / 80\n",
                "Emails in this batch: 2\n",
                "Dry run: no email was sent.\n",
            ],
            wait=lambda: 0,
        )
        with patch.object(dashboard, "jobs", {job_id: job}), \
                patch.object(dashboard.subprocess, "Popen", return_value=process):
            dashboard.run_job(job_id, ["mailer", "--dry-run"])
        self.assertEqual(job["state"], "completed")
        self.assertIsNone(job["progress"])

    def test_send_job_updates_live_progress_counts(self):
        job_id = "send-job"
        job = {
            "id": job_id, "action": "send", "state": "running",
            "output": [], "progress": None,
        }
        process = SimpleNamespace(
            pid=124,
            stdout=[
                "Sent today: 5 / 80\n",
                "Emails in this batch: 2\n",
                "[ 1/2] SENT first@example.com\n",
                "[ 2/2] UNKNOWN second@example.com\n",
            ],
            wait=lambda: 0,
        )
        with patch.object(dashboard, "jobs", {job_id: job}), \
                patch.object(dashboard.subprocess, "Popen", return_value=process):
            dashboard.run_job(job_id, ["mailer"])
        self.assertEqual(job["progress"]["current"], 2)
        self.assertEqual(job["progress"]["sent"], 1)
        self.assertEqual(job["progress"]["unknown"], 1)
        self.assertEqual(job["progress"]["daily_sent_before"], 5)

    def test_one_off_defaults_and_attachments(self):
        contact, *_ = dashboard.validate_one_off({
            "email": "recipient@example.com",
            "subject": "Hello",
            "body": "Body",
        })
        self.assertEqual(contact["name"], "Unknown")
        self.assertEqual(contact["title"], "Hr")

        encoded = base64.b64encode(b"file").decode("ascii")
        attachments = dashboard.validate_one_off_attachments([{
            "name": r"C:\temp\report.txt",
            "content": encoded,
        }])
        self.assertEqual(attachments[0][0], "report.txt")
        self.assertEqual(attachments[0][1], b"file")
        self.assertEqual(attachments[0][2:], ("text", "plain"))

    def test_one_off_message_includes_resume_and_added_files(self):
        message = cold_mailer.build_message(
            {"name": "Unknown", "email": "recipient@example.com"},
            "sender@example.com",
            "Sender",
            "Hello",
            "Hi {first_name}",
            ("resume.pdf", b"%PDF-1.0", "application", "pdf"),
            attachments=[("notes.txt", b"Notes", "text", "plain")],
        )
        filenames = [part.get_filename() for part in message.iter_attachments()]
        self.assertEqual(filenames, ["resume.pdf", "notes.txt"])
        self.assertIn("Hi there", message.get_body(preferencelist=("plain",)).get_content())

    def test_draft_persists_and_clears(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "draft.json"
            with patch.object(dashboard, "ONE_OFF_DRAFT_FILE", path):
                dashboard.save_one_off_draft({
                    "draft_id": "draft-one",
                    "email": "recipient@example.com",
                    "subject": "Draft",
                    "body": "Saved",
                    "attachments": [{
                        "name": "note.txt",
                        "content": base64.b64encode(b"file").decode("ascii"),
                    }],
                })
                dashboard.save_one_off_draft({
                    "draft_id": "draft-two",
                    "email": "second@example.com",
                    "subject": "Second draft",
                    "body": "Also saved",
                })
                draft = next(
                    item for item in dashboard.read_one_off_drafts()
                    if item["draft_id"] == "draft-one"
                )
                self.assertEqual(draft["name"], "Unknown")
                self.assertEqual(draft["title"], "Hr")
                self.assertEqual(draft["body"], "Saved")
                self.assertEqual(draft["attachments"][0]["name"], "note.txt")
                drafts = dashboard.read_one_off_drafts()
                self.assertEqual({item["draft_id"] for item in drafts}, {
                    "draft-one", "draft-two"
                })
                dashboard.save_one_off_draft({
                    "action": "clear", "draft_id": "draft-one"
                })
                self.assertEqual(
                    [item["draft_id"] for item in dashboard.read_one_off_drafts()],
                    ["draft-two"],
                )

    def test_legacy_draft_is_migrated_without_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "draft.json"
            path.write_text(
                '{"email":"legacy@example.com","subject":"Legacy","body":"Saved"}',
                encoding="utf-8",
            )
            with patch.object(dashboard, "ONE_OFF_DRAFT_FILE", path):
                draft = dashboard.read_one_off_drafts()[0]
                self.assertEqual(draft["draft_id"], "legacy")
                dashboard.save_one_off_draft({
                    **draft,
                    "draft_id": "legacy",
                    "subject": "Updated",
                })
                self.assertEqual(dashboard.read_one_off_drafts()[0]["subject"], "Updated")

    def test_conversation_history_excludes_failed_delivery_states(self):
        base = {
            "status": "sent",
            "gmail_thread_id": "12345",
            "delivery_status": "",
        }
        self.assertTrue(dashboard.is_conversation_eligible(base))
        for status in ("bounced", "delayed", "needs_review", "needs review"):
            self.assertFalse(dashboard.is_conversation_eligible({
                **base, "delivery_status": status,
            }))
        self.assertFalse(dashboard.is_conversation_eligible({
            **base, "status": "error",
        }))
        self.assertFalse(dashboard.is_conversation_eligible({
            **base, "gmail_thread_id": "",
        }))

    def test_thread_cache_expires_and_returns_isolated_results(self):
        dashboard.thread_cache.clear()
        messages = [{"message_id": "<root@example.com>", "body": "Original"}]
        cfg = {"email": "sender@example.com", "password": "secret"}
        try:
            with patch.object(
                dashboard.mailer, "load_gmail_thread", return_value=messages
            ) as load_thread, patch.object(
                dashboard.time, "monotonic", side_effect=[10.0, 11.0, 20.0, 60.0, 61.0]
            ):
                first = dashboard.load_cached_gmail_thread(cfg, "12345")
                first[0]["body"] = "Changed by caller"
                second = dashboard.load_cached_gmail_thread(cfg, "12345")
                self.assertEqual(second[0]["body"], "Original")
                self.assertEqual(load_thread.call_count, 1)

                dashboard.load_cached_gmail_thread(cfg, "12345")
                self.assertEqual(load_thread.call_count, 2)
        finally:
            dashboard.thread_cache.clear()

    def test_thread_cache_force_refresh_and_invalidation(self):
        dashboard.thread_cache.clear()
        messages = [{"message_id": "<root@example.com>", "body": "Original"}]
        cfg = {"email": "sender@example.com", "password": "secret"}
        try:
            with patch.object(
                dashboard.mailer, "load_gmail_thread", return_value=messages
            ) as load_thread:
                dashboard.load_cached_gmail_thread(cfg, "12345")
                dashboard.load_cached_gmail_thread(cfg, "12345", force=True)
                dashboard.invalidate_gmail_thread_cache("sender@example.com", "12345")
                dashboard.load_cached_gmail_thread(cfg, "12345")
            self.assertEqual(load_thread.call_count, 3)
        finally:
            dashboard.thread_cache.clear()

    def test_campaign_quota_excludes_one_off_sends(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = Path(directory) / "tracker.csv"
            legacy = Path(directory) / "legacy.csv"
            today = cold_mailer.datetime.now().date().isoformat()
            rows = []
            for email, send_type in (
                ("campaign@example.com", "campaign"),
                ("oneoff@example.com", "one_off"),
            ):
                row = dict.fromkeys(cold_mailer.TRACKER_FIELDS, "")
                row.update(
                    email=email, status="sent", timestamp=f"{today}T12:00:00",
                    send_type=send_type,
                )
                rows.append(row)
            with patch.object(cold_mailer, "TRACKER_FILE", tracker), \
                    patch.object(cold_mailer, "LEGACY_LOG_FILE", legacy):
                cold_mailer.save_tracker_rows(rows)
                done, sent_today = cold_mailer.load_log()
            self.assertEqual(done, {"campaign@example.com", "oneoff@example.com"})
            self.assertEqual(sent_today, 1)

    def test_thread_is_batched_and_nested_by_message_references(self):
        messages = [
            raw_message("<root@example.com>", "Original"),
            raw_message(
                "<reply@example.com>", "Reply",
                parent="<root@example.com>", references="<root@example.com>",
                sender="Recipient",
            ),
            raw_message(
                "<nested@example.com>", "Nested",
                parent="<reply@example.com>",
                references="<root@example.com> <reply@example.com>",
            ),
        ]
        mailbox = FakeMailbox(messages)
        with patch.object(imaplib, "IMAP4_SSL", return_value=mailbox) as connect:
            thread = cold_mailer.load_gmail_thread(
                {"email": "sender@example.com", "password": "secret"}, "12345"
            )
        self.assertEqual(connect.call_count, 1)
        self.assertEqual(mailbox.fetch_count, 1)
        self.assertEqual(len(thread), 3)
        self.assertEqual(thread[0]["parent_message_id"], "")
        self.assertEqual(thread[1]["parent_message_id"], "<root@example.com>")
        self.assertEqual(thread[2]["parent_message_id"], "<reply@example.com>")

    def test_large_thread_load_is_batched_and_capped(self):
        messages = [
            raw_message(
                f"<message-{index}@example.com>",
                f"Message body {index}",
                parent=f"<message-{index - 1}@example.com>" if index else "",
                references=(
                    f"<message-{index - 1}@example.com>" if index else ""
                ),
            )
            for index in range(150)
        ]
        mailbox = FakeMailbox(messages)
        started = time.perf_counter()
        with patch.object(imaplib, "IMAP4_SSL", return_value=mailbox):
            thread = cold_mailer.load_gmail_thread(
                {"email": "sender@example.com", "password": "secret"}, "12345"
            )
        elapsed = time.perf_counter() - started
        self.assertEqual(len(thread), 100)
        self.assertEqual(mailbox.fetch_count, 1)
        self.assertLess(elapsed, 2.0)

    def test_thread_loader_recovers_thread_id_from_root_message(self):
        mailbox = ThreadFallbackMailbox([
            raw_message("<root@example.com>", "Original"),
        ])
        with patch.object(imaplib, "IMAP4_SSL", return_value=mailbox):
            thread = cold_mailer.load_gmail_thread(
                {"email": "sender@example.com", "password": "secret"},
                "99999",
                "<root@example.com>",
            )
        self.assertEqual(len(thread), 1)
        self.assertEqual(thread[0]["message_id"], "<root@example.com>")

    def test_reply_uses_selected_parent_and_gmail_references(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = Path(directory) / "tracker.csv"
            row = dict.fromkeys(cold_mailer.TRACKER_FIELDS, "")
            row.update(
                email="recipient@example.com",
                status="sent",
                gmail_thread_id="12345",
                gmail_message_id="<root@example.com>",
                subject="Opportunity",
            )
            smtp = FakeSMTP()
            messages = [
                {
                    "message_id": "<root@example.com>",
                    "references": [],
                    "subject": "Opportunity",
                },
                {
                    "message_id": "<reply@example.com>",
                    "references": ["<root@example.com>"],
                    "subject": "Re: Opportunity",
                },
            ]
            with patch.object(cold_mailer, "TRACKER_FILE", tracker), \
                    patch.object(cold_mailer, "SUPPRESS_FILE", Path(directory) / "blocked.txt"), \
                    patch.object(cold_mailer, "load_gmail_thread", return_value=messages), \
                    patch.object(cold_mailer, "load_template", return_value=("Sender", "", "")), \
                    patch.object(cold_mailer, "check_credentials"), \
                    patch.object(cold_mailer, "connect", return_value=smtp):
                cold_mailer.save_tracker_rows([row])
                cold_mailer.send_thread_reply(
                    {"email": "sender@example.com", "password": "secret"},
                    "recipient@example.com",
                    "12345",
                    "<reply@example.com>",
                    "A nested reply",
                )
            self.assertEqual(smtp.message["In-Reply-To"], "<reply@example.com>")
            self.assertEqual(
                smtp.message["References"],
                "<root@example.com> <reply@example.com>",
            )
            self.assertEqual(str(smtp.message["To"]), "recipient@example.com")

    def test_reply_is_blocked_for_normalized_delivery_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = Path(directory) / "tracker.csv"
            row = dict.fromkeys(cold_mailer.TRACKER_FIELDS, "")
            row.update(
                email="recipient@example.com",
                status="sent",
                delivery_status="needs review",
                gmail_thread_id="12345",
                gmail_message_id="<root@example.com>",
            )
            with patch.object(cold_mailer, "TRACKER_FILE", tracker), \
                    patch.object(cold_mailer, "SUPPRESS_FILE", Path(directory) / "blocked.txt"):
                cold_mailer.save_tracker_rows([row])
                with self.assertRaisesRegex(ValueError, "delivery has a reported issue"):
                    cold_mailer.send_thread_reply(
                        {"email": "sender@example.com", "password": "secret"},
                        "recipient@example.com",
                        "12345",
                        "<root@example.com>",
                        "Reply",
                    )


if __name__ == "__main__":
    unittest.main()
