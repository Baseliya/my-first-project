"""Telegram-бот «Сімейний бюджет»: приймає чеки, суми та виписки, надсилає звіти."""

import logging
from datetime import date, datetime, timedelta
from functools import wraps

import anthropic
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from budget_agent import analytics
from budget_agent.ai import BudgetAI, ParsedPurchases
from budget_agent.config import Config, load_config
from budget_agent.db import BudgetDB, Purchase, month_bounds
from budget_agent.statement import read_rows, to_chunks

log = logging.getLogger(__name__)

HELP = """👋 Я — AI-агент «Сімейний бюджет».

Що мені надсилати протягом дня:
📸 Фото чека — розпишу кожен товар за категоріями.
✍️ Текст: «350 продукти АТБ», «хліб 32, молоко 45, кава 120», «вчора таксі 180» або просто «250».
📄 Виписку з банку чи таблицю витрат (CSV, XLSX, PDF).
❓ Питання: «скільки ми витратили на каву цього місяця?»

Команди:
/today — звіт за сьогодні з порадами
/list — список покупок за сьогодні
/month — розгорнутий аналіз поточного місяця (/month 2026-09 — за інший)
/income 45000 зарплата — внести дохід (від нього рахуються 10% на інвестиції)
/invested 4500 — відмітити, що відклали на інвестиції
/undo — скасувати останнє завантаження

⏰ Щодня о {time} — звіт за день, а в останній день місяця — великий аналіз."""

TG_LIMIT = 4000


def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    parts, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts


def parse_amount(text: str) -> float | None:
    try:
        value = float(text.replace(" ", "").replace(",", "."))
    except ValueError:
        return None
    return value if value > 0 else None


def to_purchases(parsed: ParsedPurchases, today: date) -> list[Purchase]:
    result = []
    for line in parsed.lines:
        if line.amount <= 0:
            continue
        try:
            day = date.fromisoformat(line.date) if line.date else today
        except ValueError:
            day = today
        if day > today:
            day = today
        result.append(Purchase(
            day=day, item=line.item.strip() or "Без опису", category=line.category,
            amount=line.amount, store=line.store, subcategory=line.subcategory, quantity=line.quantity,
        ))
    return result


def allowed(handler):
    """Пускає лише чати з ALLOWED_CHAT_IDS і перетворює помилки AI на зрозумілі повідомлення."""

    @wraps(handler)
    async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if not self.cfg.allowed_chat_ids:
            await update.effective_message.reply_text(
                f"Ваш chat id: {chat_id}\nДодайте його в ALLOWED_CHAT_IDS у файлі .env і перезапустіть бота."
            )
            return
        if chat_id not in self.cfg.allowed_chat_ids:
            await update.effective_message.reply_text("Цей бот приватний.")
            return
        try:
            await handler(self, update, context)
        except anthropic.APIError as e:
            log.exception("Claude API error")
            await update.effective_message.reply_text(f"⚠️ Помилка AI-сервісу: {e.__class__.__name__}. Спробуйте ще раз.")
        except RuntimeError as e:
            await update.effective_message.reply_text(f"⚠️ {e}")
    return wrapper


