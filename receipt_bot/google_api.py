"""Google side: who the user is (device flow), whether they may write (sheet permissions),
where a confirmed receipt goes (photo -> Drive as the folder owner, row -> Sheets as the service account)."""
import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import service_account

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
DEVICE_URL = "https://oauth2.googleapis.com/device/code"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
DRIVE_URL = "https://www.googleapis.com/drive/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
SHEETS_URL = "https://sheets.googleapis.com/v4/spreadsheets"

SA_SCOPES = ["https://www.googleapis.com/auth/drive.metadata.readonly",
             "https://www.googleapis.com/auth/spreadsheets"]
EDIT_ROLES = {"owner", "writer", "organizer", "fileOrganizer"}
PERMISSIONS_TTL = 60  # an album of 10 photos -> one permissions call, not ten
HEADER = ["Додано", "Дата чека", "Відправник", "Email", "Сума", "Валюта", "Фото", "Сума вручну", "ID",
          "Впевненість", "Рівень", "Відбиток фото"]
LAST_COLUMN = chr(ord("A") + len(HEADER) - 1)
HEADER_RANGE = f"A1:{LAST_COLUMN}1"
ID_COLUMN = "I:I"


class GoogleError(Exception):
    """A Google call failed. The text never contains tokens, so it is safe to log."""


class GoogleUnsure(GoogleError):
    """Timeout / network / 5xx: Google may have done it anyway."""


class GoogleUnreachable(GoogleUnsure):
    """Couldn't even connect (DNS, refused, TLS): nothing reached Google. Still "unsure" for code that just retries."""


class ServiceDown(GoogleError):
    """Drive or Sheets didn't answer the check before saving, so nothing was started."""

    def __init__(self, service: str, cause: GoogleError) -> None:
        super().__init__(f"{service} check: {cause}")
        self.service = service  # "Drive" or "Sheets"


class LoginDenied(Exception):
    pass


class LoginExpired(Exception):
    pass


@dataclass(frozen=True)
class DeviceCode:
    device_code: str
    user_code: str
    url: str
    expires_in: int
    interval: int


@dataclass(frozen=True)
class ReceiptRow:
    receipt_id: str
    added_at: str
    receipt_date: str
    sender: str
    email: str
    amount: float
    currency: str
    manual: bool
    score: int | None = None  # None: typed by a person, nothing to score
    level: str = ""
    photo_print: str = ""

    def cells(self, photo_link: str) -> list:
        return [self.added_at, self.receipt_date, self.sender, self.email, self.amount, self.currency,
                photo_link, "так" if self.manual else "", self.receipt_id,
                "" if self.score is None else self.score, self.level, self.photo_print]


def _client(client_file: str) -> tuple[str, str]:
    with open(client_file, encoding="utf-8") as f:
        installed = json.load(f)["installed"]
    return installed["client_id"], installed["client_secret"]


async def _call(http: httpx.AsyncClient, what: str, method: str, url: str, **kwargs) -> httpx.Response:
    try:
        response = await http.request(method, url, **kwargs)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
        raise GoogleUnreachable(f"{what}: {type(e).__name__}") from e  # the request never left
    except httpx.RequestError as e:
        raise GoogleUnsure(f"{what}: {type(e).__name__}") from e
    if response.status_code >= 400:
        # Only the short code ("PERMISSION_DENIED", "invalid_grant"): Google's message can carry file ids.
        try:
            error = response.json().get("error")
        except (ValueError, AttributeError):
            error = None
        reason = error.get("status", "") if isinstance(error, dict) else error if isinstance(error, str) else ""
        kind = GoogleUnsure if response.status_code >= 500 else GoogleError
        raise kind(f"{what}: HTTP {response.status_code} {reason[:40]}")
    return response


def _json(response: httpx.Response, what: str) -> dict:
    try:
        return response.json()
    except ValueError as e:
        raise GoogleError(f"{what}: not JSON") from e


