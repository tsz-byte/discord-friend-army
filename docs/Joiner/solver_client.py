import requests
import time
import random
from urllib.parse import urlparse

class Solver:
    def __init__(self, url, sitekey, rqdata="", user_agent="", proxy=None, screen_width=None, screen_height=None, window_width=None, window_height=None):
        """
        :param url: Target website URL
        :param sitekey: hCaptcha sitekey
        :param rqdata: Optional request data
        :param user_agent: User agent string
        :param proxy: "user:password@host:port" or "http://user:password@host:port"
        :param screen_width: Screen width in pixels (optional)
        :param screen_height: Screen height in pixels (optional)
        :param window_width: Window width in pixels (optional)
        :param window_height: Window height in pixels (optional)
        """
        self.url = url
        self.sitekey = sitekey
        self.rqdata = rqdata
        self.user_agent = user_agent
        self.proxy = proxy
        self.screen_width = screen_width
        self.screen_height = screen_height
        self.window_width = window_width
        self.window_height = window_height
        self.server_url = "http://localhost:5001"

    def _parse_proxy(self):
        """Parse proxy string into srv, usr, pw components"""
        if not self.proxy:
            return {}

        raw = self.proxy

        # Add http:// if no scheme (for urlparse to work properly)
        if "://" not in raw:
            raw = "http://" + raw

        try:
            parsed = urlparse(raw)
        except Exception as e:
            print(f"[Proxy Parser] Invalid proxy({self.proxy}): {e}")
            return {}

        result = {}

        # hostname:port
        if parsed.hostname and parsed.port:
            result["srv"] = f"{parsed.hostname}:{parsed.port}"

        # username
        if parsed.username:
            result["usr"] = parsed.username

        # password
        if parsed.password:
            result["pw"] = parsed.password

        return result

    def solve(self, timeout=300, poll_interval=1):
        """
        Send GET /solve with url, sitekey, and proxy info to initiate solving task
        Poll for results until success, failure, or timeout
        """
        # Extract host from URL for the server
        host = self.url
        if "://" in self.url:
            host = self.url.split("://")[-1].split("/")[0]
        
        params = {
            "url": self.url,
            "host": host,
            "sitekey": self.sitekey,
            "rqdata": self.rqdata,
            "user_agent": self.user_agent,
        }

        # Add screen dimensions if provided
        if self.screen_width is not None:
            params["screen_width"] = self.screen_width
        if self.screen_height is not None:
            params["screen_height"] = self.screen_height
        if self.window_width is not None:
            params["window_width"] = self.window_width
        if self.window_height is not None:
            params["window_height"] = self.window_height

        # Parse proxy and add srv/usr/pw parameters
        proxy_params = self._parse_proxy()
        params.update(proxy_params)

        # Debug output
        print("[Solver] Sending parameters to server:")
        for k, v in params.items():
            print(f"  {k}: {v}")

        sess = requests.Session()

        # Set User-Agent for session
        if self.user_agent:
            sess.headers.update({"User-Agent": self.user_agent})

        # --- Initiate Solve ---
        try:
            resp = sess.get(f"{self.server_url}/solve", params=params, timeout=30)
        except Exception as e:
            print(f"Error initiating solve: {e}")
            return None, None

        if resp.status_code != 200:
            body = resp.text if hasattr(resp, "text") else "<no body>"
            print(f"Error initiating solve: {resp.status_code} - {body}")
            return None, None

        # Parse JSON response
        try:
            data = resp.json()
        except Exception as e:
            print(f"Invalid JSON from server when initiating solve: {e}")
            return None, None

        taskid = data.get("taskid")
        if not taskid:
            print(f"Error: No taskid received - {data}")
            return None, None

        print(f"Task initiated: {taskid}")

        # --- Poll for Results ---
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                poll_resp = sess.get(f"{self.server_url}/task/{taskid}", timeout=30)
            except Exception as e:
                print(f"Error checking task: {e}")
                time.sleep(poll_interval + random.uniform(0, 0.5))
                continue

            if poll_resp.status_code != 200:
                body = poll_resp.text if hasattr(poll_resp, "text") else "<no body>"
                print(f"Error checking task: {poll_resp.status_code} - {body}")
                time.sleep(poll_interval + random.uniform(0, 0.5))
                continue

            try:
                data = poll_resp.json()
            except Exception as e:
                print(f"Invalid JSON while polling task: {e}")
                time.sleep(poll_interval + random.uniform(0, 0.5))
                continue

            status = data.get("status")
            uuid = data.get("uuid")
            cookies = data.get("cookies", {})

            if status == "success":
                print(f"Task {taskid} solved successfully")
                return uuid, cookies

            if status == "failed":
                error = data.get("error", "Unknown error")
                print(f"Task {taskid} failed: {error}")
                return None, None

            if status == "not_found":
                print(f"Task {taskid} not found")
                return None, None

            # Still processing
            print(f"Task {taskid} status: {status} - Waiting...")
            time.sleep(poll_interval + random.uniform(0, 0.5))

        print(f"Timeout reached for task {taskid}")
        return None, None


# Example usage
if __name__ == "__main__":
    solver = Solver(
        url="https://accounts.hcaptcha.com/demo",
        sitekey="a9b5fb07-92ff-493f-86fe-352a2803b3df",
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        # Optional: provide custom dimensions
        screen_width=1920,
        screen_height=1080,
        window_width=1536,
        window_height=864,
        # Optional: provide proxy
        # proxy="username:password@proxy.example.com:8080"
    )
    
    token, cookies = solver.solve(timeout=300, poll_interval=2)
    
    if token:
        print(f"\n✅ Success! Token: {token}")
        if cookies:
            print(f"Cookies: {cookies}")
    else:
        print("\n❌ Failed to solve captcha")