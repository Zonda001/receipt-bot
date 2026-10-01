"""Duplicates and the confidence column (Vadym 30.09: the same receipt got written twice; "confidence/scoring")."""
import asyncio
import io
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

from PIL import Image, ImageDraw

from receipt_bot import handlers
from receipt_bot.google_api import HEADER, GoogleUnsure, ReceiptRow
from receipt_bot.handlers import Pending, PendingStore, ReceiptAction, confidence, duplicate_of
from receipt_bot.recognition import (
    Candidate, Recognition, _Candidate, _ModelAnswer, normalize, photo_print, same_photo,
)

NOW = datetime(2026, 10, 1, 12, 0)
AED = Recognition(is_receipt=True, candidates=[Candidate("TOTAL", Decimal("126.00"))], has_total=True,
                  currency="AED", receipt_date=date(2026, 9, 30))


def receipt_jpeg(lines: list[str], size=(600, 1400), quality=90) -> bytes:
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        draw.text((40, 40 + i * 60), line, fill="black", font_size=36)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def resent(jpeg: bytes, side: int, quality: int) -> bytes:
    """What Telegram does to a photo someone sends again: smaller and recompressed."""
    img = Image.open(io.BytesIO(jpeg))
    img.thumbnail((side, side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def row(added="2026-09-30 21:04", day="2026-09-30", amount=126.0, currency="AED", rid="old1", print_=""):
    return ["%s" % added, day, "Андрій (@andrii, id 7)", "a@x", amount, currency, "link", "", rid, 100, "висока", print_]


# --- confidence ---

def test_confidence_is_high_when_every_check_passed():
    assert confidence(AED, 0, False) == (100, "висока")


def test_confidence_names_what_to_check():
    country = Recognition(is_receipt=True, candidates=AED.candidates, has_total=True, currency="AED",
                          currency_source="country")
    assert confidence(country, 0, False) == (70, "середня: валюта за країною, нема дати")
    assert confidence(AED, 1, False) == (70, "середня: обрано іншу суму")
    unsure = Recognition(is_receipt=True, candidates=AED.candidates, currency_source="unknown")
    assert confidence(unsure, 0, False) == (30, "низька: модель не певна підсумку, валюту не видно, нема дати")


def test_a_typed_amount_has_no_score():
    assert confidence(AED, 0, True) == (None, "вручну")
    cells = ReceiptRow("id", "t", "", "s", "e", 1.0, "UAH", True, None, "вручну", "ab").cells("link")
    assert len(cells) == len(HEADER) and cells[9:] == ["", "вручну", "ab"]


def test_normalize_says_where_the_currency_came_from():
    def norm(currency, country=None):
        return normalize(_ModelAnswer(is_receipt=True, total=126, currency=currency, date=None, country=country,
                                      candidates=[_Candidate(label="TOTAL", amount=126)])).currency_source
    assert norm("AED", "AE") == "model"
    assert norm("USD", "AE") == "country"  # Gemma reads the dirham sign as $
    assert norm(None) == "unknown" and norm("$$") == "unknown"


# --- photo print ---

def test_the_same_photo_sent_again_matches():
    original = receipt_jpeg(["CARREFOUR DUBAI", "WATER 2.00", "TOTAL 126.00 AED", "30/09/2026"])
    assert same_photo(photo_print(original), photo_print(resent(original, 1280, 87)))
    assert same_photo(photo_print(original), photo_print(resent(original, 320, 50)))


def test_another_receipt_does_not_match():
    a = receipt_jpeg(["CARREFOUR DUBAI", "WATER 2.00", "TOTAL 126.00 AED", "30/09/2026"])
    b = receipt_jpeg(["АТБ МАРКЕТ", "ХЛІБ 32.50", "СУМА 477.42 ГРН", "", "", "ДЯКУЄМО", "24.09.2026"])
    assert not same_photo(photo_print(a), photo_print(b))


def test_junk_in_the_print_cell_is_not_a_match():
    p = photo_print(receipt_jpeg(["TOTAL 1"]))
    assert not same_photo(p, "") and not same_photo(p, "zz" * 32) and not same_photo(p, "=A1")


# --- which row is a duplicate ---

def test_same_amount_currency_and_date_is_similar():
    assert duplicate_of([row()], Decimal("126.00"), AED, "", NOW) == ("similar", row())


def test_another_date_or_currency_or_amount_is_not():
    assert duplicate_of([row(day="2026-09-29")], Decimal("126.00"), AED, "", NOW) is None
    assert duplicate_of([row(currency="USD")], Decimal("126.00"), AED, "", NOW) is None
    assert duplicate_of([row(amount=126.5)], Decimal("126.00"), AED, "", NOW) is None


def test_without_a_date_only_recent_rows_count():
    undated = Recognition(is_receipt=True, currency="AED")
    assert duplicate_of([row(day="")], Decimal("126.00"), undated, "", NOW)[0] == "similar"
    assert duplicate_of([row(added="2026-09-20 10:00", day="")], Decimal("126.00"), undated, "", NOW) is None
    assert duplicate_of([row(added="2026-09-30 21:04")], Decimal("126.00"), undated, "", NOW)[0] == "similar"


def test_the_same_photo_wins_even_with_another_amount():
    p = photo_print(receipt_jpeg(["TOTAL 126.00 AED"]))
    rows = [row(rid="similar"), row(amount=5.0, day="2001-01-01", rid="photo", print_=p)]
    kind, found = duplicate_of(rows, Decimal("126.00"), AED, p, NOW)
    assert kind == "photo" and found[8] == "photo"


def test_old_short_and_broken_rows_are_skipped():
    rows = [[], ["2026-09-30 21:04"], row()[:9], "junk", [None, None, None, None, "abc", "AED"]]
    assert duplicate_of(rows, Decimal("126.00"), AED, "ff" * 32, NOW) == ("similar", row()[:9])


# --- the flow ---

def flow(rows_or_error):
    """One pending receipt; tap "ok" -> (edits, saved)."""
    edits, saved = [], []

    async def rows():
        if isinstance(rows_or_error, Exception):
            raise rows_or_error
        return rows_or_error

    async def edit(bot, item, text, markup=None):
        edits.append((text, markup))

    async def fake_finalize(bot, rid, item, amount, manual, *rest):
        saved.append((amount, manual))

    async def scenario():
        mp_edit, mp_final = handlers.edit_receipt, handlers.finalize
        handlers.edit_receipt, handlers.finalize = edit, fake_finalize
        try:
            pending = PendingStore()
            rid = pending.add(Pending(user_id=1, file_id="f", recognition=AED, created=float("inf"), chat_id=1, msg_id=1))
            google = SimpleNamespace(rows=rows)
            item = pending.get(rid)
            item.status = "processing"
            await handlers.confirm(None, rid, item, Decimal("126.00"), False, None, google, None)
            return item, rid
        finally:
            handlers.edit_receipt, handlers.finalize = mp_edit, mp_final

    item, rid = asyncio.run(scenario())
    return item, rid, edits, saved


def test_a_duplicate_waits_for_the_person():
    item, rid, edits, saved = flow([row()])
    assert saved == [] and item.status == "pending" and item.chosen == (Decimal("126.00"), False)
    text, markup = edits[-1]
    assert "Схожий чек уже є в таблиці: 126.00 AED, чек від 2026-09-30" in text and "Андрій" in text and "@andrii" not in text and "old1" in text
    actions = [ReceiptAction.unpack(b.callback_data).action for r in markup.inline_keyboard for b in r]
    assert actions == ["force", "back", "cancel"]


def test_no_duplicate_saves_right_away():
    _, _, edits, saved = flow([row(day="2026-09-29")])
    assert saved == [(Decimal("126.00"), False)] and edits == []


def test_a_failed_lookup_does_not_block_saving():
    _, _, _, saved = flow(GoogleUnsure("sheet rows: ReadTimeout"))
    assert saved == [(Decimal("126.00"), False)]


def test_save_anyway_saves_what_was_chosen(monkeypatch):
    saved = []

    async def fake_finalize(bot, rid, item, amount, manual, *rest):
        saved.append((amount, manual, item.status))

    async def no_edit(*args, **kwargs):
        pass

    async def answer(*args, **kwargs):
        pass

    monkeypatch.setattr(handlers, "finalize", fake_finalize)
    monkeypatch.setattr(handlers, "edit_receipt", no_edit)
    pending = PendingStore()
    rid = pending.add(Pending(user_id=1, file_id="f", recognition=AED, created=float("inf"), chat_id=1, msg_id=1))
    query = SimpleNamespace(from_user=SimpleNamespace(id=1), answer=answer)
    force = ReceiptAction(rid=rid, action="force")

    asyncio.run(handlers.on_action(query, force, None, None, pending, None, None, None))
    assert saved == []  # no warning was shown: nothing to force

    pending.get(rid).chosen = (Decimal("50.00"), True)
    asyncio.run(handlers.on_action(query, force, None, None, pending, None, None, None))
    assert saved == [(Decimal("50.00"), True, "processing")]
