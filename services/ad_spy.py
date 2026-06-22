"""
services/ad_spy.py
==================
Сбор рекламы конкурентов через Apify (Facebook Ad Library).

Вытаскиваем МАКСИМУМ полезного: полный текст, заголовок, кнопку (CTA),
куда ведёт реклама, площадки (FB/IG/WhatsApp...), даты запуска и длительность,
все креативы (картинки И видео), ссылку на карточку в Ad Library.

Важно про деньги: Meta НЕ раскрывает бюджет/показы для обычной коммерческой
рекламы (spend / reachEstimate / impressionsText приходят null). Это доступно
только для политической/социальной рекламы. Реальные индикаторы «работает ли
реклама» — длительность открутки и количество активных объявлений.
"""

import os
import re
import json
import logging
from urllib.parse import quote, unquote
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional
from apify_client import ApifyClient

from services.apify_pool import ApifyKeyPool, ApifyQuotaError, is_quota_error

logger = logging.getLogger(__name__)

FACEBOOK_ADS_ACTOR = "apify/facebook-ads-scraper"
GOOGLE_ADS_ACTOR   = "amernas/google-ads-transparency-analyzer"
MAX_ADS_PER_REQUEST = 30

PLATFORM_NAMES = {
    "FACEBOOK": "Facebook",
    "INSTAGRAM": "Instagram",
    "MESSENGER": "Messenger",
    "WHATSAPP": "WhatsApp",
    "AUDIENCE_NETWORK": "Audience Network",
    "THREADS": "Threads",
}


@dataclass
class AdResult:
    source: str
    competitor_name: str
    ad_id: str
    status: str                       # active / inactive
    body_text: str                    # полный текст объявления
    title: Optional[str] = None       # заголовок
    caption: Optional[str] = None     # подпись/домен
    link_description: Optional[str] = None
    cta_text: Optional[str] = None    # текст кнопки
    link_url: Optional[str] = None
    display_format: Optional[str] = None
    started_at: Optional[object] = None
    ended_at: Optional[object] = None
    platforms: list = field(default_factory=list)
    page_name: Optional[str] = None
    page_id: Optional[str] = None
    images: list = field(default_factory=list)   # list[str]
    videos: list = field(default_factory=list)   # list[dict]: {hd, sd, preview}
    impressions: Optional[str] = None
    spend: Optional[str] = None
    duplicate_count: int = 1          # сколько одинаковых копий этого объявления крутится

    # ── Производные ──
    @property
    def ad_text(self) -> str:          # обратная совместимость с reports/ad_report.py
        return self.body_text

    @property
    def image_url(self) -> Optional[str]:
        if self.images:
            return self.images[0]
        if self.videos and self.videos[0].get("preview"):
            return self.videos[0]["preview"]
        return None

    @property
    def archive_url(self) -> Optional[str]:
        if self.ad_id and self.ad_id != "unknown":
            return f"https://www.facebook.com/ads/library/?id={self.ad_id}"
        return None

    @property
    def days_running(self) -> Optional[int]:
        start = _to_datetime(self.started_at)
        if not start:
            return None
        end = _to_datetime(self.ended_at) or datetime.now(timezone.utc)
        return max((end - start).days, 0)

    @property
    def platforms_str(self) -> str:
        return ", ".join(PLATFORM_NAMES.get(p, p.title()) for p in self.platforms)


@dataclass
class CompetitorAdsReport:
    competitor_name: str
    facebook_page: Optional[str]
    google_domain: Optional[str]
    facebook_ads: list = field(default_factory=list)
    google_ads: list = field(default_factory=list)
    error_facebook: Optional[str] = None
    error_google: Optional[str] = None
    cached_at: Optional[str] = None    # ISO-время сохранения в кэш (если из кэша)
    total_count: Optional[int] = None  # сколько всего объявлений у рекламодателя

    @property
    def total_ads(self):
        return len(self.facebook_ads) + len(self.google_ads)

    @property
    def active_ads(self):
        return sum(1 for ad in self.facebook_ads + self.google_ads if ad.status == "active")

    @property
    def max_days_running(self) -> Optional[int]:
        days = [ad.days_running for ad in self.facebook_ads + self.google_ads if ad.days_running is not None]
        return max(days) if days else None


