"""
reports/pdf_report.py
=====================
Брендированный PDF-отчёт по конкуренту (тариф Pro/Start — для показа клиентам).

Стильно: цветная шапка на каждой странице, статус-плашки, аккуратные карточки
объявлений, картинки с рамкой, нумерация страниц. Видео в PDF не вставляется —
видео-креативы бот досылает отдельными сообщениями.

Шрифт DejaVuSans в assets/fonts (кириллица, работает и на Linux). Эмодзи DejaVu
не рисует — вырезаем. Сборка синхронная (fpdf2) — вызывать через to_thread.
"""

import os
import re
import logging
from io import BytesIO
from datetime import datetime

from fpdf import FPDF
from fpdf.enums import XPos, YPos
from PIL import Image

logger = logging.getLogger(__name__)

_FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "fonts")
_FONT_REG = os.path.join(_FONT_DIR, "DejaVuSans.ttf")
_FONT_BOLD = os.path.join(_FONT_DIR, "DejaVuSans-Bold.ttf")

MAX_ADS_IN_PDF = 25

# Палитра
_BRAND = (33, 47, 90)        # тёмно-синий — шапка/заголовки
_GREEN = (34, 160, 90)       # активно
_GRAY = (140, 140, 140)      # остановлено / приглушённый текст
_MUTED = (110, 110, 110)
_LINE = (225, 225, 225)

_EMOJI = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002B00-\U00002BFF"
    "\U00002190-\U000021FF"
    "\U0000FE00-\U0000FE0F"
    "\U0000200D"
    "]+",
    flags=re.UNICODE,
)


def _san(s) -> str:
    if not s:
        return ""
    return _EMOJI.sub("", str(s).replace("₸", " тг")).strip()


def _prep_image(data: bytes):
    """Байты картинки → (JPEG в BytesIO, ширина_px, высота_px) или None."""
    try:
        im = Image.open(BytesIO(data)).convert("RGB")
        im.thumbnail((900, 900))
        out = BytesIO()
        im.save(out, "JPEG", quality=82)
        out.seek(0)
        return out, im.size[0], im.size[1]
    except Exception as e:
        logger.debug(f"PDF: картинка не обработана: {e}")
        return None


