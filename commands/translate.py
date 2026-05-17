"""
圖片文字翻譯指令：/translate-img

兩種使用模式：
1. 帶 圖片1..10：直接翻譯，回傳 Discord 圖片附件（最多 10 張）。
2. 帶 壓縮檔（zip/cbz/rar/cbr）：解壓出全部圖片翻譯，回傳翻譯後的壓縮檔，
   突破 Discord 單訊息 10 張附件上限。

RAR 解壓需要系統有 UnRAR.exe（WinRAR 內建或從 rarlab.com 下載）。
找不到 UnRAR.exe 時 RAR 檔會回明確錯誤，zip/cbz 不受影響。

並行上限由 manga_translate 模組裡的 asyncio.Semaphore(MANGA_TRANSLATOR_CONCURRENCY) 控管。
"""
import asyncio
import io
import os
import time
import traceback
import zipfile

import aiohttp
import discord
from discord import app_commands

from manga_translate import translate_image

# RAR 支援：rarfile 純 Python 套件 + 外部 UnRAR.exe 二進位。
# import 失敗 / 找不到 binary 會降級為「RAR 不支援」，zip 路徑不受影響。
try:
    import rarfile
    # Windows WinRAR 預設安裝路徑；若 user 自行裝在別處可用 env override。
    _UNRAR_CANDIDATES = [
        os.environ.get('UNRAR_TOOL', ''),
        r'C:\Program Files\WinRAR\UnRAR.exe',
        r'C:\Program Files (x86)\WinRAR\UnRAR.exe',
        'unrar',  # PATH 裡的 unrar
        'unrar.exe',
    ]
    for _cand in _UNRAR_CANDIDATES:
        if _cand and (os.path.isfile(_cand) or _cand in ('unrar', 'unrar.exe')):
            rarfile.UNRAR_TOOL = _cand
            break
    _RAR_AVAILABLE = True
except ImportError:
    rarfile = None  # type: ignore
    _RAR_AVAILABLE = False


# Discord 檔案上限隨 boost level 浮動（free 8MB / boosted 50/100MB / Nitro DM 25MB）。
# `Guild.filesize_limit` 給確切值；DM 或讀不到 fallback 8MB（最保守）。
# 留 10% 緩衝避免邊緣 413（實測 14.5MB 在某些 server 也會 413）。
# 超過上限或 413 → 改傳 litterbox.catbox.moe（暫存 72h），Discord 只送下載連結。
_DISCORD_FILE_LIMIT_FALLBACK_MB = 8.0

# litterbox.catbox.moe：catbox.moe 的暫存版本，無 API key 無註冊、單檔 ≤1GB。
# 過期時間可選 1h / 12h / 24h / 72h；72h 給漫畫批次最寬鬆。
_LITTERBOX_URL = 'https://litterbox.catbox.moe/resources/internals/api.php'
_LITTERBOX_RETENTION = '72h'

_MIME_BY_EXT: dict[str, str] = {
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.png': 'image/png', '.webp': 'image/webp',
    '.gif': 'image/gif', '.bmp': 'image/bmp',
}


_LANG_CHOICES = [
    app_commands.Choice(name='繁體中文', value='繁體中文'),
    app_commands.Choice(name='簡體中文', value='簡體中文'),
    app_commands.Choice(name='English', value='English'),
    app_commands.Choice(name='日本語', value='日本語'),
    app_commands.Choice(name='한국어', value='한국어'),
]