class BudgetBot:
    def __init__(self, cfg: Config, db: BudgetDB, ai: BudgetAI):
        self.cfg, self.db, self.ai = cfg, db, ai

    def today(self) -> date:
        return datetime.now(self.cfg.timezone).date()

    async def send(self, chat_id: int, text: str, context: ContextTypes.DEFAULT_TYPE) -> None:
        for part in split_message(text):
            await context.bot.send_message(chat_id, part)

    async def reply(self, update: Update, text: str) -> None:
        for part in split_message(text):
            await update.effective_message.reply_text(part)

    # --- команди -------------------------------------------------------------

    @allowed
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.reply(update, HELP.format(time=self.cfg.daily_report_time.strftime("%H:%M")))

    @allowed
    async def cmd_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        summary = analytics.day_summary(self.db, self.today(), self.cfg.invest_rate)
        await self.reply(update, analytics.format_day_list(summary, self.cfg.currency))

    @allowed
    async def cmd_today(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.effective_chat.send_action(ChatAction.TYPING)
        await self.reply(update, await self.daily_report_text(self.today()))

    @allowed
    async def cmd_month(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        today = self.today()
        year, month = today.year, today.month
        if context.args:
            try:
                year, month = (int(x) for x in context.args[0].split("-"))
                date(year, month, 1)
            except ValueError:
                await self.reply(update, "Формат: /month 2026-09")
                return
        await self.reply(update, "⏳ Готую розгорнутий аналіз місяця, це може зайняти хвилину-дві…")
        await update.effective_chat.send_action(ChatAction.TYPING)
        await self.reply(update, await self.monthly_report_text(year, month))

    @allowed
    async def cmd_income(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        amount = parse_amount(context.args[0]) if context.args else None
        if amount is None:
            await self.reply(update, "Формат: /income 45000 зарплата")
            return
        note = " ".join(context.args[1:]) or None
        self.db.add_income(self.today(), amount, note, update.effective_chat.id)
        b = analytics.budget_block(self.db, self.today(), self.cfg.invest_rate)
        await self.reply(update, f"✅ Дохід {amount:.2f} {self.cfg.currency} записано.\n\n"
                         + analytics.format_budget(b, self.cfg.currency))

    @allowed
    async def cmd_invested(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        amount = parse_amount(context.args[0]) if context.args else None
        if amount is None:
            await self.reply(update, "Формат: /invested 4500")
            return
        self.db.add_investment(self.today(), amount, " ".join(context.args[1:]) or None, update.effective_chat.id)
        b = analytics.budget_block(self.db, self.today(), self.cfg.invest_rate)
        await self.reply(update, f"📈 Чудово! Відкладено {amount:.2f} {self.cfg.currency}.\n\n"
                         + analytics.format_budget(b, self.cfg.currency))

    @allowed
    async def cmd_undo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        kind = self.db.undo_last(update.effective_chat.id)
        names = {"receipt": "чек", "text": "запис", "statement": "виписку",
                 "incomes": "дохід", "investments": "інвестицію"}
        await self.reply(update, f"↩️ Скасовано останнє: {names.get(kind, kind)}." if kind else "Немає що скасовувати.")

    # --- вхідні дані ---------------------------------------------------------

    async def _save(self, update: Update, parsed: ParsedPurchases, source: str) -> None:
        today = self.today()
        purchases = to_purchases(parsed, today)
        user = update.effective_user
        author = user.first_name if user else None
        if purchases:
            self.db.add_purchases(purchases, source, update.effective_chat.id, author)
        for income in parsed.incomes:
            if income > 0:
                self.db.add_income(today, income, "з виписки", update.effective_chat.id)

        if not purchases and not parsed.incomes:
            await self.reply(update, "🤔 Не знайшов витрат. " + (parsed.comment or "Спробуйте чіткіше фото або текст."))
            return

        total = sum(p.amount for p in purchases)
        lines = [f"✅ Записано {len(purchases)} поз. на {total:.2f} {self.cfg.currency}"]
        if source != "statement":
            for p in purchases:
                qty = f" ×{p.quantity:g}" if p.quantity and p.quantity != 1 else ""
                lines.append(f"• {p.item}{qty} — {p.amount:.2f} [{p.category}]")
        else:
            by_cat: dict[str, float] = {}
            for p in purchases:
                by_cat[p.category] = by_cat.get(p.category, 0) + p.amount
            for cat, amt in sorted(by_cat.items(), key=lambda kv: -kv[1]):
                lines.append(f"• {cat}: {amt:.2f}")
        if parsed.incomes:
            lines.append(f"💵 Надходження: {sum(parsed.incomes):.2f} {self.cfg.currency}")
        if parsed.receipt_total and abs(parsed.receipt_total - total) > 0.5:
            lines.append(f"⚠️ Сума в чеку {parsed.receipt_total:.2f}, а позицій на {total:.2f} — перевірте.")
        if parsed.comment:
            lines.append(f"ℹ️ {parsed.comment}")
        lines.append("Помилка? /undo скасує це завантаження.")
        await self.reply(update, "\n".join(lines))

    @allowed
    async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.effective_chat.send_action(ChatAction.TYPING)
        file = await update.effective_message.photo[-1].get_file()
        data = bytes(await file.download_as_bytearray())
        parsed = await self.ai.parse_receipt(data, "image/jpeg", self.today())
        await self._save(update, parsed, "receipt")

    @allowed
    async def on_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        doc = update.effective_message.document
        name = (doc.file_name or "").lower()
        mime = doc.mime_type or ""
        await update.effective_chat.send_action(ChatAction.TYPING)
        data = bytes(await (await doc.get_file()).download_as_bytearray())

        if mime in ("image/jpeg", "image/png", "image/webp", "image/gif") or mime == "application/pdf":
            parsed = await self.ai.parse_receipt(data, mime, self.today())
            await self._save(update, parsed, "statement" if len(parsed.lines) > 40 else "receipt")
            return
        if not name.endswith((".csv", ".xlsx", ".xlsm", ".txt", ".tsv")):
            await self.reply(update, "Підтримую фото, PDF, CSV та XLSX.")
            return

        chunks = to_chunks(read_rows(data, name))
        if not chunks:
            await self.reply(update, "Файл порожній.")
            return
        await self.reply(update, f"⏳ Обробляю виписку ({len(chunks)} част.)…")
        merged = ParsedPurchases(lines=[], incomes=[], receipt_total=None, comment=None)
        for chunk in chunks:
            part = await self.ai.parse_statement_chunk(chunk, self.today())
            merged.lines.extend(part.lines)
            merged.incomes.extend(part.incomes)
        await self._save(update, merged, "statement")

    @allowed
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.effective_message.text.strip()
        await update.effective_chat.send_action(ChatAction.TYPING)
        amount = parse_amount(text)
        if amount is not None:  # просто сума — без звернення до AI
            self.db.add_purchases([Purchase(self.today(), "Без опису", "Інше", amount)], "text",
                                  update.effective_chat.id, update.effective_user.first_name)
            await self.reply(update, f"✅ Записано {amount:.2f} {self.cfg.currency} в «Інше». "
                             "Додайте опис наступного разу («350 продукти»), щоб аналіз був точнішим.")
            return
        intent = await self.ai.parse_text(text, self.today())
        if intent.kind == "purchases" and intent.purchases:
            await self._save(update, intent.purchases, "text")
            return
        await self.reply(update, await self.ai.answer_question(text, self.question_context()))

    def question_context(self) -> dict:
        today = self.today()
        prev = today.replace(day=1) - timedelta(days=1)
        return {
            "today": today.isoformat(),
            "current_month": analytics.month_summary(self.db, today.year, today.month, self.cfg.invest_rate),
            "previous_month": analytics.month_summary(self.db, prev.year, prev.month, self.cfg.invest_rate),
            "today_details": analytics.day_summary(self.db, today, self.cfg.invest_rate),
            "current_month_purchases": [
                dict(r) for r in self.db.purchases_between(*month_bounds(today.year, today.month))
            ],
        }

    # --- звіти ---------------------------------------------------------------

    async def daily_report_text(self, day: date) -> str:
        summary = analytics.day_summary(self.db, day, self.cfg.invest_rate)
        text = analytics.format_day_list(summary, self.cfg.currency)
        try:
            tips = await self.ai.daily_tips(summary)
        except anthropic.APIError:
            log.exception("daily tips failed")
            tips = ""
        return f"{text}\n\n💡 Поради:\n{tips}" if tips else text

    async def monthly_report_text(self, year: int, month: int) -> str:
        summary = analytics.month_summary(self.db, year, month, self.cfg.invest_rate)
        if not summary["purchases_count"]:
            return f"За {year}-{month:02d} даних немає."
        header = f"📊 ВЕЛИКИЙ АНАЛІЗ МІСЯЦЯ {year}-{month:02d}\n"
        return header + "\n" + await self.ai.monthly_report(summary)

    async def scheduled_daily(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        today = self.today()
        try:
            daily = await self.daily_report_text(today)
        except Exception:
            log.exception("daily report failed")
            return
        is_month_end = (today + timedelta(days=1)).month != today.month
        monthly = None
        if is_month_end:
            try:
                monthly = await self.monthly_report_text(today.year, today.month)
            except Exception:
                log.exception("monthly report failed")
                monthly = "⚠️ Не вдалося підготувати місячний аналіз — надішліть /month."
        for chat_id in self.cfg.allowed_chat_ids:
            await self.send(chat_id, daily, context)
            if monthly:
                await self.send(chat_id, monthly, context)

    # --- запуск --------------------------------------------------------------

    def build(self) -> Application:
        app = Application.builder().token(self.cfg.telegram_token).build()
        app.add_handler(CommandHandler(["start", "help"], self.cmd_start))
        app.add_handler(CommandHandler("today", self.cmd_today))
        app.add_handler(CommandHandler("list", self.cmd_list))
        app.add_handler(CommandHandler("month", self.cmd_month))
        app.add_handler(CommandHandler("income", self.cmd_income))
        app.add_handler(CommandHandler("invested", self.cmd_invested))
        app.add_handler(CommandHandler("undo", self.cmd_undo))
        app.add_handler(MessageHandler(filters.PHOTO, self.on_photo))
        app.add_handler(MessageHandler(filters.Document.ALL, self.on_document))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        app.job_queue.run_daily(self.scheduled_daily, time=self.cfg.daily_report_time, name="daily_report")
        return app


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = load_config()
    bot = BudgetBot(cfg, BudgetDB(cfg.db_path), BudgetAI(cfg.model, cfg.currency, cfg.anthropic_api_key))
    log.info("Бот запущено. Щоденний звіт о %s (%s)", cfg.daily_report_time.strftime("%H:%M"), cfg.timezone)
    bot.build().run_polling()
