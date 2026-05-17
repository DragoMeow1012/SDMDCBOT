"""
漫畫翻譯（manga-image-translator API server 接後端）。

對外：translate_image(image_bytes, mime, target_lang) -> bytes  (PNG)

server 端啟動：
    cd <manga-image-translator>/server && python main.py --use-gpu
URL 由 config.MANGA_TRANSLATOR_URL 控制（預設 http://127.0.0.1:8001）。

Gemini 翻譯後端的 API Key 由 server 進程的環境變數 GOOGLE_API_KEY 提供，
bot 端不參與 key 管理。

注意：用 streaming endpoint `/translate/with-form/image/stream` 而不是同步版本。
upstream 的 `/translate/with-form/image` 走 server/sent_data_internal.py:fetch_data，
裡面寫死 json.loads 解析 worker 回傳的 pickle bytes，永遠 fail → 包成 500 給 client。
streaming 端點走 fetch_data_stream → process_stream，正確處理 binary chunks。

stream chunk 格式（server/streaming.py）：
    1 byte status | 4 byte big-endian size | N bytes data
    status: 0=最終結果, 1=進度文字, 2=錯誤, 3=排隊位置, 4=等待 worker
"""
import asyncio
import io
import json
import time

import aiohttp
from PIL import Image

from config import (
    MANGA_TRANSLATOR_BACKEND,
    MANGA_TRANSLATOR_CONCURRENCY,
    MANGA_TRANSLATOR_URL,
)

# Bot 端 in-flight 上限。Pipeline 模式下 server 真的會 overlap GPU/LLM/GPU 三 stage，
# 設高才能餵滿 server。Server 端 gpu_sem(1) + llm_sem(N) 自己會排隊，bot 設多大都不會炸 GPU。
# 預設 8 = 1 pre + 4 LLM + 1 post + 2 buffer，30 張 manga 走完約 4-5 分鐘。
_WORKER_SEM = asyncio.Semaphore(MANGA_TRANSLATOR_CONCURRENCY)

# 中文人類名 → manga-image-translator 用的 ISO 代碼
_LANG_CODE: dict[str, str] = {
    '繁體中文': 'CHT',
    '簡體中文': 'CHS',
    'English':  'ENG',
    '日本語':   'JPN',
    '한국어':   'KOR',
}

# 單張 timeout 拉短到 180s（3 分鐘）：與其無限等慢圖，不如標記失敗、最後集中重試。
# 失敗清單會在 commands/translate.py 走第二輪 retry（仍超時就放棄那張）。
_PER_IMAGE_TIMEOUT = 180
_TIMEOUT = aiohttp.ClientTimeout(total=_PER_IMAGE_TIMEOUT)

# Worker 冷啟動時 orchestrator 還沒收到 worker /register（503）或 worker 在載模型（500 + 'starting up'）。
# 第一張圖必中招——退避重試讓 user 感覺不到。12 × 5s = 最多等 60s 暖機，超過視為真錯。
_WARMUP_RETRY_MAX = 12
_WARMUP_RETRY_DELAY = 5.0

# OpenCV cv::remap 用 16-bit signed indices，SHRT_MAX=32767。manga-translator pipeline
# 內部 resize 過程容易讓任一維度撞到上限（特別是長條 webtoon），會 raise:
#   OpenCV(...) error: (-215:Assertion failed) dst.cols < SHRT_MAX && ... in function 'cv::remap'
# 客戶端先把超大圖等比縮到 _MAX_IMG_DIM 內再送 server，避免這條 assertion。
#
# 12000 是相對 SHRT_MAX（32767）有 2.7x 緩衝的高解析度上限：
# - 普通漫畫頁 < 4000px → 完全不觸發
# - 長條 webtoon（高 20000-30000px）→ 縮到 12000，文字仍清晰可讀
# 太低（如 4000-8000）會犧牲 webtoon 的文字解析度。
_MAX_IMG_DIM = 12000

# 下限：低於這個就放大。manga-translator 的 inpainter 邏輯是「只縮不放」，
# 小圖（如 600x864）inpainter 直接在原解析度跑 → 文字渲染像素化／糊。
# 客戶端預先放大到 _MIN_IMG_DIM 強制 inpainter 在合理解析度上跑。
# 1280 = 比原始小圖大 ~1.3-1.5x，不會劇烈失真；inpainter 在 1280 跑字夠清楚。
# 之前用 2048 對 867x1024 等小圖直接 2x 放大，過度。
_MIN_IMG_DIM = 1280


