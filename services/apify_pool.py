"""
services/apify_pool.py
======================
Пул из нескольких Apify-ключей с авто-переключением.

Зачем: у бесплатного плана Apify ограниченный месячный кредит на каждый аккаунт.
Чтобы стартовать без затрат, держим НЕСКОЛЬКО ключей (из разных бесплатных аккаунтов)
и, когда у одного кончился лимит, автоматически переходим на следующий.

Состояние «какой ключ исчерпан в этом месяце» храним на диске
(data/apify_keys.json), чтобы не дёргать заведомо пустой ключ при каждом запуске.
Кредиты Apify обновляются ежемесячно — на стыке месяца пометки сбрасываются сами.
"""

import os
import json
import hashlib
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

STATE_PATH = "data/apify_keys.json"


class ApifyQuotaError(Exception):
    """Ключ Apify исчерпал месячный лимит/кредиты — нужно переключиться на другой."""


# Маркеры ошибки «кончились кредиты/лимит» в ответе Apify API (а не временный сбой).
# Держим узкими: ложное срабатывание зря выключит рабочий ключ на весь месяц,
# а пропуск — лишь покажет обычную ошибку без переключения (это безопаснее).
_QUOTA_TYPE_MARKERS = ("usage-hard-limit", "monthly-usage")
_QUOTA_MSG_MARKERS = ("usage hard limit", "monthly usage", "hard limit",
                      "out of credit", "payment required")


def is_quota_error(exc) -> bool:
    """True, если исключение Apify означает «кончились кредиты/лимит» (а не временную ошибку).

    Опирается на атрибуты ApifyApiError (status_code / type / message), но безопасно
    работает и с обычными исключениями — читает их через getattr/str.
    """
    status = getattr(exc, "status_code", None)
    if status == 429:           # rate limit — временно, ключ НЕ исчерпан
        return False
    if status == 402:           # Payment Required — точно про деньги/лимит
        return True
    etype = (getattr(exc, "type", "") or "").lower()
    if any(m in etype for m in _QUOTA_TYPE_MARKERS):
        return True
    blob = (getattr(exc, "message", "") or str(exc)).lower()
    return any(m in blob for m in _QUOTA_MSG_MARKERS)


class ApifyKeyPool:
    """Хранит список ключей и помнит, какие из них исчерпаны в текущем месяце."""

    def __init__(self, tokens, state_path: str = STATE_PATH):
        # убираем пустые и дубликаты, сохраняя порядок
        seen, clean = set(), []
        for t in (tokens or []):
            t = (t or "").strip()
            if t and t not in seen:
                seen.add(t)
                clean.append(t)
        self.tokens = clean
        self.state_path = state_path
        self._month = _current_month()
        self._exhausted = self._load()

    # ── публичный API ─────────────────────────────────────────────────
    def has_tokens(self) -> bool:
        return bool(self.tokens)

    def available(self) -> list:
        """Ключи, у которых ещё есть лимит в этом месяце."""
        return [t for t in self.tokens if not self.is_exhausted(t)]

    def is_exhausted(self, token: str) -> bool:
        self._maybe_reset_month()
        return _key_id(token) in self._exhausted

    def mark_exhausted(self, token: str) -> None:
        self._maybe_reset_month()
        self._exhausted.add(_key_id(token))
        self._save()
        logger.warning("Apify-ключ #%d исчерпан на %s — перехожу на следующий",
                       self.index(token), self._month)

    def index(self, token: str) -> int:
        """Человеческий номер ключа (1-based) для логов."""
        try:
            return self.tokens.index(token) + 1
        except ValueError:
            return 0

    # ── состояние на диске ────────────────────────────────────────────
    def _maybe_reset_month(self) -> None:
        now = _current_month()
        if now != self._month:           # новый месяц → кредиты Apify обновились
            self._month = now
            self._exhausted = set()
            self._save()

    def _load(self) -> set:
        try:
            with open(self.state_path, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("month") == self._month:
                return set(d.get("exhausted", []))
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.debug("apify keys state read fail: %s", e)
        return set()

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump({"month": self._month, "exhausted": sorted(self._exhausted)}, f)
        except Exception as e:
            logger.debug("apify keys state write fail: %s", e)


def _key_id(token: str) -> str:
    """Короткий хэш ключа — храним его, а не сам токен."""
    return hashlib.sha256((token or "").encode()).hexdigest()[:12]


def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")
