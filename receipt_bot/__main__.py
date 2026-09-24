import asyncio
import logging

from aiogram import Bot, Dispatcher

from receipt_bot.config import Settings
from receipt_bot.handlers import PHOTOS_PER_MINUTE, PendingStore, RateLimiter, router
from receipt_bot.recognition import Recognizer


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx на INFO пише кожен URL запиту; ключі в заголовках, але шуму в журналі не треба.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings()

    recognizer = Recognizer(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key.get_secret_value(),
        reasoning_effort=settings.llm_reasoning_effort,
    )
    bot = Bot(settings.bot_token.get_secret_value())
    dp = Dispatcher(
        recognizer=recognizer,
        pending=PendingStore(),
        limiter=RateLimiter(PHOTOS_PER_MINUTE),
        allowed_ids=settings.allowed_ids,
    )
    dp.include_router(router)

    try:
        await dp.start_polling(bot)
    finally:
        await recognizer.close()


if __name__ == "__main__":
    asyncio.run(main())
