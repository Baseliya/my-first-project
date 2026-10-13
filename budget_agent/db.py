"""SQLite-сховище покупок, доходів та інвестицій."""

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS purchases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    TEXT NOT NULL,
    day         TEXT NOT NULL,          -- YYYY-MM-DD
    store       TEXT,
    item        TEXT NOT NULL,
    category    TEXT NOT NULL,
    subcategory TEXT,
    quantity    REAL,
    amount      REAL NOT NULL,
    source      TEXT NOT NULL,          -- receipt | text | statement
    author      TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_purchases_day ON purchases(day);

CREATE TABLE IF NOT EXISTS incomes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id   TEXT NOT NULL,
    day        TEXT NOT NULL,
    amount     REAL NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS investments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id   TEXT NOT NULL,
    day        TEXT NOT NULL,
    amount     REAL NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id   TEXT PRIMARY KEY,
    chat_id    INTEGER,
    kind       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


@dataclass
class Purchase:
    day: date
    item: str
    category: str
    amount: float
    store: str | None = None
    subcategory: str | None = None
    quantity: float | None = None


def month_bounds(year: int, month: int) -> tuple[str, str]:
    start = date(year, month, 1)
    end = date(year + (month == 12), month % 12 + 1, 1)
    return start.isoformat(), end.isoformat()


class BudgetDB:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def _new_batch(self, kind: str, chat_id: int | None) -> str:
        batch_id = uuid.uuid4().hex
        self.conn.execute(
            "INSERT INTO batches VALUES (?, ?, ?, ?)",
            (batch_id, chat_id, kind, datetime.now().isoformat()),
        )
        return batch_id

    # --- запис ---------------------------------------------------------------

    def add_purchases(
        self, purchases: list[Purchase], source: str, chat_id: int | None = None, author: str | None = None
    ) -> str:
        now = datetime.now().isoformat()
        with self.conn:
            batch_id = self._new_batch(source, chat_id)
            self.conn.executemany(
                "INSERT INTO purchases (batch_id, day, store, item, category, subcategory, quantity,"
                " amount, source, author, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (batch_id, p.day.isoformat(), p.store, p.item, p.category, p.subcategory,
                     p.quantity, round(p.amount, 2), source, author, now)
                    for p in purchases
                ],
            )
        return batch_id

    def _add_money(self, table: str, day: date, amount: float, note: str | None, chat_id: int | None) -> str:
        with self.conn:
            batch_id = self._new_batch(table, chat_id)
            self.conn.execute(
                f"INSERT INTO {table} (batch_id, day, amount, note, created_at) VALUES (?,?,?,?,?)",
                (batch_id, day.isoformat(), round(amount, 2), note, datetime.now().isoformat()),
            )
        return batch_id

    def add_income(self, day: date, amount: float, note: str | None = None, chat_id: int | None = None) -> str:
        return self._add_money("incomes", day, amount, note, chat_id)

    def add_investment(self, day: date, amount: float, note: str | None = None, chat_id: int | None = None) -> str:
        return self._add_money("investments", day, amount, note, chat_id)

    def undo_last(self, chat_id: int) -> str | None:
        """Видаляє останнє завантаження цього чату. Повертає його тип або None."""
        row = self.conn.execute(
            "SELECT batch_id, kind FROM batches WHERE chat_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        if not row:
            return None
        with self.conn:
            for table in ("purchases", "incomes", "investments"):
                self.conn.execute(f"DELETE FROM {table} WHERE batch_id = ?", (row["batch_id"],))
            self.conn.execute("DELETE FROM batches WHERE batch_id = ?", (row["batch_id"],))
        return row["kind"]

    # --- читання -------------------------------------------------------------

    def purchases_between(self, start: str, end: str) -> list[sqlite3.Row]:
        """Покупки за [start, end) — дати у форматі YYYY-MM-DD."""
        return self.conn.execute(
            "SELECT * FROM purchases WHERE day >= ? AND day < ? ORDER BY day, id", (start, end)
        ).fetchall()

    def purchases_on(self, day: date) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM purchases WHERE day = ? ORDER BY id", (day.isoformat(),)
        ).fetchall()

    def _sum_between(self, table: str, start: str, end: str) -> float:
        row = self.conn.execute(
            f"SELECT COALESCE(SUM(amount), 0) FROM {table} WHERE day >= ? AND day < ?", (start, end)
        ).fetchone()
        return float(row[0])

    def income_between(self, start: str, end: str) -> float:
        return self._sum_between("incomes", start, end)

    def invested_between(self, start: str, end: str) -> float:
        return self._sum_between("investments", start, end)
