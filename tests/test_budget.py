import asyncio
from datetime import date
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from budget_agent import analytics
from budget_agent.ai import Line, ParsedPurchases
from budget_agent.bot import BudgetBot, parse_amount, split_message, to_purchases
from budget_agent.config import Config
from budget_agent.db import BudgetDB, Purchase
from budget_agent.statement import read_rows, to_chunks

TODAY = date(2026, 10, 13)


def make_db():
    db = BudgetDB(":memory:")
    db.add_purchases([
        Purchase(TODAY, "Молоко", "Продукти", 45, "АТБ", "молочне", 1),
        Purchase(TODAY, "Кава", "Кафе та готова їжа", 90, "Aroma"),
        Purchase(date(2026, 10, 2), "Молоко", "Продукти", 47, "Сільпо"),
        Purchase(date(2026, 9, 20), "Таксі", "Транспорт і пальне", 200),
    ], "receipt", chat_id=1)
    return db


def test_budget_with_invest_rate():
    db = make_db()
    db.add_income(date(2026, 10, 1), 40000)
    db.add_investment(date(2026, 10, 5), 1000)
    b = analytics.budget_block(db, TODAY, 0.10)
    assert b["invest_target"] == 4000
    assert b["invest_left_to_set_aside"] == 3000
    assert b["spent"] == 182
    assert b["left_to_spend"] == 36000 - 182
    assert b["days_left"] == 19
    text = analytics.format_budget(b, "грн")
    assert "4000.00" in text and "на день" in text


def test_budget_without_income_asks_for_it():
    b = analytics.budget_block(make_db(), TODAY, 0.10)
    assert not b["income_known"]
    assert "/income" in analytics.format_budget(b, "грн")


def test_day_and_month_summary():
    db = make_db()
    day = analytics.day_summary(db, TODAY, 0.1)
    assert day["total"] == 135
    assert [c["category"] for c in day["by_category"]] == ["Кафе та готова їжа", "Продукти"]
    listing = analytics.format_day_list(day, "грн")
    assert "Молоко — 45.00 (АТБ)" in listing

    month = analytics.month_summary(db, 2026, 10, 0.1)
    assert month["total_spent"] == 182
    milk = next(i for i in month["top_items"] if i["item"] == "Молоко")
    assert milk["times"] == 2 and milk["stores"] == ["АТБ", "Сільпо"]
    assert month["previous_month"]["total_spent"] == 200


def test_undo_removes_only_last_batch():
    db = make_db()
    db.add_income(TODAY, 1000, chat_id=1)
    assert db.undo_last(1) == "incomes"
    assert db.undo_last(1) == "receipt"
    assert db.undo_last(1) is None


def test_statement_chunks_repeat_header():
    csv_data = "Дата;Опис;Сума\n" + "\n".join(f"2026-10-0{i % 9 + 1};АТБ;-{i}" for i in range(250))
    rows = read_rows(csv_data.encode("cp1251"), "bank.csv")
    assert rows[0] == ["Дата", "Опис", "Сума"]
    chunks = to_chunks(rows, chunk_rows=100)
    assert len(chunks) == 3
    assert all(c.startswith("Дата | Опис | Сума") for c in chunks)


def test_to_purchases_clamps_dates_and_drops_zero():
    parsed = ParsedPurchases(
        lines=[
            Line(item="Хліб", category="Продукти", subcategory=None, quantity=None, amount=30, store=None, date="2026-10-12"),
            Line(item="Знижка", category="Інше", subcategory=None, quantity=None, amount=0, store=None, date=None),
            Line(item="X", category="Інше", subcategory=None, quantity=None, amount=5, store=None, date="2027-01-01"),
            Line(item="Y", category="Інше", subcategory=None, quantity=None, amount=5, store=None, date="bad"),
        ],
        incomes=[], receipt_total=None, comment=None,
    )
    result = to_purchases(parsed, TODAY)
    assert [p.day for p in result] == [date(2026, 10, 12), TODAY, TODAY]


def test_helpers():
    assert parse_amount("1 250,50") == 1250.5
    assert parse_amount("молоко") is None
    parts = split_message("рядок\n" * 2000, limit=500)
    assert all(len(p) <= 500 for p in parts)


class FakeAI:
    async def daily_tips(self, summary):
        return "менше кави"

    async def monthly_report(self, summary):
        return f"аналіз {summary['month']}"


def test_scheduled_report_sends_monthly_on_last_day(monkeypatch):
    tz = ZoneInfo("Europe/Kyiv")
    cfg = Config(None, "t", frozenset({1, 2}), tz, None, "грн", 0.1, ":memory:")
    db = make_db()
    db.add_purchases([Purchase(date(2026, 10, 31), "Сир", "Продукти", 120)], "text", chat_id=1)
    bot = BudgetBot(cfg, db, FakeAI())
    sent = []

    async def send_message(chat_id, text):
        sent.append((chat_id, text))

    ctx = SimpleNamespace(bot=SimpleNamespace(send_message=send_message))
    monkeypatch.setattr(bot, "today", lambda: date(2026, 10, 31))
    asyncio.run(bot.scheduled_daily(ctx))
    assert len(sent) == 4
    assert "менше кави" in sent[0][1] and "Сир" in sent[0][1]
    assert "аналіз 2026-10" in sent[1][1]

    sent.clear()
    monkeypatch.setattr(bot, "today", lambda: date(2026, 10, 30))
    asyncio.run(bot.scheduled_daily(ctx))
    assert len(sent) == 2
