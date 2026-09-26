"""Telegram-сценарій: /login -> фото -> розпізнана сума -> підтвердження -> Drive + Sheets.

Чеки в очікуванні живуть у пам'яті: після рестарту старі кнопки чесно кажуть «чек застарів».
"""
import asyncio
import logging
import math
import secrets
import time
from collections import defaultdict, deque
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BotCommand, CallbackQuery, CopyTextButton, InlineKeyboardMarkup, Message, ReplyParameters
from aiogram.utils.formatting import Code, Text
from aiogram.utils.keyboard import InlineKeyboardBuilder

from receipt_bot.google_api import (
    GoogleError, GoogleLogin, GoogleStore, GoogleUnsure, LoginDenied, LoginExpired, ReceiptRow,
)
from receipt_bot.recognition import (
    RATE_LIMIT_WAIT_MAX, NotAnImage, RateLimited, Recognition, RecognitionError, RecognizerChain, parse_amount,
    validate_image,
)
from receipt_bot.storage import Users

log = logging.getLogger(__name__)
router = Router()
# Лише особисті чати: у групі код /login бачать усі, і будь-хто міг би ввести його своїм акаунтом.
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

MAX_FILE_BYTES = 10 * 1024 * 1024
PENDING_TTL = 24 * 3600
PHOTOS_PER_MINUTE = 20  # квитанції шлють альбомами по 9-10
LOGINS_PER_MINUTE = 3
LOGINS_GLOBAL_PER_MINUTE = 20
REPLIES_PER_MINUTE = 10
SAVES_PER_DAY = 100
IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}

HELP_TEXT = (
    "Я записую чеки команди в Google-таблицю.\n\n"
    "1. /login — увійди Google-акаунтом, який має доступ до таблиці.\n"
    "2. Надішли фото чека (можна кілька одразу) — я знайду суму і попрошу її підтвердити.\n"
    "3. Після підтвердження фото піде в Google Drive, а рядок — у таблицю.\n\n"
    "/logout — вийти."
)
MANUAL_HINT = "Натисни «✏️ Ввести вручну» нижче."
LOGIN_FIRST = "Спершу увійди через Google: /login"
BOT_COMMANDS = [
    BotCommand(command="login", description="Увійти через Google"),
    BotCommand(command="logout", description="Вийти"),
    BotCommand(command="help", description="Як це працює"),
]

try:
    KYIV = ZoneInfo("Europe/Kyiv")
except ZoneInfoNotFoundError:  # Windows без tzdata — лише в тестах
    KYIV = None


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
    sender: str = ""    # ім'я в Telegram — для таблиці
    mime: str = "image/jpeg"
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


class SaveLimit:
    """Записів на людину за добу: навіть з чужим кодом входу Drive власника не заб'ють."""

    def __init__(self, per_day: int) -> None:
        self._per_day = per_day
        self._day = date.today()
        self._used: dict[int, int] = defaultdict(int)

    def take(self, user_id: int) -> bool:
        if date.today() != self._day:
            self._day, self._used = date.today(), defaultdict(int)
        if self._used[user_id] >= self._per_day:
            return False
        self._used[user_id] += 1
        return True


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


async def access_problem(user_id: int, users: Users, google: GoogleStore) -> str | None:
    """None — можна; інакше текст для людини. Перевіряємо до виклику моделі, щоб чужі не палили квоту."""
    email = users.email(user_id)
    if email is None:
        return LOGIN_FIRST
    try:
        if await google.has_access(email):
            return None
    except GoogleError as e:
        log.warning("access check failed: %s", e)
        return "Не вдалося перевірити доступ до таблиці. Спробуй за хвилину."
    return (f"Акаунт {email} не має доступу на редагування таблиці. "
            "Попроси власника відкрити доступ або увійди іншим акаунтом: /login")


def now_local() -> datetime:
    return datetime.now(KYIV) if KYIV else datetime.now().astimezone()


def display_name(user) -> str:
    # Числовий id — бо ім'я людина пише собі сама.
    name = " ".join(filter(None, [user.first_name, user.last_name])) or "без імені"
    return f"{name} (@{user.username}, id {user.id})" if user.username else f"{name} (id {user.id})"


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


