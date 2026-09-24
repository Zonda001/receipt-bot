import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message

from receipt_bot.config import Settings


async def start(message: Message) -> None:
    await message.answer(
        "Привіт! Я записую чеки команди в Google Sheets.\n"
        "Спершу /login, потім просто надішли фото чека."
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings()

    bot = Bot(settings.bot_token.get_secret_value())
    dp = Dispatcher()
    dp.message.register(start, CommandStart())

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