class GoogleLogin:
    """Device flow with scope "openid email": we only learn the address and throw the token away."""

    def __init__(self, client_file: str, http: httpx.AsyncClient):
        self._client_id, self._client_secret = _client(client_file)
        self._http = http

    async def start(self, scope: str = "openid email") -> DeviceCode:
        r = await _call(self._http, "device code", "POST", DEVICE_URL,
                        data={"client_id": self._client_id, "scope": scope})
        d = _json(r, "device code")
        return DeviceCode(d["device_code"], d["user_code"], d.get("verification_url") or d["verification_uri"],
                          int(d.get("expires_in", 1800)), int(d.get("interval", 5)))

    async def wait_for_email(self, code: DeviceCode) -> str:
        return (await self.wait_for_identity(code))[0]

    async def wait_for_identity(self, code: DeviceCode) -> tuple[str, str]:
        """The verified address and the Google Workspace domain that manages the account ("" for a personal one)."""
        return await self.identity((await self.wait_for_tokens(code))["access_token"])

    async def wait_for_tokens(self, code: DeviceCode) -> dict:
        deadline = time.monotonic() + code.expires_in
        interval = code.interval
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            try:
                r = await self._http.post(TOKEN_URL, data={
                    "client_id": self._client_id, "client_secret": self._client_secret,
                    "device_code": code.device_code, "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
            except httpx.RequestError:
                continue  # a blip while polling is not a reason to fail the login
            if r.status_code >= 500:
                continue
            if r.status_code == 200:
                tokens = _json(r, "device token")
                if not isinstance(tokens, dict) or "access_token" not in tokens:
                    raise GoogleError("device token: no access_token")
                return tokens
            try:
                error = r.json().get("error")
            except (ValueError, AttributeError):
                error = ""
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            if error == "access_denied":
                raise LoginDenied()
            if error == "expired_token":
                raise LoginExpired()
            raise GoogleError(f"device token: HTTP {r.status_code} {error}")
        raise LoginExpired()

    async def email(self, access_token: str) -> str:
        return (await self.identity(access_token))[0]

    async def identity(self, access_token: str) -> tuple[str, str]:
        # "hd" comes only from Workspace: a personal account registered on a company address has none.
        r = await _call(self._http, "userinfo", "GET", USERINFO_URL,
                        headers={"Authorization": f"Bearer {access_token}"})
        info = _json(r, "userinfo")
        if not info.get("email") or not info.get("email_verified"):
            raise GoogleError("userinfo: no verified email")
        return info["email"].lower(), str(info.get("hd") or "").lower()


class GoogleStore:
    """Sheet access check and receipt saving. Drive upload runs as the folder owner (drive.file scope):
    a service account on a personal Gmail has no Drive quota. The row and the check run as the service account."""

    def __init__(self, sa_file: str, client_file: str, owner_token_file: str, sheet_id: str, folder_id: str,
                 http: httpx.AsyncClient):
        self._sa = service_account.Credentials.from_service_account_file(sa_file, scopes=SA_SCOPES)
        self._client_id, self._client_secret = _client(client_file)
        with open(owner_token_file, encoding="utf-8") as f:
            self._owner_refresh = json.load(f)["refresh_token"]
        self._owner_token, self._owner_expires = "", 0.0
        self._sheet_id, self._folder_id = sheet_id, folder_id
        self._http = http
        self._permissions: list[dict] = []
        self._permissions_at = float("-inf")  # monotonic() starts at boot: 0.0 would look "fresh" for a minute
        self._header_ok = False
        self._sa_lock = asyncio.Lock()
        self._permissions_lock = asyncio.Lock()

    # --- tokens ---

    async def _sa_headers(self) -> dict:
        async with self._sa_lock:
            if not self._sa.valid:
                try:
                    await asyncio.to_thread(self._sa.refresh, Request())
                except Exception as e:  # google-auth raises its own zoo of errors
                    raise GoogleError(f"service account token: {type(e).__name__}") from e
        return {"Authorization": f"Bearer {self._sa.token}"}

    async def _owner_headers(self) -> dict:
        if time.monotonic() > self._owner_expires:
            r = await _call(self._http, "owner token", "POST", TOKEN_URL, data={
                "client_id": self._client_id, "client_secret": self._client_secret,
                "refresh_token": self._owner_refresh, "grant_type": "refresh_token"})
            d = _json(r, "owner token")
            self._owner_token = d["access_token"]
            self._owner_expires = time.monotonic() + int(d.get("expires_in", 3600)) - 60
        return {"Authorization": f"Bearer {self._owner_token}"}

    # --- access ---

    async def has_access(self, email: str) -> bool:
        """Edit access shared with this very person. Not "anyone with the link" (the bot is public) and not
        a domain share (a personal account can use a company address; the company domain is let in at sign-in,
        by Google's own "hd" mark instead). Groups aren't expanded (see README)."""
        if time.monotonic() - self._permissions_at > PERMISSIONS_TTL:
            # One refresh at a time: an album used to fire ten, and a late old answer restarted the minute.
            async with self._permissions_lock:
                if time.monotonic() - self._permissions_at > PERMISSIONS_TTL:
                    self._permissions = await self._list_permissions()
                    self._permissions_at = time.monotonic()
        email = email.lower()
        return any(p.get("type") == "user" and p.get("role") in EDIT_ROLES and not p.get("deleted")
                   and p.get("emailAddress", "").lower() == email for p in self._permissions)

    async def _list_permissions(self) -> list[dict]:
        permissions, token = [], None
        while True:
            params = {"fields": "nextPageToken,permissions(role,type,emailAddress,domain,deleted)", "pageSize": 100,
                      "supportsAllDrives": "true"}
            if token:
                params["pageToken"] = token
            r = await _call(self._http, "permissions.list", "GET", f"{DRIVE_URL}/files/{self._sheet_id}/permissions",
                            params=params, headers=await self._sa_headers())
            d = _json(r, "permissions.list")
            permissions += d.get("permissions", [])
            token = d.get("nextPageToken")
            if not token:
                return permissions

    # --- saving ---

    async def ping(self) -> None:
        """Before saving: Drive (as the folder owner) and Sheets both answer, or we don't upload anything."""
        try:
            await _call(self._http, "drive check", "GET", f"{DRIVE_URL}/files/{self._folder_id}",
                        params={"fields": "id"}, headers=await self._owner_headers())
        except GoogleUnsure as e:  # unreachable, timeout or 5xx; a 4xx is a setup problem and goes up as is
            raise ServiceDown("Drive", e) from e
        try:
            await _call(self._http, "sheets check", "GET", f"{SHEETS_URL}/{self._sheet_id}",
                        params={"fields": "spreadsheetId"}, headers=await self._sa_headers())
            await self._ensure_header()  # here, before the photo: a timeout on it used to read as "not sure"
        except GoogleUnsure as e:
            raise ServiceDown("Sheets", e) from e

    async def save_receipt(self, photo: bytes, mime: str, file_name: str, row: ReceiptRow) -> str:
        """Photo to Drive, then the row to Sheets; returns the photo link. Never leaves a row without its photo:
        if the append failed for sure, the photo is deleted; if the outcome is unknown, the photo stays."""
        try:
            file_id, link = await self._upload(photo, mime, file_name)
        except GoogleUnreachable:
            raise  # nothing was uploaded
        except GoogleUnsure:
            # Live 28.09: Drive made the file in 2 s, the answer never came. Take that file instead of a second one.
            found = await self._find_upload(file_name)
            if found is None:
                log.error("upload outcome unknown, check Drive for %r", file_name)
                raise
            file_id, link = found
        try:
            await self._append(row.cells(link))
        except GoogleUnreachable:
            await self._delete(file_id)  # the row request never left: a clear "not saved"
            raise
        except GoogleUnsure:
            try:
                if await self._row_written(row.receipt_id):
                    return link  # Google saved it, only the reply got lost
            except GoogleError:
                pass
            # The row may still land after our lookup: a spare photo beats a row with a dead link.
            log.error("receipt %s: row not confirmed, photo %s kept", row.receipt_id, file_id)
            raise
        except Exception:
            await self._delete(file_id)
            raise
        return link

    async def _delete(self, file_id: str) -> None:
        try:
            await _call(self._http, "drive delete", "DELETE", f"{DRIVE_URL}/files/{file_id}",
                        headers=await self._owner_headers())
        except GoogleError as e:
            log.error("orphan photo left in Drive: %s (%s)", file_id, e)

    async def _find_upload(self, file_name: str) -> tuple[str, str] | None:
        """The photo by its exact name (it carries the receipt ID) in our folder, or None if it isn't there."""
        name = file_name.replace("\\", "\\\\").replace("'", "\\'")
        try:
            r = await _call(self._http, "drive lookup", "GET", f"{DRIVE_URL}/files", headers=await self._owner_headers(),
                            params={"q": f"name = '{name}' and '{self._folder_id}' in parents and trashed = false",
                                    "fields": "files(id,webViewLink)", "pageSize": 2})
            files = _json(r, "drive lookup").get("files", [])
        except GoogleError:
            return None
        if len(files) != 1:
            return None
        return files[0]["id"], files[0].get("webViewLink") or f"https://drive.google.com/file/d/{files[0]['id']}/view"

    async def _row_written(self, receipt_id: str) -> bool:
        r = await _call(self._http, "sheet lookup", "GET", f"{SHEETS_URL}/{self._sheet_id}/values/{ID_COLUMN}",
                        headers=await self._sa_headers())
        return any(receipt_id in row for row in _json(r, "sheet lookup").get("values", []))

    async def _upload(self, photo: bytes, mime: str, file_name: str) -> tuple[str, str]:
        boundary = "receipt-" + secrets.token_hex(8)
        meta = json.dumps({"name": file_name, "parents": [self._folder_id]})
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n").encode() + photo + f"\r\n--{boundary}--\r\n".encode()
        r = await _call(self._http, "drive upload", "POST", UPLOAD_URL,
                        params={"uploadType": "multipart", "fields": "id,webViewLink"}, content=body,
                        headers={**await self._owner_headers(), "Content-Type": f"multipart/related; boundary={boundary}"})
        d = _json(r, "drive upload")
        return d["id"], d.get("webViewLink") or f"https://drive.google.com/file/d/{d['id']}/view"

    async def _ensure_header(self) -> None:
        if self._header_ok:
            return
        headers = await self._sa_headers()
        r = await _call(self._http, "sheet header", "GET", f"{SHEETS_URL}/{self._sheet_id}/values/{HEADER_RANGE}",
                        headers=headers)
        present = len((_json(r, "sheet header").get("values") or [[]])[0])
        if present < len(HEADER):
            # An empty sheet gets the whole header, an older one only the new columns: renamed ones stay as they are.
            start = chr(ord("A") + present)
            await _call(self._http, "sheet header", "PUT",
                        f"{SHEETS_URL}/{self._sheet_id}/values/{start}1:{LAST_COLUMN}1",
                        params={"valueInputOption": "RAW"}, json={"values": [HEADER[present:]]}, headers=headers)
        self._header_ok = True

    async def rows(self) -> list[list]:
        """Every receipt row (no header), as the cells are: numbers as numbers. For the duplicate check."""
        r = await _call(self._http, "sheet rows", "GET", f"{SHEETS_URL}/{self._sheet_id}/values/A2:{LAST_COLUMN}",
                        params={"valueRenderOption": "UNFORMATTED_VALUE"}, headers=await self._sa_headers())
        values = _json(r, "sheet rows").get("values", [])
        return values if isinstance(values, list) else []

    async def _append(self, cells: list) -> None:
        await self._ensure_header()  # the bot already did it in ping(); a no-op then
        headers = await self._sa_headers()
        # RAW, not USER_ENTERED: a Telegram name like "=IMPORTXML(...)" must stay text, not become a formula.
        await _call(self._http, "sheet append", "POST", f"{SHEETS_URL}/{self._sheet_id}/values/A1:append",
                    params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                    json={"values": [cells]}, headers=headers)