@router.message(Command("login"))
async def on_login(message: Message, users: Users, google: GoogleStore, login: GoogleLogin,
                   login_limiter: RateLimiter, login_global: RateLimiter, logins: dict[int, asyncio.Task]) -> None:
    user_id = message.from_user.id
    # Загальна стеля теж: кожен /login — запит до Google від нашого OAuth-клієнта.
    if not login_limiter.allow(user_id) or not login_global.allow(0):
        await message.answer("Забагато спроб входу. Зачекай хвилину.")
        return
    try:
        code = await login.start()
    except GoogleError as e:
        log.warning("login start failed: %s", e)
        await message.answer("Google зараз не відповідає. Спробуй /login ще раз за хвилину.")
        return
    # Скасувати старий і стати на його місце — без await між ними, інакше два /login поспіль лишать обидва.
    if old := logins.pop(user_id, None):
        old.cancel()
    task = asyncio.create_task(finish_login(message, code, users, google, login, logins))
    logins[user_id] = task
    if not await send_login_prompt(message, code):
        # Код ніхто не побачив — нема сенсу пів години опитувати Google.
        if logins.get(user_id) is task:
            logins.pop(user_id)
        task.cancel()


def login_prompt(code) -> dict:
    # Код моноширинним: на клієнтах без кнопки копіювання його копіює тап.
    text = Text(f"1. Відкрий {code.url}\n",
                "2. Введи код: ", Code(code.user_code), "\n",
                "3. Обери Google-акаунт, який має доступ до таблиці.\n\n",
                f"Код дійсний {code.expires_in // 60} хв. Я напишу, щойно вхід пройде.")
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Скопіювати код", copy_text=CopyTextButton(text=code.user_code))
    if urlparse(code.url).scheme == "https":  # іншу адресу Telegram однаково відкине
        kb.button(text="🔗 Відкрити Google", url=code.url)
    return {**text.as_kwargs(), "reply_markup": kb.as_markup()}


async def send_login_prompt(message: Message, code) -> bool:
    """Код входу з кнопками, а якщо Telegram не прийняв — голим текстом. False — не дійшов зовсім."""
    prompt = login_prompt(code)
    try:
        await message.answer(**prompt)
        return True
    except TelegramBadRequest as e:
        log.warning("login prompt rejected, sending plain text: %s", e.message)
    except TelegramAPIError as e:
        log.warning("login prompt not sent: %s", type(e).__name__)
        return False
    try:
        await message.answer(prompt["text"], parse_mode=None)
        return True
    except TelegramAPIError as e:
        log.warning("login prompt not sent: %s", type(e).__name__)
        return False


async def finish_login(message: Message, code, users: Users, google: GoogleStore, login: GoogleLogin,
                       logins: dict[int, asyncio.Task]) -> None:
    user_id = message.from_user.id
    try:
        email = await login.wait_for_email(code)
    except LoginDenied:
        return await say(message, "Вхід скасовано.")
    except LoginExpired:
        return await say(message, "Код прострочено. Надішли /login ще раз.")
    except Exception as e:  # фонове завдання: без цього людина просто не отримала б відповіді
        log.warning("login failed: %s", e if isinstance(e, GoogleError) else type(e).__name__)
        return await say(message, "Не вдалося завершити вхід. Спробуй /login ще раз.")
    finally:
        if logins.get(user_id) is asyncio.current_task():
            logins.pop(user_id, None)
    try:
        allowed = await google.has_access(email)
    except GoogleError as e:
        log.warning("access check failed: %s", e)
        return await say(message, "Не вдалося перевірити доступ до таблиці. Спробуй /login ще раз за хвилину.")
    if not allowed:  # email людей без доступу не зберігаємо
        log.info("user %s signed in without sheet access", user_id)  # без email, лише слід для скарг
        return await say(message, f"Акаунт {email} не має доступу на редагування таблиці. "
                                  "Попроси власника відкрити доступ і тоді /login ще раз.")
    for other in users.link(user_id, email):
        # Один email — один Telegram. Якщо код входу підсунули, справжній власник про це дізнається.
        with suppress(TelegramAPIError):
            await message.bot.send_message(other, f"⚠️ Акаунт {email} щойно підключили до іншого Telegram, "
                                                  "а тебе від нього відключено. Якщо це не ти — /login і скажи власнику таблиці.")
    log.info("user %s linked a Google account", user_id)
    # Ім'я акаунта — завжди: якщо код підсунули, людина побачить чужий email.
    await say(message, f"✅ Ти увійшов як {email}. Доступ до таблиці є — надсилай фото чеків.")


