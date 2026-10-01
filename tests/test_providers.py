"""Bank fees, labels in other languages, the provider queue and the fallback provider."""
import asyncio
import copy
import io
import json
import time
from datetime import date
from decimal import Decimal

import httpx
import pytest
from PIL import Image

import receipt_bot.recognition as recognition
from receipt_bot.handlers import rate_limited_note
from receipt_bot.recognition import (
    MAX_QUEUE, RESPONSE_SCHEMA, NotAnImage, RateLimited, Recognition, RecognitionError, Recognizer, RecognizerChain,
    _Candidate, _ModelAnswer, is_fee, is_not_total, normalize,
)


def answer(total=None, candidates=()):
    return _ModelAnswer(is_receipt=True, total=total, currency="UAH", date=None,
                        candidates=[_Candidate(label=l, amount=a) for l, a in candidates])


def jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (60, 40), "white").save(buf, "JPEG")
    return buf.getvalue()


# --- fee ---

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


# --- other languages ---

@pytest.mark.parametrize("label", ["PTU A 23%", "SUMA PTU", "RESZTA", "GOTÓWKA", "Cash", "Change"])
def test_polish_and_english_tax_cash_change_are_dropped(label):
    assert is_not_total(label)


@pytest.mark.parametrize("label", ["SUMA PLN", "DO ZAPŁATY", "TOTAL", "AMOUNT DUE", "Exchange rate", "Cashless"])
def test_polish_and_english_totals_are_kept(label):
    assert not is_not_total(label)


def test_schema_asks_for_amounts_before_the_verdict():
    # Field order isn't cosmetic: with is_receipt first the model said "not a receipt" on 19 of 24 real documents.
    order = RESPONSE_SCHEMA["required"]
    assert order.index("candidates") < order.index("total") and order[-1] == "is_receipt"
    assert list(RESPONSE_SCHEMA["properties"])[-1] == "is_receipt"
    # and the same trick the other way: the pen is described before the yes/no about it (01.10)
    assert order[:2] == ["pen_marks", "hand_edited"] and list(RESPONSE_SCHEMA["properties"])[:2] == order[:2]


# --- limit messages ---

def test_rate_limited_note_is_honest_about_the_wait():
    assert "за хвилину" in rate_limited_note(30)
    assert "19 хв" in rate_limited_note(1087)   # Groq daily limit: "try again in 18m6s"
    assert "6 год" in rate_limited_note(20000)


# --- queue and provider limits ---

def ok_body():
    return {"choices": [{"message": {"content": answer(total=10, candidates=[("СУМА", 10)]).model_dump_json()}}]}


def with_transport(rec: Recognizer, handler) -> Recognizer:
    rec._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://x")
    return rec


def test_budget_wait_follows_ratelimit_headers():
    rec = Recognizer("http://x", "m", "k")
    rec._remember_budget(httpx.Headers({"x-ratelimit-remaining-tokens": "1000", "x-ratelimit-limit-tokens": "8000"}))
    # 1600 tokens short, the quota refills at 8000/60 per second -> ~12 s
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
        assert len(calls) == 1  # the second time the provider wasn't even asked
        assert rec.blocked_for() > 1000
        await rec.close()

    asyncio.run(scenario())


# --- fallback provider ---

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
        assert not isinstance(err.value, RateLimited)  # the fallback answered with an error: "try later" would be a lie

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


# --- review findings 7714ecc ---

