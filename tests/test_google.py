"""Google side without Google: every call goes to httpx.MockTransport."""
import asyncio
import json

import httpx
import pytest

import receipt_bot.google_api as g
from receipt_bot.google_api import GoogleError, GoogleLogin, GoogleStore, LoginDenied, LoginExpired, ReceiptRow
from receipt_bot.storage import Users


@pytest.fixture
def files(tmp_path):
    client = tmp_path / "client.json"
    client.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}), encoding="utf-8")
    owner = tmp_path / "owner.json"
    owner.write_text(json.dumps({"refresh_token": "rt"}), encoding="utf-8")
    return str(client), str(owner)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_):
        return None
    monkeypatch.setattr(g.asyncio, "sleep", instant)


def store(files, handler) -> GoogleStore:
    client, owner = files
    s = GoogleStore.__new__(GoogleStore)  # без справжнього ключа service account
    s._client_id, s._client_secret, s._owner_refresh = "cid", "cs", "rt"
    s._owner_token, s._owner_expires = "", 0.0
    s._sheet_id, s._folder_id = "SHEET", "FOLDER"
    s._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    s._permissions, s._permissions_at, s._header_ok = [], 0.0, False

    async def sa_headers():
        return {"Authorization": "Bearer sa"}
    s._sa_headers = sa_headers
    return s


ROW = ReceiptRow("2026-09-25 16:00", "2026-09-24", "Denys (@zonda)", "d@gmail.com", 477.42, "UAH", False)


# --- login ---

def test_device_flow_waits_then_returns_verified_email(files):
    polls = []

    def handler(request):
        if request.url.path == "/device/code":
            return httpx.Response(200, json={"device_code": "dc", "user_code": "ABCD-EFGH",
                                             "verification_url": "https://www.google.com/device",
                                             "expires_in": 1800, "interval": 5})
        if request.url.path == "/token":
            polls.append(1)
            if len(polls) < 3:
                return httpx.Response(428, json={"error": "authorization_pending"})
            return httpx.Response(200, json={"access_token": "at"})
        if request.url.path == "/v1/userinfo":
            assert request.headers["Authorization"] == "Bearer at"
            return httpx.Response(200, json={"email": "Denys@Gmail.com", "email_verified": True})
        raise AssertionError(request.url)

    async def scenario():
        login = GoogleLogin(files[0], httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        code = await login.start()
        assert code.user_code == "ABCD-EFGH"
        assert await login.wait_for_email(code) == "denys@gmail.com"

    asyncio.run(scenario())


@pytest.mark.parametrize("error, exc", [("access_denied", LoginDenied), ("expired_token", LoginExpired)])
def test_device_flow_denied_or_expired(files, error, exc):
    async def scenario():
        login = GoogleLogin(files[0], httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(428, json={"error": error}))))
        with pytest.raises(exc):
            await login.wait_for_email(g.DeviceCode("dc", "X", "u", 1800, 5))

    asyncio.run(scenario())


def test_unverified_email_is_rejected(files):
    def handler(request):
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "at"})
        return httpx.Response(200, json={"email": "x@gmail.com", "email_verified": False})

    async def scenario():
        login = GoogleLogin(files[0], httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        with pytest.raises(GoogleError):
            await login.wait_for_email(g.DeviceCode("dc", "X", "u", 1800, 5))

    asyncio.run(scenario())


# --- access ---

PERMS = {"permissions": [
    {"role": "owner", "type": "user", "emailAddress": "owner@gmail.com"},
    {"role": "writer", "type": "user", "emailAddress": "By@Trustee.io"},
    {"role": "reader", "type": "user", "emailAddress": "reader@gmail.com"},
    {"role": "writer", "type": "domain", "domain": "team.ua"},
]}


@pytest.mark.parametrize("email, ok", [
    ("owner@gmail.com", True), ("by@trustee.io", True), ("reader@gmail.com", False),
    ("anyone@team.ua", True), ("stranger@gmail.com", False),
])
def test_access_needs_edit_rights(files, email, ok):
    async def scenario():
        s = store(files, lambda r: httpx.Response(200, json=PERMS))
        assert await s.has_access(email) is ok

    asyncio.run(scenario())


def test_permissions_are_cached_for_an_album(files):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=PERMS)

    async def scenario():
        s = store(files, handler)
        for _ in range(10):
            await s.has_access("by@trustee.io")
        assert len(calls) == 1

    asyncio.run(scenario())


