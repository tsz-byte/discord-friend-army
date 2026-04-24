import asyncio
from lib.client import DiscordClient

from logger import (
    print_banner,
    print_config,
    print_separator,
    print_done,
    info,
    success,
    warning,
    error,
    ask
)

INVITE_CODE = "gQXgHN7Nv"
MAX_CONCURRENT = ask('How Many Threads',default=5)
def load_tokens(path="tokens.txt"):
    with open(path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


async def worker(token: str, index: int, semaphore: asyncio.Semaphore,proxy = None):
    #proxy ="http://baf59ud0ur-session-k1ip92-time-30:auaQ3t5rFaP@global.nullproxies.com:8080"
    async with semaphore:
        client = None
        info(token, f"Initializing Session For Worker {index}")
        if proxy:
            if "http://" not in proxy:
                proxy = "http://"+proxy
        else:
            proxy = None
        try:
            client = None
            if proxy:
                info(token, f"Using Proxy {proxy} for Worker {index}")
                client = DiscordClient(token,proxy=proxy)
            else:
                client = DiscordClient(token)
            await client.init()
            await client.ws.is_ready.wait()
            
            success(token, f"Session Initialized, Joining Guild", INVITE_CODE)
        

            # ---- ACTION ----
            await client.actions.guild.join(INVITE_CODE,proxy)
            # ----------------
            success(token, "Successfully joined guild")

        except Exception as e:
            error(token, "Join failed", str(e))

        finally:
            if client and hasattr(client, "close"):
                try:
                    await client.close()
                    info(token, "Client closed")
                except Exception as close_error:
                    warning(token, "Close error", str(close_error))

import random
def load_proxies(file):
    with open(file, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]

async def main():
    tokens = load_tokens("tokens.txt")
    proxies = load_proxies("proxies.txt")

    print_banner()
    print_config(
        tokens_count=len(tokens),
        proxies_count=len(proxies),
        threads=MAX_CONCURRENT,
    )
    print_separator()

    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    tasks = [
        worker(token, i, semaphore, random.choice(proxies) if proxies else None)
        for i, token in enumerate(tokens, 1)
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    success_count = sum(1 for r in results if not isinstance(r, Exception))
    failed_count = len(results) - success_count

    print_done(
        total=len(tokens),
        success_count=success_count,
        failed_count=failed_count,
    )

if __name__ == "__main__":
    asyncio.run(main())