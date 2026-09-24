import io
from datetime import date, timedelta
from decimal import Decimal

import pytest
from PIL import Image

from receipt_bot.recognition import (
    MAX_SIDE, NotAnImage, _Candidate, _ModelAnswer, normalize, parse_amount, prepare_image, to_amount,
)


def answer(total=None, candidates=(), is_receipt=True, currency="UAH", date_=None):
    return _ModelAnswer(is_receipt=is_receipt, total=total, currency=currency, date=date_,
                        candidates=[_Candidate(label=l, amount=a) for l, a in candidates])


@pytest.mark.parametrize("text, expected", [
    ("123.45", Decimal("123.45")),
    ("123,45", Decimal("123.45")),
    ("1 234,50 грн", Decimal("1234.50")),
    ("347", Decimal("347.00")),
    ("  99.9 ", Decimal("99.90")),
])
def test_parse_amount_ok(text, expected):
    assert parse_amount(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "0", "-5", "12.345", "1e5", "nan", "inf", "1,2,3", "100000000"])
def test_parse_amount_rejects(text):
    assert parse_amount(text) is None


def test_to_amount_rounds_float_noise():
    assert to_amount(347.49999999) == Decimal("347.50")
    assert to_amount(0) is None and to_amount(-1) is None and to_amount(None) is None


def test_not_receipt_drops_everything():
    rec = normalize(answer(total=100, candidates=[("СУМА", 100)], is_receipt=False))
    assert not rec.is_receipt and rec.candidates == [] and rec.total is None


def test_total_first_and_labelled_from_candidates():
    rec = normalize(answer(total=347.5, candidates=[("СУМА", 362.7), ("ДО СПЛАТИ", 347.5)]))
    assert rec.total == Decimal("347.50")
    assert rec.candidates[0].label == "ДО СПЛАТИ"
    assert [c.amount for c in rec.candidates] == [Decimal("347.50"), Decimal("362.70")]


def test_total_missing_from_candidates_is_still_first():
    rec = normalize(answer(total=50, candidates=[("СУМА", 60)]))
    assert rec.total == Decimal("50.00") and len(rec.candidates) == 2


def test_vat_change_cash_are_never_candidates():
    rec = normalize(answer(total=347.5, candidates=[("ПДВ 20%", 57.92), ("РЕШТА", 52.5), ("ГОТІВКА", 400)]))
    assert [c.amount for c in rec.candidates] == [Decimal("347.50")]


def test_no_total_keeps_candidates_for_choice():
    rec = normalize(answer(total=None, candidates=[("СУМА", 10), ("РАЗОМ", 12)]))
    assert rec.is_receipt and not rec.has_total and rec.total is None and len(rec.candidates) == 2


def test_nonsense_amounts_and_currency():
    rec = normalize(answer(total=-5, candidates=[("X", 0), ("Y", 1e9)], currency="гривня"))
    assert rec.candidates == [] and rec.currency == "UAH"


def test_date_parsing():
    assert normalize(answer(date_="2026-09-24")).receipt_date == date(2026, 9, 24)
    future = (date.today() + timedelta(days=30)).isoformat()
    assert normalize(answer(date_=future)).receipt_date is None
    assert normalize(answer(date_="24.09.2026")).receipt_date is None


def test_prepare_image_downscales_and_converts():
    buf = io.BytesIO()
    Image.new("RGBA", (4000, 3000), (255, 0, 0, 128)).save(buf, "PNG")
    out = Image.open(io.BytesIO(prepare_image(buf.getvalue())))
    assert out.format == "JPEG" and max(out.size) == MAX_SIDE


def test_prepare_image_rejects_garbage():
    with pytest.raises(NotAnImage):
        prepare_image(b"definitely not an image")
