import io
from datetime import date, timedelta
from decimal import Decimal

import pytest
from PIL import Image

import receipt_bot.recognition as recognition
from receipt_bot.recognition import (
    MAX_SIDE, NotAnImage, _Candidate, _ModelAnswer, is_not_total, normalize, parse_amount, prepare_image, to_amount,
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


def test_vat_reported_as_total_is_not_preselected():
    rec = normalize(answer(total=57.92, candidates=[("ПДВ 20%", 57.92), ("ДО СПЛАТИ", 347.5)]))
    assert not rec.has_total and rec.total is None
    assert [c.amount for c in rec.candidates] == [Decimal("347.50")]


def test_cash_equal_to_total_keeps_total():
    rec = normalize(answer(total=400, candidates=[("ГОТІВКА", 400), ("ДО СПЛАТИ", 400)]))
    assert rec.has_total and rec.total == Decimal("400.00") and rec.candidates[0].label == "ДО СПЛАТИ"


def test_total_not_among_candidates_is_offered_last_and_not_called_total():
    rec = normalize(answer(total=50, candidates=[("СУМА", 60)]))
    assert not rec.has_total
    assert [(c.label, c.amount) for c in rec.candidates] == [("СУМА", Decimal("60.00")), ("Сума (розпізнано)", Decimal("50.00"))]


@pytest.mark.parametrize("label", ["БЕЗГОТІВКОВА", "Безготівкова оплата", "ВСЬОГО З ПДВ", "Сума з ПДВ"])
def test_card_payment_and_gross_totals_are_kept(label):
    rec = normalize(answer(total=None, candidates=[(label, 347.5)]))
    assert [c.amount for c in rec.candidates] == [Decimal("347.50")]


@pytest.mark.parametrize("label", ["ПДВ 20%", "У Т.Ч. ПДВ", "в т.ч. ПДВ А 20%", "Сума без ПДВ", "VAT 20%", "РЕШТА", "ГОТІВКА"])
def test_vat_cash_change_labels_are_dropped(label):
    assert normalize(answer(total=None, candidates=[(label, 57.92)])).candidates == []


def test_to_amount_rechecks_after_rounding():
    assert to_amount(0.004) is None
    assert to_amount(9999999.999) is None
    assert to_amount(1e30) is None


@pytest.mark.parametrize("fmt_name", ["BMP", "GIF", "TIFF"])
def test_prepare_image_rejects_unsupported_formats(fmt_name):
    buf = io.BytesIO()
    Image.new("RGB", (10, 10)).save(buf, fmt_name)
    with pytest.raises(NotAnImage):
        prepare_image(buf.getvalue())


def test_prepare_image_rejects_truncated_jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (800, 600), (200, 200, 200)).save(buf, "JPEG")
    with pytest.raises(NotAnImage):
        prepare_image(buf.getvalue()[:300])


@pytest.mark.parametrize("label", ["Сума із ПДВ", "СУМА ЗІ ПДВ", "Сума вкл. ПДВ", "Разом, включаючи ПДВ",
                                   "Всього з урахуванням ПДВ", "TOTAL INCL. VAT", "PRIVATBANK", "БЕЗ ГОТІВКИ"])
def test_gross_and_card_labels_are_not_excluded(label):
    assert not is_not_total(label)


def test_labels_are_cleaned_of_invisible_and_bidi_chars():
    rlo, zwsp, newline = chr(0x202E), chr(0x200B), chr(10)  # зміна напрямку тексту, невидимий пробіл, перенос
    dirty = rlo + "ДО" + zwsp + " СПЛАТИ" + newline + "  та ще дуже довгий хвіст мітки"
    label = normalize(answer(total=None, candidates=[(dirty, 10)])).candidates[0].label
    assert rlo not in label and zwsp not in label and newline not in label
    assert label.startswith("ДО СПЛАТИ") and len(label) <= 24


def test_candidates_are_capped():
    rec = normalize(answer(total=None, candidates=[(f"Рядок {i}", i + 1) for i in range(20)]))
    assert len(rec.candidates) == 8


def test_prepare_image_rejects_too_many_pixels_before_decoding(monkeypatch):
    monkeypatch.setattr(recognition, "MAX_PIXELS", 100)
    buf = io.BytesIO()
    Image.new("RGB", (20, 20)).save(buf, "JPEG", progressive=True)
    with pytest.raises(NotAnImage):
        prepare_image(buf.getvalue())


def test_prepare_image_rejects_truncated_content_rich_jpeg():
    from PIL import ImageDraw
    img = Image.new("RGB", (1500, 2000), "white")
    draw = ImageDraw.Draw(img)
    for y in range(0, 2000, 40):
        draw.text((20, y), "ДО СПЛАТИ 347.50 " * 8, fill="black")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    data = buf.getvalue()
    with pytest.raises(NotAnImage):
        prepare_image(data[: int(len(data) * 0.7)])


@pytest.mark.parametrize("label", ["ПДВ20%", "VAT20%", "Вкл. ПДВ 20%", "Incl. VAT 20%", "ПДВ_А"])
def test_vat_with_attached_rate_or_bare_incl_is_dropped(label):
    assert is_not_total(label)


def test_cash_keyword_past_button_width_is_still_detected():
    rec = normalize(answer(total=500, candidates=[("ДО СПЛАТИ", 347.5), ("Отримано від покупця готівкою", 500)]))
    assert not rec.has_total
    assert [c.amount for c in rec.candidates] == [Decimal("347.50")]  # внесена готівка не пропонується взагалі


def test_cap_applies_after_dropping_vat_rows():
    rows = [(f"ПДВ А 20% {i}", 10 + i) for i in range(8)] + [("ДО СПЛАТИ", 347.5)]
    rec = normalize(answer(total=347.5, candidates=rows))
    assert rec.has_total and rec.total == Decimal("347.50") and rec.candidates[0].label == "ДО СПЛАТИ"


def test_untrusted_total_survives_the_cap():
    rec = normalize(answer(total=999, candidates=[(f"Рядок {i}", i + 1) for i in range(20)]))
    assert len(rec.candidates) == 8 and rec.candidates[-1].amount == Decimal("999.00")
