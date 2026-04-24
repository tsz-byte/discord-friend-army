import json
import uuid
import asyncio
import base64

from solver_client import Solver
from logger import info, success, warning, error, captcha

import time
loop = asyncio.get_running_loop()

def solve_captcha(captcha_sitekey, captcha_rqdata, proxy):
    import requests

    url = "http://localhost:3001/solve"
    payload = {
        "sitekey": captcha_sitekey,
        "host": "discord.com",
        "rqdata": captcha_rqdata,
        "proxy": proxy
    }

    for attempt in range(3):
        try:
            response = requests.post(url, json=payload, timeout=30)
            token = response.json().get("token")

            if token:
                return token

        except Exception as e:
            pass  # optionally log error

    return None  # after 3 failed attempts


end = time.perf_counter()
class JoinHandler:
    def __init__(self, client):
        self.client = client
        self.token = getattr(client, "token", None)

    async def _solve_captcha_async(self, solver: Solver):
        """
        Run blocking captcha solver in executor
        (prevents event loop freeze)
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, solver.solve)

    async def join_guild(self, invite_code: str, proxy: str = None):
        # Wait for websocket ready
        await self.client.ws.is_ready.wait()

        info(self.token, "Starting guild join", invite_code)

        payload = {
            "session_id": self.client.ws.ws_data["session_id"]
        }

        # Minimal required context
        context_properties = {
            "location": "Join Guild"
        }

        context_b64 = base64.b64encode(
            json.dumps(context_properties).encode()
        ).decode()

        # ---------------- JOIN REQUEST ----------------
        res = await self.client._make_request(
            "POST",
            f"https://discord.com/api/v9/invites/{invite_code}",
            json=payload,
            headers={
                "X-Context-Properties": context_b64
            }
        )

        try:
            data = res.json()
        except Exception:
            error(self.token, "Invalid join response")
            return {
                "success": False,
                "error": "Invalid response",
                "invite_code": invite_code
            }

        # ---------------- CAPTCHA FLOW ----------------
        if "captcha_key" in data:
            captcha(self.token, "Captcha required")

            captcha_sitekey = data.get("captcha_sitekey")
            captcha_rqdata = data.get("captcha_rqdata")
            captcha_rqtoken = data.get("captcha_rqtoken")
            captcha_session_id = data.get("captcha_session_id")

            if not all([
                captcha_sitekey,
                captcha_rqdata,
                captcha_rqtoken,
                captcha_session_id
            ]):
                error(self.token, "Missing captcha fields")
                return {
                    "success": False,
                    "error": "Missing captcha fields",
                    "invite_code": invite_code
                }

            solver = Solver(
                url="https://discord.com",
                sitekey=captcha_sitekey,
                rqdata=captcha_rqdata,
                user_agent=self.client.session.headers.get(
                    "user-agent",
                    "Mozilla/5.0"
                )
            )

            info(self.token, "Solving captcha")

            # 🔥 NON-BLOCKING captcha solve
            import requests,time
            counter = time.perf_counter()
            #captcha_token, _ = await self._solve_captcha_async(solver)
            
            data = await loop.run_in_executor(None, solve_captcha, captcha_sitekey, captcha_rqdata, proxy)
            captcha_token = data
            if not captcha_token:
                error(self.token, "Captcha solve failed")
                return {
                    "success": False,
                    "error": "Failed to solve captcha",
                    "invite_code": invite_code
                }
            end = time.perf_counter()
            success(self.token, f"Captcha solved Time Taken-{end-counter}")

            retry_res = await self.client._make_request(
                "POST",
                f"https://discord.com/api/v9/invites/{invite_code}",
                json=payload,
                headers={
                    "X-Context-Properties": context_b64,
                    "X-Captcha-Key": captcha_token,
                    "X-Captcha-Rqdata": captcha_rqdata,
                    "X-Captcha-Rqtoken": captcha_rqtoken,
                    "X-Captcha-Session-Id": captcha_session_id
                }
            )

            try:
                retry_data = retry_res.json()
            except Exception:
                error(self.token, "Invalid retry response")
                return {
                    "success": False,
                    "error": "Invalid retry response",
                    "invite_code": invite_code
                }

            if retry_res.status_code == 200:
                guild_name = retry_data.get("guild", {}).get("name", "Unknown")
                success(self.token, "Joined guild after captcha", guild_name)
                return {
                    **retry_data,
                    "success": True,
                    "invite_code": invite_code
                }

            error(self.token, "Join failed after captcha")
            print(retry_data)
            return {
                "success": False,
                "error": retry_data,
                "invite_code": invite_code
            }

        # ---------------- SUCCESS ----------------
        if res.status_code == 200:
            guild_name = data.get("guild", {}).get("name", "Unknown")
            success(self.token, "Successfully joined guild", guild_name)
            return {
                **data,
                "success": True,
                "invite_code": invite_code
            }

        error(self.token, "Guild join failed", str(data))
        return {
            "success": False,
            "error": data,
            "invite_code": invite_code
        }