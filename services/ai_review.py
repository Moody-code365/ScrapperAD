"""
services/ai_review.py
=====================
AI-разбор конкурента через бесплатный тир Gemini (REST, без новых библиотек).

Мультимодально: вместе с текстами объявлений отправляем сами креативы (картинки
и превью видео) — модель их «видит» и описывает. Если запрос с картинками падает
(квота/размер) — повторяем без картинок (текстовый разбор).

Результат кэшируется в data/cache/ai_{id}.json (генерим один раз на анализ —
бережём бесплатную квоту). Кэш сбрасывается при свежем анализе и удалении.
"""

import os
import json
import base64
import asyncio
import logging
from io import BytesIO
from datetime import datetime, timezone

from typing import Optional

import aiohttp
from PIL import Image

logger = logging.getLogger(__name__)

CACHE_DIR = "data/cache"
MODEL = "gemini-2.5-flash"          # у этой модели есть бесплатная квота (проверено)
_ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"

_PROMPT = (
    "Ты — сильный таргетолог-аналитик. Разбери рекламу конкурента «{name}» как для "
    "клиента: конкретно, по делу, простым языком, на русском. НЕ используй markdown, "
    "заголовки пиши заглавными буквами, пункты — с тире.\n\n"
    "ВАЖНО: у объявлений есть креативы (видео/фото). Если к запросу приложены картинки — "
    "это кадры/превью их рекламы, опиши что на них и используй в разборе. НЕ пиши, что "
    "у конкурента «только текст».\n\n"
    "Структура:\n\n"
    "1. ГЛАВНЫЙ ОФФЕР — что предлагают и в чём суть (1–2 предложения).\n\n"
    "2. НА КОГО НАЦЕЛЕНО — портрет аудитории.\n\n"
    "3. ХУКИ И ТРИГГЕРЫ — какими приёмами цепляют (скидка, дефицит, гарантия, боль "
    "клиента, подарок и т.п.), с коротким пояснением.\n\n"
    "4. КРЕАТИВ И ПОДАЧА — что на видео/фото, тон, призыв, куда ведут (сайт/WhatsApp).\n\n"
    "5. ЧТО У НИХ РАБОТАЕТ — если реклама крутится долго или объявлений много, что это значит.\n\n"
    "6. КАК ОБОЙТИ — 3–5 конкретных идей, что протестировать, чтобы выиграть.\n\n"
    "ДАННЫЕ:\n{ads}"
)


def _ads_digest(report) -> str:
    ads = report.facebook_ads + report.google_ads
    lines = [f"Всего активных объявлений: {report.total_ads}."]
    if report.max_days_running is not None:
        lines.append(f"Дольше всех крутится: {report.max_days_running} дн.")
    lines.append("")
    for i, ad in enumerate(ads[:15], 1):
        fmt = "видео" if ad.videos else ("фото" if ad.images else (ad.display_format or "—"))
        meta = [f"формат: {fmt}"]
        if ad.days_running is not None:
            meta.append(f"крутится {ad.days_running} дн.")
        if ad.platforms_str:
            meta.append(ad.platforms_str)
        if ad.cta_text:
            meta.append(f"кнопка: {ad.cta_text}")
        head = (ad.title + ". ") if ad.title else ""
        lines.append(f"{i}) ({'; '.join(meta)}) {head}{(ad.body_text or '')[:700]}")
    return "\n".join(lines)[:7000]


def _shrink(data: bytes):
    """Картинка → маленький JPEG base64 (экономим токены), или None."""
    try:
        im = Image.open(BytesIO(data)).convert("RGB")
        im.thumbnail((512, 512))
        out = BytesIO()
        im.save(out, "JPEG", quality=70)
        return base64.b64encode(out.getvalue()).decode()
    except Exception as e:
        logger.debug(f"AI: картинка не ужалась: {e}")
        return None


