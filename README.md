# Spammer

A calm, local Gmail outreach dashboard and mailer. It sends a daily batch
from the signed-in account's contact list, records delivery state in its
tracker, and checks for replies before sending follow-ups.

## Browser dashboard

Start the local dashboard from this folder:

```powershell
py -m pip install -r requirements.txt
py dashboard.py
```

It opens at <http://127.0.0.1:8765>. The dashboard binds to localhost only and
provides an at-a-glance overview, send analytics, contact management,
tracker/history, send and follow-up controls,
Gmail thread sync, conversation history, settings, credentials, templates, resume selection, and
suppression management. While the dashboard server is running, it automatically
checks Gmail All Mail (which includes Inbox and archived mail) plus Spam at startup and every 15 minutes for delivery
notices matching sends from the last 30 days. The startup check catches up
across that window; later checks scan only mail since the last successful scan,
with a one-day overlap. It updates the tracker and suppresses confirmed hard
bounces. Resume uploads accept PDF, DOC, or DOCX files up to 8 MiB. A local
sign-in protects the dashboard; account, profile, security, Gmail, sending,
templates, resume, and appearance are organized in one searchable Settings
section. Profile photos can be positioned and zoomed before saving. The sidebar
remembers its collapsed state in the browser. Contacts and Tracker keep the focus
on searchable tables; Settings shows one focused section at a time. Gmail thread
sync runs in a background job and shows a compact status
indicator while it runs and when it completes. Notifications stay concise and
their list scrolls independently. Those preferences are
stored in the browser in use; the connected sender address is read from the
dashboard settings, and the App Password is never returned to the browser. Send
actions always ask for confirmation. Keep the
terminal window open while using it; press `Ctrl+C` there to stop the server.

The top bar shows Gmail IMAP connectivity based on the most recent automated
check (a recent successful check is considered connected; a stale result is
shown separately). The guided Gmail setup checks SMTP and IMAP separately;
credentials are saved only after Gmail accepts the SMTP sign-in. IMAP operations
use a 20-second socket timeout, and an overlong check is shown as stale instead
of remaining on “Checking” forever. The
notification menu lists follow-ups that need a reply check, detected replies,
and delivery problems; notification read-state is saved per account and synchronized across dashboard tabs. The Campaign
page header groups **Send test** and **Sync Gmail threads** together. Thread sync
does not block dashboard navigation or display verbose command output. Sending a
test opens a recipient dialog and still requires a separate send confirmation.

The Campaign page also supports a customized one-off email. Preview is
required before send; one-off sends are independent of the campaign's daily
limit. A new contact is added to the account's local database, and the send attempt, message
snapshot, status, and Gmail IDs are stored in its tracker table. Name is optional
and defaults to `Unknown`; title defaults to `Hr`. Each compose window saves
its own draft automatically and also has an explicit **Save draft** action.
Empty compose windows are not stored. Saved drafts live in the **Drafts** section;
minimized compose windows stay in the bottom dock for quick return.
Multiple compose windows can stay open at once; each can be dragged, maximized,
or minimized to the shared **Chats & drafts** dock, and saved drafts can be
reopened after closing or restarting the dashboard.
The selected resume remains attached; up to 10 additional files (8 MiB total)
can be added from the attachment button. The composer supports additional font
families and sizes, bold, italic, underline, and an expanded emoji picker.
Formatting is preserved in the HTML email, while a plain-text alternative is
included for email clients that do not display HTML. Follow-up is opt-in for each one-off send. To preserve the tracker's current
one-row-per-email history, this action is available only for recipients with
no existing tracker row; suppressed addresses are always blocked.
One-off sends are excluded from the campaign's `emails_per_day` quota. They
remain included in successful-send analytics and the daily and weekly charts.
Overview campaign metrics (including sent, due follow-ups, delivery issues,
and campaign progress) exclude one-off messages.