def _maybe_resize(image_bytes: bytes, mime: str) -> tuple[bytes, str, int]:
    """
    把圖縮到 [_MIN_IMG_DIM, _MAX_IMG_DIM] 範圍：
    - max_dim > _MAX_IMG_DIM → 等比縮（避免 OpenCV cv::remap 撞 SHRT_MAX）。
    - max_dim < _MIN_IMG_DIM → 等比放大（避免 inpainter 在低解析度跑導致字糊）。
    - 區間內 → 不動。
    回傳 (bytes, mime, 處理後 max_dim)。讀圖失敗回傳原圖。
    """
    try:
        with Image.open(io.BytesIO(image_bytes)) as im:
            w, h = im.size
            cur_max = max(w, h)
            if _MIN_IMG_DIM <= cur_max <= _MAX_IMG_DIM:
                return image_bytes, mime, cur_max
            if cur_max > _MAX_IMG_DIM:
                scale = _MAX_IMG_DIM / cur_max
                action = '縮小'
            else:
                scale = _MIN_IMG_DIM / cur_max
                action = '放大（避免字糊）'
            new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
            print(f'[TRANSLATE] 圖片 {w}x{h} {action}到 {new_size[0]}x{new_size[1]}')
            resized = im.convert('RGB').resize(new_size, Image.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, format='PNG')
            return buf.getvalue(), 'image/png', max(new_size)
    except Exception as e:
        print(f'[TRANSLATE] resize 失敗，沿用原圖: {type(e).__name__}: {e}')
        return image_bytes, mime, 0


def _build_config_payload(target_lang_code: str) -> str:
    """
    產出 manga-translator config JSON。各參數調校 rationale 看底下註解。
    重點是「不漏字」。detector/OCR 拉低門檻多撈一點，
    寧可多幾個雜訊 box（gemini 看圖會把雜訊修正成空字串自然濾掉），也不要漏對話。
    """
    return json.dumps({
        'translator': {
            'target_lang': target_lang_code,
            'translator': MANGA_TRANSLATOR_BACKEND,
            # 0：第一次譯文驗證沒過直接放行；retry 會多打一輪 5K-token prompt 浪費時間。
            'post_check_max_retry_attempts': 0,
            # 完全關掉 post-translation check：page-level target lang ratio 檢查每張多 500ms，
            # 即使檢查失敗也只是 warning，不影響輸出，純粹浪費時間。
            'enable_post_translation_check': False,
        },
        'detector': {
            'detector': 'default',
            # 1536：高解析度漫畫頁 (>3MB) 細字密集多框，1024 會把整頁縮太小漏抓。
            # 1280 對普通頁夠，但成人向密字頁需要 1536 才能不漏細小對話框。
            # Detection 時間從 1024 → 1536 約多 1.5x，但 GPU 階段不是瓶頸（K=10 並發）。
            'detection_size': 1536,
            'unclip_ratio': 1.2,
            # 0.15 → 0.20：稍拉高過濾 false positive，但不到 0.25 避免漏框。
            'text_threshold': 0.20,
            'box_threshold': 0.20,
            'det_invert': False,
            'det_auto_rotate': False,
            'det_rotate': False,
            'det_gamma_correct': False,
        },
        'ocr': {
            # 此 fork 只保留 mocr（48px/48px_ctc 已移除）。
            # mocr GPU 上每框 ~0.05-0.1s，18 框約 1-2s，瓶頸主要在 LLM 不在 OCR。
            'ocr': 'mocr',
            'use_mocr_merge': False,
            'min_text_length': 2,    # 1 → 2：丟單字噪訊（'!'、'?' 之類），少 OCR call
            'prob': 0.2,             # 0.05 → 0.2：拉高過濾，少 region
            'ignore_bubble': 0,
        },
        'inpainter': {
            'inpainter': 'lama_mpe',
            # 1792 → 1280：LaMa 推理時間 ~ dim²，1280 比 1792 快 ~50%。
            # 白底紋理單純的對話框幾乎看不出差別；複雜紋理場景才需要 1792+。
            'inpainting_size': 1280,
            'inpainting_precision': 'bf16',
        },
        'render': {
            # offset=0：fit_check 已找出能塞進 bbox 的最大值，再 +offset 會直接溢框、字看起來「怪」。
            # 想填滿氣泡靠的是 fit_check 把 size 拉到 bbox 上限，不是 offset。
            'font_size_offset': 0,
            # 14：中文最小可讀字級。-1（自動）對大圖會給更大值但小 bbox 會被夾大反而醜。
            'font_size_minimum': 14,
            # 字色：留空 = OCR per-region 自動偵測前景／背景（黑底白字、彩色字各自處理）。
            'no_hyphenation': True,
            # 多行各行頭對齊；改 center 對非對稱 bbox 會出現上下偏移看起來歪。
            'alignment': 'left',
        },
    })


