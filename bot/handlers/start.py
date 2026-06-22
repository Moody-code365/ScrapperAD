"""
bot/handlers/start.py
=====================
Онбординг и общие экраны: /start, меню, помощь, тариф, /cancel.

Разметка — HTML (включена по умолчанию в main.py). Любую динамику (имя юзера,
тариф) пропускаем через html.escape, чтобы спецсимволы не ломали разметку.
"""

import logging
from html import escape
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, LabeledPrice, PreCheckoutQuery
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from database.db import (
    get_connection, get_or_create_user, get_admin_stats, set_user_plan, active_plan, set_brand,
    find_user_by_username, get_recent_users,
)
from config.settings import (
    settings, PLANS, plan_limits, SUPPORT_USERNAME, SUBSCRIPTION_DAYS,
)
from bot.keyboards import main_menu, back_to_menu, help_kb, plan_kb, pay_kb, admin_kb, cancel_kb

logger = logging.getLogger(__name__)
router = Router()

BRAND_PLANS = ("pro", "enterprise")   # тарифы с white-label (свой бренд в PDF)


def _e(x) -> str:
    return escape(str(x)) if x is not None else ""


def _menu_for(telegram_id: int, user):
    """Главное меню с учётом прав: админ-кнопка и кнопка бренда (Pro/Enterprise)."""
    return main_menu(
        is_admin=(telegram_id == settings.admin_id),
        can_brand=active_plan(user) in BRAND_PLANS,
    )


WELCOME = (
    "👋 Привет, <b>{name}</b>!\n\n"
    "Это <b>ScrapperAD</b> — шпион за рекламой конкурентов.\n\n"
    "Покажу, <b>какую рекламу прямо сейчас крутят</b> твои конкуренты в "
    "Facebook и Instagram: тексты, картинки, видео, как давно идёт объявление.\n\n"
    "Данные — из официальной библиотеки рекламы Meta. Всё честно и легально.\n\n"
    "👇 Выбери действие:"
)

HELP_TEXT = (
    "<b>📖 Как это работает</b>\n"
    "1️⃣ Добавь конкурента — имя + ссылка на его Instagram.\n"
    "2️⃣ Открой конкурента и выбери действие.\n"
    "3️⃣ Смотри рекламу, качай отчёты, получай AI-разбор.\n\n"
    "<b>🧰 Что умеет бот</b>\n"
    "🔍 <b>Собрать рекламу</b> — находит объявления и даёт сводку (сколько, оффер, площадки).\n"
    "📋 <b>Карточки в чат</b> — показать объявления: текст + креатив.\n"
    "🧠 <b>AI-разбор</b> — ИИ смотрит креативы и пишет: оффер, хуки, как обойти.\n"
    "📑 <b>PDF-отчёт</b> — красивый файл для себя или клиента.\n"
    "📄 <b>TXT-отчёт</b> — все тексты и ссылки одним файлом.\n"
    "🎬 <b>Все креативы</b> — скачать все видео и картинки конкурента.\n"
    "📊 <b>Сводка</b> — все конкуренты на одном экране + изменения.\n"
    "🌐 <b>Анализ ниши</b> — сводный AI-разбор нескольких конкурентов + PDF.\n"
    "🏷 <b>Свой бренд в PDF</b> (Pro/Enterprise) — кнопка «🏷 Мой бренд в PDF» в меню: лого и название агентства в шапке отчётов.\n\n"
    "💡 <b>Как читать:</b> чем дольше объявление крутится и чем их больше — тем лучше "
    "оно работает. Бери связку (оффер + креатив + формат) и делай сильнее.\n\n"
    "<b>➕ Как добавить конкурента:</b>\n"
    "Кинь ссылку на его <b>Instagram</b> (или @ник). Также подойдёт Facebook-страница "
    "или ссылка из Библиотеки рекламы Meta.\n\n"
    "ℹ️ Бюджет, показы и города Meta не раскрывает для коммерческой рекламы — "
    "этих данных нет ни у кого."
)


