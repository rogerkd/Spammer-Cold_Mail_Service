"""Password and session storage for the localhost dashboard."""

import hashlib
import hmac
import re
import secrets
import sqlite3
import time
import uuid
from pathlib import Path


PASSWORD_ITERATIONS = 600_000
SESSION_LIFETIME_SECONDS = 7 * 24 * 60 * 60
EMAIL_RE = re.compile(r"^[^\s@]{1,128}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,63}$")


class LocalAuth:
    def __init__(self, database_path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    display_name TEXT NOT NULL,
                    password_salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash BLOB PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    csrf_token TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_expiry_idx ON sessions(expires_at);
                """
            )

    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _derive(password, salt):
        return hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS, dklen=32
        )

    @staticmethod
    def public_user(row):
        return {
            "id": row["id"],
            "email": row["email"],
            "display_name": row["display_name"],
        }

    def user_count(self):
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def register(self, email, display_name, password):
        email = str(email or "").strip().lower()
        display_name = re.sub(r"\s+", " ", str(display_name or "")).strip()
        password = str(password or "")
        if not EMAIL_RE.fullmatch(email):
            raise ValueError("Enter a valid email address.")
        if not display_name or len(display_name) > 80 or any(ord(ch) < 32 for ch in display_name):
            raise ValueError("Name must be 1 to 80 printable characters.")
        if len(password) < 12 or len(password) > 256:
            raise ValueError("Use a password between 12 and 256 characters.")
        user_id = uuid.uuid4().hex
        salt = secrets.token_bytes(16)
        password_hash = self._derive(password, salt)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO users(id,email,display_name,password_salt,password_hash,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (user_id, email, display_name, salt, password_hash, int(time.time())),
                )
                connection.commit()
                row = connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        except sqlite3.IntegrityError:
            raise ValueError("An account with this email already exists. Sign in instead.") from None
        return self.public_user(row)

    def authenticate(self, email, password):
        email = str(email or "").strip().lower()
        password = str(password or "")
        if len(password) > 256:
            self._derive(password[:256], bytes(16))
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE email=? COLLATE NOCASE", (email,)
            ).fetchone()
        if row is None:
            # Keep unknown-email and wrong-password attempts at a similar cost.
            salt = bytes(16)
            expected = bytes(32)
            supplied = self._derive(password[:256], salt)
            hmac.compare_digest(supplied, expected)
            return None
        supplied = self._derive(password[:256], row["password_salt"])
        if not hmac.compare_digest(supplied, row["password_hash"]):
            return None
        return self.public_user(row)

    @staticmethod
    def _token_hash(token):
        return hashlib.sha256(str(token).encode("utf-8")).digest()

    def create_session(self, user_id):
        token = secrets.token_urlsafe(40)
        csrf_token = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + SESSION_LIFETIME_SECONDS
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # The localhost dashboard has one active account at a time because
            # its mail monitors and jobs share a process-level mailer context.
            connection.execute("DELETE FROM sessions")
            connection.execute(
                "INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at) VALUES(?,?,?,?)",
                (self._token_hash(token), user_id, csrf_token, expires_at),
            )
            connection.commit()
        return token, csrf_token

    def resolve_session(self, token):
        if not token or len(str(token)) > 256:
            return None
        now = int(time.time())
        with self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
            row = connection.execute(
                "SELECT users.*, sessions.csrf_token, sessions.expires_at "
                "FROM sessions JOIN users ON users.id=sessions.user_id "
                "WHERE sessions.token_hash=? AND sessions.expires_at>?",
                (self._token_hash(token), now),
            ).fetchone()
        if row is None:
            return None
        return {
            "user": self.public_user(row),
            "csrf_token": row["csrf_token"],
            "expires_at": int(row["expires_at"]),
        }

    def revoke_session(self, token):
        if not token:
            return
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE token_hash=?", (self._token_hash(token),)
            )

    def revoke_all_sessions(self):
        with self._connect() as connection:
            connection.execute("DELETE FROM sessions")

    def delete_user(self, user_id):
        with self._connect() as connection:
            connection.execute("DELETE FROM users WHERE id=?", (user_id,))

    def change_password(self, user_id, current_password, new_password):
        current_password = str(current_password or "")
        new_password = str(new_password or "")
        if len(current_password) > 256:
            return False
        if len(new_password) < 12 or len(new_password) > 256:
            raise ValueError("Use a password between 12 and 256 characters.")
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if row is None:
                return False
            supplied = self._derive(current_password, row["password_salt"])
            if not hmac.compare_digest(supplied, row["password_hash"]):
                return False
            salt = secrets.token_bytes(16)
            password_hash = self._derive(new_password, salt)
            connection.execute(
                "UPDATE users SET password_salt=?,password_hash=? WHERE id=?",
                (salt, password_hash, user_id),
            )
        return True
