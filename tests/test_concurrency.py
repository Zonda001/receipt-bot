"""Гонки, які з'являються лише при паралельній обробці натискань (aiogram так і працює)."""
import asyncio
import io

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from PIL import Image

from receipt_bot.handlers import Pending, PendingStore, release_manual
from receipt_bot.recognition import RateLimited, Recognition, Recognizer, Truncated


class SlowBot:
    """edit_message_text висить, доки тест не відпустить: імітує мережевий await."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()

    async def edit_message_text(self, **kwargs) -> None:
        await self.gate.wait()


def test_fast_manual_taps_on_two_receipts_keep_the_last_one():
    async def scenario():
        state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
        pending = PendingStore()
        old = pending.add(Pending(user_id=1, file_id="f", recognition=Recognition(is_receipt=True),
                                  created=0, chat_id=1, msg_id=1))
        pending._items[old].created = float("inf")  # не вичищати за TTL
        await state.update_data(rid=old)
        bot = SlowBot()

        tap_a = asyncio.create_task(release_manual(bot, state, pending, new_rid="A"))
        await asyncio.sleep(0)  # A стоїть на відновленні кнопок старого чека
        assert (await state.get_data())["rid"] == "A"
        await release_manual(bot, state, pending, new_rid="B")  # другий тап, поки A чекає Telegram
        bot.gate.set()
        await tap_a
        assert (await state.get_data())["rid"] == "B"

    asyncio.run(scenario())


def test_rate_limit_on_retry_is_marked_spent(monkeypatch):
    async def scenario():
        rec = Recognizer("http://x", "m", "k")
        calls = []

        async def fake_post(body):
            calls.append(body)
            if len(calls) == 1:
                return {}
            raise RateLimited(5)

        def fake_parse(data):
            raise Truncated("cut")

        monkeypatch.setattr(rec, "_post", fake_post)
        monkeypatch.setattr(rec, "_parse", fake_parse)
        buf = io.BytesIO()
        Image.new("RGB", (50, 50), "white").save(buf, "JPEG")
        try:
            await rec.recognize(buf.getvalue())
        except RateLimited as e:
            assert e.spent and len(calls) == 2
        else:
            raise AssertionError("expected RateLimited")
        finally:
            await rec.close()

    asyncio.run(scenario())


def test_first_request_rate_limit_is_not_spent(monkeypatch):
    async def scenario():
        rec = Recognizer("http://x", "m", "k")

        async def fake_post(body):
            raise RateLimited(5)

        monkeypatch.setattr(rec, "_post", fake_post)
        buf = io.BytesIO()
        Image.new("RGB", (50, 50), "white").save(buf, "JPEG")
        try:
            await rec.recognize(buf.getvalue())
        except RateLimited as e:
            assert not e.spent
        finally:
            await rec.close()

    asyncio.run(scenario())
