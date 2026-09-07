"""Minimal API client for the MyWaterAdvisor (Harmony Encore) portal.

Reverse-engineered endpoints, documented publicly at
https://github.com/nitrogen76/ha-mywateradvisor/blob/master/API.md
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime

import aiohttp

from .const import BASE_URL, KNOWN_APP_ID_FALLBACK, PORTAL_ROOT_URL, VACATION_DEFAULT_DAILY_LIMIT_GALLONS

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=30)
_APP_ID_RE = re.compile(r'app:\s*"([0-9a-fA-F-]{36})"')
_SCRIPT_SRC_RE = re.compile(r'<script[^>]+src="([^"]+\.js)"')


class MyWaterAdvisorError(Exception):
    """Generic API error."""


class MyWaterAdvisorAuthError(MyWaterAdvisorError):
    """Raised when login fails."""


async def _safe_json(resp: aiohttp.ClientResponse):
    """Write endpoints (POST/PUT/DELETE) often return an empty body."""
    text = await resp.text()
    if not text:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        return {}


async def _discover_app_id(session: aiohttp.ClientSession) -> str | None:
    """Best-effort: pull the live app id out of the portal's JS bundle."""
    try:
        async with session.get(PORTAL_ROOT_URL, timeout=_TIMEOUT) as resp:
            html = await resp.text()
    except (aiohttp.ClientError, TimeoutError):
        return None

    for src in _SCRIPT_SRC_RE.findall(html):
        url = src if src.startswith("http") else f"{PORTAL_ROOT_URL}{src if src.startswith('/') else '/' + src}"
        try:
            async with session.get(url, timeout=_TIMEOUT) as resp:
                text = await resp.text()
        except (aiohttp.ClientError, TimeoutError):
            continue
        match = _APP_ID_RE.search(text)
        if match:
            return match.group(1)
    return None


