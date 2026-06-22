"""
bot/handlers/spy.py
===================
Ядро продукта: слежка за рекламой конкурентов.

Навигация на кнопках:
- 📊 Сводка        → дашборд по всем конкурентам (из кэша, без трат)
- 🕵️ Мои конкуренты → список → меню конкурента (реклама / отчёт / удалить)
- ➕ Добавить       → диалог (название → Instagram-ссылка)

Контроль расходов (дневной кэш):
- Каждый реальный анализ = 1 запуск Apify (деньги) и тратит лимит тарифа.
- Результат кэшируется на сутки. Повторные просмотры этого конкурента в течение
  24ч (тем же или другим юзером) отдаются ИЗ КЭША — бесплатно и без списания лимита.
- Если лимит исчерпан, но есть прошлые данные — показываем их (без свежего запроса).

Разметка — HTML (по умолчанию в main.py). Всю динамику пропускаем через _e()
(html.escape), чтобы ник/текст с символами _ * < & не ломали сообщение.
"""

import os
import asyncio
import logging
from io import BytesIO
from html import escape
from urllib.parse import quote, unquote

import aiohttp
from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery, BufferedInputFile, LinkPreviewOptions,
    InputMediaPhoto, InputMediaVideo,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from database.db import (
    get_connection, get_or_create_user, active_plan,
    add_competitor, get_user_competitors, get_competitor,
    count_user_competitors, deactivate_competitor, update_competitor_page,
    log_analysis, count_analyses_this_month, get_recent_ad_counts,
)
from services.ad_spy import (
    analyze_competitor, _format_date, PLATFORM_NAMES, CompetitorAdsReport,
    cache_report, load_cached_report, cache_age_hours, delete_cache,
    extract_search_query, search_url,
)
from services.apify_pool import ApifyKeyPool
from reports.ad_report import save_report_file
from reports.pdf_report import build_pdf, build_niche_pdf
from services.ai_review import (
    generate_review, load_ai_review, save_ai_review, delete_ai_review,
    suggest_brand_query, analyze_niche,
)
from config.settings import settings, plan_limits
from bot.keyboards import (
    main_menu, competitors_list, competitor_menu,
    back_to_menu, after_analysis, cancel_kb, upsell_kb,
    advertiser_picker, ad_library_kb,
)

logger = logging.getLogger(__name__)
router = Router()

# Пул Apify-ключей: при исчерпании лимита одного ключа сбор автоматически
# продолжится на следующем. Состояние «кто исчерпан» переживает перезапуск.
_apify_pool = ApifyKeyPool(settings.apify_tokens)

MAX_CARDS = 6                       # сколько объявлений показываем карточками
CACHE_FRESH_HOURS = 24              # сколько часов кэш считается «свежим»
MEDIA_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

UPSELL_FREE = (
    "🔓 Это был твой <b>бесплатный разбор</b> (1 в месяц на Free).\n\n"
    "Полный доступ — на тарифе <b>Start (7 900₸/мес)</b>:\n"
    "• 5 конкурентов\n"
    "• 60 разведок в месяц\n"
    "• 🧠 AI-разбор\n"
    "• 📑 PDF-отчёты\n"
    "• 🎬 все креативы\n\n"
    "Следи за конкурентами постоянно и забирай их рабочие связки."
)


def _e(x) -> str:
    """Экранирует динамику для HTML-разметки."""
    return escape(str(x)) if x is not None else ""


class AddCompetitor(StatesGroup):
    waiting_for_name = State()
    waiting_for_facebook = State()


def _ad_library_search_url(query: str) -> str:
    """Ссылка-поиск по Библиотеке рекламы Meta по нику/названию (страна — Казахстан).
    Ник оставляем как есть: «plast_garant» матчит «Plastgarant», а замена _→пробел
    («plast garant») ломала поиск. Промахи по брендам лечит выбор/фолбэк на Ad Library."""
    return (
        "https://www.facebook.com/ads/library/?active_status=all&ad_type=all"
        f"&country=KZ&q={quote(query.strip())}&search_type=keyword_unordered&media_type=all"
    )


def _page_url(page_id: str) -> str:
    """Точная ссылка на конкретного рекламодателя в Библиотеке рекламы."""
    return (
        "https://www.facebook.com/ads/library/?active_status=all&ad_type=all"
        f"&country=KZ&view_all_page_id={page_id}&media_type=all"
    )


def _normalize_page(text: str) -> str:
    """Превращает ввод пользователя в URL, который понимает актор.

    - Instagram (ссылка или @ник) → поиск по Библиотеке рекламы (актор не открывает
      инсту-профили напрямую, но поиск по нику находит рекламодателя).
    - Прямая ссылка на Facebook-страницу или Ad Library → используем как есть.
    - Голое слово/название → тоже поиск по Библиотеке.
    """
    t = text.strip().lstrip("@").strip()
    low = t.lower()

    if "ads/library" in low or "facebook.com" in low or "fb.com" in low:
        return t if t.startswith("http") else "https://" + t

    if "instagram.com" in low:
        handle = low.split("instagram.com/")[-1].split("?")[0].split("#")[0].strip("/").split("/")[0]
        return _ad_library_search_url(handle or t)

    if t.startswith("http://") or t.startswith("https://"):
        return t

    handle = t.split("?")[0].split("#")[0].strip("/")
    return _ad_library_search_url(handle)


