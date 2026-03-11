"""
網頁抓取模組。
"""
import re
import asyncio
import requests
from bs4 import BeautifulSoup

_MAX_CONTENT_LENGTH = 2000


async def fetch_url(url: str) -> str:
    """
    非同步抓取並解析網頁文字內容。
    成功回傳清理後的文字（最多 2000 字）；
    失敗回傳以「錯誤:」開頭的說明字串。
    """
    try:
        resp = await asyncio.to_thread(
            requests.get, url, timeout=(5, 15),
            headers={"User-Agent": "Mozilla/5.0"}
        )
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, 'html.parser')
        for tag in soup(['script', 'style', 'noscript']):
            tag.decompose()

        elems = soup.find_all(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'a', 'span'])
        text = '\n'.join(e.get_text(separator=' ', strip=True) for e in elems)
        cleaned = re.sub(r'\s+', ' ', text).strip()

        return cleaned[:_MAX_CONTENT_LENGTH]

    except requests.exceptions.Timeout:
        print(f"❌ 請求逾時: {url}")
        return "錯誤: 請求逾時，網頁無回應"
    except requests.exceptions.HTTPError as e:
        print(f"❌ HTTP 錯誤 {e.response.status_code}: {url}")
        return f"錯誤: HTTP {e.response.status_code}"
    except requests.exceptions.RequestException as e:
        print(f"❌ 抓取失敗 {url}: {e}")
        return f"錯誤: 無法訪問該網頁 ({e})"
    except Exception as e:
        print(f"❌ 解析失敗 {url}: {e}")
        return f"錯誤: 解析網頁內容時發生問題 ({e})"
