"""Telegram-сценарій: фото -> розпізнана сума -> підтвердження користувачем.

Поки що без Google: після підтвердження нічого не записується (наступний етап — Drive + Sheets).
Чеки в очікуванні живуть у пам'яті: після рестарту старі кнопки чесно кажуть «чек застарів».
"""
import logging
import secrets
import time
from collections import defaultdict, deque
from contextlib import suppress
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import aiohttp
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, ReplyParameters
from aiogram.utils.keyboard import InlineKeyboardBuilder

from receipt_bot.recognition import (
    NotAnImage, RateLimited, Recognition, RecognitionError, Recognizer, parse_amount,
)

log = logging.getLogger(__name__)
router = Router()

MAX_FILE_BYTES = 10 * 1024 * 1024
PENDING_TTL = 24 * 3600
PHOTOS_PER_MINUTE = 5
IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}

HELP_TEXT = (
    "Я записую чеки команди.\n"
    "Надішли фото чека — я знайду суму і попрошу її підтвердити."
)


class ReceiptAction(CallbackData, prefix="r"):
    rid: str
    action: str  # ok | manual | cancel
    idx: int = 0


class ManualAmount(StatesGroup):
    waiting = State()


@dataclass
class Pending:
    user_id: int
    file_id: str
    recognition: Recognition
    created: float
    status: str = "pending"  # pending -> processing -> done | cancelled


class PendingStore:
    """Чеки, що чекають підтвердження. Ключ — короткий випадковий id у callback_data."""

    def __init__(self) -> None:
        self._items: dict[str, Pending] = {}

    def add(self, item: Pending) -> str:
        self._evict()
        rid = secrets.token_hex(4)
        self._items[rid] = item
        return rid

    def get(self, rid: str | None) -> Pending | None:
        self._evict()
        return self._items.get(rid) if rid else None

    def _evict(self) -> None:
        now = time.time()
        for rid in [r for r, p in self._items.items() if now - p.created > PENDING_TTL]:
            del self._items[rid]


class RateLimiter:
    """Не більше N фото на хвилину від одного користувача (захист квоти vision-моделі)."""

    def __init__(self, per_minute: int) -> None:
        self._per_minute = per_minute
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    def allow(self, user_id: int) -> bool:
        now = time.time()
        hits = self._hits[user_id]
        while hits and now - hits[0] > 60:
            hits.popleft()
        if not hits:
            self._hits.pop(user_id, None)  # не тримати в пам'яті тих, хто давно не писав
            hits = self._hits[user_id]
        if len(hits) >= self._per_minute:
            return False
        hits.append(now)
        return True


