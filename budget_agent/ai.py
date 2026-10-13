"""Виклики Claude: розбір чеків/тексту/виписок, поради, місячний аналіз."""

import base64
import json
from datetime import date
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from budget_agent.categories import CATEGORIES

Category = Literal[CATEGORIES]  # type: ignore[valid-type]

FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM = f"""Ти — фінансовий помічник сімейного бюджету. Пишеш українською, тепло й по суті.
Категорії витрат: {", ".join(CATEGORIES)}.
Правило сім'ї: 10% кожного доходу йде на інвестиції, тому витрачати можна лише решту 90%.
Усі суми — у валюті сім'ї; не вигадуй цифр, яких немає в даних."""


class Line(BaseModel):
    item: str = Field(description="Зрозуміла назва товару/послуги, напр. «Молоко 2.5% 900 мл»")
    category: Category
    subcategory: str | None = Field(description="Напр. «молочне», «м'ясо», «солодощі», «пальне»")
    quantity: float | None
    amount: float = Field(description="Підсумкова сума рядка з урахуванням знижок, додатне число")
    store: str | None
    date: str | None = Field(description="YYYY-MM-DD, якщо відомо")


class ParsedPurchases(BaseModel):
    lines: list[Line]
    incomes: list[float] = Field(description="Надходження (зарплата тощо), якщо це виписка")
    receipt_total: float | None = Field(description="«Сума до сплати» з чека, для перевірки")
    comment: str | None = Field(description="Що було незрозуміло, коротко")


class TextIntent(BaseModel):
    kind: Literal["purchases", "question"]
    purchases: ParsedPurchases | None


class BudgetAI:
    def __init__(self, model: str, currency: str, api_key: str | None = None):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model
        self.currency = currency

    async def _parse(self, content: list | str, schema: type[BaseModel], effort: str = "medium"):
        response = await self.client.beta.messages.parse(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            output_config={"effort": effort},
            output_format=schema,
            messages=[{"role": "user", "content": content}],
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("Claude відмовився обробляти цей запит.")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("Відповідь вийшла задовгою — спробуйте надіслати менший файл.")
        return response.parsed_output

    async def _write(self, prompt: str, effort: str, max_tokens: int = 16000) -> str:
        async with self.client.beta.messages.stream(
            model=self.model,
            max_tokens=max_tokens,
            system=SYSTEM,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            output_config={"effort": effort},
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            response = await stream.get_final_message()
        if response.stop_reason == "refusal":
            return "Не вдалося сформувати текст цього разу."
        return "".join(b.text for b in response.content if b.type == "text").strip()

    # --- розбір вхідних даних ----------------------------------------------

    async def parse_receipt(self, data: bytes, media_type: str, today: date) -> ParsedPurchases:
        """Фото/скан чека (jpeg/png/webp/gif) або PDF."""
        block_type = "document" if media_type == "application/pdf" else "image"
        source = {"type": "base64", "media_type": media_type, "data": base64.standard_b64encode(data).decode()}
        return await self._parse(
            [
                {"type": block_type, "source": source},
                {"type": "text", "text": (
                    f"Сьогодні {today.isoformat()}. Це чек або виписка. Випиши КОЖНУ позицію окремим рядком: "
                    "зрозуміла назва товару, категорія, підкатегорія, кількість, підсумкова сума рядка зі знижкою. "
                    "Магазин і дату візьми з чека. Не додавай рядки «Сума», «Знижка», «Решта» як товари. "
                    "receipt_total — підсумок до сплати з чека."
                )},
            ],
            ParsedPurchases,
        )

    async def parse_text(self, text: str, today: date) -> TextIntent:
        """Повідомлення типу «350 продукти АТБ», «хліб 32, молоко 45» або питання."""
        return await self._parse(
            f"Сьогодні {today.isoformat()}. Повідомлення від члена сім'ї:\n<<<\n{text}\n>>>\n"
            "Якщо це запис витрат — kind=purchases і розклади на рядки (якщо лише сума без опису — "
            "item «Без опису», категорія «Інше»; «вчора» — це вчорашня дата). "
            "Якщо це питання чи прохання про бюджет — kind=question, purchases=null.",
            TextIntent,
            effort="low",
        )

    async def parse_statement_chunk(self, table_text: str, today: date) -> ParsedPurchases:
        """Частина банківської виписки/таблиці витрат у текстовому вигляді."""
        return await self._parse(
            f"Сьогодні {today.isoformat()}. Нижче фрагмент банківської виписки або таблиці витрат "
            "(рядки розділені символом |). Для кожної ВИТРАТИ створи рядок: опис (зрозумілою мовою, "
            "напр. «Сільпо — продукти»), категорія, сума (додатне число), магазин/отримувач, дата. "
            "Надходження (зарплата, повернення) додай у incomes. Перекази між власними рахунками, "
            "поповнення депозитів/інвестицій і зняття готівки не вважай витратами — пропусти їх.\n\n"
            + table_text,
            ParsedPurchases,
        )

    # --- аналітика ---------------------------------------------------------

    async def daily_tips(self, day_summary: dict) -> str:
        return await self._write(
            "Ось дані за сьогодні та місяць (JSON):\n"
            + json.dumps(day_summary, ensure_ascii=False)
            + f"\n\nВалюта: {self.currency}. Напиши 2–4 короткі пункти: що сьогодні вплинуло на бюджет найбільше "
            "і конкретно де можна заощадити (дешевші аналоги, інший магазин, відмова від імпульсних покупок). "
            "Якщо витрат немає — коротко підбадьор. Пиши простим текстом з емодзі, без Markdown (* та #).",
            effort="low",
            max_tokens=4000,
        )

    async def monthly_report(self, month_summary: dict) -> str:
        return await self._write(
            "Ось повні дані сімейного бюджету за місяць (JSON):\n"
            + json.dumps(month_summary, ensure_ascii=False)
            + f"\n\nВалюта: {self.currency}. Зроби великий розгорнутий аналіз місяця:\n"
            "1. Підсумок: скільки витрачено, порівняння з минулим місяцем.\n"
            "2. Куди йде найбільше грошей — топ категорій з частками, що змінилось.\n"
            "3. Продукти й товари: що купували найчастіше і на що пішло найбільше, імпульсні/дрібні покупки.\n"
            "4. Магазини: де витрачали найбільше, чи варто змінити магазин.\n"
            "5. Інвестиції 10%: скільки треба було відкласти, скільки відкладено, чи вистачило грошей; "
            "якщо дохід невідомий — нагадай внести його.\n"
            "6. Конкретний план економії на наступний місяць: 5–7 пунктів з орієнтовною сумою економії кожного.\n"
            "7. Ліміти на наступний місяць по основних категоріях.\n"
            "Пиши простим текстом з емодзі та нумерацією, без Markdown (* та #).",
            effort="high",
        )

    async def answer_question(self, question: str, context: dict) -> str:
        return await self._write(
            "Дані бюджету (JSON):\n" + json.dumps(context, ensure_ascii=False)
            + f"\n\nВалюта: {self.currency}. Питання члена сім'ї:\n<<<\n{question}\n>>>\n"
            "Відповідай коротко, на основі даних. Простим текстом, без Markdown.",
            effort="medium",
            max_tokens=8000,
        )