async def _safe_edit(callback: CallbackQuery, text: str, markup):
    """Редактирует сообщение; если оно с фото (нельзя edit_text) — шлёт новое."""
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception:
        await callback.message.answer(text, reply_markup=markup)


# ── /start ───────────────────────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    conn = get_connection(settings.database_path)
    user = get_or_create_user(
        conn,
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
    )
    conn.close()

    await message.answer(
        WELCOME.format(name=_e(message.from_user.first_name)),
        reply_markup=_menu_for(message.from_user.id, user),
    )


# ── Меню ───────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    conn = get_connection(settings.database_path)
    user = get_or_create_user(conn, callback.from_user.id)
    conn.close()
    await _safe_edit(
        callback, WELCOME.format(name=_e(callback.from_user.first_name)),
        _menu_for(callback.from_user.id, user),
    )
    await callback.answer()


# ── Помощь ───────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "help")
async def cb_help(callback: CallbackQuery):
    await _safe_edit(callback, HELP_TEXT, help_kb())
    await callback.answer()


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT, reply_markup=help_kb())


# ── Тариф ───────────────────────────────────────────────────────────────────────

def _plan_text(user) -> str:
    plan = active_plan(user)
    limits = plan_limits(plan)
    lines = [f"💳 <b>Твой тариф: {_e(limits['name'])}</b>"]
    exp = user["plan_expires_at"] if user else None
    if plan != "free" and exp:
        lines.append(f"<i>действует до {exp[:10]}</i>")
    lines += [
        "",
        f"• Конкурентов: до {limits['competitors']}",
        f"• Анализов в месяц: {limits['analyses_per_month']}",
        f"• Объявлений за скан: до {limits['ads_per_scan']}",
        "",
        "<b>Тарифы (оплата ⭐ в боте):</b>",
    ]
    for key, p in PLANS.items():
        kzt = f"{p['price_kzt']:,} ₸".replace(",", " ")
        if p["price_kzt"] == 0:
            price = "бесплатно"
        elif not p.get("stars"):          # Enterprise — продаём по запросу, не звёздами
            price = f"от {kzt} · по запросу"
        else:
            price = f"{kzt} / ⭐{p['stars']} · {SUBSCRIPTION_DAYS} дней"
        mark = " ← ты здесь" if key == plan else ""
        feats = ["📑 PDF" if p.get("pdf") else "📄 TXT"]
        if p.get("ai"):
            feats.append("🧠 AI-разбор")
        if key in BRAND_PLANS:
            feats.append("🏷 White-label")
        lines.append(
            f"\n<b>{_e(p['name'])}</b> — {price}{mark}\n"
            f"  до {p['competitors']} конкурентов · {p['analyses_per_month']} анализов · {' · '.join(feats)}"
        )
    lines.append(f"\n\nВопросы по оплате: @{_e(SUPPORT_USERNAME)}")
    return "\n".join(lines)


@router.callback_query(F.data == "plan")
async def cb_plan(callback: CallbackQuery):
    conn = get_connection(settings.database_path)
    user = get_or_create_user(conn, callback.from_user.id)
    conn.close()
    await _safe_edit(callback, _plan_text(user), plan_kb())
    await callback.answer()


@router.message(Command("plan"))
async def cmd_plan(message: Message):
    conn = get_connection(settings.database_path)
    user = get_or_create_user(conn, message.from_user.id)
    conn.close()
    await message.answer(_plan_text(user), reply_markup=plan_kb())


# ── Оплата картой / Kaspi (реквизиты в личке) ─────────────────────────────────

def _payment_text() -> str:
    lines = [
        "💳 <b>Оплата картой / Kaspi</b>\n",
        "Самый быстрый способ — <b>⭐ Telegram Stars</b> прямо в боте: тариф включается "
        "автоматически (кнопки ⭐ на экране «Тариф»).\n",
        "Если удобнее <b>картой или через Kaspi</b> — напиши мне в личку, пришлю "
        "реквизиты и включу тариф вручную (обычно в течение пары часов) 🙌\n",
        "<b>Тарифы:</b>",
    ]
    for p in PLANS.values():
        if p["price_kzt"] > 0:
            price = f"{p['price_kzt']:,} ₸/мес".replace(",", " ")
            suffix = " (по запросу)" if not p.get("stars") else ""
            lines.append(f"• {p['name']} — {price}{suffix}")
    return "\n".join(lines)


