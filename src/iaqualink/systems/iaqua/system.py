from __future__ import annotations

import asyncio
from dataclasses import dataclass
import httpx
import json
import logging
import re
import ssl
import threading
import time
from typing import TYPE_CHECKING
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import (
    HTTPSHandler,
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    Request,
    build_opener,
)

from iaqualink.const import MIN_SECS_TO_REFRESH
from iaqualink.exception import (
    AqualinkDeviceNotSupported,
    AqualinkServiceException,
    AqualinkSystemOfflineException,
)
from iaqualink.system import AqualinkSystem
from iaqualink.systems.iaqua.device import IaquaDevice

if TYPE_CHECKING:
    from iaqualink.client import AqualinkClient
    from iaqualink.typing import Payload

IAQUA_SESSION_URL = "https://p-api.iaqualink.net/v1/mobile/session.json"
IAQUA_WEB_SESSION_URL = "https://p-api.iaqualink.net/v2/mobile/session.json"
IAQUA_WEBTOUCH_INIT_URL = "https://prm.iaqualink.net/v2/webtouch/init"
IAQUA_WEBTOUCH_COMMAND_URL = "https://prm.iaqualink.net/v2/webtouch/command"

IAQUA_COMMAND_GET_DEVICES = "get_devices"
IAQUA_COMMAND_GET_HOME = "get_home"
IAQUA_COMMAND_GET_ONETOUCH = "get_onetouch"

IAQUA_COMMAND_SET_AUX = "set_aux"
IAQUA_COMMAND_SET_LIGHT = "set_light"
IAQUA_COMMAND_SET_POOL_HEATER = "set_pool_heater"
IAQUA_COMMAND_SET_POOL_PUMP = "set_pool_pump"
IAQUA_COMMAND_SET_SOLAR_HEATER = "set_solar_heater"
IAQUA_COMMAND_SET_SPA_HEATER = "set_spa_heater"
IAQUA_COMMAND_SET_SPA_PUMP = "set_spa_pump"
IAQUA_COMMAND_SET_TEMPS = "set_temps"
IAQUA_COMMAND_GET_WEB = "get_web"
IAQUA_COMMAND_GET_VSP_SPEED = "get_vsp_speedauxinfo"
IAQUA_COMMAND_SET_VSP_SPEED = "enable_disable_pump_speedId"

WEBTOUCH_SCREEN_PRESETS = "30"
WEBTOUCH_DEVICES_COMMAND = "24"
WEBTOUCH_HOME_COMMAND = "1"
WEBTOUCH_PRESET_COMMAND_OFFSET = 16
WEBTOUCH_SLOT_COMMAND_OFFSET = 18
WEBTOUCH_STREAM_MAX_BYTES = 196_608
WEBTOUCH_DELAY_SECS = 1
WEBTOUCH_CONTEXT_TTL = 300
WEBTOUCH_TIMEOUT_SECS = 30.0
WEBTOUCH_EVENT_RE = re.compile(r"printNL\('([^']*)','([^']*)'\)")

LOGGER = logging.getLogger("iaqualink")


@dataclass(slots=True)
class WebTouchContext:
    action_id: str
    server_connection: str
    master_id: str
    master_start: str
    master_stb: str
    fetched_at: float


class WebTouchNoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