class _PDF(FPDF):
    def __init__(self, title: str, brand: str, logo=None):
        super().__init__(format="A4")
        self._title = title
        self._brand = brand
        self._logo = logo   # BytesIO с лого агентства (white-label) или None

    def header(self):
        self.set_fill_color(*_BRAND)
        self.rect(0, 0, self.w, 22, "F")
        if self._logo:
            try:
                self.image(self._logo, x=self.w - self.r_margin - 15, y=4, h=14)
            except Exception:
                pass
        self.set_xy(self.l_margin, 5)
        self.set_text_color(255, 255, 255)
        self.set_font("DejaVu", "B", 15)
        self.cell(0, 7, _san(self._title), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_x(self.l_margin)
        self.set_font("DejaVu", "", 9)
        self.set_text_color(210, 215, 230)
        self.cell(0, 5, _san(self._brand), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_text_color(0, 0, 0)
        self.set_y(30)

    def footer(self):
        self.set_y(-12)
        self.set_font("DejaVu", "", 8)
        self.set_text_color(*_GRAY)
        self.cell(0, 8, _san(f"{self._brand} · стр. {self.page_no()}"), align="C")


def _new_pdf(title: str, brand: str, logo_bytes=None) -> _PDF:
    """Создаёт настроенный PDF с подключённым шрифтом и (опц.) лого агентства."""
    logo = None
    if logo_bytes:
        prepped = _prep_image(logo_bytes)
        if prepped:
            logo = prepped[0]
    pdf = _PDF(title=title, brand=brand, logo=logo)
    pdf.set_margins(15, 30, 15)
    pdf.set_auto_page_break(True, margin=15)
    pdf.add_font("DejaVu", "", _FONT_REG)
    pdf.add_font("DejaVu", "B", _FONT_BOLD)
    pdf.add_page()
    return pdf


def _mc(pdf, h, text, size=11, style="", color=(0, 0, 0)):
    pdf.set_font("DejaVu", style, size)
    pdf.set_text_color(*color)
    # align="L" — текст ровно по левому краю (а не «растянутый» justified)
    pdf.multi_cell(0, h, text or " ", new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="L")


def build_pdf(report, images: dict, brand: str = "ScrapperAD", logo_bytes=None) -> bytes:
    """images: {индекс_объявления: байты_картинки}. Возвращает байты PDF."""
    pdf = _new_pdf(f"Конкурент: {report.competitor_name}", brand, logo_bytes)

    # Сводка
    summ = f"Дата отчёта: {datetime.now():%d.%m.%Y}     Активных объявлений: {report.total_ads}"
    if report.max_days_running is not None:
        summ += f"     Дольше всех крутится: {report.max_days_running} дн."
    _mc(pdf, 6, _san(summ), size=10, color=_MUTED)
    pdf.ln(2)

    ads = report.facebook_ads + report.google_ads
    for i, ad in enumerate(ads[:MAX_ADS_IN_PDF]):
        _ad_block(pdf, ad, i + 1, images.get(i))

    return bytes(pdf.output())


def _ad_block(pdf: _PDF, ad, n: int, img_bytes):
    pdf.ln(2)
    pdf.set_draw_color(*_LINE)
    y = pdf.get_y()
    pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
    pdf.ln(3)

    # Статус-плашка
    active = ad.status == "active"
    label = f"  {'АКТИВНО' if active else 'ОСТАНОВЛЕНО'}  "
    pdf.set_font("DejaVu", "B", 8)
    w = pdf.get_string_width(label) + 1
    pdf.set_fill_color(*(_GREEN if active else _GRAY))
    pdf.set_text_color(255, 255, 255)
    pdf.cell(w, 6, label, fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(0, 0, 0)

    # Мета
    meta = [f"Объявление #{n}"]
    if ad.days_running is not None:
        meta.append(f"крутится {ad.days_running} дн.")
    if ad.duplicate_count > 1:
        meta.append(f"{ad.duplicate_count} копии")
    if ad.platforms_str:
        meta.append(ad.platforms_str)
    pdf.ln(1)
    _mc(pdf, 5, _san("  ·  ".join(meta)), size=8, color=_MUTED)

    if ad.title:
        _mc(pdf, 4, "ЗАГОЛОВОК", size=7, color=_MUTED)
        _mc(pdf, 6, _san(ad.title), size=12, style="B", color=_BRAND)
        pdf.ln(1)

    _mc(pdf, 4, "ТЕКСТ ОБЪЯВЛЕНИЯ", size=7, color=_MUTED)
    _mc(pdf, 6, _san(ad.body_text) or "—", size=11)

    extra = []
    if ad.cta_text:
        extra.append(f"Кнопка: {ad.cta_text}")
    if ad.link_url:
        extra.append(f"Ведёт на: {ad.link_url[:100]}")
    if ad.archive_url:
        extra.append(f"Открыть в Ad Library: {ad.archive_url}")
    if extra:
        pdf.ln(1)
        _mc(pdf, 4, "ДЕТАЛИ", size=7, color=_MUTED)
        _mc(pdf, 5, _san("\n".join(extra)), size=9, color=_MUTED)

    if img_bytes:
        prepped = _prep_image(img_bytes)
        if prepped:
            bio, wpx, hpx = prepped
            w_mm = 55
            h_mm = (w_mm * hpx / wpx) if wpx else 40
            if pdf.get_y() + h_mm > pdf.h - pdf.b_margin:
                pdf.add_page()
            x, y0 = pdf.l_margin, pdf.get_y() + 1
            try:
                pdf.image(bio, x=x, y=y0, w=w_mm)
                pdf.set_draw_color(*_LINE)
                pdf.rect(x, y0, w_mm, h_mm)
                pdf.set_y(y0 + h_mm + 2)
            except Exception as e:
                logger.debug(f"PDF: картинка не вставлена: {e}")

    pdf.ln(2)


def build_niche_pdf(niche_name, competitors, ai_text, brand="ScrapperAD", logo_bytes=None) -> bytes:
    """Сводный PDF по нише. competitors: [{name, ads, max_days, top_offer}], ai_text: разбор."""
    pdf = _new_pdf(f"Анализ ниши: {niche_name}", brand, logo_bytes)

    total = sum(c.get("ads", 0) for c in competitors)
    _mc(pdf, 6, _san(f"Конкурентов в обзоре: {len(competitors)}   ·   всего объявлений: {total}"),
        size=10, color=_MUTED)
    pdf.ln(2)

    _mc(pdf, 6, "КОНКУРЕНТЫ", size=11, style="B", color=_BRAND)
    for c in competitors:
        line = f"• {c['name']} — {c.get('ads', 0)} объявл."
        if c.get("max_days") is not None:
            line += f", до {c['max_days']} дн."
        _mc(pdf, 6, _san(line), size=11)
        if c.get("top_offer"):
            _mc(pdf, 5, _san("    оффер: " + c["top_offer"]), size=9, color=_MUTED)

    pdf.ln(3)
    pdf.set_draw_color(*_LINE)
    y = pdf.get_y()
    pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
    pdf.ln(3)

    _mc(pdf, 6, "AI-РАЗБОР НИШИ", size=11, style="B", color=_BRAND)
    _mc(pdf, 6, _san(ai_text) or "—", size=11)
    return bytes(pdf.output())