def _page_label(url: str) -> str:
    """Человеческое имя источника для показа (вместо длинного URL)."""
    if not url:
        return "—"
    if "ads/library" in url and "q=" in url:
        return "🔎 " + unquote(url.split("q=")[1].split("&")[0])
    return url.replace("https://", "").replace("http://", "")[:60]


def _safe_filename(name: str) -> str:
    base = "".join(c if c.isalnum() or c in "-_ " else "_" for c in name).strip().replace(" ", "_")
    return (base or "report")[:40]


async def _send_long(message: Message, text: str, reply_markup=None):
    """Шлёт текст, разбивая по абзацам на части ≤4000 символов (лимит Telegram)."""
    text = text or " "
    if len(text) <= 4000:
        await message.answer(text, reply_markup=reply_markup)
        return
    chunks, cur = [], ""
    for para in text.split("\n"):
        if len(cur) + len(para) + 1 > 3800:
            if cur:
                chunks.append(cur)
            cur = para
        else:
            cur = f"{cur}\n{para}" if cur else para
    if cur:
        chunks.append(cur)
    for i, ch in enumerate(chunks):
        await message.answer(ch, reply_markup=reply_markup if i == len(chunks) - 1 else None)


async def _resolve_brand(bot, user):
    """White-label: для Pro/Enterprise — название агентства + лого (байты) для PDF; иначе ScrapperAD."""
    if active_plan(user) not in ("pro", "enterprise"):
        return "ScrapperAD", None
    brand = (user["brand_name"] or "").strip() or "ScrapperAD"
    logo_bytes = None
    if user["brand_logo"]:
        try:
            buf = BytesIO()
            await bot.download(user["brand_logo"], destination=buf)
            logo_bytes = buf.getvalue()
        except Exception as e:
            logger.debug(f"Лого не скачалось: {e}")
    return brand, logo_bytes


def _age_str(hours: float) -> str:
    if hours < 1:
        return f"{int(hours * 60)} мин"
    if hours < 24:
        return f"{int(hours)} ч"
    return f"{int(hours / 24)} дн"


# ── Добавление конкурента ─────────────────────────────────────────────────────

async def _start_add(target, user_telegram_id: int, state: FSMContext):
    conn = get_connection(settings.database_path)
    user = get_or_create_user(conn, user_telegram_id)
    count = count_user_competitors(conn, user["id"])
    conn.close()

    limits = plan_limits(active_plan(user))
    if count >= limits["competitors"]:
        await target.answer(
            f"⚠️ На тарифе <b>{_e(limits['name'])}</b> можно отслеживать до "
            f"<b>{limits['competitors']}</b> конкурентов (у тебя уже {count}).\n\n"
            f"Подними тариф в разделе 💳 Тариф.",
            reply_markup=back_to_menu(),
        )
        return

    await state.set_state(AddCompetitor.waiting_for_name)
    await target.answer(
        "🕵️ <b>Новый конкурент</b>\n\n<b>Шаг 1/2.</b> Как его назвать? "
        "(для тебя, любое имя)\n<i>Например: «Магазин Ромашка»</i>",
        reply_markup=cancel_kb(),
    )


@router.callback_query(F.data == "add")
async def cb_add(callback: CallbackQuery, state: FSMContext):
    await _start_add(callback.message, callback.from_user.id, state)
    await callback.answer()


@router.message(Command("addcompetitor"))
async def cmd_add(message: Message, state: FSMContext):
    await _start_add(message, message.from_user.id, state)


@router.message(AddCompetitor.waiting_for_name)
async def add_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(AddCompetitor.waiting_for_facebook)
    await message.answer(
        "<b>Шаг 2/2.</b> Кинь <b>Instagram</b> конкурента 👇\n\n"
        "Просто ссылку на его профиль или @ник:\n"
        "• <code>instagram.com/plast_garant</code>\n"
        "• или просто <code>plast_garant</code>\n\n"
        "Найду его рекламу автоматически 🔍\n"
        "<i>(Также подойдёт ссылка на Facebook-страницу или на Библиотеку рекламы Meta.)</i>",
        reply_markup=cancel_kb(),
    )


@router.message(AddCompetitor.waiting_for_facebook)
async def add_facebook(message: Message, state: FSMContext):
    facebook = _normalize_page(message.text)
    data = await state.get_data()
    await state.clear()

    conn = get_connection(settings.database_path)
    user = get_or_create_user(conn, message.from_user.id)
    new_id = add_competitor(conn, user["id"], data["name"], facebook_page=facebook)
    conn.close()

    await message.answer(
        f"✅ <b>{_e(data['name'])}</b> добавлен!\n\n"
        f"📘 Источник: {_e(_page_label(facebook))}\n\n"
        f"👇 Жми <b>«🔍 Собрать рекламу»</b> — соберу его активную рекламу:",
        reply_markup=competitor_menu(new_id),
    )


# ── Список конкурентов ────────────────────────────────────────────────────────

async def _render_list(target, user_telegram_id: int):
    conn = get_connection(settings.database_path)
    user = get_or_create_user(conn, user_telegram_id)
    competitors = get_user_competitors(conn, user["id"])
    conn.close()

    if not competitors:
        await target.answer(
            "У тебя пока нет конкурентов.\nДобавь первого — и я покажу его рекламу 👇",
            reply_markup=main_menu(),
        )
        return

    await target.answer(
        f"🕵️ <b>Твои конкуренты</b> ({len(competitors)})\n\nВыбери конкурента:",
        reply_markup=competitors_list(competitors),
    )