class IaquaSystem(AqualinkSystem):
    NAME = "iaqua"

    def __init__(self, aqualink: AqualinkClient, data: Payload):
        super().__init__(aqualink, data)

        self.temp_unit: str = ""
        self.last_refresh: int = 0
        self._webtouch_context: WebTouchContext | None = None

    def __repr__(self) -> str:
        attrs = ["name", "serial", "data"]
        attrs = [f"{i}={getattr(self, i)!r}" for i in attrs]
        return f"{self.__class__.__name__}({' '.join(attrs)})"

    async def _send_session_request(
        self,
        command: str,
        params: Payload | None = None,
    ) -> httpx.Response:
        if not params:
            params = {}

        params.update(
            {
                "actionID": "command",
                "command": command,
                "serial": self.serial,
                "sessionID": self.aqualink.client_id,
            }
        )
        params_str = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{IAQUA_SESSION_URL}?{params_str}"
        return await self.aqualink.send_request(url)

    async def _send_home_screen_request(self) -> httpx.Response:
        return await self._send_session_request(IAQUA_COMMAND_GET_HOME)

    async def _send_devices_screen_request(self) -> httpx.Response:
        return await self._send_session_request(IAQUA_COMMAND_GET_DEVICES)

    async def update(self) -> None:
        # Be nice to Aqualink servers since we rely on polling.
        now = int(time.time())
        delta = now - self.last_refresh
        if delta < MIN_SECS_TO_REFRESH:
            LOGGER.debug(f"Only {delta}s since last refresh.")
            return

        try:
            r1 = await self._send_home_screen_request()
            r2 = await self._send_devices_screen_request()
        except AqualinkServiceException:
            self.online = None
            raise

        try:
            self._parse_home_response(r1)
            self._parse_devices_response(r2)
        except AqualinkSystemOfflineException:
            self.online = False
            raise

        self.online = True
        self.last_refresh = int(time.time())

    def _parse_home_response(self, response: httpx.Response) -> None:
        data = response.json()

        LOGGER.debug(f"Home response: {data}")

        if data["home_screen"][0]["status"] == "Offline":
            LOGGER.warning(f"Status for system {self.serial} is Offline.")
            raise AqualinkSystemOfflineException

        self.temp_unit = data["home_screen"][3]["temp_scale"]

        # Make the data a bit flatter.
        devices = {}
        for x in data["home_screen"][4:]:
            name = next(iter(x.keys()))
            state = next(iter(x.values()))
            attrs = {"name": name, "state": state}
            devices.update({name: attrs})

        for k, v in devices.items():
            if k in self.devices:
                for dk, dv in v.items():
                    self.devices[k].data[dk] = dv
            else:
                try:
                    self.devices[k] = IaquaDevice.from_data(self, v)
                except AqualinkDeviceNotSupported as e:
                    LOGGER.debug("Device found was ignored: %s", e)

    def _parse_devices_response(self, response: httpx.Response) -> None:
        data = response.json()

        LOGGER.debug(f"Devices response: {data}")

        if data["devices_screen"][0]["status"] == "Offline":
            LOGGER.warning(f"Status for system {self.serial} is Offline.")
            raise AqualinkSystemOfflineException

        # Make the data a bit flatter.
        devices = {}
        for x in data["devices_screen"][3:]:
            aux = next(iter(x.keys()))
            attrs = {"aux": aux.replace("aux_", ""), "name": aux}
            for y in next(iter(x.values())):
                attrs.update(y)
            devices.update({aux: attrs})

        for k, v in devices.items():
            if k in self.devices:
                for dk, dv in v.items():
                    self.devices[k].data[dk] = dv
            else:
                try:
                    self.devices[k] = IaquaDevice.from_data(self, v)
                except AqualinkDeviceNotSupported as e:
                    LOGGER.info("Device found was ignored: %s", e)

    async def set_switch(self, command: str) -> None:
        r = await self._send_session_request(command)
        self._parse_home_response(r)

    async def set_temps(self, temps: Payload) -> None:
        # I'm not proud of this. If you read this, please submit a PR to make it better.
        # We need to pass the temperatures for both pool and spa (if present) in the same request.
        # Set args to current target temperatures and override with the request payload.
        args = {}
        i = 1
        if "spa_set_point" in self.devices:
            args[f"temp{i}"] = self.devices["spa_set_point"].target_temperature
            i += 1
        args[f"temp{i}"] = self.devices["pool_set_point"].target_temperature
        args.update(temps)

        r = await self._send_session_request(IAQUA_COMMAND_SET_TEMPS, args)
        self._parse_home_response(r)

    async def set_aux(self, aux: str) -> None:
        aux = IAQUA_COMMAND_SET_AUX + "_" + aux.replace("aux_", "")
        r = await self._send_session_request(aux)
        self._parse_devices_response(r)

    async def set_light(self, data: Payload) -> None:
        r = await self._send_session_request(IAQUA_COMMAND_SET_LIGHT, data)
        self._parse_devices_response(r)

    async def get_vsp_speed(self, slot_id: int = 1) -> Payload:
        """Get VSP speed presets and active speed for a pump slot."""
        r = await self._send_session_request(
            IAQUA_COMMAND_GET_VSP_SPEED, {"slot_id": str(slot_id)}
        )
        return r.json()

    async def set_vsp_speed(self, speed_id: int, slot_id: int = 1) -> Payload:
        """Enable a speed preset on a VSP pump slot."""
        r = await self._send_session_request(
            IAQUA_COMMAND_SET_VSP_SPEED,
            {
                "slot_id": str(slot_id),
                "speed_id": str(speed_id),
                "on_off_action": "on",
            },
        )
        return r.json()

    async def _send_webtouch_redirect_request(self) -> httpx.Response:
        url = (
            f"{IAQUA_WEB_SESSION_URL}?actionID=command"
            f"&command={IAQUA_COMMAND_GET_WEB}&serial={self.serial}"
        )
        return await self.aqualink.send_request(
            url,
            headers={"Authorization": self.aqualink.id_token},
            expected_statuses={httpx.codes.MOVED_PERMANENTLY},
            follow_redirects=False,
            timeout=WEBTOUCH_TIMEOUT_SECS,
        )

    @staticmethod
    def _parse_webtouch_action_id(response: httpx.Response) -> str:
        location = response.headers.get("location")
        if not location:
            msg = "WebTouch bootstrap did not return a redirect location"
            raise AqualinkServiceException(msg)

        query = parse_qs(urlparse(location).query)
        action_ids = query.get("actionID")
        if not action_ids:
            msg = "WebTouch redirect did not include an actionID"
            raise AqualinkServiceException(msg)
        return action_ids[0]

    async def _get_webtouch_context(
        self, *, refresh: bool = False
    ) -> WebTouchContext:
        now = time.time()
        if (
            not refresh
            and self._webtouch_context is not None
            and now - self._webtouch_context.fetched_at < WEBTOUCH_CONTEXT_TTL
        ):
            return self._webtouch_context

        redirect = await self._send_webtouch_redirect_request()
        action_id = self._parse_webtouch_action_id(redirect)
        response = await self.aqualink.send_request(
            f"{IAQUA_WEBTOUCH_INIT_URL}?actionID={action_id}",
            headers={
                "Authorization": self.aqualink.id_token,
                "user-agent": "Mozilla/5.0",
            },
            timeout=WEBTOUCH_TIMEOUT_SECS,
        )
        data = response.json()

        required = (
            "serverConnection",
            "actionIdMasterId",
            "actionIdMasterStart",
            "actionIdMasterSTB",
        )
        if not all(key in data for key in required):
            msg = f"Incomplete WebTouch init payload: {data}"
            raise AqualinkServiceException(msg)

        self._webtouch_context = WebTouchContext(
            action_id=action_id,
            server_connection=data["serverConnection"],
            master_id=data["actionIdMasterId"],
            master_start=data["actionIdMasterStart"],
            master_stb=data["actionIdMasterSTB"],
            fetched_at=now,
        )
        return self._webtouch_context

    async def _send_webtouch_command(
        self,
        action_id: str,
        command: int | str,
        *,
        text: int | str | None = None,
    ) -> None:
        payload: dict[str, str] = {
            "actionID": action_id,
            "command": str(command),
            "dt": str(int(time.time() * 1000)),
        }
        if text is not None:
            payload["text"] = str(text)

        await self.aqualink.send_request(
            IAQUA_WEBTOUCH_COMMAND_URL,
            method="post",
            json=payload,
            headers={
                "Authorization": self.aqualink.id_token,
                "user-agent": "Mozilla/5.0",
            },
            timeout=WEBTOUCH_TIMEOUT_SECS,
        )

    @staticmethod
    def _parse_webtouch_events(stream: str) -> list[tuple[str, str]]:
        return WEBTOUCH_EVENT_RE.findall(stream)

    @staticmethod
    def _webtouch_request(
        opener,
        url: str,
        *,
        method: str = "GET",
        data: Payload | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, str, dict[str, str]]:
        request_headers = {"User-Agent": "Mozilla/5.0"}
        if headers:
            request_headers.update(headers)

        body = None if data is None else json.dumps(data).encode()
        if body is not None:
            request_headers.setdefault("Content-Type", "application/json")

        request = Request(url, data=body, headers=request_headers, method=method)

        try:
            response = opener.open(request, timeout=WEBTOUCH_TIMEOUT_SECS)
            return response.getcode(), response.read().decode(errors="replace"), dict(
                response.info()
            )
        except HTTPError as err:
            return err.code, err.read().decode(errors="replace"), dict(err.headers)

    def _collect_webtouch_events_sync(
        self,
        slot_id: int,
        *,
        preset_speed_id: int | None = None,
        rpm: int | None = None,
    ) -> list[tuple[str, str]]:
        opener = build_opener(
            HTTPSHandler(context=ssl.create_default_context()),
            HTTPCookieProcessor(CookieJar()),
            WebTouchNoRedirect(),
        )

        status, _, headers = self._webtouch_request(
            opener,
            (
                f"{IAQUA_WEB_SESSION_URL}?actionID=command"
                f"&command={IAQUA_COMMAND_GET_WEB}&serial={self.serial}"
            ),
            headers={
                "Authorization": self.aqualink.id_token,
                "User-Agent": "okhttp/3.14.7",
            },
        )
        if status != 301:
            msg = f"Unexpected WebTouch bootstrap status: {status}"
            raise AqualinkServiceException(msg)

        location = headers.get("Location") or headers.get("location")
        if not location:
            msg = "WebTouch bootstrap did not return a redirect location"
            raise AqualinkServiceException(msg)

        action_id = parse_qs(urlparse(location).query)["actionID"][0]
        status, body, _ = self._webtouch_request(
            opener,
            f"{IAQUA_WEBTOUCH_INIT_URL}?actionID={action_id}",
            headers={"Authorization": self.aqualink.id_token},
        )
        if status != 200:
            msg = f"Unexpected WebTouch init status: {status}"
            raise AqualinkServiceException(msg)

        init = json.loads(body)
        context = WebTouchContext(
            action_id=action_id,
            server_connection=init["serverConnection"],
            master_id=init["actionIdMasterId"],
            master_start=init["actionIdMasterStart"],
            master_stb=init["actionIdMasterSTB"],
            fetched_at=time.time(),
        )

        chunks: list[str] = []
        errors: list[str] = []

        def reader() -> None:
            try:
                request = Request(
                    context.server_connection,
                    headers={"User-Agent": "Mozilla/5.0"},
                    method="GET",
                )
                response = opener.open(request, timeout=WEBTOUCH_TIMEOUT_SECS)
                for _ in range(64):
                    chunk = response.read(4096)
                    if not chunk:
                        break
                    chunks.append(chunk.decode(errors="replace"))
            except Exception as err:  # noqa: BLE001
                errors.append(repr(err))

        commands = [
            (context.master_start, WEBTOUCH_HOME_COMMAND, None),
            (context.master_id, WEBTOUCH_DEVICES_COMMAND, None),
            (context.master_id, WEBTOUCH_SLOT_COMMAND_OFFSET + slot_id, None),
        ]
        if preset_speed_id is not None:
            commands.append(
                (context.master_id, WEBTOUCH_PRESET_COMMAND_OFFSET + preset_speed_id, None)
            )
        if rpm is not None:
            commands.append((context.master_stb, 128, rpm))

        thread = threading.Thread(target=reader)
        thread.start()
        time.sleep(WEBTOUCH_DELAY_SECS)

        for action, command, text in commands:
            payload: Payload = {
                "actionID": action,
                "command": str(command),
                "dt": str(int(time.time() * 1000)),
            }
            if text is not None:
                payload["text"] = str(text)

            status, _, _ = self._webtouch_request(
                opener,
                IAQUA_WEBTOUCH_COMMAND_URL,
                method="POST",
                data=payload,
                headers={"Authorization": self.aqualink.id_token},
            )
            if status != 200:
                msg = f"Unexpected WebTouch command status: {status}"
                raise AqualinkServiceException(msg)

            time.sleep(WEBTOUCH_DELAY_SECS)

        thread.join(timeout=WEBTOUCH_TIMEOUT_SECS)
        if errors:
            raise AqualinkServiceException(errors[0])

        return self._parse_webtouch_events("".join(chunks))

    @staticmethod
    def _extract_webtouch_speed_data(events: list[tuple[str, str]]) -> Payload:
        screen_id = ""
        presets: list[Payload] = []

        for code, params in events:
            if code == "23":
                screen_id = params
                continue

            if code != "24" or screen_id != WEBTOUCH_SCREEN_PRESETS:
                continue

            parts = params.split("||")
            if len(parts) < 5:
                continue

            try:
                speed_id = int(parts[0]) + 1
                speed_value = int(parts[4])
            except ValueError:
                continue

            presets.append(
                {
                    "speedid": str(speed_id),
                    "speedname": parts[3].strip(),
                    "speedvalue": str(speed_value),
                    "enabled": "true" if parts[1] == "1" else "false",
                }
            )

        if not presets:
            msg = "WebTouch did not return VSP preset data"
            raise AqualinkServiceException(msg)

        return {"vsp_speedInfo": presets}

    async def _collect_webtouch_events(
        self,
        context: WebTouchContext,
        commands: list[tuple[str, int | str, int | str | None]],
    ) -> list[tuple[str, str]]:
        stream_task = asyncio.create_task(
            asyncio.to_thread(
                self._read_webtouch_stream,
                context.server_connection,
                WEBTOUCH_STREAM_MAX_BYTES,
            )
        )

        await asyncio.sleep(WEBTOUCH_DELAY_SECS)
        for action_id, command, text in commands:
            await self._send_webtouch_command(action_id, command, text=text)
            await asyncio.sleep(WEBTOUCH_DELAY_SECS)

        return self._parse_webtouch_events(await stream_task)

    async def get_webtouch_speed(self, slot_id: int = 1) -> Payload:
        """Get VSP speed data through the WebTouch transport."""
        events = await asyncio.to_thread(
            self._collect_webtouch_events_sync,
            slot_id,
        )
        return self._extract_webtouch_speed_data(events)

    async def set_webtouch_speed(self, speed_id: int, slot_id: int = 1) -> None:
        """Activate a WebTouch VSP preset."""
        await asyncio.to_thread(
            self._collect_webtouch_events_sync,
            slot_id,
            preset_speed_id=speed_id,
        )

    async def set_webtouch_rpm(self, rpm: int, slot_id: int = 1) -> None:
        """Set an arbitrary RPM through the WebTouch transport."""
        await asyncio.to_thread(
            self._collect_webtouch_events_sync,
            slot_id,
            rpm=rpm,
        )
