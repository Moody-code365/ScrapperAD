"""
bot/keyboards.py
================
Inline-клавиатуры. Весь UX строится на кнопках, а не на командах.

callback_data — это строка, которую кнопка отправляет боту при нажатии.
Формат у нас простой: "action" или "action:id" (например "analyze:5").
"""

from urllib.parse import quote

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config.settings import SUPPORT_USERNAME, PLANS


def main_menu(is_admin: bool = False, can_brand: bool = False) -> InlineKeyboardMarkup:
    """Главное меню. is_admin → кнопка админки; can_brand → кнопка бренда (Pro/Enterprise)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить конкурента", callback_data="add")
    kb.button(text="🕵️ Мои конкуренты", callback_data="list")
    kb.button(text="📊 Сводка по конкурентам", callback_data="digest")
    kb.button(text="🌐 Анализ ниши (AI)", callback_data="niche")
    if can_brand:
        kb.button(text="🏷 Мой бренд в PDF", callback_data="brand")
    kb.button(text="💳 Тариф и оплата", callback_data="plan")
    kb.button(text="❓ Как это работает", callback_data="help")
    if is_admin:
        kb.button(text="🛠 Админка", callback_data="admin")
    kb.adjust(1)
    return kb.as_markup()


def admin_kb() -> InlineKeyboardMarkup:
    """Админ-панель: быстрые кнопки статистики и пользователей."""
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Статистика", callback_data="admin:stats")
    kb.button(text="👥 Пользователи", callback_data="admin:users")
    kb.button(text="⬅️ Меню", callback_data="menu")
    kb.adjust(2, 1)
    return kb.as_markup()


def competitors_list(competitors) -> InlineKeyboardMarkup:
    """Список конкурентов: клик открывает меню конкурента."""
    kb = InlineKeyboardBuilder()
    for c in competitors:
        kb.button(text=f"🕵️ {c['name']}", callback_data=f"comp:{c['id']}")
    kb.button(text="➕ Добавить", callback_data="add")
    kb.button(text="⬅️ Меню", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


def advertiser_picker(competitor_id: int, pages: list) -> InlineKeyboardMarkup:
    """Выбор нужного рекламодателя: pages = [(page_id, page_name, count), ...]."""
    kb = InlineKeyboardBuilder()
    for pid, name, count in pages[:6]:
        kb.button(text=f"{name} ({count})"[:60], callback_data=f"pick:{competitor_id}:{pid}")
    kb.button(text="⬅️ Меню", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


def ad_library_kb(query: str, competitor_id: int) -> InlineKeyboardMarkup:
    """Кнопка-ссылка: открыть Библиотеку рекламы с поиском по конкуренту."""
    url = ("https://www.facebook.com/ads/library/?active_status=all&ad_type=all"
           f"&country=KZ&q={quote(query)}&search_type=page&media_type=all")
    kb = InlineKeyboardBuilder()
    kb.button(text="🔎 Найти в Библиотеке рекламы", url=url)
    kb.button(text="⬅️ К конкуренту", callback_data=f"comp:{competitor_id}")
    kb.adjust(1)
    return kb.as_markup()


def competitor_menu(competitor_id: int) -> InlineKeyboardMarkup:
    """Меню одного конкурента: все действия по нему."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Собрать рекламу", callback_data=f"analyze:{competitor_id}")
    kb.button(text="📋 Карточки в чат", callback_data=f"cards:{competitor_id}")
    kb.button(text="🧠 AI-разбор", callback_data=f"ai:{competitor_id}")
    kb.button(text="📑 PDF-отчёт", callback_data=f"rpdf:{competitor_id}")
    kb.button(text="📄 TXT-отчёт", callback_data=f"rtxt:{competitor_id}")
    kb.button(text="🎬 Все креативы", callback_data=f"crea:{competitor_id}")
    kb.button(text="🗑 Удалить", callback_data=f"del:{competitor_id}")
    kb.button(text="⬅️ К списку", callback_data="list")
    kb.adjust(1, 1, 1, 2, 1, 2)
    return kb.as_markup()


def back_to_menu() -> InlineKeyboardMarkup:
    """Одна кнопка — назад в меню."""
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Меню", callback_data="menu")
    return kb.as_markup()


def plan_kb() -> InlineKeyboardMarkup:
    """Под экраном тарифа: оплата звёздами / картой-Kaspi / Enterprise / назад."""
    kb = InlineKeyboardBuilder()
    kb.button(text=f"⭐ Start — {PLANS['start']['stars']}", callback_data="buy:start")
    kb.button(text=f"⭐ Pro — {PLANS['pro']['stars']}", callback_data="buy:pro")
    kb.button(text="💳 Картой / Kaspi", callback_data="pay")
    kb.button(text="🏢 Enterprise — по запросу", callback_data="enterprise")
    kb.button(text="⬅️ Меню", callback_data="menu")
    kb.adjust(2, 1, 1, 1)
    return kb.as_markup()


def pay_kb(prefill: str = None) -> InlineKeyboardMarkup:
    """Оплата картой/Kaspi — реквизиты в личке (deep-link с готовым сообщением)."""
    text = prefill or "Здравствуйте! Хочу оплатить тариф ScrapperAD картой / Kaspi."
    url = f"https://t.me/{SUPPORT_USERNAME}?text={quote(text)}"
    kb = InlineKeyboardBuilder()
    kb.button(text="💬 Написать для оплаты", url=url)
    kb.button(text="⬅️ Назад к тарифам", callback_data="plan")
    kb.adjust(1)
    return kb.as_markup()


def upsell_kb() -> InlineKeyboardMarkup:
    """Под апселл-сообщением: к тарифам / в меню."""
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Посмотреть тарифы", callback_data="plan")
    kb.button(text="⬅️ Меню", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


def help_kb() -> InlineKeyboardMarkup:
    """Помощь: связаться с поддержкой + назад."""
    kb = InlineKeyboardBuilder()
    kb.button(text="💬 Написать в поддержку", url=f"https://t.me/{SUPPORT_USERNAME}")
    kb.button(text="⬅️ Меню", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


def after_analysis(competitor_id: int) -> InlineKeyboardMarkup:
    """Кнопки под результатом анализа."""
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Карточки", callback_data=f"cards:{competitor_id}")
    kb.button(text="🧠 AI-разбор", callback_data=f"ai:{competitor_id}")
    kb.button(text="📑 PDF", callback_data=f"rpdf:{competitor_id}")
    kb.button(text="📄 TXT", callback_data=f"rtxt:{competitor_id}")
    kb.button(text="🎬 Креативы", callback_data=f"crea:{competitor_id}")
    kb.button(text="🔁 Обновить", callback_data=f"analyze:{competitor_id}")
    kb.button(text="⬅️ Меню", callback_data="menu")
    kb.adjust(2, 2, 1, 2)
    return kb.as_markup()


def cancel_kb() -> InlineKeyboardMarkup:
    """Кнопка отмены во время диалога добавления."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✖️ Отмена", callback_data="menu")
    return kb.as_markup()
