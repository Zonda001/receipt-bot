"""Telegram-сценарій: фото -> розпізнана сума -> підтвердження користувачем.

Поки що без Google: після підтвердження нічого не записується (наступний етап — Drive + Sheets).
Чеки в очікуванні живуть у пам'яті: після рестарту старі кнопки чесно кажуть «чек застарів».
"""
import logging
import math
import secrets
import time
from collections import defaultdict, deque
from contextlib import suppress
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import aiohttp
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, ReplyParameters
from aiogram.utils.keyboard import InlineKeyboardBuilder

from receipt_bot.recognition import (
    RATE_LIMIT_WAIT_MAX, NotAnImage, RateLimited, Recognition, RecognitionError, RecognizerChain, parse_amount,
)

log = logging.getLogger(__name__)
router = Router()

MAX_FILE_BYTES = 10 * 1024 * 1024
PENDING_TTL = 24 * 3600
PHOTOS_PER_MINUTE = 20  # квитанції шлють альбомами по 9-10
IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}

HELP_TEXT = (
    "Я записую чеки команди.\n"
    "Надішли фото чека — я знайду суму і попрошу її підтвердити."
)
MANUAL_HINT = "Натисни «✏️ Ввести вручну» нижче."


class ReceiptAction(CallbackData, prefix="r"):
    rid: str
    action: str  # ok | manual | back | cancel
    idx: int = 0


class ManualAmount(StatesGroup):
    waiting = State()


@dataclass
class Pending:
    user_id: int
    file_id: str
    recognition: Recognition
    created: float
    chat_id: int
    msg_id: int         # повідомлення бота з кнопками цього чека (воно цитує фото)
    note: str = ""      # чому розпізнати не вдалося (тоді лишається тільки ручне введення)
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

    def give_back(self) -> None:
        """Повернути одиницю, якщо запит до моделі так і не дійшов (битий файл, 429)."""
        if date.today() == self._day and self._used > 0:
            self._used -= 1


def rate_limited_note(retry_after: float) -> str:
    if retry_after <= RATE_LIMIT_WAIT_MAX:
        return "Розпізнавання зараз перевантажене. Надішли фото ще раз за хвилину або введи суму вручну."
    minutes = math.ceil(retry_after / 60)
    wait = f"{minutes} хв" if minutes < 90 else f"{math.ceil(minutes / 60)} год"
    return f"Ліміт автоматичного розпізнавання вичерпано, відновиться приблизно за {wait}. Суму можна ввести вручну."


def currency_name(currency: str) -> str:
    return "грн" if currency == "UAH" else currency


def fmt(amount: Decimal, currency: str = "UAH") -> str:
    return f"{amount:,.2f}".replace(",", " ") + " " + currency_name(currency)


