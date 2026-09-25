"""Telegram user -> Google email. The only thing we keep after /login: no tokens."""
import os
import sqlite3
from datetime import datetime, timezone


class Users:
    def __init__(self, path: str):
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute("CREATE TABLE IF NOT EXISTS users ("
                         "telegram_id INTEGER PRIMARY KEY, email TEXT NOT NULL, linked_at TEXT NOT NULL)")
        self._db.commit()

    def email(self, telegram_id: int) -> str | None:
        row = self._db.execute("SELECT email FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        return row[0] if row else None

    def link(self, telegram_id: int, email: str) -> list[int]:
        """One email - one Telegram account. Returns the accounts that lost it (they get told)."""
        displaced = [row[0] for row in self._db.execute(
            "SELECT telegram_id FROM users WHERE email = ? AND telegram_id != ?", (email, telegram_id))]
        self._db.execute("DELETE FROM users WHERE email = ? AND telegram_id != ?", (email, telegram_id))
        self._db.execute("INSERT INTO users (telegram_id, email, linked_at) VALUES (?, ?, ?) "
                         "ON CONFLICT(telegram_id) DO UPDATE SET email = excluded.email, linked_at = excluded.linked_at",
                         (telegram_id, email, datetime.now(timezone.utc).isoformat(timespec="seconds")))
        self._db.commit()
        return displaced

    def unlink(self, telegram_id: int) -> None:
        self._db.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
        self._db.commit()

    def close(self) -> None:
        self._db.close()
