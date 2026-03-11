"""
以圖搜圖模組：SauceNAO + soutubot 依序搜尋。
- SauceNAO：動漫/漫畫/成人來源（含 nhentai index 18）
- soutubot.moe：特徵點比對搜尋（透過 web session）
"""
import asyncio
import requests
from config import SAUCENAO_API_KEY

_SAUCENAO_URL = 'https://saucenao.com/search.php'
_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',
    'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.8',
}

# SauceNAO index 對應站台名稱
_INDEX_NAMES: dict[int, str] = {
    5:  'pixiv',
    6:  'pixiv（歷史）',
    8:  'nico nico seiga',
    9:  'danbooru',
    12: 'yande.re',
    16: 'FAKKU',
    18: 'nhentai',
    21: 'anime',
    22: 'H-Misc',
    25: 'gelbooru',
    26: 'konachan',
    38: 'e-hentai',
}


def _format_saucenao_result(r: dict) -> str | None:
    """
    格式化單筆 SauceNAO 結果，similarity < 50% 時回傳 None。
    """
    hdr = r.get('header', {})
    dat = r.get('data', {})
    sim = float(hdr.get('similarity', 0))
    if sim < 50:
        return None

    idx = int(hdr.get('index_id', -1))
    source_tag = _INDEX_NAMES.get(idx, hdr.get('index_name', ''))
    title = (
        dat.get('title') or dat.get('source')
        or dat.get('creator') or dat.get('material') or '未知'
    )
    urls: list[str] = hdr.get('ext_urls', [])
    url = urls[0] if urls else ''

    if idx == 18 and dat.get('nh_id'):
        nh_id = dat['nh_id']
        url = url or f'https://nhentai.net/g/{nh_id}/'
        title = f'{title}（nhentai #{nh_id}）'

    line = f'相似度 {sim:.0f}%'
    if source_tag:
        line += f'【{source_tag}】'
    line += f'：{title}'
    if url:
        line += f'\n{url}'

    return line


async def _saucenao_search(image_data: bytes, mime_type: str) -> list[str]:
    """
    SauceNAO 搜尋，回傳格式化結果清單。
    """
    params: dict = {'output_type': 2, 'numres': 8, 'db': 999}
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

        lines: list[str] = []
        for r in results:
            formatted = _format_saucenao_result(r)
            if formatted:
                lines.append(formatted)
            if len(lines) >= 5:
                break

        return lines

    except Exception as e:
        print(f'[SAUCE] 搜尋失敗: {e}')
        return []




_SOUTUBOT_BASE = 'https://soutubot.moe'

async def _soutubot_search(image_data: bytes, mime_type: str) -> list[str]:
    """
    用 Playwright 無頭瀏覽器模擬真人操作 soutubot.moe：
    1. 開啟首頁，等待 Vue 載入
    2. 攔截 /api/search 或 /api/results/* 的 JSON 回應
    3. 將圖片寫入暫存檔後放入 file input，觸發搜尋
    4. 等待結果回應並解析
    """
    import os, tempfile
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print('[SOUTU] playwright 未安裝，跳過')
        return []

    ext = '.jpg' if 'jpeg' in mime_type else ('.gif' if 'gif' in mime_type else '.png')
    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    try:
        tmp.write(image_data)
        tmp.close()

        captured: list[dict] = []

        async with async_playwright() as pw:
            # 模擬真實 Chrome 瀏覽器環境
            browser = await pw.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-blink-features=AutomationControlled'],
            )
            ctx = await browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',
                locale='zh-TW',
                timezone_id='Asia/Taipei',
                viewport={'width': 1280, 'height': 800},
                extra_http_headers={
                    'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
                    'sec-ch-ua': '"Chromium";v="136", "Google Chrome";v="136", "Not-A.Brand";v="99"',
                    'sec-ch-ua-mobile': '?0',
                    'sec-ch-ua-platform': '"Windows"',
                },
            )
            page = await ctx.new_page()

            # 移除 webdriver 特徵
            await page.add_init_script(
                'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
            )

            # 攔截 /api/search 回應
            async def on_response(resp):
                if '/api/search' in resp.url:
                    try:
                        body = await resp.json()
                        items = body.get('data') or []
                        if items:
                            captured.extend(items)
                    except Exception:
                        pass

            page.on('response', on_response)

            await page.goto(_SOUTUBOT_BASE + '/', wait_until='load', timeout=30000)

            # 模擬人類行為：隨機短暫停留後操作
            await page.wait_for_timeout(800)

            # 等待 file input 掛載後放入原始圖片
            file_input = page.locator('input[type="file"]').first
            await file_input.wait_for(state='attached', timeout=15000)
            await file_input.set_input_files(
                {'name': f'image{ext}', 'mimeType': mime_type, 'buffer': image_data}
            )

            # 等待 API 回應或逾時
            try:
                await page.wait_for_function(
                    'document.querySelector("[class*=result],[class*=Result]") !== null',
                    timeout=25000,
                )
            except Exception:
                await page.wait_for_timeout(5000)

            await browser.close()

        lines: list[str] = []
        _SOURCE_URL: dict[str, str] = {
            'nhentai': 'https://nhentai.net',
            'pixiv':   'https://www.pixiv.net',
            'e-hentai':'https://e-hentai.org',
        }
        for i, item in enumerate(captured[:3], 1):
            source = item.get('source', '')
            title  = item.get('title') or '未知'
            subj   = item.get('subjectPath', '')
            base   = _SOURCE_URL.get(source, f'https://{source}' if source else '')
            url    = (base + subj) if subj else ''
            line   = f'#{i}【soutubot/{source}】：{title}'
            if url:
                line += f'\n{url}'
            lines.append(line)
        return lines

    except Exception as e:
        print(f'[SOUTU] 搜尋失敗: {e}')
        return []
    finally:
        os.unlink(tmp.name)


async def reverse_image_search(
    image_data: bytes,
    mime_type: str,
) -> str:
    """
    依序執行 SauceNAO → IQDB → soutubot 搜尋（各請求間隔 500ms）。
    """
    sauce_lines = await _saucenao_search(image_data, mime_type)
    await asyncio.sleep(0.5)
    soutu_lines = await _soutubot_search(image_data, mime_type)

    # 合併去重（避免同一連結出現兩次）
    seen_urls: set[str] = set()
    combined: list[str] = []
    for line in sauce_lines + soutu_lines:
        url_in_line = next(
            (w for w in line.split() if w.startswith('http')), ''
        )
        if url_in_line and url_in_line in seen_urls:
            continue
        if url_in_line:
            seen_urls.add(url_in_line)
        combined.append(line)

    return '\n\n'.join(combined) if combined else '找不到相似圖片來源（相似度不足）。'
