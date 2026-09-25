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
HEADER = ["Додано", "Дата чека", "Відправник", "Email", "Сума", "Валюта", "Фото", "Сума вручну"]


class GoogleError(Exception):
    """A Google call failed. The text never contains tokens, so it is safe to log."""


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
    added_at: str
    receipt_date: str
    sender: str
    email: str
    amount: float
    currency: str
    manual: bool

    def cells(self, photo_link: str) -> list:
        return [self.added_at, self.receipt_date, self.sender, self.email, self.amount, self.currency,
                photo_link, "так" if self.manual else ""]


def _client(client_file: str) -> tuple[str, str]:
    with open(client_file, encoding="utf-8") as f:
        installed = json.load(f)["installed"]
    return installed["client_id"], installed["client_secret"]


async def _call(http: httpx.AsyncClient, what: str, method: str, url: str, **kwargs) -> httpx.Response:
    try:
        response = await http.request(method, url, **kwargs)
    except httpx.RequestError as e:
        raise GoogleError(f"{what}: {type(e).__name__}") from e
    if response.status_code >= 400:
        try:
            reason = response.json()["error"].get("status") or response.json()["error"].get("message", "")
        except (ValueError, KeyError, TypeError, AttributeError):
            reason = ""
        raise GoogleError(f"{what}: HTTP {response.status_code} {str(reason)[:80]}")
    return response


class GoogleLogin:
    """Device flow with scope "openid email": we only learn the address and throw the token away."""

    def __init__(self, client_file: str, http: httpx.AsyncClient):
        self._client_id, self._client_secret = _client(client_file)
        self._http = http

    async def start(self) -> DeviceCode:
        r = await _call(self._http, "device code", "POST", DEVICE_URL,
                        data={"client_id": self._client_id, "scope": "openid email"})
        d = r.json()
        return DeviceCode(d["device_code"], d["user_code"], d.get("verification_url") or d["verification_uri"],
                          int(d.get("expires_in", 1800)), int(d.get("interval", 5)))

    async def wait_for_email(self, code: DeviceCode) -> str:
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
            if r.status_code == 200:
                return await self._email(r.json()["access_token"])
            error = r.json().get("error") if r.headers.get("content-type", "").startswith("application/json") else ""
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

    async def _email(self, access_token: str) -> str:
        r = await _call(self._http, "userinfo", "GET", USERINFO_URL,
                        headers={"Authorization": f"Bearer {access_token}"})
        info = r.json()
        if not info.get("email") or not info.get("email_verified"):
            raise GoogleError("userinfo: no verified email")
        return info["email"].lower()


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
        self._permissions_at = 0.0
        self._header_ok = False
        self._sa_lock = asyncio.Lock()

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
            d = r.json()
            self._owner_token = d["access_token"]
            self._owner_expires = time.monotonic() + int(d.get("expires_in", 3600)) - 60
        return {"Authorization": f"Bearer {self._owner_token}"}

    # --- access ---

    async def has_access(self, email: str) -> bool:
        """Edit access to the sheet: direct share, the whole domain, or "anyone with the link can edit".
        Google groups are not expanded (known limitation, see README)."""
        if time.monotonic() - self._permissions_at > PERMISSIONS_TTL:
            self._permissions = await self._list_permissions()
            self._permissions_at = time.monotonic()
        email = email.lower()
        domain = email.rsplit("@", 1)[-1]
        for p in self._permissions:
            if p.get("role") not in EDIT_ROLES:
                continue
            if p.get("type") == "user" and p.get("emailAddress", "").lower() == email:
                return True
            if p.get("type") == "domain" and p.get("domain", "").lower() == domain:
                return True
            if p.get("type") == "anyone":
                return True
        return False

    async def _list_permissions(self) -> list[dict]:
        permissions, token = [], None
        while True:
            params = {"fields": "nextPageToken,permissions(role,type,emailAddress,domain)", "pageSize": 100,
                      "supportsAllDrives": "true"}
            if token:
                params["pageToken"] = token
            r = await _call(self._http, "permissions.list", "GET", f"{DRIVE_URL}/files/{self._sheet_id}/permissions",
                            params=params, headers=await self._sa_headers())
            d = r.json()
            permissions += d.get("permissions", [])
            token = d.get("nextPageToken")
            if not token:
                return permissions

    # --- saving ---

    async def save_receipt(self, photo: bytes, mime: str, file_name: str, row: ReceiptRow) -> str:
        """Photo to Drive, then the row to Sheets. If Sheets fails, the photo is deleted again:
        no half-written receipts. Returns the photo link."""
        file_id, link = await self._upload(photo, mime, file_name)
        try:
            await self._append(row.cells(link))
        except GoogleError:
            try:
                await _call(self._http, "drive delete", "DELETE", f"{DRIVE_URL}/files/{file_id}",
                            headers=await self._owner_headers())
            except GoogleError as e:
                log.error("orphan photo left in Drive: %s (%s)", file_id, e)
            raise
        return link

    async def _upload(self, photo: bytes, mime: str, file_name: str) -> tuple[str, str]:
        boundary = "receipt-" + secrets.token_hex(8)
        meta = json.dumps({"name": file_name, "parents": [self._folder_id]})
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n").encode() + photo + f"\r\n--{boundary}--\r\n".encode()
        r = await _call(self._http, "drive upload", "POST", UPLOAD_URL,
                        params={"uploadType": "multipart", "fields": "id,webViewLink"}, content=body,
                        headers={**await self._owner_headers(), "Content-Type": f"multipart/related; boundary={boundary}"})
        d = r.json()
        return d["id"], d.get("webViewLink") or f"https://drive.google.com/file/d/{d['id']}/view"

    async def _append(self, cells: list) -> None:
        headers = await self._sa_headers()
        if not self._header_ok:
            r = await _call(self._http, "sheet header", "GET", f"{SHEETS_URL}/{self._sheet_id}/values/A1:H1",
                            headers=headers)
            if not r.json().get("values"):
                await _call(self._http, "sheet header", "PUT", f"{SHEETS_URL}/{self._sheet_id}/values/A1:H1",
                            params={"valueInputOption": "RAW"}, json={"values": [HEADER]}, headers=headers)
            self._header_ok = True
        # RAW, not USER_ENTERED: a Telegram name like "=IMPORTXML(...)" must stay text, not become a formula.
        await _call(self._http, "sheet append", "POST", f"{SHEETS_URL}/{self._sheet_id}/values/A1:append",
                    params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                    json={"values": [cells]}, headers=headers)
