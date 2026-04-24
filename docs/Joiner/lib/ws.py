import json
import asyncio
from curl_cffi import AsyncSession


class DiscordWS:
    def __init__(
        self,
        token,
        device_prop,
        fingerprint,
        browser_version,
        client_identity,
        properties,
        client=None
    ):
        self.token = token
        self.client = client

        self.ws_data = {
            'session_id': None,
            'analytics_token': None,
            'heartbeat_interval': None
        }

        self.ws_connected = False
        self.ws = None
        self.async_session = None
        self.heartbeat_task = None
        self.packets_recv = 0
        self._closing = False

        self.connected_event = asyncio.Event()
        self.is_ready = asyncio.Event()

        self.message_handlers = {}
        self.handle_task = None

    async def connect(self):
        browser = self.client.device_prop['browser']
        impersonate = f"{browser}"

        self.async_session = AsyncSession(impersonate=impersonate)
        self.ws = await self.async_session.ws_connect(
            "wss://gateway.discord.gg/?v=9&encoding=json"
        )

        self.ws_connected = True
        self.handle_task = asyncio.create_task(self.handle_messages())

    async def handle_messages(self):
        try:
            async for message in self.ws:
                await self.handle_message(message)
        except Exception:
            pass

    async def handle_message(self, message):
        if isinstance(message, bytes):
            message = message.decode("utf-8")

        try:
            d = json.loads(message)
            await self.process_message(d)
        except Exception:
            pass

    async def process_message(self, d):
        op = d.get("op")

        if op == 10:  # HELLO
            self.ws_data["heartbeat_interval"] = d["d"]["heartbeat_interval"]

            auth = {
                "op": 2,
                "d": {
                    "token": self.token,
                    "capabilities": 1734653,
                    "properties": {
                        "os": self.client.properties["os"],
                        "browser": self.client.properties["browser"],
                        "device": self.client.properties["device"],
                        "system_locale": self.client.properties["system_locale"],
                        "has_client_mods": True,
                        "browser_user_agent": self.client.properties["browser_user_agent"],
                        "browser_version": self.client.properties["browser_version"],
                        "os_version": self.client.properties["os_version"],
                        "release_channel": self.client.properties["release_channel"],
                        "client_build_number": self.client.properties["client_build_number"],
                        "client_event_source": None,
                        "client_launch_id": self.client.client_identity["client_launch_id"],
                        "launch_signature": self.client.client_identity["launch_signature"],
                        "client_app_state": "focused",
                        "is_fast_connect": False,
                        "gateway_connect_reasons": "AppSkeleton",
                    },
                    "presence": {
                        "status": "unknown",
                        "since": 0,
                        "activities": [],
                        "afk": False
                    },
                    "compress": False,
                    "client_state": {"guild_versions": {}}
                }
            }

            await self.ws.send_str(json.dumps(auth))
            self.heartbeat_task = asyncio.create_task(self.heartbeat())
            self.connected_event.set()

        elif d.get("t") == "READY":
            self.ws_data.update({
                "session_id": d["d"]["session_id"],
                "analytics_token": d["d"]["analytics_token"],
                "private_channels": d["d"]["private_channels"]
            })

            self.is_ready.set()

            self.client.science.analytics_token = self.ws_data["analytics_token"]
            self.client.science.reset()
            if self.client.science.analytics_token:
                await self.client.science.submit()

        if d.get("t") in self.message_handlers:
            for handler in self.message_handlers[d["t"]][:]:
                try:
                    await handler(d)
                except Exception:
                    pass

    async def heartbeat(self):
        while self.ws_connected and not self._closing:
            try:
                await asyncio.sleep(self.ws_data["heartbeat_interval"] / 1000)
                await self.ws.send_str(
                    json.dumps({"op": 1, "d": self.packets_recv})
                )
                self.packets_recv += 1
            except Exception:
                break

    async def send_custom_data(self, data):
        if not self.ws_connected:
            raise ConnectionError("WebSocket not connected")

        await self.ws.send_str(
            json.dumps(data) if isinstance(data, (dict, list)) else data
        )

    async def close(self):
        if self._closing:
            return

        self._closing = True
        self.ws_connected = False

        if self.heartbeat_task:
            self.heartbeat_task.cancel()

        if self.handle_task:
            self.handle_task.cancel()

        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass

        if self.async_session:
            try:
                await self.async_session.close()
            except Exception:
                pass