async def _read_stream(resp: aiohttp.ClientResponse) -> bytes:
    """
    從 streaming endpoint 累積 chunk 直到拿到最終結果（status 0）或錯誤（status 2）。
    chunk 格式：1 byte status | 4 byte big-endian size | N bytes data
    其他 status（progress / queue pos / waiting）只用來印 log。
    """
    buffer = b''
    async for chunk in resp.content.iter_any():
        if not chunk:
            continue
        buffer += chunk
        while len(buffer) >= 5:
            status = buffer[0]
            size = int.from_bytes(buffer[1:5], 'big')
            if len(buffer) < 5 + size:
                break  # 等下一個 chunk
            payload = buffer[5:5 + size]
            buffer = buffer[5 + size:]

            if status == 0:
                return payload  # 最終 PNG bytes
            if status == 2:
                raise RuntimeError(f'manga-translator worker error: {payload.decode("utf-8", errors="replace")[:300]}')
            if status == 1:
                try:
                    print(f'[TRANSLATE]   進度: {payload.decode("utf-8", errors="replace")[:120]}')
                except Exception:
                    pass
            elif status == 3:
                print(f'[TRANSLATE]   排隊位置: {payload.decode("utf-8", errors="replace")}')
            elif status == 4:
                print('[TRANSLATE]   等待 worker 取件...')
    raise RuntimeError('manga-translator stream 結束但未收到結果')


async def translate_image(image_bytes: bytes, mime: str, target_lang: str) -> bytes:
    """
    把漫畫圖丟到 manga-image-translator server 翻譯，回傳翻譯後 PNG bytes。

    走舊的 streaming endpoint /translate/with-form/image/stream，worker 端用 gpu_lock
    在進程內讓 GPU 階段排隊、LLM 階段重疊。bot 側只要送 K 張並行（K = MANGA_TRANSLATOR_CONCURRENCY），
    server 自己處理 overlap。

    target_lang：接受 _LANG_CODE 內中文鍵名，未知預設 CHT。
    """
    image_bytes, mime, _ = await asyncio.to_thread(_maybe_resize, image_bytes, mime)
    code = _LANG_CODE.get(target_lang, 'CHT')
    config_payload = _build_config_payload(code)
    url = f'{MANGA_TRANSLATOR_URL.rstrip("/")}/translate/with-form/image/stream'
    size_kb = len(image_bytes) / 1024

    queued_at = time.monotonic()
    if _WORKER_SEM.locked():
        print(f'[TRANSLATE] in-flight 滿（上限 {MANGA_TRANSLATOR_CONCURRENCY}），排隊中... ({size_kb:.0f}KB, {code})')
    async with _WORKER_SEM:
        waited = time.monotonic() - queued_at
        print(f'[TRANSLATE] 送出 ({size_kb:.0f}KB, {code}'
              f'{f", 等待 {waited:.1f}s" if waited > 1 else ""})')
        started = time.monotonic()

        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            for attempt in range(_WARMUP_RETRY_MAX):
                try:
                    form = aiohttp.FormData()
                    form.add_field('image', image_bytes, filename='page.png',
                                   content_type=mime or 'image/png')
                    form.add_field('config', config_payload)
                    async with session.post(url, data=form) as resp:
                        if resp.status != 200:
                            body = await resp.text()
                            elapsed = time.monotonic() - started
                            print(f'[TRANSLATE] 失敗 ({elapsed:.1f}s) HTTP {resp.status}: {body[:200]}')
                            raise RuntimeError(
                                f'manga-translator HTTP {resp.status}: {body[:300]}')
                        data = await _read_stream(resp)
                        elapsed = time.monotonic() - started
                        print(f'[TRANSLATE] 完成 ({elapsed:.1f}s, 回傳 {len(data)/1024:.0f}KB)')
                        return data
                except asyncio.TimeoutError:
                    elapsed = time.monotonic() - started
                    print(f'[TRANSLATE] 超時 ({elapsed:.1f}s > {_PER_IMAGE_TIMEOUT}s)，跳過這張')
                    raise RuntimeError(f'這張圖翻譯超過 {_PER_IMAGE_TIMEOUT} 秒，已跳過')
                except RuntimeError as e:
                    msg = str(e).lower()
                    if any(k in msg for k in ('starting up', 'no executor')) and attempt < _WARMUP_RETRY_MAX - 1:
                        print(f'[TRANSLATE] worker 暖機中，{_WARMUP_RETRY_DELAY:.0f}s 後重試 '
                              f'({attempt + 1}/{_WARMUP_RETRY_MAX})')
                        await asyncio.sleep(_WARMUP_RETRY_DELAY)
                        continue
                    raise
        raise RuntimeError('translate_image: unreachable')