@router.callback_query(F.data == "list")
async def cb_list(callback: CallbackQuery):
    await _render_list(callback.message, callback.from_user.id)
    await callback.answer()


@router.message(Command("competitors"))
async def cmd_list(message: Message):
    await _render_list(message, message.from_user.id)


# ── Меню одного конкурента ─────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("comp:"))
async def cb_competitor(callback: CallbackQuery):
    cid = int(callback.data.split(":", 1)[1])
    conn = get_connection(settings.database_path)
    user = get_or_create_user(conn, callback.from_user.id)
    competitor = get_competitor(conn, cid, user["id"])
    conn.close()
    if not competitor:
        await callback.answer("Конкурент не найден", show_alert=True)
        return

    cached = load_cached_report(cid)
    if cached and (cached.facebook_ads or cached.google_ads):
        age = cache_age_hours(cached.cached_at)
        info = f"\n\n📊 По последним данным: <b>{cached.total_ads}</b> объявлений"
        if cached.max_days_running is not None:
            info += f", дольше всех {cached.max_days_running} дн."
        if age is not None:
            info += f"\n🕒 Проверено {_age_str(age)} назад"
    else:
        info = "\n\n<i>Ещё не сканировался — нажми «Показать рекламу».</i>"

    await callback.message.answer(
        f"🕵️ <b>{_e(competitor['name'])}</b>\n📘 {_e(_page_label(competitor['facebook_page']))}{info}",
        reply_markup=competitor_menu(cid),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("del:"))
async def cb_delete(callback: CallbackQuery):
    cid = int(callback.data.split(":", 1)[1])
    conn = get_connection(settings.database_path)
    user = get_or_create_user(conn, callback.from_user.id)
    deactivate_competitor(conn, cid, user["id"])
    conn.close()
    delete_cache(cid)
    delete_ai_review(cid)
    await callback.answer("Удалён")
    await _render_list(callback.message, callback.from_user.id)


# ── Сводка по всем конкурентам (дашборд) ──────────────────────────────────────

@router.callback_query(F.data == "digest")
async def cb_digest(callback: CallbackQuery):
    conn = get_connection(settings.database_path)
    user = get_or_create_user(conn, callback.from_user.id)
    competitors = get_user_competitors(conn, user["id"])

    if not competitors:
        conn.close()
        await callback.message.answer(
            "Пока некого сравнивать — добавь конкурентов 👇", reply_markup=main_menu(),
        )
        await callback.answer()
        return

    lines = [f"📊 <b>СВОДКА ПО КОНКУРЕНТАМ</b> ({len(competitors)})"]
    scanned = 0
    for c in competitors:
        cached = load_cached_report(c["id"])
        if cached and (cached.facebook_ads or cached.google_ads):
            scanned += 1
            counts = get_recent_ad_counts(conn, c["id"], 2)
            delta = ""
            if len(counts) >= 2:
                d = counts[0] - counts[1]
                delta = f"  📈 +{d}" if d > 0 else (f"  📉 {d}" if d < 0 else "")
            days = f" · до {cached.max_days_running} дн." if cached.max_days_running is not None else ""
            age = cache_age_hours(cached.cached_at)
            age_s = f" · 🕒 {_age_str(age)} назад" if age is not None else ""
            lines.append(
                f"\n🟢 <b>{_e(c['name'])}</b>\n"
                f"   📦 {cached.total_ads} объявл.{delta}{days}{age_s}"
            )
        else:
            lines.append(f"\n⚪️ <b>{_e(c['name'])}</b>\n   ещё не сканировался — открой и нажми 🔍")
    conn.close()

    if scanned:
        lines.append("\n<i>📈/📉 — изменение числа объявлений с прошлой проверки.</i>")
    else:
        lines.append("\n<i>Открой конкурента → «🔍 Собрать рекламу», чтобы собрать данные.</i>")
    await callback.message.answer("\n".join(lines), reply_markup=back_to_menu())
    await callback.answer()


# ── Анализ ниши (несколько конкурентов → AI + PDF) ────────────────────────────