# ── Facebook ───────────────────────────────────────────────────────────────────

def fetch_facebook_ads(client, facebook_page, competitor_name, results_limit=MAX_ADS_PER_REQUEST):
    try:
        logger.info(f"Запрашиваю Facebook рекламу для: {facebook_page} (лимит {results_limit})")
        run_input = {
            "startUrls": [{
                "url": facebook_page if facebook_page.startswith("http")
                       else f"https://www.facebook.com/{facebook_page}"
            }],
            # ВАЖНО: у актора поле называется resultsLimit (НЕ maxAds!). Иначе лимит
            # игнорируется и он выкачивает ВСЮ рекламу страницы (706 у 1Fit → сжёг $5).
            "resultsLimit": results_limit,
        }
        run = client.actor(FACEBOOK_ADS_ACTOR).call(run_input=run_input)
        dataset_id = getattr(run, "default_dataset_id", None) or run["defaultDatasetId"]

        ads = []
        api_error = None
        for item in client.dataset(dataset_id).iterate_items():
            if not isinstance(item, dict):
                continue
            # Актор возвращает объект-ошибку, если страница не найдена/закрыта/неверная
            is_ad = item.get("snapshot") or item.get("adArchiveID") or item.get("adArchiveId")
            if item.get("error") or not is_ad:
                api_error = item.get("errorDescription") or item.get("error") or api_error
                continue
            ads.append(_parse_facebook_item(item, competitor_name))

        logger.info(f"Facebook: найдено {len(ads)} объявлений для {competitor_name}")
        if not ads and api_error:
            return [], (
                "Не удалось открыть эту страницу в библиотеке Meta. Прямая ссылка на "
                "профиль Instagram часто не подходит — дай ссылку на Facebook-страницу "
                "конкурента или ссылку из Библиотеки рекламы Meta (facebook.com/ads/library)."
            )
        return ads, None

    except ApifyQuotaError:
        raise                       # пусть пул переключится на следующий ключ
    except Exception as e:
        if is_quota_error(e):       # у ключа кончился лимит → сигналим пулу
            raise ApifyQuotaError(str(getattr(e, "message", None) or e))
        logger.error(f"Facebook Ads для {facebook_page}: {e}", exc_info=True)
        return [], f"Ошибка: {str(e)[:150]}"


def _parse_facebook_item(item: dict, competitor_name: str) -> AdResult:
    snap = item.get("snapshot") or {}
    if not isinstance(snap, dict):
        snap = {}

    return AdResult(
        source="facebook",
        competitor_name=competitor_name,
        ad_id=str(item.get("adArchiveID") or item.get("adArchiveId") or item.get("adId") or "unknown"),
        status="active" if item.get("isActive", True) else "inactive",
        body_text=_extract_text(snap),
        title=_clean(snap.get("title")),
        caption=_clean(snap.get("caption")),
        link_description=_clean(snap.get("linkDescription") or snap.get("link_description")),
        cta_text=_clean(snap.get("ctaText") or snap.get("cta_text")),
        link_url=_clean(snap.get("linkUrl") or snap.get("link_url")),
        display_format=_clean(snap.get("displayFormat")),
        started_at=item.get("startDate") or item.get("startDateFormatted"),
        ended_at=item.get("endDate") or item.get("endDateFormatted"),
        platforms=item.get("publisherPlatform") or [],
        page_name=_clean(item.get("pageName") or snap.get("pageName")),
        page_id=_clean(item.get("pageID") or item.get("pageId") or snap.get("pageId")),
        images=_extract_images(snap),
        videos=_extract_videos(snap),
        impressions=_extract_impressions(item),
        spend=_clean(item.get("spend")),
    )


