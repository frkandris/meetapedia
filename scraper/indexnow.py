"""IndexNow: tell Bing (and Yandex, Seznam, Naver) which URLs changed today.

Google does not take IndexNow, and its Indexing API does not cover pages like
ours, so Google keeps finding changes through the sitemap. Bing does, and
Bing's index is what ChatGPT search and Copilot answer from — the answer
engines are the reason this exists.

Off unless `INDEXNOW_KEY` is set. The key is published at `/indexnow-key.txt`
on both domains (the file proves we own the host); IndexNow shares a submission
with every participating engine, so one POST per site reaches all of them.
"""
import re
from collections.abc import Callable, Iterable

import httpx
import structlog

log = structlog.get_logger()

ENDPOINT = "https://api.indexnow.org/indexnow"
KEY_PATH = "/indexnow-key.txt"
#: The protocol's per-request ceiling.
MAX_URLS = 10_000
_KEY_RE = re.compile(r"^[A-Za-z0-9-]{8,128}$")


def valid_key(key: str | None) -> str | None:
    key = (key or "").strip()
    return key if _KEY_RE.match(key) else None


def changed_since(entries: Iterable[tuple[str, str | None]], since_day: str) -> list[str]:
    """URLs whose sitemap lastmod is on or after `since_day` (YYYY-MM-DD).

    The sitemap's lastmod advances only on a real content change (see
    `get_community_lastmods`), so this is the set worth a recrawl — not every
    page a re-extraction touched.
    """
    return [loc for loc, lastmod in entries if lastmod and lastmod >= since_day]


async def submit(site_url: str, urls: list[str], key: str,
                 post: Callable | None = None) -> list[int]:
    """POST `urls` for one host, in protocol-sized chunks. Returns the statuses.

    200 and 202 are success (202: received, key not yet verified). A 4xx is
    logged, not raised — a refused ping must never stop the worker.
    """
    host = site_url.split("://", 1)[1]
    statuses: list[int] = []
    async with httpx.AsyncClient(timeout=30) as client:
        send = post or client.post
        for i in range(0, len(urls), MAX_URLS):
            resp = await send(ENDPOINT, json={
                "host": host, "key": key, "keyLocation": f"{site_url}{KEY_PATH}",
                "urlList": urls[i:i + MAX_URLS],
            })
            statuses.append(resp.status_code)
            if resp.status_code not in (200, 202):
                log.warning("indexnow_refused", host=host, status=resp.status_code,
                            body=resp.text[:200])
    return statuses
