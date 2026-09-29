"""Telegram user -> Google email. The only thing we keep after /login: no tokens."""
import os
import sqlite3
from datetime import datetime, timezone


class Users:
    def __init__(self, path: str, domain: str = ""):
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self.domain = domain.strip().lstrip("@").lower()
        self._db = sqlite3.connect(path)
        self._db.execute("CREATE TABLE IF NOT EXISTS users ("
                         "telegram_id INTEGER PRIMARY KEY, email TEXT NOT NULL, linked_at TEXT NOT NULL)")
        # Added with sign-in by company domain; an older database gets it here.
        if "via_domain" not in [row[1] for row in self._db.execute("PRAGMA table_info(users)")]:
            self._db.execute("ALTER TABLE users ADD COLUMN via_domain INTEGER NOT NULL DEFAULT 0")
        self._db.commit()

    def email(self, telegram_id: int) -> str | None:
        row = self._db.execute("SELECT email FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        return row[0] if row else None

    def via_domain(self, telegram_id: int) -> bool:
        """Signed in with an account the company's Google Workspace manages, and that domain is still allowed."""
        row = self._db.execute("SELECT email, via_domain FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        return bool(row and row[1] and self.domain and row[0].endswith("@" + self.domain))

    def link(self, telegram_id: int, email: str, via_domain: bool = False) -> list[int]:
        """One email - one Telegram account. Returns the accounts that lost it (they get told)."""
        displaced = [row[0] for row in self._db.execute(
            "SELECT telegram_id FROM users WHERE email = ? AND telegram_id != ?", (email, telegram_id))]
        self._db.execute("DELETE FROM users WHERE email = ? AND telegram_id != ?", (email, telegram_id))
        self._db.execute("INSERT INTO users (telegram_id, email, linked_at, via_domain) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(telegram_id) DO UPDATE SET email = excluded.email, "
                         "linked_at = excluded.linked_at, via_domain = excluded.via_domain",
                         (telegram_id, email, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          int(via_domain)))
        self._db.commit()
        return displaced

    def unlink(self, telegram_id: int) -> None:
        self._db.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
        self._db.commit()

    def close(self) -> None:
        self._db.close()