def _extract_text(snap: dict) -> str:
    """Полный текст без обрезки. Собираем body + extraTexts + тексты карточек."""
    parts = []
    body = snap.get("body")
    if isinstance(body, dict):
        t = body.get("text") or body.get("markup", {}).get("__html__", "")
        if t:
            parts.append(str(t))
    elif isinstance(body, str) and body:
        parts.append(body)

    for extra in snap.get("extraTexts") or []:
        t = extra.get("text") if isinstance(extra, dict) else str(extra)
        if t:
            parts.append(str(t))

    for card in snap.get("cards") or []:
        if isinstance(card, dict):
            t = card.get("body") or card.get("text")
            if t and str(t) not in parts:
                parts.append(str(t))

    return "\n\n".join(parts).strip() or "—"


def _extract_images(snap: dict) -> list:
    urls = []
    sources = (snap.get("images") or []) + (snap.get("extraImages") or [])
    for card in snap.get("cards") or []:
        if isinstance(card, dict):
            sources.append(card)
    for im in sources:
        if isinstance(im, dict):
            url = (im.get("originalImageUrl") or im.get("original_image_url")
                   or im.get("resizedImageUrl") or im.get("url"))
            if url and url not in urls:
                urls.append(url)
    return urls


def _extract_videos(snap: dict) -> list:
    vids = []
    sources = list(snap.get("videos") or []) + list(snap.get("extraVideos") or [])
    for card in snap.get("cards") or []:
        if isinstance(card, dict) and (card.get("videoSdUrl") or card.get("videoHdUrl")):
            sources.append(card)
    for v in sources:
        if not isinstance(v, dict):
            continue
        hd = v.get("videoHdUrl"); sd = v.get("videoSdUrl")
        preview = v.get("videoPreviewImageUrl")
        if hd or sd or preview:
            vids.append({"hd": hd, "sd": sd, "preview": preview})
    return vids


def _extract_impressions(item: dict) -> Optional[str]:
    imp = item.get("impressionsWithIndex")
    if isinstance(imp, dict) and imp.get("impressionsText"):
        return imp["impressionsText"]
    reach = item.get("reachEstimate")
    if isinstance(reach, dict):
        lo, hi = reach.get("lowerBound"), reach.get("upperBound")
        if lo or hi:
            return f"{lo}–{hi}"
    elif reach:
        return str(reach)
    return None


def _clean(value) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


# ── Google (Фаза 2 — пока не используется ботом) ────────────────────────────────

def fetch_google_ads(client, domain, competitor_name):
    try:
        clean_domain = domain.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]
        run_input = {"keywords": clean_domain, "maxResults": MAX_ADS_PER_REQUEST, "mode": "FULL"}
        run = client.actor(GOOGLE_ADS_ACTOR).call(run_input=run_input)
        dataset_id = getattr(run, "default_dataset_id", None) or run["defaultDatasetId"]
        ads = []
        for item in client.dataset(dataset_id).iterate_items():
            if not isinstance(item, dict):
                continue
            ads.append(AdResult(
                source="google", competitor_name=competitor_name,
                ad_id=str(item.get("adId") or item.get("id") or "unknown"),
                status="active",
                body_text=_clean(item.get("headline") or item.get("description") or item.get("text")) or "—",
                link_url=_clean(item.get("url") or item.get("displayUrl")),
                started_at=item.get("firstShown") or item.get("date"),
            ))
        return ads, None
    except Exception as e:
        err = str(e)
        if any(w in err.lower() for w in ("rent", "trial", "forbidden")):
            return [], "⏳ Google Ads — скоро (функция в разработке)"
        logger.error(f"Google Ads для {domain}: {e}", exc_info=True)
        return [], f"Ошибка: {err[:100]}"


# ── Несколько сценариев поиска по нику ───────────────────────────────────────────

_SUFFIX_RE = re.compile(
    r"[_.\- ]?(kz|kaz|kazakhstan|almaty|astana|ru|rus|uz|official|store|shop|online)$", re.I)