The Compose button is in the sidebar. In Tracker, click a sent recipient
address to open its Gmail conversation. The **Threads** section separately
lists sent messages with Gmail conversation IDs, excluding bounced, delayed,
and delivery-review records. Open a thread there or from Tracker to view its
Reddit-style nested replies; long messages can be expanded individually or
together, while the message feed scrolls independently of the pinned reply
form. Thread windows can be dragged, maximized, or minimized into the shared
bottom dock. Replies are sent only after confirmation and are addressed to the
tracked recipient. Conversation data is cached briefly in memory and refreshed
on demand; replying invalidates the cache. Existing **Reply detected**
notifications open the corresponding conversation. A detected reply shows as
one unread conversation update until opened; read state is saved in the signed-in
account. This badge counts detected conversation updates, not every individual
Gmail message. Every dialog has a close control. Each sent message also has a simple follow-up
switch. Choose **Select emails** to show
checkboxes, then use **Select all visible tracker rows** or choose individual
rows. Follow-up bulk actions are available only when all selected rows are sent
messages; updates are validated as a group, so a blocked address prevents a
partial update. **Delete selected** permanently removes the selected tracker
history after confirmation. Contacts remain in the contact list, so deleting
their tracker record may make them eligible for another campaign send.
In Contacts, choose **Select contacts** to select individual rows or all
contacts visible on the current page, then remove the selected contacts in one
confirmed action. Removing contacts does not delete tracker history.
Existing campaign sends default to follow-up enabled; one-off messages follow
their per-send opt-in. Settings let you set the delay in days (1–90, default
7) before the first follow-up and between later follow-ups, plus a maximum of
1–3 follow-ups per recipient (default 1). The follow-up action checks replies
in Gmail Inbox and Spam before sending, and only considers messages that are
due, have a Gmail thread, remain opted in, and have no delivery issue or
suppression. A successful follow-up count is recorded; uncertain or failed
outcomes are held for review.

## Local sign-in and first run

Run `py -m pip install -r requirements.txt`, then `py dashboard.py` and open `http://127.0.0.1:8765`. Create a name, email, and password on first launch; use them to sign in later. Each local installation has one account. Passwords are stored as salted hashes in `data/local_auth.sqlite3`, and the browser session uses an HTTP-only, same-site cookie. Nothing is registered online. Existing app data is copied into the account data folder on first setup and left in place as a backup.

After creating your local account, Spammer opens **Settings → Gmail connection** and guides you through the one-time setup:

1. Turn on Google 2-Step Verification.
2. Create a Google App Password (a separate 16-character password; do not use your normal Gmail password).
3. Enter your Gmail address and App Password. Spammer checks access and saves the password on this computer only after Gmail accepts it.

The sign-in email and Gmail sender address are separate. The App Password is kept in the local account's ignored settings file; it is never shown back in the dashboard. If Inbox monitoring is not available, sending can still be ready; enable IMAP in Gmail to sync replies and conversations.

Then add contacts in **Contacts**, review templates, add a resume if needed, and preview the campaign before sending.

Contacts supports CSV and Excel (`.xls`, `.xlsx`, `.xlsm`) imports up to 8 MiB. Import first previews the columns, detects duplicate or blocked addresses, and asks you to confirm. Existing contacts are preserved and only new valid contacts are appended. The account database is the source of truth; **Export CSV** creates a current CSV download for portability. Excel imports use the dependencies in `requirements.txt`.

Existing CSV, text, and JSON data is imported into SQLite the first time a profile is opened. The import is repeat-safe and leaves the original files untouched as a recovery copy. Migration audit paths are stored relative to each profile so moving the project does not leave stale absolute paths. To migrate/check every existing profile manually, run `py migrate_data.py`; the command reports imported row counts and verifies that its source files were not changed.

Thread updates use Gmail IMAP IDLE to notice mailbox changes, an incremental background check to update conversations, and a local Server-Sent Events stream to refresh open dashboards. A periodic check is kept as a fallback. The dashboard stays interactive while background work runs; tracker writes use short SQLite transactions. If Gmail changes during a send, Threads shows that an update is queued and syncs it when the send releases the tracker. Gmail's cloud push API requires Google Cloud Pub/Sub configuration, so it is not used by this local-only install.

| File | Purpose |
| --- | --- |
| `accounts/<account-id>/` | Private local account data, settings, and templates. |
| `data/local_auth.sqlite3` | Local user and session records. Managed by the app. |
| `<profile>/data/app.sqlite3` | Per-profile contacts, tracker, suppression, drafts, conversation state, and sync cursors. Managed by the app. |
| `static/dashboard.html` | Browser dashboard UI. |
| `local_auth.py` | Password and session storage. |
| `profile_store.py` | Per-profile SQLite storage and safe legacy import. |
| `cold_mailer.py` | Command-line mailer. |
| `dashboard.py` | Local dashboard server. |

## Advanced command line

Dashboard users should use the web interface so commands run against their signed-in account. Direct `cold_mailer.py` commands continue to use the project-root profile database.

