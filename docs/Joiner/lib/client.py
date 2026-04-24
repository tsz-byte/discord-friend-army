import json, base64, uuid, re
import asyncio
import tls_client
from browserforge.fingerprints import FingerprintGenerator
from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor

from lib.ws import DiscordWS
from lib.actions import ActionsContainer
from lib.science import SciencePayload, Client_UUID


# ✅ cache heavy objects (NO behavior change)
_FINGERPRINT_GENERATOR = FingerprintGenerator()
_FIREFOX_REGEX = re.compile(r"Firefox/([\d\.]+)")


class DiscordClient:
    def __init__(
        self,
        token: str,
        email="",
        password="",
        device_prop={"browser": "firefox", "os": "macos", "client_build_number": 459631},
        proxy=None
    ):
        self.token = token
        self.device_prop = device_prop
        self.proxy = proxy

        # ✅ reuse ONE executor
        self._executor = ThreadPoolExecutor(max_workers=8)

        self.client_identity = {
            k: str(uuid.uuid4())
            for k in ["client_launch_id", "launch_signature", "client_heartbeat_session_id"]
        }

        # ✅ reuse cached generator (BIG speedup)
        self.fingerprint = asdict(
            _FINGERPRINT_GENERATOR.generate(
                browser=device_prop["browser"],
                os=device_prop["os"]
            )
        )

        navigator = self.fingerprint["navigator"]
        user_agent = navigator["userAgent"]

        # ✅ cached regex usage
        if navigator.get("userAgentData"):
            self.browser_version = navigator["userAgentData"]["brands"][-1]["version"]
        else:
            m = _FIREFOX_REGEX.search(user_agent)
            self.browser_version = m.group(1) if m else "0"

        self.session = tls_client.Session(
            client_identifier="firefox",
            random_tls_extension_order=True
        )

        self.session.headers = {
            **self.fingerprint["headers"],
            "Authorization": self.token,
            "x-debug-options": "bugReporterEnabled",
            "x-discord-locale": "en-US",
        }

        # 🔒 token validation (UNCHANGED)
        self.me = self.session.get(
            "https://discord.com/api/v9/users/@me",
            proxy=self.proxy
        )
        if self.me.status_code != 200:
            raise Exception("Invalid token. -> " + json.dumps(self.me.json()))

        user_info = self.me.json()
        locale = user_info.get("locale", "en")
        self.session.headers["x-discord-locale"] = locale

        self.properties = {
            "os": device_prop["os"],
            "browser": device_prop["browser"],
            "device": "",
            "system_locale": locale,
            "has_client_mods": True,
            "browser_user_agent": user_agent,
            "browser_version": self.browser_version,
            "os_version": "10",
            "referrer": "",
            "referring_domain": "",
            "referrer_current": "",
            "referring_domain_current": "",
            "release_channel": "stable",
            "client_build_number": device_prop["client_build_number"],
            "client_event_source": None,
            **self.client_identity,
            "client_app_state": "focused",
        }

        self.session.headers["X-Super-Properties"] = base64.b64encode(
            json.dumps(self.properties).encode()
        ).decode()

        self.ws = DiscordWS(
            token,
            device_prop,
            self.fingerprint,
            self.browser_version,
            self.client_identity,
            self.properties,
            client=self,
        )

        self.science = SciencePayload(self)
        self.actions = ActionsContainer(self)

    # async wrapper (UNCHANGED logic)
    async def _make_request(self, method: str, url: str, **kwargs):
        loop = asyncio.get_running_loop()

        headers = kwargs.pop("headers", {})
        merged_headers = {**headers}

        def _do_request():
            return getattr(self.session, method.lower())(
                url,
                headers=merged_headers,
                proxy=self.proxy,
                **kwargs,
            )

        return await loop.run_in_executor(self._executor, _do_request)

    @property
    def ws_connected(self):
        return self.ws.ws_connected if self.ws else False

    @property
    def ws_data(self):
        return getattr(self.ws, "ws_data", {}) if self.ws else {}

    async def init(self):
        await self.ws.connect()
        await self.ws.connected_event.wait()

    async def send_custom_data(self, data):
        await self.ws.send_custom_data(data)

    async def close(self):
        if self.ws:
            await self.ws.close()
        self._executor.shutdown(wait=False)

    def run(self):
        asyncio.run(self.init())