@router.callback_query(F.data == "niche")
async def cb_niche(callback: CallbackQuery):
    conn = get_connection(settings.database_path)
    user = get_or_create_user(conn, callback.from_user.id)
    comps = get_user_competitors(conn, user["id"])
    conn.close()

    if not plan_limits(active_plan(user)).get("ai"):
        await callback.answer()
        await callback.message.answer(
            "🌐 Анализ ниши — на тарифах <b>Start</b> и <b>Pro</b>. Открой 💳 Тариф.",
            reply_markup=back_to_menu())
        return

    data = []
    for c in comps:
        rep = load_cached_report(c["id"])
        if rep and rep.facebook_ads:
            top = rep.facebook_ads[0]
            sample = ((top.title + ". ") if top.title else "") + (top.body_text or "")
            data.append({
                "name": c["name"], "ads": rep.total_ads, "max_days": rep.max_days_running,
                "top_offer": (top.title or (top.body_text or "")[:120]), "sample": sample[:700],
            })

    if len(data) < 2:
        await callback.answer()
        await callback.message.answer(
            "Для анализа ниши собери рекламу хотя бы у <b>2 конкурентов</b> "
            "(открой каждого → «🔍 Собрать рекламу»).",
            reply_markup=back_to_menu())
        return

    if not settings.gemini_api_key:
        await callback.answer()
        await callback.message.answer("⚠️ AI временно недоступен (нет ключа).", reply_markup=back_to_menu())
        return

    await callback.answer()
    note = await callback.message.answer("🌐 Анализирую нишу по твоим конкурентам… ⏳")
    try:
        ai_text = await analyze_niche(settings.gemini_api_key, data)
    except Exception as e:
        logger.error(f"Анализ ниши упал: {e}")
        try:
            await note.edit_text("❌ Анализ ниши недоступен (лимит/ошибка), попробуй позже.")
        except Exception:
            pass
        return
    try:
        await note.delete()
    except Exception:
        pass

    await _send_long(callback.message, f"🌐 <b>Анализ ниши</b>\n\n{_e(ai_text)}", back_to_menu())

    if plan_limits(active_plan(user)).get("pdf"):
        brand, logo_bytes = await _resolve_brand(callback.message.bot, user)
        try:
            pdf = await asyncio.to_thread(build_niche_pdf, "Ниша", data, ai_text, brand, logo_bytes)
            await callback.message.answer_document(
                BufferedInputFile(pdf, "niche_analysis.pdf"), caption="📑 PDF: анализ ниши")
        except Exception as e:
            logger.error(f"Niche PDF не собрался: {e}", exc_info=True)


# ── Анализ рекламы (с дневным кэшем) ──────────────────────────────────────────

@router.callback_query(F.data.startswith("analyze:"))
async def cb_analyze(callback: CallbackQuery):
    competitor_id = int(callback.data.split(":", 1)[1])

    conn = get_connection(settings.database_path)
    user = get_or_create_user(conn, callback.from_user.id)
    competitor = get_competitor(conn, competitor_id, user["id"])
    if not competitor:
        conn.close()
        await callback.answer("Конкурент не найден", show_alert=True)
        return
    limits = plan_limits(active_plan(user))
    used = count_analyses_this_month(conn, user["id"])
    conn.close()

    await callback.answer()

    # 1) Свежий кэш (за сутки) — отдаём бесплатно, лимит не тратим
    cached = load_cached_report(competitor_id)
    age = cache_age_hours(cached.cached_at) if cached else None
    if cached and age is not None and age < CACHE_FRESH_HOURS:
        note = f"📂 Данные собраны {_age_str(age)} назад (обновятся при следующем анализе)."
        await _render_analysis(callback.message, competitor, cached, competitor_id, note)
        return

    # 2) Нужен свежий запуск Apify
    if not _apify_pool.has_tokens():
        await callback.message.answer(
            "⚠️ Apify-ключ не настроен (<code>APIFY_API_TOKENS</code> в .env).",
            reply_markup=back_to_menu(),
        )
        return

    if used >= limits["analyses_per_month"]:
        if cached and (cached.facebook_ads or cached.google_ads):
            note = (f"📂 Показываю прошлые данные — лимит свежих анализов на тарифе "
                    f"{_e(limits['name'])} исчерпан ({used}/{limits['analyses_per_month']} в месяц).")
            await _render_analysis(callback.message, competitor, cached, competitor_id, note)
        else:
            await callback.message.answer(
                f"⚠️ Лимит анализов на тарифе <b>{_e(limits['name'])}</b> исчерпан "
                f"({used}/{limits['analyses_per_month']} в этом месяце).\n\n"
                f"Подними тариф в разделе 💳 Тариф.",
                reply_markup=back_to_menu(),
            )
        return

    status_msg = await callback.message.answer(
        f"🔍 Собираю рекламу <b>{_e(competitor['name'])}</b>...\nЭто 1–3 минуты ⏳",
    )

    # Apify блокирующий — уводим в поток, чтобы не подвешивать бота
    try:
        report = await asyncio.to_thread(
            analyze_competitor,
            apify_tokens=_apify_pool,
            competitor_name=competitor["name"],
            facebook_page=_normalize_page(competitor["facebook_page"]),
            google_domain=None,
            results_limit=limits["ads_per_scan"],
        )
    except Exception as ex:
        logger.error(f"Анализ {competitor['name']} упал: {ex}", exc_info=True)
        await status_msg.edit_text(f"❌ Не получилось собрать рекламу: {_e(str(ex)[:120])}")
        await callback.message.answer("Что дальше?", reply_markup=after_analysis(competitor_id))
        return

    # AI-фолбэк: по нику ничего не нашли → Gemini угадывает бренд → пробуем ещё раз
    if not report.error_facebook and not report.facebook_ads and settings.gemini_api_key:
        q = extract_search_query(_normalize_page(competitor["facebook_page"]))
        if q:
            brand = await suggest_brand_query(settings.gemini_api_key, q)
            if brand and brand.lower() != q.lower():
                logger.info(f"AI бренд-подсказка для {q!r}: {brand!r}")
                try:
                    await status_msg.edit_text(f"🤖 Не нашёл по нику — пробую как «{_e(brand)}»…")
                except Exception:
                    pass
                try:
                    report = await asyncio.to_thread(
                        analyze_competitor,
                        apify_tokens=_apify_pool,
                        competitor_name=competitor["name"],
                        facebook_page=search_url(brand),
                        google_domain=None,
                        results_limit=limits["ads_per_scan"],
                    )
                except Exception as e:
                    logger.error(f"AI-фолбэк анализ упал: {e}")

    # Лимит тратим и кэшируем ТОЛЬКО при успехе (ошибку Apify не кэшируем)
    if not report.error_facebook:
        conn = get_connection(settings.database_path)
        log_analysis(conn, user["id"], competitor_id, "facebook", len(report.facebook_ads))
        conn.close()
        try:
            cache_report(report, competitor_id)
        except Exception as ex:
            logger.debug(f"Кэш не сохранился: {ex}")
        delete_ai_review(competitor_id)   # новые данные → старый AI-разбор неактуален

    try:
        await status_msg.delete()
    except Exception:
        pass
    await _render_analysis(callback.message, competitor, report, competitor_id, None)

    # Апселл после бесплатного разбора (Free)
    if active_plan(user) == "free":
        await callback.message.answer(UPSELL_FREE, reply_markup=upsell_kb())