async def _call(api_key: str, parts: list) -> str:
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 2048,
            "thinkingConfig": {"thinkingBudget": 0},   # без «размышлений» — иначе ответ обрезается
        },
    }
    last = None
    for attempt in range(2):
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_ENDPOINT}?key={api_key}", json=payload,
                timeout=aiohttp.ClientTimeout(total=90),
            ) as r:
                data = await r.json()
                if r.status == 200:
                    try:
                        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
                    except (KeyError, IndexError):
                        raise RuntimeError("Пустой ответ модели (возможно, сработал фильтр).")
                msg = (data.get("error") or {}).get("message", f"HTTP {r.status}")
                last = RuntimeError(str(msg)[:150])
                if r.status not in (429, 500, 502, 503, 504):
                    break  # не транзиентная ошибка — повтор не поможет
        if attempt == 0:
            await asyncio.sleep(2)
    raise last


async def generate_review(api_key: str, report, image_bytes=None) -> str:
    """Запрос к Gemini (с картинками, если есть). Возвращает текст или бросает исключение."""
    prompt = _PROMPT.format(name=report.competitor_name, ads=_ads_digest(report))
    parts = [{"text": prompt}]
    for b in (image_bytes or [])[:3]:
        b64 = _shrink(b)
        if b64:
            parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})

    # 1-я попытка с картинками, 2-я — без (вдруг дело в них / разовый сбой), с паузой
    attempts = [parts, parts[:1]] if len(parts) > 1 else [parts, parts]
    last_exc = None
    for i, p in enumerate(attempts):
        try:
            return await _call(api_key, p)
        except Exception as e:
            last_exc = e
            if i < len(attempts) - 1:
                await asyncio.sleep(1.5)
    raise last_exc


async def analyze_niche(api_key: str, items: list) -> str:
    """Сводный AI-разбор ниши по нескольким конкурентам. items: [{name, ads, sample}]."""
    blocks = []
    for it in items:
        blocks.append(f"# {it['name']} ({it.get('ads', 0)} объявл.)\n{it.get('sample', '')}")
    data = "\n\n".join(blocks)[:8000]
    prompt = (
        "Ты — маркетолог-аналитик. Ниже реклама нескольких конкурентов одной ниши. "
        "Дай СВОДНЫЙ разбор ниши на русском, без markdown, заголовки заглавными, пункты с тире.\n\n"
        "1. ОБЩАЯ КАРТИНА — что за ниша и какие предложения преобладают.\n\n"
        "2. КТО ЛИДЕР И ПОЧЕМУ — у кого больше/дольше реклама, чем выделяется.\n\n"
        "3. ОБЩИЕ ОФФЕРЫ И ХУКИ — что используют почти все (скидки, рассрочка, гарантии, подарки…).\n\n"
        "4. СВОБОДНЫЕ УГЛЫ — чем можно отстроиться, чего никто не делает.\n\n"
        "5. КАК ЗАЙТИ СИЛЬНЕЕ — 3–5 конкретных идей.\n\n"
        f"ДАННЫЕ:\n{data}"
    )
    return await _call(api_key, [{"text": prompt}])


async def suggest_brand_query(api_key: str, handle: str) -> Optional[str]:
    """Последний шанс найти конкурента: Gemini угадывает название бренда по инста-нику."""
    prompt = (
        f"Инстаграм-аккаунт: '{handle}'. Назови вероятное короткое название бренда или "
        f"компании для поиска в рекламной библиотеке Facebook (Meta Ad Library). "
        f"Ответь ТОЛЬКО названием, 1–3 слова, без кавычек и пояснений."
    )
    try:
        text = await _call(api_key, [{"text": prompt}])
        line = (text or "").strip().splitlines()[0].strip(' "\'.')
        return line[:50] or None
    except Exception as e:
        logger.debug(f"suggest_brand_query упал: {e}")
        return None


# ── Кэш разбора ──────────────────────────────────────────────────────────────────

def _path(competitor_id: int) -> str:
    return os.path.join(CACHE_DIR, f"ai_{competitor_id}.json")


def load_ai_review(competitor_id: int):
    try:
        with open(_path(competitor_id), encoding="utf-8") as f:
            return json.load(f).get("text")
    except Exception:
        return None


def save_ai_review(competitor_id: int, text: str) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(_path(competitor_id), "w", encoding="utf-8") as f:
        json.dump({"text": text, "created_at": datetime.now(timezone.utc).isoformat()},
                  f, ensure_ascii=False)


def delete_ai_review(competitor_id: int) -> None:
    try:
        os.remove(_path(competitor_id))
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.debug(f"Не удалось удалить AI-кэш {competitor_id}: {e}")