ENTERPRISE_TEXT = (
    "🏢 <b>Enterprise — для агентств и больших задач</b>\n\n"
    "• Сбор <b>всей</b> рекламы конкурента-гиганта (сотни объявлений за один скан)\n"
    f"• До {PLANS['enterprise']['competitors']} конкурентов · "
    f"{PLANS['enterprise']['analyses_per_month']} больших анализов в месяц\n"
    f"• До {PLANS['enterprise']['ads_per_scan']} объявлений за скан\n"
    "• 🧠 AI-разбор · 📑 PDF · 🏷 White-label\n\n"
    "Объём и цену подбираем под задачу. Напиши — рассчитаю под тебя 👇"
)


@router.callback_query(F.data == "pay")
async def cb_pay(callback: CallbackQuery):
    await _safe_edit(callback, _payment_text(), pay_kb())
    await callback.answer()


@router.callback_query(F.data == "enterprise")
async def cb_enterprise(callback: CallbackQuery):
    await _safe_edit(
        callback, ENTERPRISE_TEXT,
        pay_kb("Здравствуйте! Интересует тариф Enterprise в ScrapperAD."),
    )
    await callback.answer()


@router.message(Command("grant"))
async def cmd_grant(message: Message):
    """Админ: выдать тариф пользователю. /grant <id или @ник> <free|start|pro|enterprise>"""
    if message.from_user.id != settings.admin_id:
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or parts[2] not in PLANS:
        await message.answer(f"Использование: /grant &lt;id или @username&gt; &lt;{' / '.join(PLANS)}&gt;")
        return
    target, plan = parts[1], parts[2]
    conn = get_connection(settings.database_path)
    if target.lstrip("@").isdigit():
        tid = int(target.lstrip("@"))
        get_or_create_user(conn, tid)
    else:
        tid = find_user_by_username(conn, target)
    if not tid:
        conn.close()
        await message.answer(
            f"Не нашёл {_e(target)} в базе. Сначала пусть нажмёт /start в боте — "
            f"потом выдавай по @username или id (список: /users)."
        )
        return
    set_user_plan(conn, tid, plan)
    conn.close()
    await message.answer(f"✅ Тариф <b>{PLANS[plan]['name']}</b> выдан (id <code>{tid}</code>, бессрочно).")
    try:
        await message.bot.send_message(
            tid, f"🎉 Твой тариф активирован: <b>{PLANS[plan]['name']}</b>! Спасибо 🙌")
    except Exception:
        pass


@router.message(Command("users"))
async def cmd_users(message: Message):
    """Админ: последние пользователи (id · @ник · тариф) — для выдачи тарифов."""
    if message.from_user.id != settings.admin_id:
        return
    conn = get_connection(settings.database_path)
    text = _users_text(conn)
    conn.close()
    await message.answer(text)


# ── Автооплата через Telegram Stars ───────────────────────────────────────────