async def _render_analysis(message: Message, competitor, report, competitor_id, note):
    """Результат: при мешанине рекламодателей — выбор; иначе — карточки."""
    if report.error_facebook:
        await message.answer(f"⚠️ {_e(report.error_facebook)}", reply_markup=after_analysis(competitor_id))
        return

    ads = report.facebook_ads
    if not ads:
        await _no_ads(message, competitor, competitor_id)
        return

    # Несколько разных рекламодателей в выдаче (частое при поиске по инста-нику) →
    # просим выбрать нужного, чтобы не мешать чужие объявления.
    pages = {}
    for ad in ads:
        if ad.page_id:
            name, cnt = pages.get(ad.page_id, (ad.page_name or "—", 0))
            pages[ad.page_id] = (ad.page_name or name, cnt + 1)
    if len(pages) > 1:
        ordered = sorted(([pid, nm, c] for pid, (nm, c) in pages.items()), key=lambda x: -x[2])
        await message.answer(
            f"🔎 Под «<b>{_e(competitor['name'])}</b>» нашлось несколько рекламодателей.\n"
            f"Выбери нужного — покажу только его рекламу:",
            reply_markup=advertiser_picker(competitor_id, ordered),
        )
        return

    await _render_summary(message, competitor, report, competitor_id, note)


async def _render_summary(message: Message, competitor, report, competitor_id, note):
    """Сначала сводка (не вываливаем карточки): сколько объявлений + что делать дальше."""
    ads = report.facebook_ads
    uniq = len({(a.title, a.body_text) for a in ads})
    max_days = max([a.days_running for a in ads if a.days_running is not None], default=None)
    plats = sorted({p for a in ads for p in (a.platforms or [])})
    plats_s = ", ".join(PLATFORM_NAMES.get(p, p.title()) for p in plats)
    top = ads[0]
    offer = top.title or ((top.body_text or "")[:140] + "…")

    conn = get_connection(settings.database_path)
    counts = get_recent_ad_counts(conn, competitor_id, 2)
    conn.close()
    delta = ""
    if len(counts) >= 2:
        d = counts[0] - counts[1]
        delta = f"  📈 +{d}" if d > 0 else (f"  📉 {d}" if d < 0 else "")

    count_line = f"📦 Собрано объявлений: <b>{len(ads)}</b>{delta}"
    if uniq != len(ads):
        count_line += f" ({uniq} уникальных)"
    lines = [f"✅ <b>{_e(competitor['name'])}</b> — реклама собрана\n", count_line]
    if report.total_count and report.total_count > len(ads):
        lines.append(
            f"📊 Всего у конкурента: <b>{report.total_count}</b> объявл. — показано {len(ads)} "
            f"(лимит твоего тарифа). Больше — на старшем тарифе."
        )
    if max_days is not None:
        lines.append(f"⏱ Дольше всех крутится: <b>{max_days} дн.</b>")
    if plats_s:
        lines.append(f"📱 Площадки: {_e(plats_s)}")
    lines.append(f"\n🏷 Оффер: {_e(offer)}")
    if note:
        lines.append(f"\n{_e(note)}")
    lines.append("\n👇 Что показать?")
    await message.answer("\n".join(lines), reply_markup=after_analysis(competitor_id))


async def _no_ads(message: Message, competitor, competitor_id):
    await message.answer(
        f"ℹ️ Не нашёл активную рекламу для <b>{_e(competitor['name'])}</b>.\n\n"
        f"Скорее всего, ник/ссылка не совпадает с названием их страницы в Meta. "
        f"Открой Библиотеку рекламы, найди конкурента и пришли ссылку оттуда:",
        reply_markup=ad_library_kb(competitor["name"], competitor_id),
    )