@pytest.mark.parametrize("rows, total, expected", [
    # "Разом до сплати" already includes the fee
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
        assert not isinstance(err.value, RateLimited)  # the primary is alive: don't say "limit for 19 min"

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
        assert len(primary_calls) == calls  # the second photo goes straight to the fallback

    monkeypatch.setattr(recognition.asyncio, "sleep", no_sleep)
    asyncio.run(scenario())


@pytest.mark.parametrize("value", [float("inf"), float("nan"), -5.0, 10 ** 9])
def test_retry_after_from_provider_is_clamped(value):
    e = RateLimited(value)
    assert 0 <= e.retry_after <= 24 * 3600
    assert rate_limited_note(e.retry_after)  # doesn't crash


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


# --- second look when no amount was found ---

def check_reply(content) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def network_down() -> httpx.Response:
    raise httpx.ConnectError("down")


def two_step(main: _ModelAnswer, check, calls: list):
    """The recognition request gets `main`, the document check gets `check` (JSON text or a response factory)."""
    def handler(request):
        name = json.loads(request.read())["response_format"]["json_schema"]["name"]
        calls.append(name)
        if name == "receipt":
            return check_reply(main.model_dump_json())
        return check() if callable(check) else check_reply(check)
    return handler


def recognize_with(handler):
    """-> (result, recognizer): the recognizer is closed, but its cooldown and limits can still be read."""
    async def scenario():
        rec = with_transport(Recognizer("http://x", "m", "k"), handler)
        try:
            return await rec.recognize_prepared(jpeg()), rec
        finally:
            await rec.close()

    return asyncio.run(scenario())


def no_amounts(is_receipt=True, currency="UAH", date_=None) -> _ModelAnswer:
    return _ModelAnswer(is_receipt=is_receipt, total=None, currency=currency, date=date_, candidates=[])


def test_photo_without_amounts_that_is_not_a_document_is_not_a_receipt():
    calls = []
    rec, _ = recognize_with(two_step(no_amounts(), '{"shows": "earphones", "is_payment_document": false}', calls))
    assert not rec.is_receipt and calls == ["receipt", "document_check"]


def test_not_a_document_keeps_the_date_and_currency_it_read():
    first = no_amounts(currency="PLN", date_="2026-09-24")
    rec, _ = recognize_with(two_step(first, '{"shows": "a desk", "is_payment_document": false}', []))
    assert not rec.is_receipt and rec.currency == "PLN" and rec.receipt_date == date(2026, 9, 24)


def test_unreadable_document_keeps_manual_entry_date_and_currency():
    calls = []
    first = no_amounts(currency="PLN", date_="2026-09-24")
    rec, _ = recognize_with(two_step(first, '{"shows": "a blurry receipt", "is_payment_document": true}', calls))
    assert rec.is_receipt and rec.candidates == [] and calls == ["receipt", "document_check"]
    assert rec.currency == "PLN" and rec.receipt_date == date(2026, 9, 24)


def test_cut_off_receipt_is_not_called_not_a_receipt():
    # Live 27.09: the top of a real receipt without amounts got is_receipt=false from the main request.
    calls = []
    rec, _ = recognize_with(two_step(no_amounts(is_receipt=False, currency=None),
                                     '{"shows": "top of a shop receipt", "is_payment_document": true}', calls))
    assert rec.is_receipt and rec.candidates == [] and calls == ["receipt", "document_check"]


FAILED_CHECKS = {
    "http-400": lambda: httpx.Response(400, text="bad request"),
    "http-500": lambda: httpx.Response(500),
    "http-429": lambda: httpx.Response(429),
    "network": network_down,
    "null-content": lambda: check_reply(None),
    "not-json": "not json",
    "no-verdict": '{"shows": "x"}',
}


@pytest.mark.parametrize("verdict", [True, False])
@pytest.mark.parametrize("check", FAILED_CHECKS.values(), ids=FAILED_CHECKS.keys())
def test_failed_second_look_keeps_the_first_answer_and_the_provider(check, verdict):
    calls = []
    rec, provider = recognize_with(two_step(no_amounts(is_receipt=verdict), check, calls))
    assert rec.is_receipt is verdict and rec.candidates == [] and calls == ["receipt", "document_check"]
    # an optional request: no cooldown, so the next photo still goes to this provider, not to the fallback
    assert provider._down_until == 0.0 and provider.blocked_for() == 0.0


def test_second_look_is_skipped_rather_than_waited_for():
    calls = []

    async def scenario():
        rec = with_transport(Recognizer("http://x", "m", "k"),
                             two_step(no_amounts(), '{"shows": "x", "is_payment_document": false}', calls))
        rec._remember_budget(httpx.Headers({"x-ratelimit-remaining-tokens": "0", "x-ratelimit-limit-tokens": "8000"}))
        try:
            return await rec._check_document(jpeg(), Recognition(is_receipt=True))
        finally:
            await rec.close()

    started = time.monotonic()
    result = asyncio.run(scenario())
    assert result.is_receipt and calls == [] and time.monotonic() - started < 5  # waiting would take ~20 s


def test_found_amount_needs_no_second_look():
    calls = []
    rec, _ = recognize_with(two_step(answer(total=10, candidates=[("СУМА", 10)]), "unused", calls))
    assert rec.total == Decimal("10.00") and calls == ["receipt"]


def test_second_look_leaves_the_measured_request_alone():
    rec = Recognizer("http://x", "m", "k", extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    img = jpeg()
    schema, extra = copy.deepcopy(RESPONSE_SCHEMA), copy.deepcopy(rec._extra_body)
    main = json.dumps(rec._request_body(img), sort_keys=True)
    check = rec._check_body(img)
    rec._check_body(img)
    assert json.dumps(rec._request_body(img), sort_keys=True) == main  # nothing shared was mutated
    assert RESPONSE_SCHEMA == schema and rec._extra_body == extra
    body = rec._request_body(img)
    assert body["messages"][0]["content"] == recognition.SYSTEM_PROMPT  # the measured prompt and schema
    assert body["response_format"]["json_schema"] == {"name": "receipt", "strict": True, "schema": RESPONSE_SCHEMA}
    assert check["messages"][0]["content"] == recognition.CHECK_PROMPT
    assert check["response_format"]["json_schema"]["schema"] == recognition.CHECK_SCHEMA
    assert check["chat_template_kwargs"] == {"enable_thinking": False}  # provider options still apply
    assert check["messages"][1]["content"][1] == body["messages"][1]["content"][1]  # the same photo


def test_provider_error_text_cannot_fake_journal_lines():
    # a 400 body can echo text from the photo; the error goes to the journal as one escaped line
    async def scenario():
        rec = with_transport(Recognizer("http://x", "m", "k"),
                             lambda r: httpx.Response(400, text="bad\nINFO receipt_bot: fake line"))
        try:
            with pytest.raises(RecognitionError) as err:
                await rec.recognize_prepared(jpeg())
            return str(err.value)
        finally:
            await rec.close()

    text = asyncio.run(scenario())
    assert "\n" not in text and "fake line" in text