def test_anyone_with_link_can_edit_means_access(files):
    async def scenario():
        s = store(files, lambda r: httpx.Response(200, json={"permissions": [{"role": "writer", "type": "anyone"}]}))
        assert await s.has_access("whoever@gmail.com")

    asyncio.run(scenario())


# --- saving ---

def google_ok(log, sheet_fails=False, header=None):
    def handler(request):
        log.append((request.method, request.url.path))
        path = request.url.path
        if path == "/token":
            return httpx.Response(200, json={"access_token": "owner", "expires_in": 3600})
        if path == "/upload/drive/v3/files":
            assert request.headers["Authorization"] == "Bearer owner"
            body = request.read()
            assert b'"parents": ["FOLDER"]' in body and b"JPEGDATA" in body
            return httpx.Response(200, json={"id": "F1", "webViewLink": "https://drive/F1"})
        if path.endswith("/values/A1:H1") and request.method == "GET":
            return httpx.Response(200, json={"values": header} if header else {})
        if path.endswith("/values/A1:H1") and request.method == "PUT":
            return httpx.Response(200, json={})
        if path.endswith("/values/A1:append"):
            assert request.headers["Authorization"] == "Bearer sa"
            assert request.url.params["valueInputOption"] == "RAW"
            if sheet_fails:
                return httpx.Response(503, json={"error": {"status": "UNAVAILABLE"}})
            assert json.loads(request.read())["values"][0][6] == "https://drive/F1"
            return httpx.Response(200, json={})
        if request.method == "DELETE" and path == "/drive/v3/files/F1":
            return httpx.Response(204)
        raise AssertionError((request.method, path))
    return handler


def test_receipt_goes_to_drive_then_sheets(files):
    log = []

    async def scenario():
        s = store(files, google_ok(log))
        assert await s.save_receipt(b"JPEGDATA", "image/jpeg", "r.jpg", ROW) == "https://drive/F1"

    asyncio.run(scenario())
    order = [p for _, p in log]
    assert order.index("/upload/drive/v3/files") < order.index("/v4/spreadsheets/SHEET/values/A1:append")
    assert ("PUT", "/v4/spreadsheets/SHEET/values/A1:H1") in log  # порожня таблиця -> заголовок


def test_header_is_not_rewritten(files):
    log = []

    async def scenario():
        await store(files, google_ok(log, header=[["Додано"]])).save_receipt(b"JPEGDATA", "image/jpeg", "r.jpg", ROW)

    asyncio.run(scenario())
    assert ("PUT", "/v4/spreadsheets/SHEET/values/A1:H1") not in log


def test_sheet_failure_removes_the_photo(files):
    log = []

    async def scenario():
        s = store(files, google_ok(log, sheet_fails=True))
        with pytest.raises(GoogleError):
            await s.save_receipt(b"JPEGDATA", "image/jpeg", "r.jpg", ROW)

    asyncio.run(scenario())
    assert ("DELETE", "/drive/v3/files/F1") in log  # жодного фото без рядка


def test_drive_failure_writes_nothing(files):
    log = []

    def handler(request):
        log.append(request.url.path)
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "owner", "expires_in": 3600})
        return httpx.Response(500, json={"error": {"status": "INTERNAL"}})

    async def scenario():
        with pytest.raises(GoogleError):
            await store(files, handler).save_receipt(b"JPEGDATA", "image/jpeg", "r.jpg", ROW)

    asyncio.run(scenario())
    assert not any("spreadsheets" in p for p in log)


def test_formula_like_name_stays_text():
    row = ReceiptRow("t", "", '=IMPORTXML("http://evil")', "e@x", 1.0, "UAH", True)
    assert row.cells("link")[2] == '=IMPORTXML("http://evil")'  # а RAW у запиті не дає Sheets її виконати


# --- storage ---

def test_users_link_relink_unlink(tmp_path):
    users = Users(str(tmp_path / "sub" / "bot.db"))
    assert users.email(1) is None
    users.link(1, "a@gmail.com")
    users.link(1, "b@gmail.com")
    assert users.email(1) == "b@gmail.com"
    users.unlink(1)
    assert users.email(1) is None
    users.close()