def _candidate_queries(handle: str) -> list:
    """Варианты запроса из ника: как есть, без страны-суффикса, без разделителей."""
    h = handle.strip().lstrip("@")
    seen, out = set(), []

    def add(x):
        x = (x or "").strip()
        if x and x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)

    add(h)                                          # plast_garant / biotechusa_kz
    base = _SUFFIX_RE.sub("", h)
    add(base)                                       # biotechusa
    add(h.replace("_", "").replace(".", ""))        # plastgarant
    add(base.replace("_", "").replace(".", ""))
    return out[:4]


def search_url(query: str) -> str:
    """Ссылка-поиск по Библиотеке рекламы (страна — Казахстан)."""
    return (
        "https://www.facebook.com/ads/library/?active_status=all&ad_type=all"
        f"&country=KZ&q={quote(query.strip())}&search_type=keyword_unordered&media_type=all"
    )


def extract_search_query(url) -> Optional[str]:
    """Если это ссылка-поиск (q=…&search_type=…) — вернуть сам запрос, иначе None."""
    if isinstance(url, str) and "ads/library" in url and "q=" in url and "search_type" in url:
        return unquote(url.split("q=")[1].split("&")[0])
    return None


def _fetch_facebook_multi(client, query, competitor_name, results_limit=MAX_ADS_PER_REQUEST):
    """Пробует варианты ника по очереди, останавливается на первом с результатами.
    Пустые запросы при pay-per-event почти бесплатны, так что перебор недорогой."""
    last_err = None
    for variant in _candidate_queries(query):
        ads, err = fetch_facebook_ads(client, search_url(variant), competitor_name, results_limit)
        if ads:
            logger.info(f"Нашёл по варианту '{variant}' ({len(ads)} объявл.)")
            return ads, None
        last_err = err
    return [], last_err


def fetch_total_count(client, page_url):
    """Общее число объявлений рекламодателя (дёшево, через onlyTotal). None — если не вышло."""
    try:
        run = client.actor(FACEBOOK_ADS_ACTOR).call(
            run_input={"startUrls": [{"url": page_url}], "onlyTotal": True})
        ds = getattr(run, "default_dataset_id", None) or run["defaultDatasetId"]
        for item in client.dataset(ds).iterate_items():
            if isinstance(item, dict) and item.get("totalCount") is not None:
                return int(item["totalCount"])
    except Exception as e:
        logger.debug(f"total count fail: {e}")
    return None


# ── Главная функция ────────────────────────────────────────────────────────────

def analyze_competitor(apify_tokens, competitor_name, facebook_page=None, google_domain=None,
                       results_limit=MAX_ADS_PER_REQUEST):
    """Собирает рекламу конкурента.

    apify_tokens — пул ключей (ApifyKeyPool), список ключей или одна строка-ключ.
    Если у текущего ключа кончился месячный лимит Apify, автоматически переходит
    на следующий ключ из пула. Когда все ключи исчерпаны — отдаёт понятную ошибку.
    """
    pool = apify_tokens if isinstance(apify_tokens, ApifyKeyPool) else ApifyKeyPool(_as_token_list(apify_tokens))

    for token in pool.tokens:
        if pool.is_exhausted(token):
            continue
        try:
            return _collect_report(ApifyClient(token), competitor_name,
                                   facebook_page, google_domain, results_limit)
        except ApifyQuotaError:
            pool.mark_exhausted(token)   # этот ключ кончился — пробуем следующий
            continue

    # Ключей нет или у всех кончился месячный лимит
    report = CompetitorAdsReport(competitor_name, facebook_page, google_domain)
    report.error_facebook = (
        "🙏 Лимит бесплатного сбора рекламы на сегодня исчерпан. "
        "Попробуй позже — ресурсы скоро обновятся."
    )
    return report