class MyWaterAdvisorClient:
    """Talks to the MyWaterAdvisor customer portal API."""

    def __init__(self, session: aiohttp.ClientSession, email: str, password: str) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._token: str | None = None
        self._app_id: str | None = None
        self._meter_id: str | None = None
        self._meter_info: dict | None = None

    async def _get_app_id(self) -> str:
        if self._app_id is None:
            self._app_id = await _discover_app_id(self._session) or KNOWN_APP_ID_FALLBACK
        return self._app_id

    async def async_login(self) -> str:
        app_id = await self._get_app_id()
        payload = {
            "email": self._email,
            "pw": self._password,
            "type": 1,
            "app": app_id,
            "deviceId": str(uuid.uuid4()),
            "osType": 3,
        }
        headers = {"x-app-id": app_id, "Content-Type": "application/json"}
        try:
            async with self._session.post(
                f"{BASE_URL}/consumer/login", json=payload, headers=headers, timeout=_TIMEOUT
            ) as resp:
                if resp.status in (401, 403):
                    raise MyWaterAdvisorAuthError("Invalid email or password")
                resp.raise_for_status()
                data = await resp.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise MyWaterAdvisorError(f"Login request failed: {err}") from err

        token = data.get("token")
        if not token:
            raise MyWaterAdvisorAuthError("Login response did not include a token")
        self._token = token
        return token

    async def _authed_headers(self) -> dict[str, str]:
        if self._token is None:
            await self.async_login()
        app_id = await self._get_app_id()
        return {"x-app-id": app_id, "x-access-token": self._token}

    async def _request(self, method: str, path: str, json_body=None):
        headers = await self._authed_headers()
        url = f"{BASE_URL}{path}"
        try:
            async with self._session.request(
                method, url, headers=headers, json=json_body, timeout=_TIMEOUT
            ) as resp:
                if resp.status in (401, 403):
                    self._token = None
                    headers = await self._authed_headers()
                    async with self._session.request(
                        method, url, headers=headers, json=json_body, timeout=_TIMEOUT
                    ) as retry:
                        if retry.status >= 400:
                            body = await retry.text()
                            raise MyWaterAdvisorError(
                                f"Request to {path} failed: {retry.status} {body}"
                            )
                        return await _safe_json(retry)
                if resp.status >= 400:
                    body = await resp.text()
                    raise MyWaterAdvisorError(f"Request to {path} failed: {resp.status} {body}")
                return await _safe_json(resp)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise MyWaterAdvisorError(f"Request to {path} failed: {err}") from err

    async def async_get_meters(self):
        return await self._request("GET", "/consumer/meters")

    async def _ensure_meter_info(self) -> dict:
        """Fetch and cache the account's meter object. One network call, ever."""
        if self._meter_info is not None:
            return self._meter_info

        data = await self.async_get_meters()
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("meters") or data.get("data") or []
        else:
            items = []

        if not items:
            raise MyWaterAdvisorError(f"No meters found in response: {data}")

        self._meter_info = items[0]
        return self._meter_info

    async def async_get_meter_info(self) -> dict:
        return await self._ensure_meter_info()

    async def async_get_meter_id(self) -> str:
        if self._meter_id is not None:
            return self._meter_id

        meter = await self._ensure_meter_info()
        # Despite the name, the portal's path parameter is this "meterCount"
        # value, not the meter serial number (meterSn) — confirmed by the
        # portal returning {"error": "Invalid meter count"} when meterSn
        # was used instead.
        for key in ("meterCount", "id", "meterId", "meter_id", "meterID", "meterSn"):
            if key in meter:
                self._meter_id = meter[key]
                return self._meter_id

        raise MyWaterAdvisorError(
            f"Could not find a meter id field. Meter object keys: {list(meter.keys())}"
        )

    async def async_get_hourly_consumption(self, start: datetime, end: datetime):
        meter_id = await self.async_get_meter_id()
        start_str = start.strftime("%m-%d-%Y")
        end_str = end.strftime("%m-%d-%Y")
        return await self._request("GET", f"/consumption/hourly/{meter_id}/{start_str}/{end_str}")

    async def async_get_daily_consumption(self, start: datetime, end: datetime):
        meter_id = await self.async_get_meter_id()
        start_str = start.strftime("%m-%d-%Y")
        end_str = end.strftime("%m-%d-%Y")
        return await self._request("GET", f"/consumption/daily/{meter_id}/{start_str}/{end_str}")

    async def async_get_alerts(self):
        """GET /consumer/myalerts — items carry logId, alertTypeId, alertTypeName,
        alertTime, meterSn, address (confirmed from the portal's own JS bundle)."""
        return await self._request("GET", "/consumer/myalerts")

    async def async_get_forecast(self):
        """GET /v1/consumption/forecast/{meterId} — {"estimatedConsumption": <gallons>}."""
        meter_id = await self.async_get_meter_id()
        return await self._request("GET", f"/v1/consumption/forecast/{meter_id}")

    async def async_get_vacations(self):
        """GET /consumer/vacations/ — list of {vacationID, startDate, endDate,
        consumptionDailyLimit, meterCount} scheduled date ranges."""
        return await self._request("GET", "/consumer/vacations/")

    async def async_get_billing_cycles(self):
        """GET /v1.1/meters/billing-cycles — list of {type: current|previous,
        billingCycleStart, billingCycleEnd} per meter."""
        return await self._request("GET", "/v1.1/meters/billing-cycles")

    async def async_get_monthly_limit(self):
        """GET /v1.1/consumer/settings/monthlylimit/{meterId} — the user's configured
        billing-cycle consumption budget. Response shape unconfirmed against a live
        account; parsed defensively by the caller."""
        meter_id = await self.async_get_meter_id()
        return await self._request("GET", f"/v1.1/consumer/settings/monthlylimit/{meter_id}")

    async def async_get_avg_households(self, start: datetime, end: datetime):
        """GET /consumption/avghouseholds/{start}/{end} — neighborhood comparison data.

        Least-confirmed endpoint: the portal's own JS only shows the call site, not
        the response shape or exact date-string format it expects. Guessing MM-YYYY
        to match this month-granularity feature; caller must treat failures/odd
        shapes as non-fatal.
        """
        start_str = start.strftime("%m-%Y")
        end_str = end.strftime("%m-%Y")
        return await self._request("GET", f"/consumption/avghouseholds/{start_str}/{end_str}")

    @staticmethod
    def _standard_datetime(value: datetime) -> str:
        """Match the portal's moment.js STANDARD_FORMAT: YYYY-MM-DDTHH:mm:ss.SSSZ."""
        return value.isoformat(timespec="milliseconds")

    async def async_create_vacation(
        self, start: datetime, end: datetime, daily_limit: float | None
    ) -> None:
        """POST /consumer/vacations/ — schedule a new vacation date range.

        The portal 400s with {"errors":{"ConsumptionDailyLimit":["...required"]}}
        if this is null — confirmed live. Falls back to a default rather than
        sending null.
        """
        meter_id = await self.async_get_meter_id()
        if daily_limit is None:
            daily_limit = VACATION_DEFAULT_DAILY_LIMIT_GALLONS
        body = {
            "vacationID": 0,
            "startDate": self._standard_datetime(start),
            "endDate": self._standard_datetime(end),
            "consumptionDailyLimit": daily_limit,
            "meterCount": meter_id,
        }
        await self._request("POST", "/consumer/vacations/", json_body=body)

    async def async_cancel_vacation(self, vacation_id) -> None:
        """DELETE /consumer/vacations/{vacationID}.

        The portal sends an empty JSON object body (F(a,{},!0,t) in the JS),
        so we send {} too rather than a bodyless DELETE.
        """
        await self._request("DELETE", f"/consumer/vacations/{vacation_id}", json_body={})

    async def async_set_monthly_limit(self, limit: float) -> None:
        """POST /v1.1/consumer/settings/monthlylimit/{meterId}/{limit}.

        The value is a URL path segment, not a body field — matches the
        portal's own call site. Order confirmed from the JS call site itself:
        function(e,t,a){...+t+"/"+e}(monthlyLimit, meterCount, cb) means the
        URL is .../{meterCount}/{monthlyLimit}, meter id first.

        The portal's POST wrapper is j(n,{},!0,a): a POST whose body is an
        empty JSON object with Content-Type: application/json. We send {} to
        reproduce that exactly rather than a bodyless POST (aiohttp sends no
        body and no Content-Type when json_body is None), since the server
        accepted the empty-object form and may otherwise silently no-op.
        """
        meter_id = await self.async_get_meter_id()
        limit_str = str(int(limit)) if float(limit).is_integer() else str(limit)
        await self._request(
            "POST",
            f"/v1.1/consumer/settings/monthlylimit/{meter_id}/{limit_str}",
            json_body={},
        )

    async def async_clear_monthly_limit(self) -> None:
        """DELETE /v1.1/consumer/settings/monthlylimit/{meterId}.

        Portal sends an empty JSON object body (F(a,{},!0,t)); we match it.
        """
        meter_id = await self.async_get_meter_id()
        await self._request(
            "DELETE", f"/v1.1/consumer/settings/monthlylimit/{meter_id}", json_body={}
        )