@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(callback: CallbackQuery):
    plan = callback.data.split(":", 1)[1]
    p = PLANS.get(plan)
    if not p or not p.get("stars"):
        await callback.answer("Тариф недоступен", show_alert=True)
        return
    await callback.answer()
    await callback.message.answer_invoice(
        title=f"ScrapperAD — {p['name']}",
        description=(
            f"Тариф {p['name']} на {SUBSCRIPTION_DAYS} дней: до {p['competitors']} конкурентов, "
            f"{p['analyses_per_month']} анализов/мес, AI-разбор и PDF-отчёты."
        ),
        payload=f"plan:{plan}",
        provider_token="",          # пусто для оплаты звёздами (XTR)
        currency="XTR",
        prices=[LabeledPrice(label=f"{p['name']} · {SUBSCRIPTION_DAYS} дней", amount=p["stars"])],
    )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_paid(message: Message):
    payload = message.successful_payment.invoice_payload or ""
    plan = payload.split(":", 1)[1] if payload.startswith("plan:") else None
    if plan not in PLANS:
        return
    conn = get_connection(settings.database_path)
    set_user_plan(conn, message.from_user.id, plan, days=SUBSCRIPTION_DAYS)
    user = get_or_create_user(conn, message.from_user.id)
    conn.close()
    extra = ""
    if plan in BRAND_PLANS:
        extra = ("\n\n🏷 Тебе доступен <b>white-label</b>: жми «🏷 Мой бренд в PDF» в меню — "
                 "добавь логотип и название, они появятся в отчётах для клиентов.")
    await message.answer(
        f"🎉 Оплата получена! Тариф <b>{PLANS[plan]['name']}</b> активен {SUBSCRIPTION_DAYS} дней.\n"
        f"Спасибо 🙌 Открывай 🕵️ «Мои конкуренты».{extra}",
        reply_markup=_menu_for(message.from_user.id, user),
    )


# ── Админ-панель (только админ) ───────────────────────────────────────────────

ADMIN_TEXT = (
    "🛠 <b>Админ-панель</b>\n\n"
    "<b>Команды:</b>\n"
    "• <code>/grant &lt;id или @ник&gt; &lt;free|start|pro|enterprise&gt;</code> — выдать тариф (бессрочно)\n"
    "• <code>/users</code> — последние пользователи\n"
    "• <code>/stats</code> — статистика\n"
    "• <code>/admin</code> — открыть эту панель\n\n"
    "💡 <b>Enterprise</b> выдаётся только так: <code>/grant &lt;id&gt; enterprise</code>.\n"
    "id бери в 👥 «Пользователи» (юзер должен сперва нажать /start в боте).\n"
    "Оплату картой/Kaspi подтверждаешь в личке → выдаёшь тариф через /grant."
)


def _stats_text(conn) -> str:
    s = get_admin_stats(conn)
    bp = s["by_plan"]
    return (
        "📊 <b>Статистика ScrapperAD</b>\n\n"
        f"👥 Пользователей: <b>{s['users']}</b> (+{s['new_users_7d']} за 7 дней)\n"
        f"   free: {bp.get('free', 0)} · start: {bp.get('start', 0)} · "
        f"pro: {bp.get('pro', 0)} · enterprise: {bp.get('enterprise', 0)}\n"
        f"🟢 Активны за 7 дней: <b>{s['active_7d']}</b>\n\n"
        f"🕵️ Конкурентов: <b>{s['competitors']}</b>\n"
        f"🔍 Анализов всего: <b>{s['analyses_total']}</b> · за месяц: <b>{s['analyses_month']}</b>"
    )


def _users_text(conn) -> str:
    rows = get_recent_users(conn, 20)
    if not rows:
        return "Пользователей пока нет."
    lines = ["👥 <b>Последние пользователи</b> (id · ник · тариф):\n"]
    for r in rows:
        uname = f"@{r['username']}" if r["username"] else "—"
        lines.append(f"<code>{r['telegram_id']}</code> · {_e(uname)} · {r['plan']}")
    return "\n".join(lines)


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != settings.admin_id:
        return
    await message.answer(ADMIN_TEXT, reply_markup=admin_kb())


@router.callback_query(F.data == "admin")
async def cb_admin(callback: CallbackQuery):
    if callback.from_user.id != settings.admin_id:
        await callback.answer("Только для администратора", show_alert=True)
        return
    await callback.answer()
    await callback.message.answer(ADMIN_TEXT, reply_markup=admin_kb())


@router.callback_query(F.data == "admin:stats")
async def cb_admin_stats(callback: CallbackQuery):
    if callback.from_user.id != settings.admin_id:
        await callback.answer("Только для администратора", show_alert=True)
        return
    conn = get_connection(settings.database_path)
    text = _stats_text(conn)
    conn.close()
    await callback.answer()
    await callback.message.answer(text, reply_markup=admin_kb())


