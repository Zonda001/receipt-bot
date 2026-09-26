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
    s = GoogleStore.__new__(GoogleStore)  # without a real service account key
    s._client_id, s._client_secret, s._owner_refresh = "cid", "cs", "rt"
    s._owner_token, s._owner_expires = "", 0.0
    s._sheet_id, s._folder_id = "SHEET", "FOLDER"
    s._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    s._permissions, s._permissions_at, s._header_ok = [], float("-inf"), False

    async def sa_headers():
        return {"Authorization": "Bearer sa"}
    s._sa_headers = sa_headers
    return s


ROW = ReceiptRow("rid1", "2026-09-25 16:00", "2026-09-24", "Denys (@zonda)", "d@gmail.com", 477.42, "UAH", False)


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
    ("anyone@team.ua", False),  # domain-wide access doesn't count: a personal account can use a company address
    ("stranger@gmail.com", False),
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


def test_anyone_with_link_does_not_count(files):
    # the bot is public: "anyone with the link" would let in any Google account
    async def scenario():
        s = store(files, lambda r: httpx.Response(200, json={"permissions": [{"role": "writer", "type": "anyone"}]}))
        assert not await s.has_access("whoever@gmail.com")

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
        if path.endswith("/values/A1:I1") and request.method == "GET":
            return httpx.Response(200, json={"values": header} if header else {})
        if path.endswith("/values/A1:I1") and request.method == "PUT":
            return httpx.Response(200, json={})
        if path.endswith("/values/A1:append"):
            assert request.headers["Authorization"] == "Bearer sa"
            assert request.url.params["valueInputOption"] == "RAW"
            if sheet_fails:
                return httpx.Response(503, json={"error": {"status": "UNAVAILABLE"}})
            cells = json.loads(request.read())["values"][0]
            assert cells[6] == "https://drive/F1" and cells[8] == "rid1"
            return httpx.Response(200, json={})
        if path.endswith("/values/I:I"):
            return httpx.Response(200, json={"values": [["ID"]]})  # 503 on append -> look for the row, it isn't there
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
    assert ("PUT", "/v4/spreadsheets/SHEET/values/A1:I1") in log  # empty sheet -> header


def test_header_is_not_rewritten(files):
    log = []

    async def scenario():
        await store(files, google_ok(log, header=[["Додано"]])).save_receipt(b"JPEGDATA", "image/jpeg", "r.jpg", ROW)

    asyncio.run(scenario())
    assert ("PUT", "/v4/spreadsheets/SHEET/values/A1:I1") not in log


def test_sheet_failure_removes_the_photo(files):
    log = []

    async def scenario():
        s = store(files, google_ok(log, sheet_fails=True))
        with pytest.raises(GoogleError):
            await s.save_receipt(b"JPEGDATA", "image/jpeg", "r.jpg", ROW)

    asyncio.run(scenario())
    assert ("DELETE", "/drive/v3/files/F1") in log  # no photo without a row


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
    row = ReceiptRow("id", "t", "", '=IMPORTXML("http://evil")', "e@x", 1.0, "UAH", True)
    assert row.cells("link")[2] == '=IMPORTXML("http://evil")'  # and RAW in the request keeps Sheets from running it


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


# --- review findings f93b051 ---

def unsure_append(log, row_there=None, lookup_fails=False):
    def handler(request):
        log.append((request.method, request.url.path))
        path = request.url.path
        if path == "/token":
            return httpx.Response(200, json={"access_token": "owner", "expires_in": 3600})
        if path == "/upload/drive/v3/files":
            return httpx.Response(200, json={"id": "F1", "webViewLink": "https://drive/F1"})
        if path.endswith("/values/A1:I1"):
            return httpx.Response(200, json={"values": [["Додано"]]})
        if path.endswith("/values/A1:append"):
            raise httpx.ReadTimeout("slow")  # Google may have saved it, but the reply got lost
        if path.endswith("/values/I:I"):
            if lookup_fails:
                return httpx.Response(503, json={"error": {"status": "UNAVAILABLE"}})
            return httpx.Response(200, json={"values": [["ID"], ["rid1"]] if row_there else [["ID"]]})
        if request.method == "DELETE":
            return httpx.Response(204)
        raise AssertionError((request.method, path))
    return handler


