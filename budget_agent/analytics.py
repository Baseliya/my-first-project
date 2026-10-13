"""Підрахунки без AI: суми за категоріями, топ товарів, ліміти з урахуванням інвестицій."""

import calendar
from collections import defaultdict
from datetime import date, timedelta

from budget_agent.db import BudgetDB, month_bounds


def _money(x: float) -> float:
    return round(x, 2)


def _by_category(rows) -> list[dict]:
    totals: dict[str, float] = defaultdict(float)
    for r in rows:
        totals[r["category"]] += r["amount"]
    grand = sum(totals.values()) or 1
    return [
        {"category": c, "amount": _money(a), "share_pct": round(a / grand * 100, 1)}
        for c, a in sorted(totals.items(), key=lambda kv: -kv[1])
    ]


def _top_items(rows, limit: int = 15) -> list[dict]:
    agg: dict[str, dict] = {}
    for r in rows:
        key = r["item"].strip().lower()
        entry = agg.setdefault(
            key, {"item": r["item"], "category": r["category"], "amount": 0.0, "times": 0, "stores": set()}
        )
        entry["amount"] += r["amount"]
        entry["times"] += 1
        if r["store"]:
            entry["stores"].add(r["store"])
    items = sorted(agg.values(), key=lambda e: -e["amount"])[:limit]
    return [{**e, "amount": _money(e["amount"]), "stores": sorted(e["stores"])} for e in items]


def _by_store(rows, limit: int = 10) -> list[dict]:
    totals: dict[str, float] = defaultdict(float)
    visits: dict[str, set] = defaultdict(set)
    for r in rows:
        if r["store"]:
            totals[r["store"]] += r["amount"]
            visits[r["store"]].add(r["day"])
    return [
        {"store": s, "amount": _money(a), "visit_days": len(visits[s])}
        for s, a in sorted(totals.items(), key=lambda kv: -kv[1])[:limit]
    ]


def budget_block(db: BudgetDB, today: date, invest_rate: float) -> dict:
    """Стан місяця: дохід, 10% на інвестиції, скільки можна витрачати далі."""
    start, end = month_bounds(today.year, today.month)
    income = db.income_between(start, end)
    spent = sum(r["amount"] for r in db.purchases_between(start, end))
    invest_target = income * invest_rate
    invested = db.invested_between(start, end)
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    days_left = days_in_month - today.day + 1  # включно з сьогодні
    spendable = income - invest_target
    left = spendable - spent
    return {
        "month": f"{today.year}-{today.month:02d}",
        "income": _money(income),
        "invest_rate_pct": round(invest_rate * 100),
        "invest_target": _money(invest_target),
        "invested": _money(invested),
        "invest_left_to_set_aside": _money(max(invest_target - invested, 0)),
        "spent": _money(spent),
        "spendable_after_invest": _money(spendable),
        "left_to_spend": _money(left),
        "days_left": days_left,
        "daily_allowance": _money(left / days_left) if income and days_left else None,
        "income_known": income > 0,
    }


def day_summary(db: BudgetDB, day: date, invest_rate: float) -> dict:
    rows = db.purchases_on(day)
    start, _ = month_bounds(day.year, day.month)
    month_rows = db.purchases_between(start, (day + timedelta(days=1)).isoformat())
    return {
        "date": day.isoformat(),
        "total": _money(sum(r["amount"] for r in rows)),
        "items": [
            {"item": r["item"], "category": r["category"], "subcategory": r["subcategory"],
             "quantity": r["quantity"], "amount": r["amount"], "store": r["store"]}
            for r in rows
        ],
        "by_category": _by_category(rows),
        "month_to_date_by_category": _by_category(month_rows),
        "budget": budget_block(db, day, invest_rate),
    }


def month_summary(db: BudgetDB, year: int, month: int, invest_rate: float) -> dict:
    start, end = month_bounds(year, month)
    rows = db.purchases_between(start, end)
    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    prev_start, prev_end = month_bounds(prev_y, prev_m)
    prev_rows = db.purchases_between(prev_start, prev_end)

    daily: dict[str, float] = defaultdict(float)
    for r in rows:
        daily[r["day"]] += r["amount"]

    last_day = date(year, month, calendar.monthrange(year, month)[1])
    budget = budget_block(db, last_day, invest_rate)
    return {
        "month": f"{year}-{month:02d}",
        "total_spent": _money(sum(r["amount"] for r in rows)),
        "purchases_count": len(rows),
        "days_with_spending": len(daily),
        "by_category": _by_category(rows),
        "top_items": _top_items(rows),
        "by_store": _by_store(rows),
        "biggest_days": [
            {"date": d, "amount": _money(a)} for d, a in sorted(daily.items(), key=lambda kv: -kv[1])[:5]
        ],
        "previous_month": {
            "month": f"{prev_y}-{prev_m:02d}",
            "total_spent": _money(sum(r["amount"] for r in prev_rows)),
            "by_category": _by_category(prev_rows),
        },
        "budget": {k: budget[k] for k in (
            "income", "invest_rate_pct", "invest_target", "invested", "invest_left_to_set_aside",
            "spent", "spendable_after_invest", "left_to_spend", "income_known",
        )},
    }


def format_day_list(summary: dict, currency: str) -> str:
    """Список покупок за день — точні цифри з бази, без участі AI."""
    lines = [f"🧾 Покупки за {summary['date']}"]
    if not summary["items"]:
        lines.append("Сьогодні витрат не внесено.")
    else:
        by_cat: dict[str, list] = defaultdict(list)
        for it in summary["items"]:
            by_cat[it["category"]].append(it)
        for cat in (c["category"] for c in summary["by_category"]):
            cat_total = sum(i["amount"] for i in by_cat[cat])
            lines.append(f"\n▪️ {cat} — {cat_total:.2f} {currency}")
            for it in by_cat[cat]:
                qty = f" ×{it['quantity']:g}" if it["quantity"] and it["quantity"] != 1 else ""
                store = f" ({it['store']})" if it["store"] else ""
                lines.append(f"   • {it['item']}{qty} — {it['amount']:.2f}{store}")
        lines.append(f"\nРазом за день: {summary['total']:.2f} {currency}")
    lines.append("")
    lines.append(format_budget(summary["budget"], currency))
    return "\n".join(lines)


def format_budget(b: dict, currency: str) -> str:
    lines = [f"💰 Місяць {b['month']}: витрачено {b['spent']:.2f} {currency}"]
    if not b["income_known"]:
        lines.append(
            f"📈 Інвестиції {b['invest_rate_pct']}%: внесіть дохід командою /income 45000, "
            "щоб розрахувати суму для інвестицій і денний ліміт."
        )
        return "\n".join(lines)
    lines.append(
        f"📈 Інвестиції {b['invest_rate_pct']}% від доходу {b['income']:.2f}: {b['invest_target']:.2f} {currency} "
        f"(відкладено {b['invested']:.2f}, залишилось {b['invest_left_to_set_aside']:.2f})"
    )
    lines.append(f"🛒 Можна витратити до кінця місяця: {b['left_to_spend']:.2f} {currency}")
    if b["daily_allowance"] is not None:
        lines.append(f"📅 Це ≈ {b['daily_allowance']:.2f} {currency} на день ({b['days_left']} дн.)")
    if b["left_to_spend"] < 0:
        lines.append("⚠️ Ліміт перевищено — інвестиційні 10% під загрозою.")
    return "\n".join(lines)