async def say(message: Message, text: str) -> None:
    with suppress(TelegramAPIError):  # людина могла заблокувати бота, поки логінилась
        await message.answer(text)


@router.message(Command("logout"))
async def on_logout(message: Message, users: Users, logins: dict[int, asyncio.Task]) -> None:
    if pending_login := logins.pop(message.from_user.id, None):
        pending_login.cancel()  # інакше незавершений вхід прив'язав би акаунт уже після виходу
    users.unlink(message.from_user.id)
    await message.answer("Вийшов. Щоб знову надсилати чеки — /login")


@router.message(F.photo | F.document)
async def on_photo(message: Message, bot: Bot, state: FSMContext, recognizer: RecognizerChain,
                   pending: PendingStore, limiter: RateLimiter, quota: DailyQuota,
                   users: Users, google: GoogleStore) -> None:
    user_id = message.from_user.id
    if not limiter.allow(user_id):  # до перевірки доступу: інакше незнайомці не обмежені зовсім
        if users.email(user_id):
            await message.answer("Забагато чеків за хвилину. Зачекай трохи і надішли ще раз.")
        return
    if problem := await access_problem(user_id, users, google):
        await message.answer(problem)
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

    # Відповідь цитує фото: якщо чеків кілька, видно, яка сума до якого.
    reply_to = ReplyParameters(message_id=message.message_id, allow_sending_without_reply=True)
    try:
        status = await message.answer("Розпізнаю…", reply_parameters=reply_to)
    except TelegramRetryAfter as e:  # Telegram просить почекати (флуд) — одна спроба після паузи
        await asyncio.sleep(min(e.retry_after, 30))
        try:
            status = await message.answer("Розпізнаю…", reply_parameters=reply_to)
        except TelegramAPIError:
            log.warning("can't reply to a photo: still rate limited by Telegram")
            return
    except TelegramAPIError as e:
        log.warning("can't reply to a photo: %s", type(e).__name__)
        return

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

    mime = message.document.mime_type if message.document else "image/jpeg"
    item = Pending(user_id=user_id, file_id=file.file_id, recognition=rec, created=time.time(),
                   chat_id=status.chat.id, msg_id=status.message_id, note=note,
                   sender=display_name(message.from_user), mime=mime)
    rid = pending.add(item)
    await edit_receipt(bot, item, *result_view(rid, rec, note))


class NotSaved(Exception):
    """Чек не записано; текст — для людини."""


async def reply(bot: Bot, item: Pending, text: str) -> None:
    # Відповіддю на повідомлення саме цього чека: видно, яка сума до якого фото.
    with suppress(TelegramAPIError):
        await bot.send_message(item.chat_id, text, reply_parameters=ReplyParameters(
            message_id=item.msg_id, allow_sending_without_reply=True))


async def finalize(bot: Bot, rid: str, item: Pending, amount: Decimal, manual: bool,
                   users: Users, google: GoogleStore, saves: SaveLimit) -> None:
    """Викликати одразу після item.status = "processing". Записує чек або повертає його в pending з кнопками."""
    shown = fmt(amount, item.recognition.currency) + (" (введено вручну)" if manual else "")
    try:
        link = await save(bot, rid, item, amount, manual, users, google, saves)
    except NotSaved as e:
        item.status = "pending"
        await edit_receipt(bot, item, *result_view(rid, item.recognition, item.note))
        await reply(bot, item, f"⚠️ {e} Чек не записано — можна натиснути ще раз.")
        return
    item.status = "done"
    await edit_receipt(bot, item, f"✅ Записано: {shown}")
    await reply(bot, item, f"✅ Записано в таблицю: {shown}\nФото: {link}")


