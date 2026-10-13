"""Налаштування агента з змінних середовища (.env)."""

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Мінімальний читач .env, щоб не тягнути зайву залежність."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Config:
    anthropic_api_key: str | None
    telegram_token: str
    allowed_chat_ids: frozenset[int]
    timezone: ZoneInfo
    daily_report_time: time
    currency: str
    invest_rate: float
    db_path: Path
    model: str = "claude-opus-5-5"


def load_config() -> Config:
    _load_dotenv()
    tz = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Kyiv"))
    hh, mm = os.environ.get("DAILY_REPORT_TIME", "21:00").split(":")
    chat_ids = frozenset(
        int(x) for x in os.environ.get("ALLOWED_CHAT_IDS", "").replace(" ", "").split(",") if x
    )
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise SystemExit("Вкажіть TELEGRAM_BOT_TOKEN у файлі .env (див. .env.example)")
    return Config(
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        telegram_token=token,
        allowed_chat_ids=chat_ids,
        timezone=tz,
        daily_report_time=time(int(hh), int(mm), tzinfo=tz),
        currency=os.environ.get("CURRENCY", "грн"),
        invest_rate=float(os.environ.get("INVEST_RATE", "0.10")),
        db_path=Path(os.environ.get("DB_PATH", "data/budget.db")),
    )