def _collect_report(client, competitor_name, facebook_page, google_domain, results_limit):
    """Одна попытка сбора на конкретном ключе (client). При нехватке лимита бросает
    ApifyQuotaError — её ловит analyze_competitor и переключает ключ."""
    report = CompetitorAdsReport(
        competitor_name=competitor_name,
        facebook_page=facebook_page,
        google_domain=google_domain,
    )
    if facebook_page:
        query = extract_search_query(facebook_page)
        if query is not None:   # ссылка-поиск → пробуем варианты ника
            report.facebook_ads, report.error_facebook = _fetch_facebook_multi(
                client, query, competitor_name, results_limit)
        else:                   # прямая страница / view_all_page_id → как есть + общее число
            report.facebook_ads, report.error_facebook = fetch_facebook_ads(
                client, facebook_page, competitor_name, results_limit)
            if report.facebook_ads:
                report.total_count = fetch_total_count(client, facebook_page)
    if google_domain:
        report.google_ads, report.error_google = fetch_google_ads(client, google_domain, competitor_name)
    return report


def _as_token_list(tokens):
    """Нормализует вход (строка / список / None) в список непустых ключей."""
    if isinstance(tokens, str):
        return [tokens] if tokens.strip() else []
    return list(tokens or [])


# ── Даты ─────────────────────────────────────────────────────────────────────────

def _to_datetime(value) -> Optional[datetime]:
    """Unix (сек или мс) или ISO-строку → aware datetime (UTC)."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            ts = value / 1000 if value > 1e11 else value
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        if isinstance(value, str):
            v = value.replace("Z", "+00:00")
            try:
                dt = datetime.fromisoformat(v)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except ValueError:
                return datetime.fromisoformat(value[:10]).replace(tzinfo=timezone.utc)
    except Exception:
        return None
    return None


def _format_date(value) -> Optional[str]:
    dt = _to_datetime(value)
    return dt.strftime("%Y-%m-%d") if dt else None


# ── Короткая сводка (необязательная, бот рендерит карточки сам) ──────────────────

def format_ads_report(report: CompetitorAdsReport, max_ads: int = 5) -> str:
    lines = [
        f"🕵️ *{report.competitor_name}*",
        f"📊 Объявлений: *{report.total_ads}* · активных: *{report.active_ads}*",
    ]
    if report.max_days_running is not None:
        lines.append(f"⏱ Дольше всех крутится: *{report.max_days_running} дн.*")
    return "\n".join(lines)


# ── Кэш результата (чтобы «Полный отчёт» не запускал Apify заново) ───────────────

CACHE_DIR = "data/cache"


def cache_report(report: CompetitorAdsReport, competitor_id: int) -> None:
    """Сохраняет результат анализа на диск — потом отчёт строим без новых трат."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    payload = {
        "competitor_name": report.competitor_name,
        "facebook_page": report.facebook_page,
        "google_domain": report.google_domain,
        "facebook_ads": [asdict(a) for a in report.facebook_ads],
        "google_ads": [asdict(a) for a in report.google_ads],
        "total_count": report.total_count,
        "cached_at": datetime.now(timezone.utc).isoformat(),
    }
    path = os.path.join(CACHE_DIR, f"competitor_{competitor_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def load_cached_report(competitor_id: int) -> Optional[CompetitorAdsReport]:
    """Достаёт последний сохранённый результат анализа (или None)."""
    path = os.path.join(CACHE_DIR, f"competitor_{competitor_id}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        rep = CompetitorAdsReport(d["competitor_name"], d.get("facebook_page"), d.get("google_domain"))
        rep.facebook_ads = [AdResult(**a) for a in d.get("facebook_ads", [])]
        rep.google_ads = [AdResult(**a) for a in d.get("google_ads", [])]
        rep.total_count = d.get("total_count")
        rep.cached_at = d.get("cached_at")
        return rep
    except Exception as e:
        logger.warning(f"Не удалось прочитать кэш {competitor_id}: {e}")
        return None


def delete_cache(competitor_id: int) -> None:
    """Удаляет кэш конкурента (при удалении конкурента)."""
    path = os.path.join(CACHE_DIR, f"competitor_{competitor_id}.json")
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.debug(f"Не удалось удалить кэш {competitor_id}: {e}")


def cache_age_hours(cached_at_iso: Optional[str]) -> Optional[float]:
    """Возраст кэша в часах (или None)."""
    if not cached_at_iso:
        return None
    try:
        dt = datetime.fromisoformat(cached_at_iso)
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except Exception:
        return None