async def save(bot: Bot, rid: str, item: Pending, amount: Decimal, manual: bool, users: Users,
               google: GoogleStore, saves: SaveLimit) -> str:
    """Доступ (ще раз: його могли забрати) -> фото з Telegram -> Drive -> Sheets. Повертає посилання на фото."""
    email = users.email(item.user_id)
    if email is None:
        raise NotSaved("Ти вийшов з Google-акаунта (/login).")
    try:
        if not await google.has_access(email):
            raise NotSaved(f"Акаунт {email} більше не має доступу до таблиці.")
        if not saves.take(item.user_id):
            raise NotSaved(f"Ліміт {SAVES_PER_DAY} записів на добу вичерпано.")
        photo = (await bot.download(item.file_id)).read()
        await validate_image(photo)  # у Drive — лише справжнє фото, навіть якщо розпізнавання пропустили
        now = now_local()
        rec = item.recognition
        row = ReceiptRow(receipt_id=rid, added_at=now.strftime("%Y-%m-%d %H:%M"),
                         receipt_date=rec.receipt_date.isoformat() if rec.receipt_date else "",
                         sender=item.sender, email=email, amount=float(amount), currency=rec.currency, manual=manual)
        name = f"{now:%Y-%m-%d %H-%M} {amount} {rec.currency} {rid}.{EXTENSIONS.get(item.mime, 'jpg')}"
        return await google.save_receipt(photo, item.mime, name, row)
    except NotSaved:
        raise
    except NotAnImage as e:
        raise NotSaved("Файл не відкривається як фото — не записую.") from e
    except GoogleUnsure as e:
        log.warning("receipt %s: outcome unknown: %s", rid, e)
        raise NotSaved(f"Google не відповів вчасно — не впевнений, чи чек записався. "
                       f"Перевір таблицю (ID {rid}), перш ніж натискати ще раз.") from e
    except GoogleError as e:
        log.warning("saving receipt failed: %s", e)
        raise NotSaved("Не вдалося записати в Google (Drive або таблиця).") from e
    except (TelegramAPIError, aiohttp.ClientError, TimeoutError) as e:
        log.warning("photo re-download failed: %s", type(e).__name__)  # текст містить URL з токеном
        raise NotSaved("Не вдалося отримати фото з Telegram.") from e
    except Exception as e:
        log.exception("unexpected error while saving")
        raise NotSaved("Не вдалося записати чек.") from e


@router.callback_query(ReceiptAction.filter())
async def on_action(query: CallbackQuery, callback_data: ReceiptAction, bot: Bot, state: FSMContext,
                    pending: PendingStore, users: Users, google: GoogleStore, saves: SaveLimit) -> None:
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
        await edit_receipt(bot, item, f"Сума: {fmt(amount, item.recognition.currency)} — записую…")
        await finalize(bot, rid, item, amount, False, users, google, saves)
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
                           users: Users, google: GoogleStore, saves: SaveLimit) -> None:
    rid = (await state.get_data()).get("rid")
    item = pending.get(rid)
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
    log.info("receipt %s: manual amount", rid)
    await edit_receipt(bot, item, f"Сума: {fmt(amount, item.recognition.currency)} (введено вручну) — записую…")
    await finalize(bot, rid, item, amount, True, users, google, saves)


@router.message()
async def on_other(message: Message, chatter: RateLimiter) -> None:
    if not chatter.allow(message.from_user.id):
        return  # на спам не відповідаємо: у Telegram загальний ліміт відправки
    if message.text and parse_amount(message.text) is not None:
        await message.answer("Щоб записати суму, натисни «✏️ Ввести вручну» під потрібним чеком. "
                             "Якщо кнопок уже нема — надішли фото чека ще раз.")
        return
    await message.answer(HELP_TEXT)
