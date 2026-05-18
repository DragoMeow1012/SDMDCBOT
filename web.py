"""
網頁抓取模組：用 aiohttp 做真正的非同步抓取 + BeautifulSoup 解析。
"""
import asyncio
import re

import aiohttp
from bs4 import BeautifulSoup

_MAX_CONTENT_LENGTH = 2000
_HEADERS = {"User-Agent": "Mozilla/5.0"}
_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=5)

_ELEMENTS = ('p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'a', 'span')

# Module-level shared session：避免每次 fetch_url 都做 TCP/TLS 握手。
# Connection pool 也讓打同個 host 多次時直接重用連線。
_session: aiohttp.ClientSession | None = None
_session_lock = asyncio.Lock()


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        async with _session_lock:
            if _session is None or _session.closed:
                _session = aiohttp.ClientSession(timeout=_TIMEOUT, headers=_HEADERS)
    return _session


def _parse(html: str) -> str:
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(['script', 'style', 'noscript']):
        tag.decompose()
    text = '\n'.join(
        e.get_text(separator=' ', strip=True)
        for e in soup.find_all(_ELEMENTS)
    )
    return re.sub(r'\s+', ' ', text).strip()[:_MAX_CONTENT_LENGTH]


async def fetch_url(url: str) -> str:
    """抓並解析網頁；成功回清理後文字，失敗回「錯誤: ...」。"""
    if 'nhentai.net' in url:
        return '錯誤: 不支援抓取此網站'

    try:
        session = await _get_session()
        async with session.get(url) as resp:
            resp.raise_for_status()
            html = await resp.text()

        # HTML 解析交給 thread pool，避免阻塞 event loop
        return await asyncio.to_thread(_parse, html)

    except asyncio.TimeoutError:
        print(f"❌ 請求逾時: {url}")
        return "錯誤: 請求逾時，網頁無回應"
    except aiohttp.ClientResponseError as e:
        print(f"❌ HTTP 錯誤 {e.status}: {url}")
        return f"錯誤: HTTP {e.status}"
    except aiohttp.ClientError as e:
        print(f"❌ 抓取失敗 {url}: {e}")
        return f"錯誤: 無法訪問該網頁 ({e})"
    except Exception as e:
        print(f"❌ 解析失敗 {url}: {e}")
        return f"錯誤: 解析網頁內容時發生問題 ({e})"
