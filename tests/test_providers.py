"""Комісія банку, мітки інших мов, черга до провайдера і запасний провайдер."""
import asyncio
import io
from decimal import Decimal

import httpx
import pytest
from PIL import Image

import receipt_bot.recognition as recognition
from receipt_bot.handlers import rate_limited_note
from receipt_bot.recognition import (
    MAX_QUEUE, RESPONSE_SCHEMA, NotAnImage, RateLimited, RecognitionError, Recognizer, RecognizerChain,
    _Candidate, _ModelAnswer, is_fee, is_not_total, normalize,
)


def answer(total=None, candidates=()):
    return _ModelAnswer(is_receipt=True, total=total, currency="UAH", date=None,
                        candidates=[_Candidate(label=l, amount=a) for l, a in candidates])


def jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (60, 40), "white").save(buf, "JPEG")
    return buf.getvalue()


# --- комісія ---

def test_bank_receipt_offers_sum_and_sum_with_fee_but_not_bare_fee():
    rec = normalize(answer(total=477.42, candidates=[("Сума", 477.42), ("Комісія", 5.00)]))
    assert rec.has_total and rec.total == Decimal("477.42")
    assert [(c.label, c.amount) for c in rec.candidates] == [
        ("Сума", Decimal("477.42")), ("Сума + комісія", Decimal("482.42"))]


def test_zero_fee_adds_no_extra_button():
    rec = normalize(answer(total=1147.67, candidates=[("Сума", 1147.67), ("Комісія", 0.0)]))
    assert [c.amount for c in rec.candidates] == [Decimal("1147.67")]


def test_fee_reported_as_total_is_not_preselected():
    rec = normalize(answer(total=5.00, candidates=[("Сума", 311.00), ("Комісія", 5.00)]))
    assert not rec.has_total and [c.amount for c in rec.candidates] == [Decimal("311.00")]


@pytest.mark.parametrize("label, expected", [
    ("Комісія", True), ("Сума комісійної винагороди", True), ("Fee", True), ("Service fees", True),
    ("Prowizja", True), ("Сума з комісією", False), ("Сума", False), ("Coffee", False), ("Feedback", False),
    ("Сума без комісії", False), ("До сплати (вкл. комісію)", False), ("incl. service fee", False),
    ("TOTAL with fee", False),
])
def test_fee_labels(label, expected):
    assert is_fee(label) is expected


# --- інші мови ---

@pytest.mark.parametrize("label", ["PTU A 23%", "SUMA PTU", "RESZTA", "GOTÓWKA", "Cash", "Change"])
def test_polish_and_english_tax_cash_change_are_dropped(label):
    assert is_not_total(label)


@pytest.mark.parametrize("label", ["SUMA PLN", "DO ZAPŁATY", "TOTAL", "AMOUNT DUE", "Exchange rate", "Cashless"])
def test_polish_and_english_totals_are_kept(label):
    assert not is_not_total(label)


def test_schema_asks_for_amounts_before_the_verdict():
    # Порядок полів — не косметика: з is_receipt першим модель казала "не чек" на 19 із 24 справжніх документів.
    assert RESPONSE_SCHEMA["required"][0] == "candidates" and RESPONSE_SCHEMA["required"][-1] == "is_receipt"
    assert list(RESPONSE_SCHEMA["properties"])[-1] == "is_receipt"


# --- тексти про ліміт ---

def test_rate_limited_note_is_honest_about_the_wait():
    assert "за хвилину" in rate_limited_note(30)
    assert "19 хв" in rate_limited_note(1087)   # денний ліміт Groq: "try again in 18m6s"
    assert "6 год" in rate_limited_note(20000)


# --- черга і ліміти провайдера ---

def ok_body():
    return {"choices": [{"message": {"content": answer(total=10, candidates=[("СУМА", 10)]).model_dump_json()}}]}


def with_transport(rec: Recognizer, handler) -> Recognizer:
    rec._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://x")
    return rec


def test_budget_wait_follows_ratelimit_headers():
    rec = Recognizer("http://x", "m", "k")
    rec._remember_budget(httpx.Headers({"x-ratelimit-remaining-tokens": "1000", "x-ratelimit-limit-tokens": "8000"}))
    # бракує 1600 токенів, квота відновлюється по 8000/60 на секунду -> ~12 с
    assert 11 < rec._budget_wait() < 12.5
    rec._remember_budget(httpx.Headers({"x-ratelimit-remaining-tokens": "8000", "x-ratelimit-limit-tokens": "8000"}))
    assert rec._budget_wait() == 0