def test_timeout_but_row_saved_is_success(files):
    log = []

    async def scenario():
        assert await store(files, unsure_append(log, row_there=True)).save_receipt(
            b"JPEGDATA", "image/jpeg", "r.jpg", ROW) == "https://drive/F1"

    asyncio.run(scenario())
    assert not any(m == "DELETE" for m, _ in log)  # the photo stays: the row points to it


def test_timeout_and_no_row_removes_photo(files):
    log = []

    async def scenario():
        with pytest.raises(GoogleError):
            await store(files, unsure_append(log, row_there=False)).save_receipt(b"JPEGDATA", "image/jpeg", "r.jpg", ROW)

    asyncio.run(scenario())
    assert ("DELETE", "/drive/v3/files/F1") in log


def test_timeout_and_unknown_keeps_photo(files):
    log = []

    async def scenario():
        with pytest.raises(g.GoogleUnsure):
            await store(files, unsure_append(log, lookup_fails=True)).save_receipt(b"JPEGDATA", "image/jpeg", "r.jpg", ROW)

    asyncio.run(scenario())
    assert not any(m == "DELETE" for m, _ in log)  # we don't know whether the row exists, so the photo stays


def test_non_json_reply_still_cleans_up(files):
    log = []

    def handler(request):
        log.append((request.method, request.url.path))
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "owner", "expires_in": 3600})
        if request.url.path == "/upload/drive/v3/files":
            return httpx.Response(200, json={"id": "F1", "webViewLink": "https://drive/F1"})
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, text="<html>proxy error</html>")

    async def scenario():
        with pytest.raises(GoogleError):
            await store(files, handler).save_receipt(b"JPEGDATA", "image/jpeg", "r.jpg", ROW)

    asyncio.run(scenario())
    assert ("DELETE", "/drive/v3/files/F1") in log


def test_first_check_right_after_boot_asks_google(files, monkeypatch):
    monkeypatch.setattr(g.time, "monotonic", lambda: 5.0)  # the VM has just booted

    async def scenario():
        s = store(files, lambda r: httpx.Response(200, json=PERMS))
        assert await s.has_access("by@trustee.io")

    asyncio.run(scenario())


def test_google_error_text_has_no_message_details():
    async def scenario():
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(
            404, json={"error": {"status": "NOT_FOUND", "message": "File not found: SECRET_SHEET_ID"}})))
        with pytest.raises(GoogleError) as err:
            await g._call(http, "permissions.list", "GET", "https://x")
        assert "SECRET_SHEET_ID" not in str(err.value) and "NOT_FOUND" in str(err.value)

    asyncio.run(scenario())


