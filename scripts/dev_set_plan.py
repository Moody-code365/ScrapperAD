"""
scripts/dev_set_plan.py
=======================
Утилита для разработки: вручную выставить тариф пользователю.

Запуск (из корня проекта):
    python -m scripts.dev_set_plan <telegram_id> <plan>

Пример:
    python -m scripts.dev_set_plan 123456789 pro

Тарифы: free / start / pro
"""

import sys

from config.settings import settings, PLANS
from database.db import get_connection, set_user_plan


def main():
    if len(sys.argv) != 3:
        print("Использование: python -m scripts.dev_set_plan <telegram_id> <plan>")
        print(f"Тарифы: {', '.join(PLANS)}")
        sys.exit(1)

    telegram_id = int(sys.argv[1])
    plan = sys.argv[2]

    if plan not in PLANS:
        print(f"❌ Неизвестный тариф '{plan}'. Доступны: {', '.join(PLANS)}")
        sys.exit(1)

    conn = get_connection(settings.database_path)
    set_user_plan(conn, telegram_id, plan)
    row = conn.execute(
        "SELECT telegram_id, plan FROM users WHERE telegram_id = ?", (telegram_id,)
    ).fetchone()
    conn.close()

    if row:
        print(f"✅ Пользователь {row['telegram_id']} → тариф '{row['plan']}'")
    else:
        print(f"⚠️ Пользователь {telegram_id} не найден (ещё не писал боту /start).")


if __name__ == "__main__":
    main()