def test_long_429_blocks_provider_without_further_requests():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"retry-after": "1087"})

    async def scenario():
        rec = with_transport(Recognizer("http://x", "m", "k"), handler)
        with pytest.raises(RateLimited) as first:
            await rec.recognize_prepared(jpeg())
        assert first.value.retry_after == 1087 and not first.value.spent
        with pytest.raises(RateLimited):
            await rec.recognize_prepared(jpeg())
        assert len(calls) == 1  # другий раз провайдера навіть не питали
        assert rec.blocked_for() > 1000
        await rec.close()

    asyncio.run(scenario())


# --- запасний провайдер ---

def test_chain_uses_fallback_when_primary_is_out_of_daily_quota():
    async def scenario():
        primary = with_transport(Recognizer("http://x", "m", "k", name="primary"),
                                 lambda r: httpx.Response(429, headers={"retry-after": "1087"}))
        fallback = with_transport(Recognizer("http://x", "m", "k", name="fallback"),
                                  lambda r: httpx.Response(200, json=ok_body()))
        chain = RecognizerChain(primary, fallback)
        rec = await chain.recognize(jpeg())
        assert rec.total == Decimal("10.00") and chain.in_flight == 0
        await chain.close()

    asyncio.run(scenario())


def test_chain_without_fallback_reports_primary_limit():
    async def scenario():
        primary = with_transport(Recognizer("http://x", "m", "k"),
                                 lambda r: httpx.Response(429, headers={"retry-after": "1087"}))
        chain = RecognizerChain(primary)
        with pytest.raises(RateLimited) as err:
            await chain.recognize(jpeg())
        assert err.value.retry_after == 1087 and not err.value.spent
        await chain.close()

    asyncio.run(scenario())


def test_chain_reports_soonest_retry_when_both_are_limited():
    async def scenario():
        primary = with_transport(Recognizer("http://x", "m", "k"),
                                 lambda r: httpx.Response(429, headers={"retry-after": "1087"}))
        fallback = with_transport(Recognizer("http://x", "m", "k"),
                                  lambda r: httpx.Response(429, headers={"retry-after": "300"}))
        with pytest.raises(RateLimited) as err:
            await RecognizerChain(primary, fallback).recognize(jpeg())
        assert err.value.retry_after == 300

    asyncio.run(scenario())


def test_chain_prefers_real_error_over_rate_limit():
    async def scenario():
        primary = with_transport(Recognizer("http://x", "m", "k"),
                                 lambda r: httpx.Response(429, headers={"retry-after": "1087"}))
        fallback = with_transport(Recognizer("http://x", "m", "k"), lambda r: httpx.Response(400, text="bad request"))
        with pytest.raises(RecognitionError) as err:
            await RecognizerChain(primary, fallback).recognize(jpeg())
        assert not isinstance(err.value, RateLimited)  # запасний відповів помилкою: "спробуй пізніше" було б неправдою

    asyncio.run(scenario())


def test_chain_does_not_send_broken_images_anywhere():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=ok_body())

    async def scenario():
        chain = RecognizerChain(with_transport(Recognizer("http://x", "m", "k"), handler),
                                with_transport(Recognizer("http://x", "m", "k"), handler))
        with pytest.raises(NotAnImage):
            await chain.recognize(b"not an image")
        assert not calls and chain.in_flight == 0

    asyncio.run(scenario())


def test_fallback_extra_body_is_sent():
    seen = []

    def handler(request):
        seen.append(request.read())
        return httpx.Response(200, json=ok_body())

    async def scenario():
        rec = with_transport(Recognizer("http://x", "m", "k",
                                        extra_body={"chat_template_kwargs": {"enable_thinking": False}}), handler)
        await rec.recognize_prepared(jpeg())
        assert b'"enable_thinking":false' in seen[0].replace(b" ", b"")

    asyncio.run(scenario())


# --- знахідки ревю 7714ecc ---