async def _render_cards(message: Message, name: str, ads: list, competitor_id, note):
    """Заголовок → карточки (с дедупом) → сноска."""
    unique, index = [], {}
    for ad in ads:
        sig = (ad.title, ad.body_text)
        if sig in index:
            index[sig].duplicate_count += 1
        else:
            index[sig] = ad
            unique.append(ad)

    max_days = max([a.days_running for a in ads if a.days_running is not None], default=None)
    header = f"✅ <b>{_e(name)}</b> — <b>{len(ads)}</b> активных объявлений"
    if len(unique) != len(ads):
        header += f" ({len(unique)} уникальных)"
    if max_days is not None:
        header += f"\n⏱ Дольше всех крутится: <b>{max_days} дн.</b> — значит, окупается"
    if note:
        header += f"\n\n{_e(note)}"
    header += f"\n\nПоказываю {min(len(unique), MAX_CARDS)} 👇"
    await message.answer(header)

    async with aiohttp.ClientSession(headers=MEDIA_UA) as session:
        for ad in unique[:MAX_CARDS]:
            await _send_card(message, ad, session)

    foot = (
        "ℹ️ Бюджет и показы Meta не раскрывает для коммерческой рекламы — их нет "
        "ни у одного сервиса. Но длительность открутки и число объявлений ясно "
        "показывают, что у конкурента работает. Бери идеи и делай сильнее 😉"
    )
    extra = len(unique) - MAX_CARDS
    if extra > 0:
        foot = f"…и ещё {extra} объявлений.\n\n" + foot
    await message.answer(foot, reply_markup=after_analysis(competitor_id))


@router.callback_query(F.data.startswith("pick:"))
async def cb_pick(callback: CallbackQuery):
    _, cid_s, pid = callback.data.split(":", 2)
    cid = int(cid_s)
    user, competitor, report = await _get_report_for(callback, cid)
    if not report:
        return
    await callback.answer()

    ads = [a for a in report.facebook_ads if (a.page_id or "") == pid]
    if not ads:
        await callback.message.answer("Не нашёл объявления этой страницы — запусти 🔍 заново.",
                                      reply_markup=competitor_menu(cid))
        return

    # Закрепляем точную страницу + перекэшируем только её объявления (дальше всё точно)
    page_url = _page_url(pid)
    conn = get_connection(settings.database_path)
    u = get_or_create_user(conn, callback.from_user.id)
    update_competitor_page(conn, cid, u["id"], page_url)
    conn.close()
    pinned = CompetitorAdsReport(competitor["name"], page_url, None, facebook_ads=ads)
    try:
        cache_report(pinned, cid)
    except Exception as ex:
        logger.debug(f"re-cache fail: {ex}")
    delete_ai_review(cid)

    await _render_summary(callback.message, competitor, pinned, cid, None)


# ── AI-разбор конкурента (Gemini, бесплатный тир) ─────────────────────────────

@router.callback_query(F.data.startswith("ai:"))
async def cb_ai(callback: CallbackQuery):
    cid = int(callback.data.split(":", 1)[1])
    conn = get_connection(settings.database_path)
    user = get_or_create_user(conn, callback.from_user.id)
    competitor = get_competitor(conn, cid, user["id"])
    conn.close()
    if not competitor:
        await callback.answer("Конкурент не найден", show_alert=True)
        return

    if not plan_limits(active_plan(user)).get("ai"):
        await callback.answer()
        await callback.message.answer(
            "🧠 AI-разбор доступен на тарифах <b>Start</b> и <b>Pro</b>. Открой 💳 Тариф.",
            reply_markup=back_to_menu(),
        )
        return

    report = load_cached_report(cid)
    if not report or report.total_ads == 0:
        await callback.answer()
        await callback.message.answer(
            "Сначала собери рекламу кнопкой 🔍 — потом сделаю AI-разбор.",
            reply_markup=competitor_menu(cid),
        )
        return

    await callback.answer()
    header = f"🧠 <b>AI-разбор: {_e(competitor['name'])}</b>\n\n"

    cached = load_ai_review(cid)
    if cached:
        await _send_long(callback.message, header + _e(cached), after_analysis(cid))
        return

    if not settings.gemini_api_key:
        await callback.message.answer(
            "⚠️ AI-разбор временно недоступен (не настроен ключ).",
            reply_markup=after_analysis(cid),
        )
        return

    note = await callback.message.answer("🧠 Смотрю креативы и думаю над разбором… ⏳")

    # подгружаем до 3 креативов, чтобы модель их «увидела» (мультимодально)
    imgs = []
    async with aiohttp.ClientSession(headers=MEDIA_UA) as session:
        for ad in report.facebook_ads + report.google_ads:
            if len(imgs) >= 3:
                break
            d = await _download(session, ad.image_url)
            if d:
                imgs.append(d)

    try:
        text = await generate_review(settings.gemini_api_key, report, imgs)
    except Exception as ex:
        logger.error(f"AI-разбор {competitor['name']} упал: {ex}")
        try:
            await note.edit_text("❌ AI-разбор сейчас недоступен (лимит или ошибка). Попробуй позже.")
        except Exception:
            pass
        return

    save_ai_review(cid, text)
    try:
        await note.delete()
    except Exception:
        pass
    await _send_long(callback.message, header + _e(text), after_analysis(cid))


# ── Карточки в чат (по запросу, из кэша) ──────────────────────────────────────

@router.callback_query(F.data.startswith("cards:"))
async def cb_cards(callback: CallbackQuery):
    cid = int(callback.data.split(":", 1)[1])
    _, competitor, report = await _get_report_for(callback, cid)
    if not report:
        return
    await callback.answer()
    await _render_cards(callback.message, competitor["name"], report.facebook_ads, cid, None)


# ── Все креативы (видео + картинки) ───────────────────────────────────────────

