"""
database/db.py
==============
Работа с базой данных SQLite (файл на диске, без отдельного сервера).

Таблицы:
- users       — пользователи бота и их тариф
- competitors — конкуренты, за рекламой которых следим
- analyses    — лог запусков анализа (для лимитов и истории; контроль расходов на Apify)
"""

import sqlite3
import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def get_connection(db_path: str) -> sqlite3.Connection:
    """Открывает соединение. row['col'] доступ по имени колонки."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_database(db_path: str) -> None:
    """Создаёт таблицы при первом запуске (IF NOT EXISTS — безопасно вызывать всегда)."""
    conn = get_connection(db_path)

    with conn:
        # ── Пользователи ──────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id     INTEGER UNIQUE NOT NULL,
                username        TEXT,
                full_name       TEXT,
                plan            TEXT DEFAULT 'free',   -- free / start / pro
                plan_expires_at TEXT,
                brand_name      TEXT,                  -- white-label: название агентства в PDF
                brand_logo      TEXT,                  -- white-label: file_id лого в Telegram
                is_active       INTEGER DEFAULT 1,
                created_at      TEXT DEFAULT (datetime('now')),
                updated_at      TEXT DEFAULT (datetime('now'))
            )
        """)

        # ── Конкуренты ────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS competitors (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL REFERENCES users(id),
                name            TEXT NOT NULL,
                facebook_page   TEXT,                  -- ссылка или ID страницы FB/Instagram
                google_domain   TEXT,                  -- домен (Google Ads — Фаза 2)
                is_active       INTEGER DEFAULT 1,
                created_at      TEXT DEFAULT (datetime('now'))
            )
        """)

        # ── Лог анализов (лимиты + история) ───────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS analyses (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL REFERENCES users(id),
                competitor_id   INTEGER REFERENCES competitors(id),
                source          TEXT,                  -- facebook / google
                ads_found       INTEGER DEFAULT 0,
                created_at      TEXT DEFAULT (datetime('now'))
            )
        """)

    # Миграции для уже существующих баз: добавляем недостающие колонки
    for col, decl in [("brand_name", "TEXT"), ("brand_logo", "TEXT"), ("plan_expires_at", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass  # колонка уже есть
    conn.commit()

    logger.info(f"✅ База данных инициализирована: {db_path}")
    conn.close()


# ── Пользователи ───────────────────────────────────────────────────────────────

def get_or_create_user(conn: sqlite3.Connection, telegram_id: int,
                       username: str = None, full_name: str = None) -> sqlite3.Row:
    """Находит пользователя по telegram_id или создаёт нового."""
    user = conn.execute(
        "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
    ).fetchone()

    if not user:
        conn.execute(
            "INSERT INTO users (telegram_id, username, full_name) VALUES (?, ?, ?)",
            (telegram_id, username, full_name)
        )
        conn.commit()
        user = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        logger.info(f"Новый пользователь: {telegram_id} (@{username})")
    elif username and user["username"] != username:
        # держим @username актуальным — нужно для выдачи тарифа по нику
        conn.execute(
            "UPDATE users SET username = ?, full_name = ? WHERE telegram_id = ?",
            (username, full_name, telegram_id)
        )
        conn.commit()
        user = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()

    return user


def find_user_by_username(conn: sqlite3.Connection, username: str) -> Optional[int]:
    """telegram_id по @username (если юзер уже писал боту). Регистронезависимо."""
    row = conn.execute(
        "SELECT telegram_id FROM users WHERE username = ? COLLATE NOCASE",
        (username.lstrip("@"),)
    ).fetchone()
    return row["telegram_id"] if row else None


def get_recent_users(conn: sqlite3.Connection, limit: int = 20) -> list:
    """Последние пользователи — для админ-команды /users."""
    return conn.execute(
        "SELECT telegram_id, username, full_name, plan FROM users ORDER BY id DESC LIMIT ?",
        (limit,)
    ).fetchall()


def get_user_plan(conn: sqlite3.Connection, telegram_id: int) -> str:
    """Тариф пользователя: free / start / pro."""
    row = conn.execute(
        "SELECT plan FROM users WHERE telegram_id = ?", (telegram_id,)
    ).fetchone()
    return row["plan"] if row else "free"


def set_user_plan(conn: sqlite3.Connection, telegram_id: int, plan: str, days: int = None) -> None:
    """Меняет тариф. days=N → подписка на N дней; days=None → бессрочно (выдача админом)."""
    expires = None
    if days:
        expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    conn.execute(
        "UPDATE users SET plan = ?, plan_expires_at = ?, updated_at = datetime('now') WHERE telegram_id = ?",
        (plan, expires, telegram_id)
    )
    conn.commit()


def set_brand(conn: sqlite3.Connection, telegram_id: int, name: str, logo: str = None) -> None:
    """White-label: название агентства + file_id лого для PDF."""
    conn.execute(
        "UPDATE users SET brand_name = ?, brand_logo = ? WHERE telegram_id = ?",
        (name, logo, telegram_id)
    )
    conn.commit()


def active_plan(user_row) -> str:
    """Эффективный тариф с учётом срока подписки (истёк → free; NULL-срок → бессрочно)."""
    if not user_row:
        return "free"
    plan = user_row["plan"] or "free"
    if plan == "free":
        return "free"
    exp = user_row["plan_expires_at"]
    if not exp:
        return plan
    try:
        dt = datetime.fromisoformat(exp)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return plan if dt > datetime.now(timezone.utc) else "free"
    except Exception:
        return plan


# ── Конкуренты ───────────────────────────────────────────────────────────────────

def add_competitor(conn: sqlite3.Connection, user_id: int, name: str,
                   facebook_page: str = None, google_domain: str = None) -> int:
    """Добавляет конкурента, возвращает его id."""
    cursor = conn.execute(
        """INSERT INTO competitors (user_id, name, facebook_page, google_domain)
           VALUES (?, ?, ?, ?)""",
        (user_id, name, facebook_page, google_domain)
    )
    conn.commit()
    return cursor.lastrowid


def get_user_competitors(conn: sqlite3.Connection, user_id: int) -> list:
    """Активные конкуренты пользователя."""
    return conn.execute(
        "SELECT * FROM competitors WHERE user_id = ? AND is_active = 1 ORDER BY id",
        (user_id,)
    ).fetchall()


def get_competitor(conn: sqlite3.Connection, competitor_id: int,
                   user_id: int) -> Optional[sqlite3.Row]:
    """Один конкурент по id (с проверкой, что он принадлежит пользователю)."""
    return conn.execute(
        "SELECT * FROM competitors WHERE id = ? AND user_id = ? AND is_active = 1",
        (competitor_id, user_id)
    ).fetchone()


def count_user_competitors(conn: sqlite3.Connection, user_id: int) -> int:
    """Сколько активных конкурентов у пользователя."""
    return conn.execute(
        "SELECT COUNT(*) FROM competitors WHERE user_id = ? AND is_active = 1",
        (user_id,)
    ).fetchone()[0]


def update_competitor_page(conn: sqlite3.Connection, competitor_id: int,
                           user_id: int, facebook_page: str) -> None:
    """Закрепляет точную страницу конкурента (после выбора рекламодателя)."""
    conn.execute(
        "UPDATE competitors SET facebook_page = ? WHERE id = ? AND user_id = ?",
        (facebook_page, competitor_id, user_id)
    )
    conn.commit()


def deactivate_competitor(conn: sqlite3.Connection, competitor_id: int,
                          user_id: int) -> None:
    """«Удаляет» конкурента (мягко — is_active = 0)."""
    conn.execute(
        "UPDATE competitors SET is_active = 0 WHERE id = ? AND user_id = ?",
        (competitor_id, user_id)
    )
    conn.commit()


# ── Анализы (лимиты + контроль расходов) ─────────────────────────────────────────

def log_analysis(conn: sqlite3.Connection, user_id: int, competitor_id: int,
                 source: str, ads_found: int) -> None:
    """Записывает факт запуска анализа."""
    conn.execute(
        """INSERT INTO analyses (user_id, competitor_id, source, ads_found)
           VALUES (?, ?, ?, ?)""",
        (user_id, competitor_id, source, ads_found)
    )
    conn.commit()


def count_analyses_this_month(conn: sqlite3.Connection, user_id: int) -> int:
    """Сколько анализов пользователь запустил за текущий календарный месяц."""
    return conn.execute(
        """SELECT COUNT(*) FROM analyses
           WHERE user_id = ?
             AND strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')""",
        (user_id,)
    ).fetchone()[0]


def get_recent_ad_counts(conn: sqlite3.Connection, competitor_id: int, limit: int = 2) -> list:
    """Последние N значений «найдено объявлений» по конкуренту — для дельты в сводке."""
    rows = conn.execute(
        "SELECT ads_found FROM analyses WHERE competitor_id = ? ORDER BY id DESC LIMIT ?",
        (competitor_id, limit)
    ).fetchall()
    return [r["ads_found"] for r in rows]


def get_admin_stats(conn: sqlite3.Connection) -> dict:
    """Сводная статистика для админ-команды /stats."""
    one = lambda q, *a: conn.execute(q, a).fetchone()[0]
    by_plan = {r["plan"]: r["c"] for r in conn.execute(
        "SELECT plan, COUNT(*) AS c FROM users GROUP BY plan").fetchall()}
    return {
        "users": one("SELECT COUNT(*) FROM users"),
        "by_plan": by_plan,
        "new_users_7d": one("SELECT COUNT(*) FROM users WHERE created_at >= datetime('now','-7 days')"),
        "competitors": one("SELECT COUNT(*) FROM competitors WHERE is_active = 1"),
        "analyses_total": one("SELECT COUNT(*) FROM analyses"),
        "analyses_month": one(
            "SELECT COUNT(*) FROM analyses WHERE strftime('%Y-%m', created_at) = strftime('%Y-%m','now')"),
        "active_7d": one(
            "SELECT COUNT(DISTINCT user_id) FROM analyses WHERE created_at >= datetime('now','-7 days')"),
    }