def result_view(rid: str, rec: Recognition, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardBuilder()
    if note:
        text = f"{note}\n{MANUAL_HINT}"
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
        text = f"Не знайшов суму на чеку. {MANUAL_HINT} Або надішли чіткіше фото."
        others = []

    for idx, c in others:
        # Спершу сума, потім мітка з чека: якщо Telegram обріже довгу кнопку, обріжеться мітка, а не число.
        kb.button(text=f"{fmt(c.amount, rec.currency)} — {c.label}",
                  callback_data=ReceiptAction(rid=rid, action="ok", idx=idx))
    kb.button(text="✏️ Ввести вручну", callback_data=ReceiptAction(rid=rid, action="manual"))
    kb.button(text="✖️ Скасувати", callback_data=ReceiptAction(rid=rid, action="cancel"))
    kb.adjust(1)
    return text, kb.as_markup()


def manual_view(rid: str, rec: Recognition) -> tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ Назад", callback_data=ReceiptAction(rid=rid, action="back"))
    kb.button(text="✖️ Скасувати", callback_data=ReceiptAction(rid=rid, action="cancel"))
    kb.adjust(2)
    return (f"Введи суму для цього чека числом ({currency_name(rec.currency)}), наприклад 123.45",
            kb.as_markup())


def is_allowed(user_id: int, allowed_ids: set[int]) -> bool:
    # Тимчасово, до Google-авторизації: бот публічний, тож без списку будь-хто спалює квоту моделі.
    return user_id in allowed_ids


async def edit_receipt(bot: Bot, item: Pending, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Оновити повідомлення чека. Косметика: збій Telegram не повинен ламати сценарій."""
    with suppress(TelegramAPIError):
        await bot.edit_message_text(text=text, chat_id=item.chat_id, message_id=item.msg_id, reply_markup=markup)


async def release_manual(bot: Bot, state: FSMContext, pending: PendingStore, new_rid: str | None = None) -> None:
    """Скинути очікування ручної суми (або переключити його на new_rid). Попередньому чеку — повернути кнопки.

    Ручне введення одне на користувача: без цього сума, набрана для чека A, могла б піти в чек B.
    Стан міняється до першого мережевого await: інакше швидкий тап по іншому чеку
    встиг би записати свій rid, а цей виклик потім перезаписав би його.
    """
    rid = (await state.get_data()).get("rid")
    if new_rid is None:
        await state.clear()
    else:
        await state.set_state(ManualAmount.waiting)
        await state.set_data({"rid": new_rid})
    if not rid or rid == new_rid:
        return
    item = pending.get(rid)
    if item is not None and item.status == "pending":
        await edit_receipt(bot, item, *result_view(rid, item.recognition, item.note))


@router.message(CommandStart())
@router.message(Command("help"))
async def on_start(message: Message, bot: Bot, state: FSMContext, pending: PendingStore) -> None:
    await release_manual(bot, state, pending)
    await message.answer(HELP_TEXT)


@router.message(F.photo | F.document)
async def on_photo(message: Message, bot: Bot, state: FSMContext, recognizer: RecognizerChain,
                   pending: PendingStore, limiter: RateLimiter, quota: DailyQuota, allowed_ids: set[int]) -> None:
    user_id = message.from_user.id
    if not is_allowed(user_id, allowed_ids):
        log.info("rejected user %s (not in allowlist)", user_id)
        await message.answer(f"Бот поки в розробці. Твій Telegram ID: {user_id} — передай його адміністратору.")
        return
    await release_manual(bot, state, pending)

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
            rec = await recognizer.recognize(image)
        else:
            note = "Ліміт автоматичного розпізнавання на сьогодні вичерпано — суму можна ввести вручну."
    except NotAnImage:
        quota.give_back()  # до моделі запит не дійшов
        await status.edit_text(
            "Не вдалося відкрити зображення (пошкоджене, завелике або формат не підтримується). "
            "Надішли чек як звичайне фото, не файлом.")
        return
    except RateLimited as e:
        if not e.spent:
            quota.give_back()  # 429 на першому ж запиті: модель чек не бачила
        log.warning("rate limited, retry after %.0fs", e.retry_after)
        note = rate_limited_note(e.retry_after)
    except RecognitionError as e:
        log.warning("recognition failed: %s", e)
        note = "Не вдалося розпізнати чек. Спробуй ще раз пізніше або введи суму вручну."
    except Exception:
        log.exception("unexpected error while recognizing")
        note = "Не вдалося розпізнати чек. Спробуй ще раз пізніше або введи суму вручну."

    if rec is not None and not rec.is_receipt:
        note = "Не схоже на чек чи квитанцію про оплату."  # модель може помилитись — ручне введення лишаємо
        rec = None
    if rec is None:
        rec = Recognition(is_receipt=True)  # сум нема: лишаються тільки "ввести вручну" і "скасувати"

    item = Pending(user_id=user_id, file_id=file.file_id, recognition=rec, created=time.time(),
                   chat_id=status.chat.id, msg_id=status.message_id, note=note)
    rid = pending.add(item)
    await edit_receipt(bot, item, *result_view(rid, rec, note))


async def finalize(message: Message, item: Pending, amount: Decimal, manual: bool) -> None:
    """Викликати тільки після item.status = "processing" (ставиться до першого await у хендлері).

    Між зміною статусу і цим викликом не повинно бути нічого, що може кинути виняток,
    інакше чек назавжди лишиться в "processing".
    """
    # Наступний етап: тут перевірка доступу -> Drive -> Sheets; при збої статус повернеться в "pending".
    item.status = "done"
    note = " (введено вручну)" if manual else ""
    with suppress(TelegramAPIError):
        # Відповіддю на повідомлення саме цього чека: видно, яку суму до якого фото підтверджено.
        await message.answer(
            f"✅ Підтверджено: {fmt(amount, item.recognition.currency)}{note}\n"
            "Запис у Google Drive і Sheets додамо на наступному етапі — зараз нічого не збережено.",
            reply_parameters=ReplyParameters(message_id=item.msg_id, allow_sending_without_reply=True),
        )


@router.callback_query(ReceiptAction.filter())
async def on_action(query: CallbackQuery, callback_data: ReceiptAction, bot: Bot, state: FSMContext,
                    pending: PendingStore, allowed_ids: set[int]) -> None:
    if not is_allowed(query.from_user.id, allowed_ids):
        # Прибраний зі списку не повинен дописати свої старі чеки (з Google-етапом це вже запис у таблицю).
        with suppress(TelegramAPIError):
            await query.answer("Немає доступу.", show_alert=True)
        return
    rid = callback_data.rid
    item = pending.get(rid)
    if item is None:
        with suppress(TelegramAPIError):
            await query.answer("Чек застарів — надішли фото ще раз.", show_alert=True)
            if isinstance(query.message, Message):
                await query.message.edit_reply_markup(reply_markup=None)
        return
    if item.user_id != query.from_user.id:
        with suppress(TelegramAPIError):
            await query.answer("Це не твій чек.", show_alert=True)
        return
    if item.status != "pending":
        with suppress(TelegramAPIError):
            await query.answer("Вже обробляю…" if item.status == "processing" else "Цей чек уже оброблено.")
        return
    log.info("receipt %s: %r %d", rid, callback_data.action, callback_data.idx)  # %r: action приходить від клієнта

    if callback_data.action == "ok":
        if not 0 <= callback_data.idx < len(item.recognition.candidates):
            with suppress(TelegramAPIError):
                await query.answer("Невідома сума.", show_alert=True)
            return
        amount = item.recognition.candidates[callback_data.idx].amount
        item.status = "processing"  # одразу, до першого await: aiogram обробляє натискання паралельно
        if (await state.get_data()).get("rid") == rid:
            await state.clear()  # сума обрана кнопкою — ручне введення для цього чека вже не чекаємо
        with suppress(TelegramAPIError):
            await query.answer()
        await edit_receipt(bot, item, f"Сума: {fmt(amount, item.recognition.currency)}")
        if isinstance(query.message, Message):
            await finalize(query.message, item, amount, manual=False)
        else:
            item.status = "done"
    elif callback_data.action == "manual":
        await release_manual(bot, state, pending, new_rid=rid)
        with suppress(TelegramAPIError):
            await query.answer()
        if item.status == "pending":  # поки чекали Telegram, чек могли підтвердити кнопкою або скасувати
            await edit_receipt(bot, item, *manual_view(rid, item.recognition))
    elif callback_data.action == "back":
        if (await state.get_data()).get("rid") == rid:
            await state.clear()
        with suppress(TelegramAPIError):
            await query.answer()
        if item.status == "pending":
            await edit_receipt(bot, item, *result_view(rid, item.recognition, item.note))
    elif callback_data.action == "cancel":
        item.status = "cancelled"
        if (await state.get_data()).get("rid") == rid:
            await state.clear()
        with suppress(TelegramAPIError):
            await query.answer("Скасовано")
        await edit_receipt(bot, item, "Скасовано. Нічого не записано.")
    else:
        with suppress(TelegramAPIError):
            await query.answer()


@router.message(ManualAmount.waiting, F.text)
async def on_manual_amount(message: Message, bot: Bot, state: FSMContext, pending: PendingStore,
                           allowed_ids: set[int]) -> None:
    rid = (await state.get_data()).get("rid")
    item = pending.get(rid)
    if item is None or item.user_id != message.from_user.id or not is_allowed(message.from_user.id, allowed_ids):
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
    log.info("receipt %s: manual amount", rid)
    await edit_receipt(bot, item, f"Сума: {fmt(amount, item.recognition.currency)} (введено вручну)")
    await finalize(message, item, amount, manual=True)


@router.message()
async def on_other(message: Message) -> None:
    if message.text and parse_amount(message.text) is not None:
        await message.answer("Щоб записати суму, натисни «✏️ Ввести вручну» під потрібним чеком. "
                             "Якщо кнопок уже нема — надішли фото чека ще раз.")
        return
    await message.answer(HELP_TEXT)
