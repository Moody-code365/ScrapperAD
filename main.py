"""
main.py
=======
Точка входа. Запуск: python main.py
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeDefault, BotCommandScopeChat

from config.settings import settings
from database.db import init_database
from bot.handlers.start import router as start_router
from bot.handlers.spy import router as spy_router


logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


async def setup_commands(bot: Bot) -> None:
    """Список команд в меню бота (синяя кнопка «/»). Админу — расширенный набор."""
    common = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="addcompetitor", description="➕ Добавить конкурента"),
        BotCommand(command="competitors", description="🕵️ Мои конкуренты"),
        BotCommand(command="plan", description="💳 Тариф и оплата"),
        BotCommand(command="brand", description="🏷 Свой бренд в PDF (Pro/Enterprise)"),
        BotCommand(command="help", description="❓ Как это работает"),
        BotCommand(command="cancel", description="✖️ Отменить действие"),
    ]
    await bot.set_my_commands(common, scope=BotCommandScopeDefault())

    if settings.admin_id:
        admin = common + [
            BotCommand(command="admin", description="🛠 Админ-панель"),
            BotCommand(command="grant", description="Выдать тариф пользователю"),
            BotCommand(command="users", description="Список пользователей"),
            BotCommand(command="stats", description="Статистика бота"),
        ]
        try:
            await bot.set_my_commands(
                admin, scope=BotCommandScopeChat(chat_id=settings.admin_id))
        except Exception as e:
            logger.warning(f"Не удалось задать админ-команды: {e}")


async def main():
    logger.info("=" * 50)
    logger.info("   ScrapperAD запускается...")
    logger.info("=" * 50)

    # 1. База данных
    init_database(settings.database_path)

    # 2. Бот + диспетчер (MemoryStorage хранит FSM-состояния в памяти)
    #    HTML по умолчанию — безопасно экранируем динамику через html.escape
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # 3. Роутеры. Порядок важен: start перехватывает /start, /help и меню.
    dp.include_router(start_router)
    dp.include_router(spy_router)

    # 4. Список команд в меню бота (для пользователя и расширенный для админа)
    await setup_commands(bot)

    logger.info("✅ Бот запущен! Нажми Ctrl+C для остановки.")

    try:
        await dp.start_polling(bot, skip_updates=True)
    finally:
        await bot.session.close()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