@pytest.mark.parametrize("rows, total, expected", [
    # у "Разом до сплати" комісія вже є
    ([("Сума платежу", 500), ("Комісія", 10), ("Разом до сплати", 510)], 510, ["510.00", "500.00"]),
    ([("Сума платежу", 500), ("Комісія", 10), ("Разом до сплати", 510)], 500, ["500.00", "510.00"]),
    ([("Service fee", 50), ("TOTAL", 550)], 550, ["550.00"]),
    ([("Сума без комісії", 1000), ("Комісія", 10), ("Сума з комісією", 1010)], 1010, ["1010.00", "1000.00"]),
    ([("Сума", 100), ("Комісія", 5), ("До сплати (вкл. комісію)", 105)], 105, ["105.00", "100.00"]),
])
def test_fee_is_never_added_twice(rows, total, expected):
    rec = normalize(answer(total=total, candidates=rows))
    assert rec.has_total and [str(c.amount) for c in rec.candidates] == expected


def test_several_fees_are_summed():
    rec = normalize(answer(total=1000, candidates=[("Сума", 1000), ("Комісія", 10), ("Комісія банку", 15)]))
    assert [str(c.amount) for c in rec.candidates] == ["1000.00", "1025.00"]


@pytest.mark.parametrize("label, dropped", [("GOTOWKA", True), ("Gotówka", True), ("Bezgotówkowa", False),
                                            ("bez gotówki", False)])
def test_polish_cash_labels(label, dropped):
    assert is_not_total(label) is dropped


def test_real_error_wins_over_fallback_rate_limit():
    async def scenario():
        primary = with_transport(Recognizer("http://x", "m", "k"), lambda r: httpx.Response(400, text="bad"))
        fallback = with_transport(Recognizer("http://x", "m", "k"),
                                  lambda r: httpx.Response(429, headers={"retry-after": "1087"}))
        with pytest.raises(RecognitionError) as err:
            await RecognizerChain(primary, fallback).recognize(jpeg())
        assert not isinstance(err.value, RateLimited)  # основний живий — не кажемо "ліміт на 19 хв"

    asyncio.run(scenario())


def test_down_primary_is_skipped_for_a_while(monkeypatch):
    primary_calls = []

    def down(request):
        primary_calls.append(request)
        return httpx.Response(503)

    async def no_sleep(_):
        return None

    async def scenario():
        primary = with_transport(Recognizer("http://x", "m", "k"), down)
        fallback = with_transport(Recognizer("http://x", "m", "k"), lambda r: httpx.Response(200, json=ok_body()))
        chain = RecognizerChain(primary, fallback)
        assert (await chain.recognize(jpeg())).total == Decimal("10.00")
        calls = len(primary_calls)
        assert (await chain.recognize(jpeg())).total == Decimal("10.00")
        assert len(primary_calls) == calls  # друге фото — одразу в запасний

    monkeypatch.setattr(recognition.asyncio, "sleep", no_sleep)
    asyncio.run(scenario())


@pytest.mark.parametrize("value", [float("inf"), float("nan"), -5.0, 10 ** 9])
def test_retry_after_from_provider_is_clamped(value):
    e = RateLimited(value)
    assert 0 <= e.retry_after <= 24 * 3600
    assert rate_limited_note(e.retry_after)  # не падає


def test_queue_is_capped():
    async def scenario():
        chain = RecognizerChain(with_transport(Recognizer("http://x", "m", "k"),
                                               lambda r: httpx.Response(200, json=ok_body())))
        chain.in_flight = MAX_QUEUE
        with pytest.raises(RateLimited) as err:
            await chain.recognize(jpeg())
        assert not err.value.spent and err.value.retry_after <= 60

    asyncio.run(scenario())


def test_telegram_file_is_closed_after_reading():
    async def scenario():
        chain = RecognizerChain(with_transport(Recognizer("http://x", "m", "k"),
                                               lambda r: httpx.Response(200, json=ok_body())))
        f = io.BytesIO(jpeg())
        await chain.recognize(f)
        assert f.closed

    asyncio.run(scenario())


@pytest.mark.parametrize("raw", ['["x"]', '{"model": "other"}', '{"messages": []}', '"text"'])
def test_bad_extra_body_stops_startup(raw):
    from receipt_bot.__main__ import parse_extra_body
    with pytest.raises(SystemExit):
        parse_extra_body(raw)


def test_extra_body_ok():
    from receipt_bot.__main__ import parse_extra_body
    assert parse_extra_body('{"chat_template_kwargs": {"enable_thinking": false}}') == {
        "chat_template_kwargs": {"enable_thinking": False}}
    assert parse_extra_body("  ") is None
