"""Хранение заявок на запись в SQLite."""
import sqlite3
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Booking:
    user_id: int
    username: str
    service: str
    when: str
    name: str
    phone: str
    id: int | None = None
    created_at: str = ""


class Storage:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                service TEXT NOT NULL,
                desired_time TEXT NOT NULL,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL
            )"""
        )
        self.conn.commit()

    def add(self, b: Booking) -> Booking:
        b.created_at = datetime.now().isoformat(timespec="seconds")
        cur = self.conn.execute(
            "INSERT INTO bookings (user_id, username, service, desired_time, name, phone, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (b.user_id, b.username, b.service, b.when, b.name, b.phone, b.created_at),
        )
        self.conn.commit()
        b.id = cur.lastrowid
        return b

    def set_status(self, booking_id: int, status: str) -> int | None:
        """Меняет статус, возвращает user_id клиента (или None, если заявки нет)."""
        row = self.conn.execute("SELECT user_id FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        if not row:
            return None
        self.conn.execute("UPDATE bookings SET status = ? WHERE id = ?", (status, booking_id))
        self.conn.commit()
        return row[0]

    def status(self, booking_id: int) -> str | None:
        row = self.conn.execute("SELECT status FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        return row[0] if row else None