@router.callback_query(F.data.startswith("crea:"))
async def cb_creatives(callback: CallbackQuery):
    cid = int(callback.data.split(":", 1)[1])
    _, competitor, report = await _get_report_for(callback, cid)
    if not report:
        return
    await callback.answer()
    await callback.message.answer(f"🎬 Отправляю все креативы <b>{_e(competitor['name'])}</b>…")
    total = 0
    async with aiohttp.ClientSession(headers=MEDIA_UA) as session:
        for ad in report.facebook_ads + report.google_ads:
            if total >= 25:
                break
            total += await _send_media(callback.message, ad, session)
    await callback.message.answer(
        f"✅ Готово — {total} креативов." if total else "Креативов не нашлось.",
        reply_markup=after_analysis(cid),
    )


# ── Полный отчёт (из кэша, без новых трат на Apify) ───────────────────────────

async def _get_report_for(callback: CallbackQuery, competitor_id: int):
    """Общая загрузка конкурента + кэша. Вернёт (user, competitor, report) или (None,…)."""
    conn = get_connection(settings.database_path)
    user = get_or_create_user(conn, callback.from_user.id)
    competitor = get_competitor(conn, competitor_id, user["id"])
    conn.close()
    if not competitor:
        await callback.answer("Конкурент не найден", show_alert=True)
        return None, None, None
    report = load_cached_report(competitor_id)
    if not report or report.total_ads == 0:
        await callback.answer()
        await callback.message.answer(
            "Сначала собери рекламу кнопкой 🔍 — потом будет готов отчёт.",
            reply_markup=competitor_menu(competitor_id),
        )
        return None, None, None
    return user, competitor, report


@router.callback_query(F.data.startswith("rpdf:"))
async def cb_report_pdf(callback: CallbackQuery):
    cid = int(callback.data.split(":", 1)[1])
    user, competitor, report = await _get_report_for(callback, cid)
    if not report:
        return
    if not plan_limits(active_plan(user)).get("pdf"):
        await callback.answer()
        await callback.message.answer(
            "📑 PDF-отчёт доступен на тарифах <b>Start</b> и <b>Pro</b>.\n"
            "На Free — текстовый отчёт (кнопка 📄 Отчёт TXT).",
            reply_markup=competitor_menu(cid),
        )
        return
    await callback.answer()
    brand, logo_bytes = await _resolve_brand(callback.message.bot, user)
    await _send_pdf_report(callback.message, report, competitor, cid, brand, logo_bytes)


@router.callback_query(F.data.startswith("rtxt:"))
async def cb_report_txt(callback: CallbackQuery):
    cid = int(callback.data.split(":", 1)[1])
    user, competitor, report = await _get_report_for(callback, cid)
    if not report:
        return
    await callback.answer()
    await _send_txt_report(callback.message, report, competitor, cid)


async def _send_pdf_report(message: Message, report, competitor, competitor_id,
                           brand="ScrapperAD", logo_bytes=None):
    """Только стильный PDF (тексты + картинки внутри). Видео — кнопкой «🎬 Все креативы»."""
    note = await message.answer(f"📑 Готовлю стильный PDF по <b>{_e(competitor['name'])}</b>…")
    ads = report.facebook_ads + report.google_ads
    images = {}
    async with aiohttp.ClientSession(headers=MEDIA_UA) as session:
        for i, ad in enumerate(ads):
            if i >= 25:
                break
            data = await _download(session, ad.image_url)
            if data:
                images[i] = data
    try:
        pdf_bytes = await asyncio.to_thread(build_pdf, report, images, brand, logo_bytes)
        await message.answer_document(
            BufferedInputFile(pdf_bytes, filename=f"{_safe_filename(competitor['name'])}.pdf"),
            caption=f"📑 PDF-отчёт: {_e(competitor['name'])} — готов к показу клиенту",
            reply_markup=after_analysis(competitor_id),
        )
    except Exception as ex:
        logger.error(f"PDF не собрался: {ex}", exc_info=True)
        await message.answer("❌ Не удалось собрать PDF, попробуй ещё раз.",
                             reply_markup=after_analysis(competitor_id))
    finally:
        try:
            await note.delete()
        except Exception:
            pass


async def _send_txt_report(message: Message, report, competitor, competitor_id):
    """Только текстовый .txt со всеми объявлениями (креативы — кнопкой «🎬 Все креативы»)."""
    try:
        filepath = await save_report_file(report)
        with open(filepath, "rb") as f:
            data = f.read()
        try:
            os.remove(filepath)          # на диске не храним — файл уже в байтах
        except Exception:
            pass
        await message.answer_document(
            BufferedInputFile(data, filename=f"{_safe_filename(competitor['name'])}.txt"),
            caption=f"📄 Текстовый отчёт: {_e(competitor['name'])} — все тексты и ссылки",
            reply_markup=after_analysis(competitor_id),
        )
    except Exception as ex:
        logger.error(f"Отчёт-файл не собрался: {ex}", exc_info=True)
        await message.answer("❌ Не удалось собрать TXT-отчёт.",
                             reply_markup=after_analysis(competitor_id))


# ── Карточки и медиа ───────────────────────────────────────────────────────────

async def _download(session, url, limit=45 * 1024 * 1024):
    """Скачивает медиа в байты. None — если не вышло или слишком большое."""
    if not url or not isinstance(url, str):
        return None
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status == 200:
                data = await r.read()
                if 0 < len(data) <= limit:
                    return data
    except Exception as ex:
        logger.debug(f"Скачивание не удалось ({url[:60]}): {ex}")
    return None