| Command | What it does |
| --- | --- |
| `py cold_mailer.py --status` | Show progress, next recipient, and last successful send. |
| `py cold_mailer.py --dry-run` | Preview recipients without sending. |
| `py cold_mailer.py --test you@example.com` | Send one test email. |
| `py cold_mailer.py --follow-ups --dry-run` | Check for due follow-ups and preview without sending. |
| `py cold_mailer.py --follow-ups` | Check for replies and send eligible follow-ups. |
| `py cold_mailer.py --check-deliveries --dry-run` | Preview failures for tracked sends from the last 30 days; changes nothing. |
| `py cold_mailer.py --check-deliveries --lookback-days 7` | Check a custom recent-send window (1–90 days); suppress confirmed hard bounces. |
| `py cold_mailer.py --sync-threads` | Look up and save missing Gmail thread IDs; sends no email. |
| `py cold_mailer.py --suppress person@example.com` | Exclude an address from future sends and follow-ups. |

## Follow-ups

- Enable IMAP for the Gmail account. The script connects over IMAP to check
  Inbox replies and look up Gmail thread IDs in Sent Mail.
- A follow-up becomes eligible after the configured `followup_interval_days`
  (1–90, default 7) if no reply is detected. If multiple follow-ups are
  enabled, each subsequent one waits at least that same interval.
- The maximum number of follow-ups is configurable from 1 to 3 per recipient
  (`max_followup_weeks` in `config.ini` or Settings). The default is one.
- Tracker switches allow a single recipient's follow-ups to be paused or
  resumed; checkboxes and bulk controls apply that choice to selected visible
  rows. Bounced, delayed, or unclear delivery failures permanently turn the
  follow-up switch off. Blocked addresses, replies, uncertain outcomes, and
  the configured follow-up cap also prevent enabling follow-ups.
- The daily follow-up limit is capped by `emails_per_day`.
- Follow-ups are sent as replies in the original Gmail thread. The thread and
  message IDs are stored in the signed-in account tracker.
- If an older sent message cannot be matched confidently, it is skipped rather
  than risking a follow-up in the wrong thread.
- Run `--follow-ups --dry-run` first. The follow-up wording is currently in
  the signed-in account's `templates/followup_template.txt`.

## Tracking and privacy

Each signed-in account's `data/app.sqlite3` stores one tracker record per recipient and records send and follow-up state. The old CSV and JSON files remain as unchanged migration copies; current changes are stored in SQLite. Use the dashboard's import/export controls to exchange contact CSV files. Addresses that reply with
“unsubscribe” should be blocked with `--suppress`. Blocked addresses are
removed from the active contacts file while their tracker history remains.
The dashboard has no suppression-list page; the internal block file prevents
accidental re-imports from resulting in another send.

### Delivery failures and retries

- An SMTP `SENT` status means Gmail accepted the message from this script; it
  does not prove the recipient's mail server accepted it. Delayed bounce
  notices may arrive later.
- The dashboard automatically checks All Mail and Spam on startup and every 15
  minutes, matching delivery notices to tracked sends from the last 30 days.
  All Mail covers Inbox and archived mail; Spam is scanned separately because
  Gmail excludes it from All Mail. The check reports inspected notices and
  matched recipients. This detects bounce and delay notices Gmail exposes over
  IMAP; absence of a notice does not confirm delivery.
  The first scan catches up over that send window; later scans only revisit
  mail since the last successful check, with a one-day overlap. Checks update
  the tracker but do not send email. The dashboard shows check status and errors.
- The command-line `--check-deliveries --dry-run` option previews without
  changing files; `--lookback-days` can set a bounded 1–90 day window.
- A confirmed permanent “address not found” bounce is marked `bounced`, blocked
  from future sends, and removed from the active contacts file. Temporary
  deferrals and ambiguous notices are marked for review. Every delivery issue
  permanently turns follow-ups off for that tracker row.
- The original SMTP status and timestamp remain in the tracker; delivery
  outcome is recorded separately. A missing bounce notice is not proof of
  delivery, and Gmail may delete Spam messages after 30 days.
- If the SMTP connection drops while sending, the tracker records an `unknown`
  outcome and holds that recipient from automatic retry to avoid duplicate
  mail. Run `--sync-threads` to check Gmail Sent by the saved Message-ID; a
  confirmed message is reconciled to `sent`, while unmatched messages remain
  unknown for manual review.
- To retry after a confirmed bounce, verify the corrected address first and
  add it as a new contact. The old address remains blocked. Do not retry the
  old address just because a message is marked `sent`.
- Follow-up reply checks also inspect your Gmail Inbox and Spam. This can
  detect replies filtered into your own Spam folder, not whether a recipient
  put your outbound email in their Spam folder.
- IMAP failures stop the check with an error; they are not treated as “no
  reply” or “no bounce”.

The project `.gitignore` excludes `.env`, `data/`, and `accounts/`, with the old `local_profiles/` folders kept ignored for compatibility. Never paste credentials into chat or
commit them to source control. If an App Password is exposed, revoke it and
create a replacement.