@router.callback_query(F.data == "admin:users")
async def cb_admin_users(callback: CallbackQuery):
    if callback.from_user.id != settings.admin_id:
        await callback.answer("Только для администратора", show_alert=True)
        return
    conn = get_connection(settings.database_path)
    text = _users_text(conn)
    conn.close()
    await callback.answer()
    await callback.message.answer(text, reply_markup=admin_kb())


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id != settings.admin_id:
        return
    conn = get_connection(settings.database_path)
    text = _stats_text(conn)
    conn.close()
    await message.answer(text)


# ── White-label: свой бренд в PDF (Pro) ───────────────────────────────────────

class BrandStates(StatesGroup):
    waiting_name = State()
    waiting_logo = State()


async def _start_brand(target: Message, user_telegram_id: int, state: FSMContext):
    """Запуск настройки бренда — общий для команды /brand и кнопки «🏷 Мой бренд в PDF»."""
    conn = get_connection(settings.database_path)
    user = get_or_create_user(conn, user_telegram_id)
    conn.close()
    if active_plan(user) not in BRAND_PLANS:
        await target.answer(
            "🏷 <b>Свой бренд в PDF</b> (логотип и название агентства в шапке отчётов) — "
            "на тарифах <b>Pro</b> и <b>Enterprise</b>.\nОткрой 💳 «Тариф и оплата».",
            reply_markup=back_to_menu())
        return
    await state.set_state(BrandStates.waiting_name)
    await target.answer(
        "🏷 <b>Свой бренд в PDF</b>\n\n"
        "<b>Шаг 1/2.</b> Введи <b>название</b> для шапки отчётов "
        "(или отправь /skip — без названия):",
        reply_markup=cancel_kb())


@router.message(Command("brand"))
async def cmd_brand(message: Message, state: FSMContext):
    await _start_brand(message, message.from_user.id, state)


@router.callback_query(F.data == "brand")
async def cb_brand(callback: CallbackQuery, state: FSMContext):
    await _start_brand(callback.message, callback.from_user.id, state)
    await callback.answer()


@router.message(BrandStates.waiting_name)
async def brand_name(message: Message, state: FSMContext):
    name = None if message.text.strip() == "/skip" else message.text.strip()
    await state.update_data(name=name)
    await state.set_state(BrandStates.waiting_logo)
    await message.answer(
        "<b>Шаг 2/2.</b> Пришли <b>логотип</b> картинкой "
        "(или отправь /skip — без логотипа):",
        reply_markup=cancel_kb())


async def _finish_brand(message: Message, state: FSMContext, logo_file_id):
    data = await state.get_data()
    await state.clear()
    conn = get_connection(settings.database_path)
    set_brand(conn, message.from_user.id, data.get("name"), logo_file_id)
    user = get_or_create_user(conn, message.from_user.id)
    conn.close()
    if not data.get("name") and not logo_file_id:
        msg = "✅ Бренд сброшен — в PDF снова будет ScrapperAD."
    else:
        what = "название и логотип" if logo_file_id else "название"
        msg = f"✅ Бренд сохранён — {what} появятся в шапке твоих PDF-отчётов."
    await message.answer(msg, reply_markup=_menu_for(message.from_user.id, user))


@router.message(BrandStates.waiting_logo, F.photo)
async def brand_logo(message: Message, state: FSMContext):
    await _finish_brand(message, state, message.photo[-1].file_id)


@router.message(BrandStates.waiting_logo)
async def brand_logo_skip(message: Message, state: FSMContext):
    await _finish_brand(message, state, None)


# ── /cancel ───────────────────────────────────────────────────────────────────────

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    conn = get_connection(settings.database_path)
    user = get_or_create_user(conn, message.from_user.id)
    conn.close()
    await message.answer("❌ Отменено.", reply_markup=_menu_for(message.from_user.id, user))