def _card_text(ad, body=None) -> str:
    """Текст карточки (HTML). body можно подменить (укоротить) для подписи к медиа."""
    body = ad.body_text if body is None else body
    head = "🟢 Активно" if ad.status == "active" else "⚫ Остановлено"
    if ad.days_running is not None:
        head += f" · ⏱ крутится {ad.days_running} дн."
    if ad.duplicate_count > 1:
        head += f" · 🔁 {ad.duplicate_count} копии"
    lines = [head]
    if ad.platforms_str:
        lines.append(f"📱 {_e(ad.platforms_str)}")
    if ad.title:
        lines.append(f"\n🏷 <b>{_e(ad.title)}</b>")
    lines.append(f"\n{_e(body)}")
    if ad.link_description:
        lines.append(f"\n💬 {_e(ad.link_description)}")
    if ad.cta_text:
        lines.append(f"🔘 Кнопка: {_e(ad.cta_text)}")
    if ad.link_url:
        lines.append(f"🔗 Ведёт на: {_e(ad.link_url)}")
    started = _format_date(ad.started_at)
    if started:
        ended = _format_date(ad.ended_at)
        suffix = f" · остановлено: {ended}" if (ad.status != "active" and ended) else ""
        lines.append(f"📅 Запущено: {started}{suffix}")
    if ad.archive_url:
        lines.append(f"🔎 В Ad Library: {_e(ad.archive_url)}")
    return "\n".join(lines)


def _card_caption(ad) -> str:
    """Подпись к медиа: та же карточка, влезающая в лимит подписи Telegram (1024).
    Укорачиваем сырое тело (его и экранируем) бинарным поиском — теги не рвём."""
    full = _card_text(ad)
    if len(full) <= 1024:
        return full
    raw = ad.body_text or ""
    suffix = "… (полный текст — в отчёте 📄)"
    lo, hi, best = 0, len(raw), ""
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = _card_text(ad, body=raw[:mid].rstrip() + suffix)
        if len(candidate) <= 1024:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best or _card_text(ad, body=suffix)


async def _send_card(message: Message, ad, session):
    """Одно сообщение на объявление: креатив + вся инфа подписью (чат не засоряется)."""
    caption = _card_caption(ad)

    # Собираем медиа (скачиваем в байты): сначала видео, потом картинки, максимум 4
    media = []
    for v in ad.videos[:2]:
        data = await _download(session, v.get("sd") or v.get("hd"))
        if data:
            media.append(("video", data))
        else:
            prev = await _download(session, v.get("preview"))
            if prev:
                media.append(("photo", prev))
        if len(media) >= 4:
            break
    for img in ad.images:
        if len(media) >= 4:
            break
        data = await _download(session, img)
        if data:
            media.append(("photo", data))

    # Нет медиа — просто текст
    if not media:
        await message.answer(caption, link_preview_options=NO_PREVIEW)
        return

    # Одно медиа — фото/видео с подписью
    if len(media) == 1:
        kind, data = media[0]
        try:
            if kind == "video":
                await message.answer_video(BufferedInputFile(data, "creative.mp4"), caption=caption)
            else:
                await message.answer_photo(BufferedInputFile(data, "creative.jpg"), caption=caption)
        except Exception as ex:
            logger.debug(f"Карточка с медиа не отправилась: {ex}")
            await message.answer(caption, link_preview_options=NO_PREVIEW)
        return

    # Несколько медиа — альбом, подпись на первом
    group = []
    for i, (kind, data) in enumerate(media):
        cap = caption if i == 0 else None
        if kind == "video":
            group.append(InputMediaVideo(media=BufferedInputFile(data, f"c{i}.mp4"), caption=cap))
        else:
            group.append(InputMediaPhoto(media=BufferedInputFile(data, f"c{i}.jpg"), caption=cap))
    try:
        await message.answer_media_group(group)
    except Exception as ex:
        logger.debug(f"Альбом не отправился: {ex}")
        await message.answer(caption, link_preview_options=NO_PREVIEW)


async def _send_media(message: Message, ad, session) -> int:
    """Креативы: видео (или его превью), затем картинки. Возвращает число отправленных."""
    sent = 0
    for v in ad.videos[:2]:
        data = await _download(session, v.get("sd") or v.get("hd"))
        if data:
            try:
                await message.answer_video(BufferedInputFile(data, "creative.mp4"))
                sent += 1
                continue
            except Exception as ex:
                logger.debug(f"Видео не отправилось: {ex}")
        data = await _download(session, v.get("preview"))
        if data:
            try:
                await message.answer_photo(BufferedInputFile(data, "preview.jpg"))
                sent += 1
            except Exception as ex:
                logger.debug(f"Превью не отправилось: {ex}")

    for img in ad.images:
        if sent >= 6:
            break
        data = await _download(session, img)
        if data:
            try:
                await message.answer_photo(BufferedInputFile(data, "creative.jpg"))
                sent += 1
            except Exception as ex:
                logger.debug(f"Картинка не отправилась: {ex}")

    if sent == 0 and ad.image_url:
        try:
            await message.answer_photo(ad.image_url)
            sent += 1
        except Exception:
            pass
    return sent
