"""
以圖搜圖模組：使用 SauceNAO 進行反向圖片搜尋。
免費 99次/天；.env 設定 SAUCENAO_API_KEY 可提升至 200次/天。
"""
import asyncio
import requests
from config import SAUCENAO_API_KEY

_SAUCENAO_URL = 'https://saucenao.com/search.php'
_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0',
}


async def reverse_image_search(image_data: bytes, mime_type: str) -> str:
    """
    以圖搜圖，回傳格式化的來源清單（最多 3 筆）或錯誤訊息。
    """
    params: dict = {'output_type': 2, 'numres': 5}
    if SAUCENAO_API_KEY:
        params['api_key'] = SAUCENAO_API_KEY

    try:
        resp = await asyncio.to_thread(
            requests.post,
            _SAUCENAO_URL,
            headers=_HEADERS,
            files={'file': ('image', image_data, mime_type)},
            params=params,
            timeout=20,
        )
        resp.raise_for_status()
        results = resp.json().get('results', [])

        if not results:
            return '找不到相似圖片來源。'

        lines = []
        for r in results[:3]:
            hdr = r.get('header', {})
            dat = r.get('data', {})
            sim = float(hdr.get('similarity', 0))
            if sim < 60:
                continue
            title = (dat.get('title')
                     or dat.get('source')
                     or dat.get('creator')
                     or dat.get('material')
                     or '未知')
            urls = hdr.get('ext_urls', [])
            url = urls[0] if urls else '（無連結）'
            lines.append(f'相似度 {sim:.0f}%：{title}\n{url}')

        return '\n\n'.join(lines) if lines else '找不到相似圖片來源。'

    except requests.exceptions.Timeout:
        return '以圖搜圖逾時，請稍後再試。'
    except requests.exceptions.HTTPError as e:
        return f'以圖搜圖 HTTP 錯誤 {e.response.status_code}。'
    except Exception as e:
        return f'以圖搜圖失敗: {e}'
