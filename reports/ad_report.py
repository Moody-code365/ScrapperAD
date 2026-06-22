"""
reports/competitor_report.py
=============================
Генерирует полный отчёт по конкуренту в виде файла.

Что делает:
1. Собирает ВСЕ данные по объявлениям без обрезки
2. Сохраняет в .txt файл
3. Скачивает изображения объявлений
4. Отправляет всё в Telegram
"""

import os
import logging
import aiohttp
from datetime import datetime

from services.ad_spy import CompetitorAdsReport, AdResult, _format_date

logger = logging.getLogger(__name__)

REPORTS_DIR = "data/reports"


def generate_full_report_text(report: CompetitorAdsReport) -> str:
    """
    Генерирует полный текст отчёта без обрезки.
    Сохраняется в .txt файл.
    """
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    lines = [
        "=" * 60,
        "ОТЧЁТ ПО РЕКЛАМЕ КОНКУРЕНТА",
        f"Конкурент: {report.competitor_name}",
        f"Дата: {now}",
        "=" * 60,
        "",
        f"Всего объявлений: {report.total_ads}",
        f"Активных: {report.active_ads}",
        "",
    ]

    # ── Facebook ──────────────────────────────────────────────────────────
    if report.facebook_page:
        lines += [
            "─" * 60,
            "FACEBOOK / INSTAGRAM",
            f"Страница: {report.facebook_page}",
            f"Объявлений найдено: {len(report.facebook_ads)}",
            "─" * 60,
        ]

        if report.error_facebook:
            lines.append(f"Ошибка: {report.error_facebook}")
        else:
            for i, ad in enumerate(report.facebook_ads, 1):
                lines += _format_ad_full(ad, i)

    # ── Google ────────────────────────────────────────────────────────────
    if report.google_domain:
        lines += [
            "",
            "─" * 60,
            "GOOGLE ADS",
            f"Домен: {report.google_domain}",
            f"Объявлений найдено: {len(report.google_ads)}",
            "─" * 60,
        ]

        if report.error_google:
            lines.append(f"Статус: {report.error_google}")
        else:
            for i, ad in enumerate(report.google_ads, 1):
                lines += _format_ad_full(ad, i)

    lines += ["", "=" * 60, "Отчёт создан ScrapperAD", "=" * 60]
    return "\n".join(lines)


def _format_ad_full(ad: AdResult, index: int) -> list:
    """Полное описание одного объявления для текстового файла"""
    status = "Активно" if ad.status == "active" else "Неактивно"
    date_str = _format_date(ad.started_at) or "Неизвестно"

    lines = [
        "",
        f"[ Объявление #{index} ]",
        f"ID: {ad.ad_id}",
        f"Статус: {status}",
        f"Запущено: {date_str}",
    ]
    if ad.days_running is not None:
        lines.append(f"Крутится дней: {ad.days_running}")
    if ad.duplicate_count > 1:
        lines.append(f"Копий объявления: {ad.duplicate_count}")
    if ad.platforms_str:
        lines.append(f"Площадки: {ad.platforms_str}")
    if ad.impressions:
        lines.append(f"Показы: {ad.impressions}")

    if ad.title:
        lines += ["", f"ЗАГОЛОВОК: {ad.title}"]

    lines += [
        "",
        "ТЕКСТ ОБЪЯВЛЕНИЯ:",
        ad.body_text or "Текст недоступен",
        "",
    ]

    if ad.cta_text:
        lines.append(f"Кнопка (CTA): {ad.cta_text}")
    if ad.link_url:
        lines.append(f"Ведёт на: {ad.link_url}")
    if ad.archive_url:
        lines.append(f"Карточка в Ad Library: {ad.archive_url}")
    for i, img in enumerate(ad.images, 1):
        lines.append(f"Картинка {i}: {img}")
    for i, v in enumerate(ad.videos, 1):
        url = v.get("hd") or v.get("sd") or v.get("preview")
        if url:
            lines.append(f"Видео {i}: {url}")

    return lines


async def save_report_file(report: CompetitorAdsReport) -> str:
    """
    Сохраняет отчёт в .txt файл.
    Возвращает путь к файлу.
    """
    os.makedirs(REPORTS_DIR, exist_ok=True)

    # Имя файла: competitor_name_дата.txt
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in report.competitor_name)
    date_str = datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"{safe_name}_{date_str}.txt"
    filepath = os.path.join(REPORTS_DIR, filename)

    text = generate_full_report_text(report)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(text)

    logger.info(f"Отчёт сохранён: {filepath}")
    return filepath


async def download_ad_images(report: CompetitorAdsReport) -> list[tuple[str, bytes]]:
    """
    Скачивает изображения всех объявлений.
    Возвращает список (название_файла, байты_изображения).
    """
    images = []
    all_ads = report.facebook_ads + report.google_ads

    async with aiohttp.ClientSession() as session:
        for i, ad in enumerate(all_ads, 1):
            if not ad.image_url or not isinstance(ad.image_url, str):
                continue
            try:
                async with session.get(ad.image_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        content_type = resp.headers.get("Content-Type", "")
                        ext = "jpg" if "jpeg" in content_type else (
                              "png" if "png" in content_type else "jpg")
                        filename = f"ad_{i}_{ad.source}.{ext}"
                        data = await resp.read()
                        images.append((filename, data))
                        logger.info(f"Скачано изображение: {filename}")
            except Exception as e:
                logger.warning(f"Не удалось скачать изображение {ad.image_url}: {e}")

    return images