class DailyQuota:
    """Скільки розпізнавань на добу на всю команду: безкоштовний тариф провайдера має денну стелю."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._day = date.today()
        self._used = 0

    def take(self) -> bool:
        if date.today() != self._day:
            self._day, self._used = date.today(), 0
        if self._used >= self._limit:
            return False
        self._used += 1
        return True


def fmt(amount: Decimal, currency: str = "UAH") -> str:
    text = f"{amount:,.2f}".replace(",", " ")
    return f"{text} грн" if currency == "UAH" else f"{text} {currency}"


def result_view(rid: str, rec: Recognition, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardBuilder()
    if note:
        text = note
        others = []
    elif rec.has_total:
        text = f"Сума: {fmt(rec.total, rec.currency)}\nПідтвердити?"
        kb.button(text=f"✅ Підтвердити {fmt(rec.total, rec.currency)}",
                  callback_data=ReceiptAction(rid=rid, action="ok", idx=0))
        others = list(enumerate(rec.candidates))[1:]
        if others:
            text += "\n\nНа чеку є й інші суми — можна обрати іншу:"
    elif rec.candidates:
        text = "Не впевнений, яка сума підсумкова. Обери:"
        others = list(enumerate(rec.candidates))
    else:
        text = "Не знайшов суму на чеку. Введи її вручну або надішли чіткіше фото."
        others = []

    for idx, c in others:
        kb.button(text=f"{c.label}: {fmt(c.amount, rec.currency)}",
                  callback_data=ReceiptAction(rid=rid, action="ok", idx=idx))
    kb.button(text="✏️ Ввести вручну", callback_data=ReceiptAction(rid=rid, action="manual"))
    kb.button(text="✖️ Скасувати", callback_data=ReceiptAction(rid=rid, action="cancel"))
    kb.adjust(1)
    return text, kb.as_markup()


def is_allowed(user_id: int, allowed_ids: set[int]) -> bool:
    # Тимчасово, до Google-авторизації: бот публічний, тож без списку будь-хто спалює квоту моделі.
    return user_id in allowed_ids


async def drop_keyboard(query: CallbackQuery) -> None:
    """Прибрати кнопки. Косметика: старе/недоступне повідомлення не повинно ламати сценарій."""
    if isinstance(query.message, Message):
        with suppress(TelegramBadRequest):
            await query.message.edit_reply_markup(reply_markup=None)


@router.message(CommandStart())
@router.message(Command("help"))
async def on_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(HELP_TEXT)


@router.message(F.photo | F.document)
async def on_photo(message: Message, bot: Bot, state: FSMContext, recognizer: Recognizer,
                   pending: PendingStore, limiter: RateLimiter, quota: DailyQuota, allowed_ids: set[int]) -> None:
    await state.clear()
    user_id = message.from_user.id
    if not is_allowed(user_id, allowed_ids):
        log.info("rejected user %s (not in allowlist)", user_id)
        await message.answer(f"Бот поки в розробці. Твій Telegram ID: {user_id} — передай його адміністратору.")
        return

    if message.document:
        mime = message.document.mime_type or ""
        if mime in {"image/heic", "image/heif"}:
            await message.answer("Формат HEIC не підтримується. Надішли чек як фото, а не файлом.")
            return
        if mime not in IMAGE_MIME_TYPES:
            await message.answer("Надішли фото чека (JPG або PNG), а не інший файл.")
            return
    file = message.photo[-1] if message.photo else message.document
    if file.file_size and file.file_size > MAX_FILE_BYTES:
        await message.answer("Файл завеликий (більше 10 МБ). Надішли звичайне фото чека.")
        return
    if not limiter.allow(user_id):
        await message.answer("Забагато чеків за хвилину. Зачекай трохи і надішли ще раз.")
        return

    # Відповідь цитує фото: якщо чеків кілька, видно, яка сума до якого.
    status = await message.answer(
        "Розпізнаю…", reply_parameters=ReplyParameters(message_id=message.message_id, allow_sending_without_reply=True))

    try:
        image = await bot.download(file.file_id)
    except (TelegramAPIError, aiohttp.ClientError, TimeoutError) as e:
        # Тільки тип помилки: текст aiohttp-винятку містить URL файлу разом із токеном бота.
        log.warning("photo download failed: %s", type(e).__name__)
        await status.edit_text("Не вдалося отримати фото з Telegram. Надішли його ще раз.")
        return

    rec: Recognition | None = None
    note = ""  # непорожня — розпізнати не вдалося, пропонуємо ввести суму вручну
    try:
        if quota.take():
            rec = await recognizer.recognize(image.read())
        else:
            note = "Ліміт автоматичного розпізнавання на сьогодні вичерпано. Можеш ввести суму вручну."
    except NotAnImage:
        await status.edit_text(
            "Не вдалося відкрити зображення (пошкоджене, завелике або формат не підтримується). "
            "Надішли чек як звичайне фото, не файлом.")
        return
    except RateLimited as e:
        log.warning("rate limited, retry after %.0fs", e.retry_after)
        note = "Розпізнавання зараз перевантажене. Спробуй надіслати фото за хвилину або введи суму вручну."
    except RecognitionError as e:
        log.warning("recognition failed: %s", e)
        note = "Не вдалося розпізнати чек. Спробуй ще раз пізніше або введи суму вручну."
    except Exception:
        log.exception("unexpected error while recognizing")
        note = "Не вдалося розпізнати чек. Спробуй ще раз пізніше або введи суму вручну."

    if rec is None:
        rec = Recognition(is_receipt=True)  # сум нема: лишаються тільки "ввести вручну" і "скасувати"
    elif not rec.is_receipt:
        await status.edit_text("Це не схоже на чек. Надішли фото чека.")
        return

    rid = pending.add(Pending(user_id=user_id, file_id=file.file_id, recognition=rec, created=time.time()))
    text, markup = result_view(rid, rec, note=note)
    await status.edit_text(text, reply_markup=markup)


async def finalize(message: Message, item: Pending, amount: Decimal, manual: bool) -> None:
    """Викликати тільки після item.status = "processing" (ставиться до першого await у хендлері)."""
    # Наступний етап: тут перевірка доступу -> Drive -> Sheets; при збої статус повернеться в "pending".
    item.status = "done"
    note = " (введено вручну)" if manual else ""
    with suppress(TelegramAPIError):
        await message.answer(
            f"✅ Підтверджено: {fmt(amount, item.recognition.currency)}{note}\n"
            "Запис у Google Drive і Sheets додамо на наступному етапі — зараз нічого не збережено."
        )


@router.callback_query(ReceiptAction.filter())
async def on_action(query: CallbackQuery, callback_data: ReceiptAction, state: FSMContext,
                    pending: PendingStore) -> None:
    item = pending.get(callback_data.rid)
    if item is None:
        await query.answer("Чек застарів — надішли фото ще раз.", show_alert=True)
        await drop_keyboard(query)
        return
    if item.user_id != query.from_user.id:
        await query.answer("Це не твій чек.", show_alert=True)
        return
    if item.status == "processing":
        await query.answer("Вже обробляю…")
        return
    if item.status != "pending":
        await query.answer("Цей чек уже оброблено.")
        return

    if callback_data.action == "ok":
        if not 0 <= callback_data.idx < len(item.recognition.candidates):
            await query.answer("Невідома сума.", show_alert=True)
            return
        amount = item.recognition.candidates[callback_data.idx].amount
        item.status = "processing"  # одразу, до першого await: aiogram обробляє натискання паралельно
        await query.answer()
        if (await state.get_data()).get("rid") == callback_data.rid:
            await state.clear()  # якщо до цього натискали "ввести вручну" для цього ж чека
        await drop_keyboard(query)
        await finalize(query.message, item, amount, manual=False)
    elif callback_data.action == "manual":
        await state.set_state(ManualAmount.waiting)
        await state.update_data(rid=callback_data.rid)
        await query.answer()
        if isinstance(query.message, Message):
            with suppress(TelegramBadRequest):
                # Редагуємо саме це повідомлення: воно цитує фото, тож видно, для якого чека сума.
                await query.message.edit_text("Введи суму для цього чека числом, наприклад 123.45")
    elif callback_data.action == "cancel":
        item.status = "cancelled"
        await query.answer("Скасовано")
        if isinstance(query.message, Message):
            with suppress(TelegramBadRequest):
                await query.message.edit_text("Скасовано. Нічого не записано.")
    else:
        await query.answer()


@router.message(ManualAmount.waiting, F.text)
async def on_manual_amount(message: Message, state: FSMContext, pending: PendingStore) -> None:
    item = pending.get((await state.get_data()).get("rid"))
    if item is None or item.user_id != message.from_user.id:
        await state.clear()
        await message.answer("Чек застарів — надішли фото ще раз.")
        return
    if item.status != "pending":
        await state.clear()
        await message.answer("Цей чек уже оброблено.")
        return
    amount = parse_amount(message.text)
    if amount is None:
        await message.answer("Не схоже на суму. Введи число, наприклад 123.45")
        return
    item.status = "processing"
    await state.clear()
    await finalize(message, item, amount, manual=True)


@router.message()
async def on_other(message: Message) -> None:
    if message.text and parse_amount(message.text) is not None:
        # Схоже на суму, але чека для неї нема (наприклад, бот перезапускався посеред введення).
        await message.answer("Не бачу чека, до якого ця сума. Надішли фото чека ще раз.")
        return
    await message.answer(HELP_TEXT)
