"""
config/settings.py
==================
Все настройки в одном месте. Читаются из файла .env.
Смотри .env.example — там описаны все переменные.
"""

import os
import re
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


# ── Тарифы ───────────────────────────────────────────────────────────────────────
# Один источник правды по лимитам и ценам. Цены в тенге (₸).
# competitors        — сколько конкурентов можно держать на отслеживании
# analyses_per_month — сколько запусков анализа в месяц (контроль расходов на Apify)
# ads_per_scan       — потолок объявлений за один скан. ⚠️ ПРЯМО влияет на расходы Apify:
#                      ~$0.006 за объявление. Худший случай за месяц ≈
#                      analyses_per_month × ads_per_scan × $0.006. Держи цену тарифа
#                      заметно выше этой суммы (особенно для Enterprise с большим скан-лимитом).

PLANS = {
    "free": {
        "name": "Free",
        "price_kzt": 0,
        "stars": 0,
        "competitors": 1,
        "analyses_per_month": 1,
        "ads_per_scan": 20,    # потолок объявлений за один скан (контроль расходов Apify)
        "pdf": False,
        "ai": False,
    },
    "start": {
        "name": "Start",
        "price_kzt": 7900,
        "stars": 1300,   # ~$16.9 выплаты (1⭐≈$0.013) ≈ 7900₸ — see расчёт
        "competitors": 5,
        "analyses_per_month": 50,
        "ads_per_scan": 30,
        "pdf": True,     # все фичи доступны уже на самом дешёвом платном
        "ai": True,
    },
    "pro": {
        "name": "Pro",
        "price_kzt": 19900,
        "stars": 3300,   # ~$42.9 выплаты ≈ 19900₸
        "competitors": 30,
        "analyses_per_month": 80,
        "ads_per_scan": 50,
        "pdf": True,
        "ai": True,
    },
    # Enterprise — для агентств и «выкачать ВСЮ рекламу гиганта» (1Fit ~700 объявл.).
    # Не продаётся через Stars (stars=0) — оформляется ПО ЗАПРОСУ и выдаётся вручную
    # (/grant <id> enterprise). Цена ниже — якорь «от», финальную называешь в личке.
    # ⚠️ Худший случай расходов Apify ≈ 20 × 1000 × $0.006 ≈ $120 (~62 000₸) — поэтому
    #    цена-якорь держится выше, и объём согласуется под конкретного клиента.
    "enterprise": {
        "name": "Enterprise",
        "price_kzt": 99000,
        "stars": 0,            # 0 → продаём не звёздами, а по запросу
        "competitors": 50,
        "analyses_per_month": 20,
        "ads_per_scan": 1000,  # собираем всю рекламу крупного конкурента за один скан
        "pdf": True,
        "ai": True,
    },
}

# Длительность подписки за одну оплату (дней)
SUBSCRIPTION_DAYS = 30

DEFAULT_PLAN = "free"

# Контакт поддержки (Telegram username без @) — задаётся в .env как SUPPORT_USERNAME.
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "")

# Реквизиты Kaspi/карты в коде НЕ храним (приватность + код может стать публичным).
# Оплату картой принимаем в личке. Если всё же хочешь показывать номер в боте —
# задай KASPI_NUMBER в .env; по умолчанию пусто и номер нигде не светится.
KASPI_NUMBER = os.getenv("KASPI_NUMBER", "")
KASPI_NAME = os.getenv("KASPI_NAME", "")


def plan_limits(plan: str) -> dict:
    """Возвращает лимиты тарифа (или Free, если тариф неизвестен)."""
    return PLANS.get(plan, PLANS[DEFAULT_PLAN])


@dataclass
class Settings:
    """Настройки бота."""

    # --- Токены ---
    bot_token: str
    apify_token: str         # первый Apify-ключ (обратная совместимость)
    apify_tokens: list       # все ключи Apify (авто-переключение при исчерпании лимита)
    gemini_api_key: str      # AI-разбор конкурентов (бесплатный тир Gemini)
    anthropic_api_key: str   # для AI-креативов (Фаза 2). Сейчас может быть пустым.

    # --- База данных ---
    database_path: str

    # --- Администратор ---
    admin_id: int

    # --- Режим отладки ---
    debug: bool


def load_settings() -> Settings:
    """Читает .env. Падает с понятной ошибкой, если нет BOT_TOKEN."""
    bot_token = os.getenv("BOT_TOKEN", "")
    if not bot_token or bot_token == "your_telegram_bot_token_here":
        raise ValueError(
            "❌ BOT_TOKEN не задан!\n"
            "Скопируй .env.example в .env и вставь токен от @BotFather"
        )

    # Apify: можно задать несколько ключей через запятую/пробел/перенос строки в
    # APIFY_API_TOKENS — бот переключится на следующий, когда у текущего кончится
    # лимит. Если задан только старый APIFY_API_TOKEN — используем его.
    raw_tokens = os.getenv("APIFY_API_TOKENS", "") or os.getenv("APIFY_API_TOKEN", "")
    apify_tokens = [t.strip() for t in re.split(r"[,\s]+", raw_tokens) if t.strip()]

    return Settings(
        bot_token=bot_token,
        apify_token=apify_tokens[0] if apify_tokens else "",
        apify_tokens=apify_tokens,
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        database_path=os.getenv("DATABASE_PATH", "data/scrapperad.db"),
        admin_id=int(os.getenv("ADMIN_TELEGRAM_ID", "0")),
        debug=os.getenv("DEBUG", "True").lower() == "true",
    )


settings = load_settings()