def _extract_images_from_zip(zip_bytes: bytes) -> list[tuple[str, bytes, str]]:
    """
    解壓 zip/cbz，找出圖片檔。回傳 [(filename, bytes, mime), ...]，依檔名排序。
    BadZipFile 抛 ValueError；個別檔解壓失敗 print 跳過。
    """
    out: list[tuple[str, bytes, str]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            # 過濾資料夾項目、排序確保翻譯順序穩定
            names = sorted(n for n in zf.namelist() if not n.endswith('/'))
            for n in names:
                ext = os.path.splitext(n)[1].lower()
                if ext not in _MIME_BY_EXT:
                    continue
                try:
                    data = zf.read(n)
                    if not data:
                        continue
                    out.append((os.path.basename(n), data, _MIME_BY_EXT[ext]))
                except Exception as e:
                    print(f'[TRANSLATE-ZIP] 解 {n} 失敗，跳過: {type(e).__name__}: {e}')
    except zipfile.BadZipFile:
        raise ValueError('壓縮檔格式錯誤或損毀（請確認是 zip/cbz）')
    return out


def _extract_images_from_rar(rar_bytes: bytes) -> list[tuple[str, bytes, str]]:
    """
    解壓 rar/cbr，找出圖片檔。需要 rarfile 套件 + 系統 UnRAR.exe。
    rarfile 拿到 BytesIO 會 spool 到 tempfile（unrar 需要實體檔），自動處理。
    """
    if not _RAR_AVAILABLE:
        raise ValueError('沒裝 rarfile 套件喵：pip install rarfile')
    out: list[tuple[str, bytes, str]] = []
    try:
        with rarfile.RarFile(io.BytesIO(rar_bytes)) as rf:
            names = sorted(n for n in rf.namelist() if not n.endswith('/'))
            for n in names:
                ext = os.path.splitext(n)[1].lower()
                if ext not in _MIME_BY_EXT:
                    continue
                try:
                    data = rf.read(n)
                    if not data:
                        continue
                    out.append((os.path.basename(n), data, _MIME_BY_EXT[ext]))
                except Exception as e:
                    print(f'[TRANSLATE-RAR] 解 {n} 失敗，跳過: {type(e).__name__}: {e}')
    except rarfile.RarCannotExec as e:
        raise ValueError(
            f'找不到 UnRAR.exe 喵：{e}。請裝 WinRAR 或從 rarlab.com 下載 unrar 並放到 PATH，'
            f'或設 env UNRAR_TOOL=完整路徑'
        )
    except rarfile.BadRarFile:
        raise ValueError('RAR 格式錯誤或損毀')
    except rarfile.NeedFirstVolume:
        raise ValueError('RAR 是分卷壓縮（part1.rar 等），請傳完整單檔')
    except rarfile.PasswordRequired:
        raise ValueError('RAR 有密碼保護，無法解開')
    return out


def _extract_images_from_archive(
    archive_bytes: bytes, filename: str,
) -> list[tuple[str, bytes, str]]:
    """依附檔名分派 zip/rar 解壓。未知格式預設當 zip 試（cbz/cbr 也走這條 dispatch）。"""
    ext = os.path.splitext(filename)[1].lower()
    if ext in ('.rar', '.cbr'):
        return _extract_images_from_rar(archive_bytes)
    # 預設 zip（含 .zip / .cbz / 沒附檔名 等）
    return _extract_images_from_zip(archive_bytes)


def _detect_format_ext(image_bytes: bytes) -> str:
    """從 magic header 判副檔名（含點）。對應 server 端 mirror input format 的輸出。"""
    if not image_bytes or len(image_bytes) < 12:
        return '.png'
    if image_bytes[:3] == b'\xff\xd8\xff':
        return '.jpg'
    if image_bytes[:8].startswith(b'\x89PNG'):
        return '.png'
    if image_bytes[:4] == b'RIFF' and image_bytes[8:12] == b'WEBP':
        return '.webp'
    if image_bytes[:6] in (b'GIF87a', b'GIF89a'):
        return '.gif'
    if image_bytes[:2] == b'BM':
        return '.bmp'
    return '.png'


async def _upload_to_litterbox(zip_bytes: bytes, filename: str) -> str | None:
    """
    丟到 litterbox.catbox.moe（catbox 暫存版）。成功回傳 download URL，失敗回 None。
    無 API key、無註冊；reqtype=fileupload + time={1h,12h,24h,72h} 三 form 欄位即可。
    超時設 120s 給大檔上傳餘裕（30 張漫畫 ~30-50MB）。
    """
    form = aiohttp.FormData()
    form.add_field('reqtype', 'fileupload')
    form.add_field('time', _LITTERBOX_RETENTION)
    form.add_field('fileToUpload', zip_bytes,
                   filename=filename, content_type='application/zip')
    timeout = aiohttp.ClientTimeout(total=120)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(_LITTERBOX_URL, data=form) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    print(f'[TRANSLATE-ZIP] litterbox HTTP {resp.status}: {body[:200]}')
                    return None
                url = (await resp.text()).strip()
        if not url.startswith('http'):
            print(f'[TRANSLATE-ZIP] litterbox 回非 URL: {url[:200]!r}')
            return None
        return url
    except Exception as e:
        print(f'[TRANSLATE-ZIP] litterbox 上傳失敗: {type(e).__name__}: {e}')
        traceback.print_exc()
        return None


def _build_output_zip(results: list[tuple[int, str, bytes | None, str | None]]) -> tuple[bytes, int]:
    """
    把翻譯結果打包成 zip。輸入 list 元素：(idx, original_filename, image_bytes, err_str)。
    err 不是 None 或 out 為空（None / b''）的略過。

    檔名完全沿用原 zip 的檔名（含副檔名）。server 端 mirror input format → 輸出格式
    跟輸入一致（webp 進就 webp 出），所以延用原檔名安全。原檔名缺失才退回 translated_001。
    輸出 zip 用 ZIP_STORED 不再壓（每張本身已是壓縮過的 webp/jpg/png）。

    回傳 (bytes, written_count)；上層拿 written_count 做 sanity check，避免送出空 zip。
    """
    buf = io.BytesIO()
    written = 0
    with zipfile.ZipFile(buf, mode='w', compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for idx, name, out, err in results:
            if err is not None or not out:
                continue
            out_name = name if name else f'translated_{idx:03d}{_detect_format_ext(out)}'
            zf.writestr(out_name, out)
            written += 1
    return buf.getvalue(), written


async def _safe_edit(notice, content: str) -> None:
    """notice.edit 包 try/except；webhook token 過期、訊息被刪等失敗一律忽略，僅 log。"""
    try:
        await notice.edit(content=content)
    except Exception as e:
        print(f'[TRANSLATE-ZIP] notice.edit 略過: {type(e).__name__}: {e}')


def _get_file_limit_mb(interaction: discord.Interaction) -> float:
    """讀 guild.filesize_limit 換算 MB；DM 或讀不到 fallback 8MB。留 10% 緩衝避免邊緣 413。"""
    g = interaction.guild
    if g is None:
        return _DISCORD_FILE_LIMIT_FALLBACK_MB
    try:
        return max(_DISCORD_FILE_LIMIT_FALLBACK_MB,
                   g.filesize_limit / 1024 / 1024 * 0.9)
    except Exception:
        return _DISCORD_FILE_LIMIT_FALLBACK_MB


async def _safe_reply(interaction: discord.Interaction, content: str,
                      files: list[discord.File] | None = None) -> bool:
    """
    回覆原 /translate-img 指令訊息（視覺上有 reply 箭頭關聯）。
    三層降級：original_response().reply() → channel.send → followup.send。
    全部失敗才放棄並 log。

    每層 fallback 前都會 file.reset(seek=True) — 否則前一層上傳已經把 BytesIO read 到
    EOF，下一層送出去就會是 0 bytes 廢檔。discord.py 的 retry 也有同樣的坑。
    """
    files = files or []

    def _reset_files() -> None:
        for f in files:
            try:
                f.reset(seek=True)
            except Exception as e:
                print(f'[TRANSLATE-ZIP] file.reset 失敗（已忽略）: {type(e).__name__}: {e}')

    # 第一層：reply 原 interaction 訊息（slash command 的回應就是被 reply 的對象）
    try:
        original = await interaction.original_response()
        await original.reply(content=content, files=files, mention_author=False)
        return True
    except Exception as e:
        print(f'[TRANSLATE-ZIP] reply 原訊息失敗，改試 channel.send: '
              f'{type(e).__name__}: {e}')
    _reset_files()

    # 第二層：直接發到頻道
    channel = interaction.channel
    if channel is not None:
        try:
            await channel.send(content=content, files=files)
            return True
        except Exception as e:
            print(f'[TRANSLATE-ZIP] channel.send 失敗，改試 followup: '
                  f'{type(e).__name__}: {e}')
        _reset_files()

    # 第三層：webhook followup（token 還活時可用）
    try:
        await interaction.followup.send(content=content, files=files, wait=True)
        return True
    except Exception as e:
        print(f'[TRANSLATE-ZIP] followup.send 也失敗: {type(e).__name__}: {e}')
        traceback.print_exc()
        return False


def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name='translate-img', description='翻譯圖片或整本漫畫壓縮檔（zip/cbz/rar/cbr）')
    @app_commands.describe(
        圖片1='(可選) 要翻譯的圖片',
        圖片2='(可選) 第 2 張',
        圖片3='(可選) 第 3 張',
        圖片4='(可選) 第 4 張',
        圖片5='(可選) 第 5 張',
        圖片6='(可選) 第 6 張',
        圖片7='(可選) 第 7 張',
        圖片8='(可選) 第 8 張',
        圖片9='(可選) 第 9 張',
        圖片10='(可選) 第 10 張',
        壓縮檔='(可選) zip/cbz/rar/cbr 壓縮檔（內含多張圖片）；翻譯結果回傳 zip',
        目標語言='翻譯成什麼語言（預設繁體中文）',
    )
    @app_commands.choices(目標語言=_LANG_CHOICES)
    async def slash_translate_img(
        interaction: discord.Interaction,
        圖片1: discord.Attachment | None = None,
        圖片2: discord.Attachment | None = None,
        圖片3: discord.Attachment | None = None,
        圖片4: discord.Attachment | None = None,
        圖片5: discord.Attachment | None = None,
        圖片6: discord.Attachment | None = None,
        圖片7: discord.Attachment | None = None,
        圖片8: discord.Attachment | None = None,
        圖片9: discord.Attachment | None = None,
        圖片10: discord.Attachment | None = None,
        壓縮檔: discord.Attachment | None = None,
        目標語言: app_commands.Choice[str] | None = None,
    ):
        target_lang = 目標語言.value if 目標語言 else '繁體中文'

        # 走壓縮檔路線（突破 10 圖上限）
        if 壓縮檔 is not None:
            await interaction.response.defer()
            await _handle_zip_flow(interaction, 壓縮檔, target_lang)
            return

        # 圖片附件路線（最多 10 張）
        attachments = [
            a for a in (圖片1, 圖片2, 圖片3, 圖片4, 圖片5,
                        圖片6, 圖片7, 圖片8, 圖片9, 圖片10)
            if a is not None
        ]

        if not attachments:
            await interaction.response.send_message(
                '請上傳圖片或壓縮檔喵！（圖片1..10 任選或 壓縮檔 二擇一）',
                ephemeral=True,
            )
            return

        # 過濾非圖片
        valid: list[tuple[discord.Attachment, str]] = []
        skipped: list[str] = []
        for a in attachments:
            mime = (a.content_type or '').split(';')[0].strip()
            if mime.startswith('image/'):
                valid.append((a, mime))
            else:
                skipped.append(a.filename)

        if not valid:
            await interaction.response.send_message('請上傳圖片檔案喵！', ephemeral=True)
            return

        await interaction.response.defer()

        await interaction.followup.send('小龍喵正在翻譯圖片喵...')

        async with aiohttp.ClientSession() as session:
            async def _one(idx: int, att: discord.Attachment, mime: str):
                try:
                    async with session.get(att.url) as resp:
                        image_data = await resp.read()
                    out = await translate_image(image_data, mime, target_lang)
                except Exception as e:
                    print(f'[TRANSLATE] 第 {idx} 張失敗: {type(e).__name__}: {e}')
                    traceback.print_exc()
                    return idx, att.filename, None, f'{type(e).__name__}: {e}'
                return idx, att.filename, out, None

            results = await asyncio.gather(
                *(_one(i, a, m) for i, (a, m) in enumerate(valid, 1))
            )

        files: list[discord.File] = []
        for idx, name, out, err in results:
            if err is not None:
                print(f'[TRANSLATE] 第 {idx} 張（{name}）失敗: {err}')
                continue
            files.append(discord.File(io.BytesIO(out), filename=f'translated_{idx}.png'))

        content = '翻譯完成了喵!' if files else '翻譯失敗了喵...'
        ok = await _safe_reply(interaction, content, files=files)
        if not ok:
            print(f'[TRANSLATE] 圖片路線送訊息失敗（已 log）')


async def _handle_zip_flow(
    interaction: discord.Interaction,
    zip_att: discord.Attachment,
    target_lang: str,
) -> None:
    """
    壓縮檔翻譯流程：下載 → 解壓 → 翻譯 → 打包回傳。
    任何階段失敗都用 notice.edit 回報，不留 user 在「翻譯中...」。
    """
    notice = await interaction.followup.send(
        f'下載壓縮檔 `{zip_att.filename}`（{zip_att.size / 1024 / 1024:.1f}MB）中...',
        wait=True,
    )

    # 下載 zip
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(zip_att.url) as resp:
                zip_bytes = await resp.read()
    except Exception as e:
        print(f'[TRANSLATE-ZIP] 下載失敗: {type(e).__name__}: {e}')
        traceback.print_exc()
        await _safe_edit(notice, f'下載失敗喵: {type(e).__name__}: {e}')
        return

    # 解壓挑圖（同步邏輯放 thread）；依附檔名分派 zip/rar
    try:
        images = await asyncio.to_thread(
            _extract_images_from_archive, zip_bytes, zip_att.filename,
        )
    except ValueError as e:
        await _safe_edit(notice, f'{e}')
        return
    except Exception as e:
        print(f'[TRANSLATE-ZIP] 解壓異常: {type(e).__name__}: {e}')
        traceback.print_exc()
        await _safe_edit(notice, f'解壓失敗喵: {type(e).__name__}: {e}')
        return

    total = len(images)
    if total == 0:
        await _safe_edit(notice, '壓縮檔內沒有任何圖片喵（支援 png/jpg/webp/gif/bmp）')
        return

    await _safe_edit(notice, f'解壓完成 {total} 張，開始翻譯（一張約20秒）喵...')
    started = time.monotonic()
    done = 0
    fail = 0
    progress_lock = asyncio.Lock()

    async def _one(idx: int, name: str, data: bytes, mime: str, label: str = ''):
        nonlocal done, fail
        try:
            out = await translate_image(data, mime, target_lang)
            # server 偶爾回傳空 bytes（worker 中途斷掉、stream 斷線）→ 視為失敗，
            # 否則會被打包成 0 bytes entry，整包 zip 變成廢檔
            if not out:
                raise RuntimeError('server 回傳空 bytes')
            result = (idx, name, out, None)
        except Exception as e:
            print(f'[TRANSLATE-ZIP] {label}{name} 失敗: {type(e).__name__}: {e}')
            traceback.print_exc()
            result = (idx, name, None, f'{type(e).__name__}: {e}')
        async with progress_lock:
            if result[2] is not None:
                done += 1
            else:
                fail += 1
            finished = done + fail
            elapsed = time.monotonic() - started
            eta_str = ''
            if 0 < finished < total:
                avg = elapsed / finished
                eta = avg * (total - finished)
                eta_str = f'、預估還剩 {eta:.0f}s'
            fail_str = f'（失敗 {fail}）' if fail else ''
            print(f'[TRANSLATE-ZIP] 進度 {finished}/{total}{fail_str}'
                  f' 已耗時 {elapsed:.0f}s{eta_str} ← {label}{name}')
        return result

    # 第一輪：全部翻譯
    results = list(await asyncio.gather(
        *(_one(i, n, d, m) for i, (n, d, m) in enumerate(images, 1))
    ))

    # 第二輪：失敗的抓出來重翻一次。單張 180s timeout，失敗多半是 Google 503 storm 那一刻
    # 卡住，過幾秒重試多半能過。仍失敗就放棄那張。
    failed_positions = [pos for pos, r in enumerate(results) if r[2] is None]
    if failed_positions:
        print(f'[TRANSLATE-ZIP] 第一輪完成 {done}/{total}，'
              f'{len(failed_positions)} 張失敗，重試中...')

        # 重置 counter：第二輪 retry 進度顯示獨立，否則 done/fail 會疊加成 99/98 之類
        # 第一輪這些 image 已經算 fail 一次；retry 成功時 _one 會 done+=1 但原本的 fail 沒抵消
        done = total - len(failed_positions)  # 第一輪確定成功的張數
        fail = 0  # retry 階段重新計，仍失敗才再 +1

        retry_tasks = []
        for pos in failed_positions:
            name_, data_, mime_ = images[pos]
            orig_idx = results[pos][0]  # 保留原 idx 供 _build_output_zip 對位置
            retry_tasks.append(_one(orig_idx, name_, data_, mime_, label='[retry] '))
        retry_results = await asyncio.gather(*retry_tasks)

        # 成功的 retry 覆寫回 results；失敗的留原本的錯誤訊息
        for pos, r in zip(failed_positions, retry_results):
            if r[2] is not None:
                results[pos] = r

    # 打包輸出 zip（同步壓縮放 thread）
    out_zip_bytes, written = await asyncio.to_thread(_build_output_zip, results)
    out_size_mb = len(out_zip_bytes) / 1024 / 1024

    success = sum(1 for _, _, out, _ in results if out)
    elapsed = time.monotonic() - started
    print(f'[TRANSLATE-ZIP] 翻譯完成 {success}/{total} 張（zip 寫入 {written} 個 entry），'
          f'耗時 {elapsed:.0f}s，輸出 zip {out_size_mb:.2f}MB ({len(out_zip_bytes)} bytes)')
    for _, name, _, err in results:
        if err is not None:
            print(f'[TRANSLATE-ZIP]   失敗: {name}: {err[:120]}')

    # Sanity check：zip 沒任何有效 entry 就視為失敗，避免送出空 zip 給用戶
    if written == 0:
        await _safe_reply(interaction, '翻譯失敗了喵...')
        return

    out_filename = (os.path.splitext(zip_att.filename)[0] or 'manga') + '_translated.zip'
    limit_mb = _get_file_limit_mb(interaction)

    # size 在 channel 上限內 → 試送 Discord；413（或其他失敗）自動 fallback litterbox
    if out_size_mb <= limit_mb:
        file = discord.File(io.BytesIO(out_zip_bytes), filename=out_filename)
        if await _safe_reply(interaction, '翻譯完成了喵!', files=[file]):
            return
        print(f'[TRANSLATE-ZIP] Discord 上傳失敗（可能 413 / 權限），'
              f'改 litterbox（zip {out_size_mb:.1f}MB，channel 上限 {limit_mb:.1f}MB）')
    else:
        print(f'[TRANSLATE-ZIP] zip {out_size_mb:.1f}MB > channel 上限 {limit_mb:.1f}MB，'
              f'改上傳 litterbox')

    # litterbox 路徑：超過上限或 Discord 直接拒
    url = await _upload_to_litterbox(out_zip_bytes, out_filename)
    if url is None:
        await _safe_reply(interaction,
                          f'翻譯完成了喵! ⚠️ zip {out_size_mb:.1f}MB Discord 塞不下，'
                          f'litterbox 上傳也失敗了喵。')
        return
    await _safe_reply(interaction, f'翻譯完成了喵!\n{url}')