def test_device_polling_survives_a_5xx(files):
    answers = iter([httpx.Response(503), httpx.Response(200, json={"access_token": "at"})])

    def handler(request):
        if request.url.path == "/token":
            return next(answers)
        return httpx.Response(200, json={"email": "d@gmail.com", "email_verified": True})

    async def scenario():
        login = GoogleLogin(files[0], httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        assert await login.wait_for_email(g.DeviceCode("dc", "X", "u", 1800, 5)) == "d@gmail.com"

    asyncio.run(scenario())


# --- whole-repo review 91fe2ea ---

def test_one_email_one_telegram(tmp_path):
    users = Users(str(tmp_path / "bot.db"))
    assert users.link(1, "a@gmail.com") == []
    assert users.link(2, "a@gmail.com") == [1]  # the first account is unlinked, and it will be told
    assert users.email(1) is None and users.email(2) == "a@gmail.com"
    assert users.link(2, "a@gmail.com") == []  # signing in again with the same account affects nobody
    users.close()


def test_save_limit_per_user():
    from receipt_bot.handlers import SaveLimit
    limit = SaveLimit(2)
    assert limit.take(1) and limit.take(1) and not limit.take(1)
    assert limit.take(2)  # another person has their own limit


def test_sender_has_numeric_id():
    from types import SimpleNamespace
    from receipt_bot.handlers import display_name
    assert display_name(SimpleNamespace(id=42, first_name="Admin", last_name=None, username=None)) == "Admin (id 42)"
    assert "id 42" in display_name(SimpleNamespace(id=42, first_name="A", last_name="B", username="ab"))


def test_non_image_is_never_uploaded():
    from receipt_bot.recognition import NotAnImage, validate_image
    with pytest.raises(NotAnImage):
        asyncio.run(validate_image(b"\x00" * 1000))


# --- owner login (python -m receipt_bot.owner_login) ---

DRIVE_SCOPE = "openid https://www.googleapis.com/auth/drive.file https://www.googleapis.com/auth/userinfo.email"


def owner_handler(scope: str = DRIVE_SCOPE, verified: bool = True, folder=None):
    def handler(request):
        path = request.url.path
        if path == "/device/code":
            assert "drive.file" in request.content.decode()
            return httpx.Response(200, json={"device_code": "dc", "user_code": "OWN-ER", "expires_in": 1800,
                                             "verification_url": "https://www.google.com/device", "interval": 5})
        if path == "/token":
            return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt-secret", "scope": scope})
        if path == "/v1/userinfo":
            return httpx.Response(200, json={"email": "Owner@Gmail.com", "email_verified": verified})
        assert request.headers["Authorization"] == "Bearer at"
        if path == "/drive/v3/files" and request.method == "POST":
            assert json.loads(request.content)["mimeType"] == "application/vnd.google-apps.folder"
            return httpx.Response(200, json={"id": "NEWFOLDER"})
        if path == "/drive/v3/files/FOLDER" and request.method == "GET":
            if folder == "down":
                return httpx.Response(503, json={"error": {"status": "UNAVAILABLE"}})
            return (httpx.Response(200, json=folder) if folder
                    else httpx.Response(404, json={"error": {"status": "NOT_FOUND"}}))
        raise AssertionError(request.url)
    return handler


def run_owner(files, tmp_path, handler, token_file=None, folder_id=""):
    from receipt_bot.owner_login import run

    shown = []
    target = str(token_file or tmp_path / "owner-token.json")

    async def scenario():
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return await run(files[0], target, http, shown.append, folder_id=folder_id)

    return asyncio.run(scenario()), target, "\n".join(shown)


def test_owner_login_creates_the_folder_and_saves_the_token_quietly(files, tmp_path):
    import os
    import sys

    email, target, shown = run_owner(files, tmp_path, owner_handler())
    assert email == "owner@gmail.com"
    assert json.loads(open(target, encoding="utf-8").read()) == {"refresh_token": "rt-secret", "scope": DRIVE_SCOPE}
    assert "OWN-ER" in shown and "DRIVE_FOLDER_ID=NEWFOLDER" in shown and "rt-secret" not in shown
    if sys.platform != "win32":
        assert oct(os.stat(target).st_mode & 0o777) == "0o600"
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".owner-token-")] == []


FOLDER = {"name": "Чеки (бот)", "mimeType": "application/vnd.google-apps.folder", "trashed": False, "ownedByMe": True}


def test_owner_login_checks_an_existing_folder(files, tmp_path):
    _, target, shown = run_owner(files, tmp_path, owner_handler(folder=FOLDER), folder_id="FOLDER")
    assert "Чеки (бот)" in shown and "NEWFOLDER" not in shown
    assert shown.index("owner@gmail.com") < shown.index("Чеки (бот)")  # who signed in is shown before the folder check
    assert json.loads(open(target, encoding="utf-8").read())["refresh_token"] == "rt-secret"


@pytest.mark.parametrize("handler_kwargs, folder_id", [
    ({"scope": "openid email"}, ""),  # the Drive checkbox was unticked on the consent screen
    ({"verified": False}, ""),
    ({}, "FOLDER"),  # wrong account: the folder isn't visible
    ({"folder": {**FOLDER, "trashed": True}}, "FOLDER"),
    ({"folder": {**FOLDER, "ownedByMe": False}}, "FOLDER"),  # the team sees it, but this account isn't the owner
    ({"folder": "down"}, "FOLDER"),
])
def test_owner_login_keeps_the_old_token_on_failure(files, tmp_path, handler_kwargs, folder_id):
    old = tmp_path / "owner-token.json"
    old.write_text('{"refresh_token": "old"}', encoding="utf-8")
    with pytest.raises(GoogleError):
        run_owner(files, tmp_path, owner_handler(**handler_kwargs), token_file=old, folder_id=folder_id)
    assert json.loads(old.read_text(encoding="utf-8")) == {"refresh_token": "old"}


def test_owner_login_does_not_blame_the_folder_for_a_google_outage(files, tmp_path):
    with pytest.raises(g.GoogleUnsure) as e:
        run_owner(files, tmp_path, owner_handler(folder="down"), folder_id="FOLDER")
    assert "clear DRIVE_FOLDER_ID" not in str(e.value)
