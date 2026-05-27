#!/usr/bin/env python3
"""Запуск бота"""

import asyncio
import sys
from pathlib import Path

# Добавляем корень проекта в path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from app.bot import create_bot, create_dispatcher
from app.config import settings
from redis.asyncio import Redis
import asyncio

async def main():
    # Проверяем конфигурацию
    if not settings.BOT_TOKEN:
        print("❌ Ошибка: BOT_TOKEN не задан в .env")
        sys.exit(1)
    if not settings.OPENROUTER_API_KEY:
        print("❌ Ошибка: OPENROUTER_API_KEY не задан в .env")
        sys.exit(1)

    print("🤖 Запуск бота...")
    print(f"📡 OpenRouter: {settings.OPENROUTER_API_KEY[:10]}...")

    # Создаем бота и диспатчер
    bot = create_bot()
    redis = Redis.from_url(settings.REDIS_URL)
    dp = create_dispatcher(redis)

    try:
        print("✅ Бот запущен. Нажми Ctrl+C для остановки.")
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        print("\n🛑 Остановка...")
    finally:
        await bot.session.close()
        await redis.aclose()

if __name__ == '__main__':
    asyncio.run(main())
