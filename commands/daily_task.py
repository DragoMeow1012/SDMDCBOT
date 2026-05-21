"""
/每日任務 — 開啟「每日任務」表單頁。

流程：
  1. /每日任務            → ephemeral 首頁，列獎勵 / 今日進度，含按鈕「每日分享本子」
  2. 點「每日分享本子」  → 刪掉首頁，發新的 ephemeral：
                            - Select：來源網站（W站 / NH / JM）
                            - 按鈕「填寫內容」 → 開 Modal
  3. Modal 三欄：連結或編號（必填）、Tag/簡介（選填）、推薦度 1~10（選填）
  4. Modal 送出 →
       (a) 被禁用者擋下；
       (b) 解析 URL / 爬標題、縮圖 → 發 embed 到 _TARGET_CHANNEL_ID（含「檢舉」按鈕）；
       (c) 結算今日獎勵：第 1/2/3 次 → 2000/1000/500，第 4 次起 0；
           填一個選填欄位 +200（基礎為 0 時 bonus 也不發）；
       (d) 同頻道公告獎勵；
       (e) 收掉所有 ephemeral。

URL 補完規則（只填數字時自動補成標準 URL）：
  - NH : https://nhentai.net/g/{id}/
  - W  : https://wnacg.com/photos-index-aid-{id}.html
  - JM : https://18comic.vip/album/{id}/（meta 走 curl_cffi 過 Cloudflare）

檢舉系統：
  - 每張卡片底下有「檢舉」按鈕，同一人對同一張只計一次。
  - 累積到 _REPORT_THRESHOLD 次 → 轉發到 _ADMIN_CHANNEL_ID，
    附「3 天禁用 / 解除限制」按鈕（頻道權限自行把關）；轉送後撤下原訊息。
  - 禁用紀錄存 data/daily_share_bans.json；下次提交 Modal 時擋下。
  - /管理員 可列出目前所有禁用中的用戶並挑一位解禁（manage_guild 權限）。

跨重啟：
  - ReportView / AdminReportView 都是 persistent view（固定 custom_id、timeout=None）。
  - main.py on_ready 透過 register_persistent_views(client) 在登入後註冊一次即可。
"""
from __future__ import annotations

import asyncio
import io
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
import discord
from discord import app_commands

# curl_cffi：用來過 JM 18comic 的 Cloudflare TLS 指紋檢查（chrome120 impersonate）。
# 沒裝就只是 JM 抓不到 meta，aiohttp 路徑（NH / W 站）不受影響。
try:
    from curl_cffi.requests import AsyncSession as CurlCffiSession  # type: ignore
    _CURL_CFFI_AVAILABLE = True
except ImportError:
    CurlCffiSession = None  # type: ignore
    _CURL_CFFI_AVAILABLE = False

from utils.json_store import load_json, save_json_async
from commands._wallet import apply_delta, get_balance


# ── 頻道 ID ─────────────────────────────────────────────────────────────
_TARGET_CHANNEL_ID = 1431025223054131230  # 每日分享本子發布頻道
_ADMIN_CHANNEL_ID  = 1505872725107806259  # 被檢舉內容轉送的管理員頻道

# ── 檔案路徑 ────────────────────────────────────────────────────────────
_WALLET_FILE  = os.path.join('data', 'morning_records.json')
_REPORTS_FILE = os.path.join('data', 'daily_share_reports.json')
_BANS_FILE    = os.path.join('data', 'daily_share_bans.json')
_COUNTER_FILE = os.path.join('data', 'daily_share_counter.json')
_FAVS_FILE    = os.path.join('data', 'manga_favorites.json')

# ── 獎勵設定 ────────────────────────────────────────────────────────────
_TZ              = timezone(timedelta(hours=8))
_BASE_REWARDS    = (2000, 1000, 500)  # 第 1/2/3 次基礎獎勵
_BONUS_PER_FIELD = 200                # 每填一個選填欄位
_MAX_REWARD_NTH  = len(_BASE_REWARDS)
_OPTIONAL_FIELDS = ('tag', 'rating')  # 計入 bonus 的選填欄位

# ── 罰則設定 ────────────────────────────────────────────────────────────
_FINE_RATE = 0.05   # 下架罰金 = 總資產 * 比例
_FINE_MIN  = 5000   # 下架罰金最低值

# ── 管理員白名單 ────────────────────────────────────────────────────────
_ADMIN_IDS: frozenset[int] = frozenset({
    690339490010759229,
    404111257008865280,
})

# ── 檢舉 / 禁用設定 ─────────────────────────────────────────────────────
_REPORT_THRESHOLD = 2   # 累積 N 票檢舉 → 轉送管理員
_TEMP_BAN_DAYS    = 3   # 「禁 3 天」按鈕的天數
_ADMIN_ROLE_NAME  = '本子管理員'  # （保留：未來如要改回身分組授權用）

# ── 讚 ──────────────────────────────────────────────────────────────────
_LIKE_REWARD = 500   # 每收到一個讚，分享者拿到的碎片數

# ── HTTP ────────────────────────────────────────────────────────────────
_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'
)
_HEADERS = {
    'User-Agent': _USER_AGENT,
    'Accept-Language': 'zh-TW,zh;q=0.9,ja;q=0.8,en;q=0.7',
}
_TIMEOUT = aiohttp.ClientTimeout(total=15)

# ── Regex（hot-path 一律 module-level 編譯）────────────────────────────
_DIGIT_RE       = re.compile(r'^\d+$')
_WNACG_H2_RE    = re.compile(r'<h2[^>]*>\s*([^<]+?)\s*</h2>', re.I)
_WNACG_COVER_RE = re.compile(
    r'<div[^>]*class="[^"]*\buwthumb\b[^"]*"[^>]*>\s*<img[^>]+src="([^"]+)"',
    re.I,
)
_JM_H1_RE = re.compile(r'<h1[^>]*>\s*([^<]+?)\s*</h1>', re.I)
# 從頁面挖第一張 media/albums 的圖（封面）；attr 順序不固定，先匹 itemprop=image
# 包住的 img 標籤，再從中抓 src。
_JM_COVER_TAG_RE = re.compile(
    r'<img\b[^>]*itemprop=["\']image["\'][^>]*>', re.I | re.S,
)
_JM_COVER_SRC_RE = re.compile(
    r'src=["\']([^"\']+/media/albums/\d+\.[a-z]+[^"\']*)["\']', re.I,
)
_SHARER_FOOTER_RE = re.compile(r'sharer_id=(\d+)')

# ── 站點設定 ────────────────────────────────────────────────────────────
_SITES: dict[str, dict[str, Any]] = {
    'nh': {
        'label':   'NH (nhentai)',
        'emoji':   '🟥',
        'url_tpl': 'https://nhentai.net/g/{id}/',
        'link_re': re.compile(r'nhentai\.(?:net|to)/g/(\d+)', re.I),
    },
    'w': {
        'label':   'W站 (wnacg)',
        'emoji':   '🟦',
        'url_tpl': 'https://wnacg.com/photos-index-aid-{id}.html',
        'link_re': re.compile(r'wnacg\.(?:com|org|cc)/[^\s]*?aid[-=](\d+)', re.I),
    },
    'jm': {
        'label':   'JM (18comic)',
        'emoji':   '🟨',
        'url_tpl': 'https://18comic.vip/album/{id}/',
        'link_re': re.compile(r'18comic(?:\.[a-z]+)+/album/(\d+)', re.I),
    },
}


# ─────────────────────────────────────────────────────────────────────────
# URL 解析
# ─────────────────────────────────────────────────────────────────────────
def _resolve_url(site: str, raw: str) -> tuple[str, str] | None:
    """回 (canonical_url, id) 或 None。"""
    raw = (raw or '').strip()
    if not raw or site not in _SITES:
        return None
    cfg = _SITES[site]
    if _DIGIT_RE.match(raw):
        return cfg['url_tpl'].format(id=raw), raw
    m = cfg['link_re'].search(raw)
    if m:
        gid = m.group(1)
        return cfg['url_tpl'].format(id=gid), gid
    return None


# ─────────────────────────────────────────────────────────────────────────
# 漫畫標題 / 縮圖爬取
# ─────────────────────────────────────────────────────────────────────────
# 回 (title, thumbnail_url, thumbnail_bytes)。任一可能為 None。
# thumbnail_bytes 非 None 時：呼叫端應把它當 discord.File 附上 + embed 改用
# 'attachment://<filename>' 引用（JM CDN 對 Discord proxy 不友善，必須走附件）。
async def _fetch_meta(site: str, gid: str) -> tuple[str | None, str | None, bytes | None]:
    try:
        async with aiohttp.ClientSession(headers=_HEADERS) as session:
            if site == 'nh':
                async with session.get(
                    f'https://nhentai.net/api/v2/galleries/{gid}',
                    timeout=_TIMEOUT,
                ) as r:
                    if r.status != 200:
                        return None, None, None
                    data = await r.json()
                titles = data.get('title') or {}
                title = (titles.get('pretty')
                         or titles.get('english')
                         or titles.get('japanese'))
                thumb_path = ((data.get('thumbnail') or {}).get('path')
                              or (data.get('cover') or {}).get('path'))
                thumb = (f'https://t.nhentai.net/{thumb_path.lstrip("/")}'
                         if thumb_path else None)
                return title, thumb, None

            if site == 'w':
                url = _SITES[site]['url_tpl'].format(id=gid)
                async with session.get(url, timeout=_TIMEOUT) as r:
                    if r.status != 200:
                        return None, None, None
                    html = await r.text(errors='replace')
                title = None
                m = _WNACG_H2_RE.search(html)
                if m:
                    title = m.group(1).strip()
                thumb = None
                m = _WNACG_COVER_RE.search(html)
                if m:
                    raw = m.group(1).strip().lstrip('/')
                    if not raw.startswith(('http://', 'https://')):
                        raw = f'https://{raw}'
                    thumb = raw
                return title, thumb, None

            if site == 'jm':
                return await _fetch_meta_jm(gid)

            return None, None, None
    except Exception as e:
        print(f'[每日任務] fetch_meta 失敗 site={site} id={gid}: '
              f'{type(e).__name__}: {e}')
        return None, None, None


async def _download_jm_cover(thumb_url: str, referer: str) -> bytes | None:
    """單獨下載 JM 封面 bytes（帶 Referer 過 hotlink 防護）。失敗回 None。"""
    if not _CURL_CFFI_AVAILABLE:
        return None
    try:
        async with CurlCffiSession() as s:
            r = await s.get(
                thumb_url, impersonate='chrome120',
                headers={'Referer': referer}, timeout=15,
            )
        if r.status_code == 200 and r.content:
            return bytes(r.content)
    except Exception as e:
        print(f'[每日任務] JM cover download 失敗 url={thumb_url}: '
              f'{type(e).__name__}: {e}')
    return None


async def _fetch_meta_jm(gid: str) -> tuple[str | None, str | None, bytes | None]:
    """JM 18comic：用 curl_cffi 過 Cloudflare TLS 指紋。
    回 (title, thumb_url, thumb_bytes)。Discord proxy 抓不到 JM CDN，呼叫端
    應該優先用 thumb_bytes 走附件。
    """
    fallback_thumb = f'https://cdn-msp.18comic.vip/media/albums/{gid}.jpg'
    if not _CURL_CFFI_AVAILABLE:
        print('[每日任務] JM 需要 curl_cffi，未安裝 → 只回 fallback URL')
        return None, fallback_thumb, None
    url = _SITES['jm']['url_tpl'].format(id=gid)
    title: str | None = None
    thumb: str | None = None
    try:
        async with CurlCffiSession() as s:
            r = await s.get(url, impersonate='chrome120', timeout=15)
            final_url = str(getattr(r, 'url', '') or '')
            if 'album_missing' in final_url or r.status_code != 200:
                return None, None, None
            html = r.content.decode('utf-8', errors='replace')
            m = _JM_H1_RE.search(html)
            if m:
                title = m.group(1).strip()
            # 封面：頁面裡多個 itemprop=image 的 img（含 logo gif），找 src 帶
            # /media/albums/ 的那張才是真正封面。
            for tag_m in _JM_COVER_TAG_RE.finditer(html):
                src_m = _JM_COVER_SRC_RE.search(tag_m.group(0))
                if src_m:
                    thumb = src_m.group(1)
                    break
            if thumb is None:
                thumb = fallback_thumb
            # 同一個 session 順手把封面 bytes 抓回來（共用 TLS 指紋 + Referer）
            thumb_bytes: bytes | None = None
            try:
                rb = await s.get(
                    thumb, impersonate='chrome120',
                    headers={'Referer': url}, timeout=15,
                )
                if rb.status_code == 200 and rb.content:
                    thumb_bytes = bytes(rb.content)
            except Exception as e:
                print(f'[每日任務] JM cover bytes 失敗 url={thumb}: '
                      f'{type(e).__name__}: {e}')
    except Exception as e:
        print(f'[每日任務] JM fetch 失敗 id={gid}: '
              f'{type(e).__name__}: {e}')
        return None, fallback_thumb, None

    return title, thumb, thumb_bytes


# ─────────────────────────────────────────────────────────────────────────
# 獎勵
# ─────────────────────────────────────────────────────────────────────────
def _today_key() -> str:
    return datetime.now(_TZ).date().isoformat()


def _calc_reward(nth: int, optional_filled: int) -> int:
    """nth: 已是第幾次（1-based）。第 4 次起回 0；基礎為 0 時 bonus 也不發。"""
    if nth <= 0 or nth > _MAX_REWARD_NTH:
        return 0
    base = _BASE_REWARDS[nth - 1]
    return base + optional_filled * _BONUS_PER_FIELD


def _today_share_count(uid: str) -> int:
    rec = load_json(_WALLET_FILE).get('users', {}).get(uid, {})
    if rec.get('daily_share_day') != _today_key():
        return 0
    return int(rec.get('daily_share_count', 0))


async def _claim_daily_share_reward(uid: str, optional_filled: int) -> tuple[int, int]:
    """原子操作：今日計數 +1，並把獎勵加進餘額。回 (nth, reward)。"""
    data  = load_json(_WALLET_FILE)
    users = data.setdefault('users', {})
    rec   = users.setdefault(uid, {
        'balance': 0, 'total_days': 0, 'streak': 0, 'last_day': None,
    })
    today = _today_key()
    if rec.get('daily_share_day') != today:
        rec['daily_share_day']   = today
        rec['daily_share_count'] = 0
    nth = int(rec.get('daily_share_count', 0)) + 1
    rec['daily_share_count'] = nth
    reward = _calc_reward(nth, optional_filled)
    if reward > 0:
        rec['balance'] = int(rec.get('balance', 0)) + reward
    await save_json_async(_WALLET_FILE, data)
    return nth, reward


# ─────────────────────────────────────────────────────────────────────────
# 禁用
# ─────────────────────────────────────────────────────────────────────────
def _load_bans() -> dict:
    return load_json(_BANS_FILE) or {}


async def _save_bans(data: dict) -> None:
    await save_json_async(_BANS_FILE, data)


def _ban_status(uid: str) -> tuple[bool, str]:
    """回 (是否仍被禁, 描述文字)。temp 已過期者自動視為解禁。"""
    entry = _load_bans().get(uid)
    if not entry:
        return False, ''
    btype = entry.get('type')
    if btype == 'perm':
        return True, '永久禁止使用每日分享本子'
    if btype == 'temp':
        until_str = entry.get('until')
        if not until_str:
            return False, ''
        try:
            until = datetime.fromisoformat(until_str)
        except ValueError:
            return False, ''
        if until <= datetime.now(_TZ):
            return False, ''
        return True, f'禁用至 <t:{int(until.timestamp())}:f>（剩 <t:{int(until.timestamp())}:R>）'
    return False, ''


async def _set_ban(uid: str, *, btype: str, days: int | None,
                   by: int) -> dict:
    data  = _load_bans()
    now   = datetime.now(_TZ)
    entry: dict[str, Any] = {
        'type':  btype,
        'by':    int(by),
        'at':    now.isoformat(),
        'until': None,
    }
    if btype == 'temp' and days:
        entry['until'] = (now + timedelta(days=days)).isoformat()
    data[uid] = entry
    await _save_bans(data)
    return entry


async def _remove_ban(uid: str) -> bool:
    data = _load_bans()
    if uid not in data:
        return False
    data.pop(uid, None)
    await _save_bans(data)
    return True


def _list_active_bans() -> list[tuple[str, dict]]:
    """回 [(uid, entry), ...]；已過期的 temp ban 不算。"""
    out: list[tuple[str, dict]] = []
    now = datetime.now(_TZ)
    for uid, entry in _load_bans().items():
        btype = entry.get('type')
        if btype == 'perm':
            out.append((uid, entry))
            continue
        if btype == 'temp':
            until_str = entry.get('until')
            if not until_str:
                continue
            try:
                until = datetime.fromisoformat(until_str)
            except ValueError:
                continue
            if until > now:
                out.append((uid, entry))
    return out


# ─────────────────────────────────────────────────────────────────────────
# 檢舉
# ─────────────────────────────────────────────────────────────────────────
def _load_reports() -> dict:
    return load_json(_REPORTS_FILE) or {}


async def _save_reports(data: dict) -> None:
    await save_json_async(_REPORTS_FILE, data)


_serial_lock = asyncio.Lock()


async def _allocate_serial() -> int:
    """配置一個新的流水號。受 _serial_lock 保護，避免並發競態。"""
    async with _serial_lock:
        data = load_json(_COUNTER_FILE) or {}
        n = int(data.get('next', 1))
        data['next'] = n + 1
        await save_json_async(_COUNTER_FILE, data)
        return n


def _format_serial(n: int | None) -> str:
    """流水號顯示格式。沒值就回空字串。"""
    if n is None:
        return ''
    return f'#{int(n):04d}'


def _resolve_sharer_by_serial(serial: int) -> tuple[int | None, dict | None]:
    """從 reports.json 用流水號反查 sharer_id。已刪除/已放行的歷史紀錄也找得到。
    回 (sharer_id, entry) 或 (None, None)。"""
    for entry in _load_reports().values():
        try:
            if int(entry.get('serial') or -1) == int(serial):
                return int(entry.get('sharer_id') or 0) or None, entry
        except (TypeError, ValueError):
            continue
    return None, None


async def _register_share_record(*, message_id: int, sharer_id: int,
                                 site: str, url: str, title: str,
                                 tag: str, rating: int | None,
                                 thumb: str | None, reward: int = 0,
                                 serial: int | None = None) -> int:
    """寫一筆分享紀錄。沒給 serial 就配一個新的，回最終 serial。"""
    if serial is None:
        serial = await _allocate_serial()
    data = _load_reports()
    data[str(message_id)] = {
        'sharer_id':        int(sharer_id),
        'site':             site,
        'url':              url,
        'title':            title,
        'tag':              tag,
        'rating':           rating,
        'thumb':            thumb,
        'reporters':        [],
        'likers':           [],
        'admin_message_id': None,
        'serial':           int(serial),
        'reward':           int(reward),
        'status':           'active',  # active / deleted / released
        'penalty_applied':  False,
    }
    await _save_reports(data)
    return int(serial)


async def _add_liker(message_id: int, liker_id: int) -> tuple[str, dict | None]:
    """回 (status, entry)。status:
       - 'no_record' : 找不到該訊息
       - 'duplicate' : 該人已按過讚
       - 'recorded'  : 成功 +1
    """
    data  = _load_reports()
    entry = data.get(str(message_id))
    if entry is None:
        return 'no_record', None
    likers = entry.setdefault('likers', [])
    if str(liker_id) in likers:
        return 'duplicate', entry
    likers.append(str(liker_id))
    await _save_reports(data)
    return 'recorded', entry


async def _add_reporter(message_id: int, reporter_id: int) -> tuple[str, dict | None]:
    """回 (status, entry)。status:
       - 'no_record'    : 找不到該訊息（資料遺失 / 舊訊息）
       - 'duplicate'    : 該人已檢舉過
       - 'recorded'     : 票數 < 閾值
       - 'forward'      : 票數剛達閾值，呼叫端應立即轉送管理員
       - 'already_full' : 票數已經達到 / 超過閾值，已轉送過了
    """
    data  = _load_reports()
    entry = data.get(str(message_id))
    if entry is None:
        return 'no_record', None
    if entry.get('admin_message_id') is not None:
        return 'already_full', entry
    reporters = entry.setdefault('reporters', [])
    if str(reporter_id) in reporters:
        return 'duplicate', entry
    reporters.append(str(reporter_id))
    await _save_reports(data)
    if len(reporters) >= _REPORT_THRESHOLD:
        return 'forward', entry
    return 'recorded', entry


async def _mark_forwarded(message_id: int, admin_message_id: int) -> None:
    data  = _load_reports()
    entry = data.get(str(message_id))
    if entry is None:
        return
    entry['admin_message_id'] = int(admin_message_id)
    await _save_reports(data)


def _find_report_by_admin_msg(admin_message_id: int) -> tuple[str | None, dict | None]:
    """從 reports.json 找出 admin_message_id 對應的那筆紀錄，回 (key, entry)。"""
    for key, entry in _load_reports().items():
        if entry.get('admin_message_id') == int(admin_message_id):
            return key, entry
    return None, None


# ─────────────────────────────────────────────────────────────────────────
# 罰則：被檢舉達標未還原 或 被管理員下架 → 收回獎勵 + 5%/最低 5000 罰金
# ─────────────────────────────────────────────────────────────────────────
def _calc_fine(net_worth: int) -> int:
    """罰金 = max(淨資產 * _FINE_RATE, _FINE_MIN)。淨資產為負時用 0 計算保底。"""
    base = max(int(net_worth), 0)
    return max(int(base * _FINE_RATE), _FINE_MIN)


async def _apply_penalty(client: discord.Client, entry_key: str,
                         entry: dict) -> bool:
    """對一筆分享套用「下架罰則」：扣回獎勵 + 罰金，公開 @通知。
    用 entry['penalty_applied'] 防重扣（同一筆被多按一次只扣一次）。
    回 True 表示這次有實際執行扣款。"""
    if entry.get('penalty_applied'):
        return False
    sharer_id = int(entry.get('sharer_id') or 0)
    if sharer_id <= 0:
        return False

    reward = int(entry.get('reward') or 0)
    uid    = str(sharer_id)

    # 罰金基準 = 淨資產（錢包 + 銀行 + 股票市值）；股票模組 lazy import 避循環
    try:
        from commands.stock import calc_net_worth
        net_worth = await calc_net_worth(sharer_id)
    except Exception as e:
        print(f'[每日任務] calc_net_worth 失敗，退回錢包餘額: '
              f'{type(e).__name__}: {e}')
        net_worth = get_balance(uid)

    fine  = _calc_fine(net_worth)
    total = reward + fine
    if total > 0:
        await apply_delta(uid, -total)

    # 在 reports.json 同步標記，避免重扣
    reports = _load_reports()
    rec     = reports.get(entry_key)
    if rec is not None:
        rec['penalty_applied'] = True
        rec['penalty'] = {
            'reward':    reward,
            'fine':      fine,
            'net_worth': net_worth,
            'at':        datetime.now(_TZ).isoformat(),
        }
        await _save_reports(reports)
    entry['penalty_applied'] = True  # 同步給呼叫端在用的 dict

    # 公開 @通知（_TARGET_CHANNEL_ID，作為下架的後續訊息）
    try:
        ch = client.get_channel(_TARGET_CHANNEL_ID)
        if ch is None:
            ch = await client.fetch_channel(_TARGET_CHANNEL_ID)
        await ch.send(
            f'<@{sharer_id}> 你分享的本子不合規範，已收回你本次任務所得 '
            f'{reward:,} 並罰款 {fine:,}'
            f'(淨資產{int(_FINE_RATE * 100)}%，最低 {_FINE_MIN:,})',
            allowed_mentions=discord.AllowedMentions(users=True),
        )
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        print(f'[每日任務] 罰則通知發送失敗 uid={sharer_id}: '
              f'{type(e).__name__}: {e}')
    return True


async def _mark_status(entry_key: str, status: str,
                       extra: dict | None = None) -> None:
    """更新一筆 entry 的 status（active / deleted / released）。"""
    reports = _load_reports()
    rec     = reports.get(entry_key)
    if rec is None:
        return
    rec['status'] = status
    if extra:
        rec.update(extra)
    await _save_reports(reports)


def _resolve_active_share_by_serial(serial: int) -> tuple[str | None, dict | None]:
    """找該 serial 目前處於可下架狀態（status=active 且尚未轉送 admin）的紀錄。
    回 (message_id_str, entry) 或 (None, None)。"""
    for key, entry in _load_reports().items():
        try:
            if int(entry.get('serial') or -1) != int(serial):
                continue
        except (TypeError, ValueError):
            continue
        if entry.get('status') != 'active':
            continue
        if entry.get('admin_message_id'):
            continue
        return key, entry
    return None, None


async def _forward_to_admin_via_serial(
    client: discord.Client, entry_key: str, entry: dict,
    *, banned_by: int,
) -> int | None:
    """走檢舉機制：刪 _TARGET_CHANNEL_ID 的原訊息 + 在 _ADMIN_CHANNEL_ID 發
    「流水號封禁」review embed（含 AdminReportView 三按鈕）。
    回傳 admin_message_id；失敗回 None。"""
    # 1. 刪原訊息
    target_channel = client.get_channel(_TARGET_CHANNEL_ID)
    if target_channel is None:
        try:
            target_channel = await client.fetch_channel(_TARGET_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            print(f'[每日任務] 流水號封禁取不到 target channel: '
                  f'{type(e).__name__}: {e}')
            target_channel = None
    if target_channel is not None:
        try:
            msg = await target_channel.fetch_message(int(entry_key))
            await msg.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            print(f'[每日任務] 流水號封禁刪訊息失敗 msg={entry_key}: '
                  f'{type(e).__name__}: {e}')

    # 2. 轉送 admin channel
    admin_channel = client.get_channel(_ADMIN_CHANNEL_ID)
    if admin_channel is None:
        try:
            admin_channel = await client.fetch_channel(_ADMIN_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            print(f'[每日任務] 流水號封禁取不到 admin channel: '
                  f'{type(e).__name__}: {e}')
            return None
    try:
        admin_msg = await admin_channel.send(
            embed=_build_admin_review_embed(
                entry, entry.get('reporters', []),
                kind='serial_ban', banned_by=banned_by,
            ),
            view=AdminReportView(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException as e:
        print(f'[每日任務] 流水號封禁轉送 admin 失敗: '
              f'{type(e).__name__}: {e}')
        return None

    # 3. 標記 admin_message_id（讓三按鈕能反查回 entry）
    await _mark_forwarded(int(entry_key), admin_msg.id)
    return admin_msg.id


# ─────────────────────────────────────────────────────────────────────────
# Embeds
# ─────────────────────────────────────────────────────────────────────────
def _reward_info_lines() -> list[str]:
    return [
        '**獎勵：** 第 1 次 **2000**、第 2 次 **1000**、第 3 次 **500**、第 4 次起 0',
        f'　　　　填 Tag 或 推薦度 各 +{_BONUS_PER_FIELD}（基礎 0 時 bonus 也不發）',
        (f'⚠️ **被檢舉或被管理員指令下架** 會收回你本次任務所得'
         f'並罰款總資產{int(_FINE_RATE * 100)}%(最低{_FINE_MIN:,})'),
    ]


def _progress_line(uid: str) -> str:
    n = _today_share_count(uid)
    nxt = n + 1
    if nxt > _MAX_REWARD_NTH:
        return f'**今日進度：** 已分享 {n} 次（今日獎勵已領滿）'
    base = _BASE_REWARDS[nxt - 1]
    return (f'**今日進度：** 已分享 {n} 次，下一次（第 {nxt} 次）'
            f'可得基礎 **{base:,}** 碎片')


_INVITE_BLOCK = (
    '還沒加進群組?點擊底下連結加入!\n'
    'https://discord.gg/XuWmVck6uH'
)


def _home_embed(uid: str) -> discord.Embed:
    banned, ban_desc = _ban_status(uid)
    lines = [
        '選擇要完成的任務：',
        '',
        '📚 **每日分享本子** — 分享一本本到指定頻道',
        '',
        *_reward_info_lines(),
        '',
        _progress_line(uid),
    ]
    if banned:
        lines += ['', f'⛔ **你目前被禁用每日分享本子**：{ban_desc}']
    lines += ['', _INVITE_BLOCK]
    return discord.Embed(
        title='每日任務',
        description='\n'.join(lines),
        color=discord.Color.teal(),
    )


def _share_embed(site: str | None) -> discord.Embed:
    if site:
        cfg = _SITES[site]
        body = (
            f'已選網站：{cfg["emoji"]} **{cfg["label"]}**\n\n'
            '按下 **填寫內容** 輸入連結 / 編號 + (選填) Tag / 推薦度。'
        )
    else:
        opts = '\n'.join(
            f'- {cfg["emoji"]} **{cfg["label"]}**'
            for cfg in _SITES.values()
        )
        body = (
            '請先從下方選單選擇來源網站：\n'
            f'{opts}\n\n'
            '選好之後按 **填寫內容** 按鈕。\n'
            '_提示：只填編號數字時會依網站自動補完成完整連結。_'
        )
    return discord.Embed(
        title='每日任務 - 每日分享本子',
        description=body,
        color=discord.Color.magenta(),
    )


def _build_share_post_embed(*, recommender: str, title: str,
                            tag: str, rating: int | None,
                            url: str, thumb: str | None,
                            sharer_id: int,
                            serial: int | None = None) -> discord.Embed:
    lines = [
        f'推薦人: {recommender}',
        f'漫畫名: {title}',
    ]
    if tag:
        lines.append(f'Tag / 簡介: {tag}')
    if rating is not None:
        stars = '★' * rating + '☆' * (10 - rating)
        lines.append(f'推薦度: {stars} ({rating}/10)')
    lines.append(f'連結: {url}')
    embed = discord.Embed(
        title='每日任務 - 每日分享本子',
        description='\n'.join(lines),
        color=discord.Color.magenta(),
    )
    if thumb:
        embed.set_image(url=thumb)
    # AdminReportView 透過 footer.text 解析出 sharer_id；流水號額外另起一行
    footer_lines = [f'sharer_id={sharer_id}']
    if serial is not None:
        footer_lines.append(f'流水號:{int(serial):04d}')
    embed.set_footer(text='\n'.join(footer_lines))
    return embed


def _build_admin_review_embed(entry: dict, reporters: list[str],
                              *, kind: str = 'report',
                              banned_by: int | None = None) -> discord.Embed:
    """kind:
       - 'report'     → ⚠️ 每日分享本子 — 檢舉達標（由檢舉達閾值觸發）
       - 'serial_ban' → 每日分享本子 — 流水號封禁（由管理員手動下架觸發）
    banned_by 只在 serial_ban 模式下顯示。"""
    rating = entry.get('rating')
    title  = entry.get('title') or '(未取得)'
    tag    = entry.get('tag') or ''
    serial = entry.get('serial')
    desc_lines = [
        f'推薦人: <@{entry["sharer_id"]}>',
        f'漫畫名: {title}',
    ]
    if tag:
        desc_lines.append(f'Tag / 簡介: {tag}')
    if rating is not None:
        stars = '★' * rating + '☆' * (10 - rating)
        desc_lines.append(f'推薦度: {stars} ({rating}/10)')
    desc_lines.append(f'連結: {entry.get("url")}')
    desc_lines.append('')
    if kind == 'serial_ban':
        if banned_by:
            desc_lines.append(f'操作管理員: <@{banned_by}>')
        title_text = '⚠️ 每日分享本子 — 流水號封禁'
        color = discord.Color.dark_purple()
    else:
        reporters_lines = '\n'.join(f'- <@{r}>' for r in reporters) or '_(無)_'
        desc_lines += [
            f'檢舉人 ({len(reporters)}):',
            reporters_lines,
        ]
        title_text = '⚠️ 每日分享本子 — 檢舉達標'
        color = discord.Color.dark_red()
    embed = discord.Embed(
        title=title_text,
        description='\n'.join(desc_lines),
        color=color,
    )
    if thumb := entry.get('thumb'):
        embed.set_image(url=thumb)
    footer_lines = [f'sharer_id={entry["sharer_id"]}']
    if serial is not None:
        footer_lines.append(f'流水號:{int(serial):04d}')
    embed.set_footer(text='\n'.join(footer_lines))
    return embed


# ─────────────────────────────────────────────────────────────────────────
# Persistent Views
# ─────────────────────────────────────────────────────────────────────────
def _parse_sharer_id_from_message(message: discord.Message | None) -> int | None:
    if message is None or not message.embeds:
        return None
    footer = message.embeds[0].footer
    if footer is None or not footer.text:
        return None
    m = _SHARER_FOOTER_RE.search(footer.text)
    return int(m.group(1)) if m else None


# ── 收藏：每位用戶私人書籤 ────────────────────────────────────────────
def _load_favs() -> dict:
    return load_json(_FAVS_FILE) or {}


async def _save_favs(data: dict) -> None:
    await save_json_async(_FAVS_FILE, data)


def get_user_favs(uid: str) -> list[dict]:
    return list((_load_favs().get('users') or {}).get(uid) or [])


async def _add_user_fav(uid: str, entry: dict) -> tuple[bool, str]:
    """加入收藏；若 url 已在清單中則回 False。新加的擺最前面（最新→最舊）。"""
    if not entry.get('url'):
        return False, '此卡片解析不到連結'
    data = _load_favs()
    users = data.setdefault('users', {})
    arr = users.setdefault(uid, [])
    if any(e.get('url') == entry['url'] for e in arr):
        return False, '已經在收藏裡了'
    arr.insert(0, entry)
    # 上限 200 防止無限長
    if len(arr) > 200:
        del arr[200:]
    await _save_favs(data)
    return True, ''


async def _remove_user_fav(uid: str, url: str) -> bool:
    data = _load_favs()
    arr = (data.get('users') or {}).get(uid) or []
    new_arr = [e for e in arr if e.get('url') != url]
    if len(new_arr) == len(arr):
        return False
    data.setdefault('users', {})[uid] = new_arr
    await _save_favs(data)
    return True


# 從每日分享 embed 解析出 title / url / thumb（給「加入收藏」用）
_FAV_PARSE_TITLE_RE = re.compile(r'^漫畫名:\s*(.+)$', re.MULTILINE)
_FAV_PARSE_URL_RE   = re.compile(r'^連結:\s*(.+)$',   re.MULTILINE)


def _parse_share_card(msg: discord.Message) -> dict | None:
    """從每日分享卡片訊息抓 title / url / thumb，組成可收藏的條目。
    無法解析時回 None。"""
    if not msg.embeds:
        return None
    embed = msg.embeds[0]
    desc = embed.description or ''
    m_t = _FAV_PARSE_TITLE_RE.search(desc)
    m_u = _FAV_PARSE_URL_RE.search(desc)
    if not (m_t and m_u):
        return None
    thumb = None
    if embed.image and embed.image.url:
        thumb = embed.image.url
    elif embed.thumbnail and embed.thumbnail.url:
        thumb = embed.thumbnail.url
    return {
        'title':     m_t.group(1).strip(),
        'url':       m_u.group(1).strip(),
        'thumb':     thumb,
        'sharer_id': _parse_sharer_id_from_message(msg),
        'msg_id':    int(msg.id),
        'added_at':  datetime.now(_TZ).isoformat(timespec='seconds'),
    }


class ReportView(discord.ui.View):
    """貼在 _TARGET_CHANNEL_ID 每張卡片底下的按鈕列：「🚩 檢舉」+「👍 讚」+ 收藏。

    Persistent — label 上的票數 / 讚數是即時 render，每次點按完會 msg.edit
    把整個 view 換成新數字。custom_id 固定（'daily_share:report' /
    'daily_share:like' / 'daily_share:fav_add' / 'daily_share:fav_list'），
    bot 重啟後仍能 route 到註冊過的 dispatcher。

    with_report=False：放行還原時不掛檢舉按鈕，只剩讚。
    """

    def __init__(self, report_count: int = 0, like_count: int = 0,
                 *, with_report: bool = True) -> None:
        super().__init__(timeout=None)
        if with_report:
            report_btn = discord.ui.Button(
                label=f'檢舉 ({report_count}/{_REPORT_THRESHOLD})',
                emoji='🚩',
                style=discord.ButtonStyle.danger,
                custom_id='daily_share:report',
            )
            report_btn.callback = self._on_report
            self.add_item(report_btn)
        like_btn = discord.ui.Button(
            label=f'讚 ({like_count})',
            emoji='👍',
            style=discord.ButtonStyle.success,
            custom_id='daily_share:like',
        )
        like_btn.callback = self._on_like
        self.add_item(like_btn)

        fav_add_btn = discord.ui.Button(
            label='加入收藏',
            emoji='📚',
            style=discord.ButtonStyle.secondary,
            custom_id='daily_share:fav_add',
        )
        fav_add_btn.callback = self._on_fav_add
        self.add_item(fav_add_btn)

        fav_list_btn = discord.ui.Button(
            label='我的收藏',
            emoji='📖',
            style=discord.ButtonStyle.secondary,
            custom_id='daily_share:fav_list',
        )
        fav_list_btn.callback = self._on_fav_list
        self.add_item(fav_list_btn)

    async def _on_report(self, interaction: discord.Interaction) -> None:
        msg = interaction.message
        if msg is None:
            await interaction.response.send_message(
                '找不到對應訊息喵', ephemeral=True,
            )
            return

        # 不能檢舉自己
        sharer_id = _parse_sharer_id_from_message(msg)
        if sharer_id is not None and interaction.user.id == sharer_id:
            await interaction.response.send_message(
                '不能檢舉自己的分享喵！', ephemeral=True,
            )
            return

        status, entry = await _add_reporter(msg.id, interaction.user.id)
        if status == 'no_record':
            await interaction.response.send_message(
                '找不到這則分享的紀錄（可能是 bot 升級前的舊訊息）',
                ephemeral=True,
            )
            return
        if status == 'duplicate':
            await interaction.response.send_message(
                '你已經檢舉過這篇了喵', ephemeral=True,
            )
            return
        if status == 'already_full':
            await interaction.response.send_message(
                '此篇已轉送管理員審核中，謝謝關心喵', ephemeral=True,
            )
            return
        if status == 'recorded':
            count = len((entry or {}).get('reporters', []))
            like_count = len((entry or {}).get('likers', []))
            # 用 edit_message 一次 ACK 互動 + 更新按鈕 label，不發任何訊息
            try:
                await interaction.response.edit_message(view=ReportView(
                    report_count=count, like_count=like_count,
                ))
            except discord.HTTPException as e:
                print(f'[每日任務] 更新檢舉票數 label 失敗 msg={msg.id}: '
                      f'{type(e).__name__}: {e}')
            return

        # status == 'forward'：剛達閾值，轉送管理員
        assert entry is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        admin_channel = interaction.client.get_channel(_ADMIN_CHANNEL_ID)
        if admin_channel is None:
            try:
                admin_channel = await interaction.client.fetch_channel(_ADMIN_CHANNEL_ID)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                await interaction.followup.send(
                    f'達到檢舉門檻，但找不到管理員頻道：{type(e).__name__}',
                    ephemeral=True,
                )
                return
        try:
            admin_msg = await admin_channel.send(
                embed=_build_admin_review_embed(entry, entry.get('reporters', [])),
                view=AdminReportView(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as e:
            await interaction.followup.send(
                f'轉送管理員頻道失敗：{e}', ephemeral=True,
            )
            return
        await _mark_forwarded(msg.id, admin_msg.id)
        # 達閾值 → 把被檢舉的原訊息撤掉
        try:
            await msg.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            print(f'[每日任務] 刪除被檢舉訊息失敗 msg={msg.id}: '
                  f'{type(e).__name__}: {e}')
        await interaction.followup.send(
            '已記錄檢舉並轉送管理員審核，原訊息已撤下',
            ephemeral=True,
        )

    async def _on_like(self, interaction: discord.Interaction) -> None:
        msg = interaction.message
        if msg is None:
            await interaction.response.send_message(
                '找不到對應訊息喵', ephemeral=True,
            )
            return

        sharer_id = _parse_sharer_id_from_message(msg)
        if sharer_id is None:
            await interaction.response.send_message(
                '解析不到分享者 ID 喵', ephemeral=True,
            )
            return
        if interaction.user.id == sharer_id:
            await interaction.response.send_message(
                '不能給自己的分享按讚喵！', ephemeral=True,
            )
            return

        status, entry = await _add_liker(msg.id, interaction.user.id)
        if status == 'no_record':
            await interaction.response.send_message(
                '找不到這則分享的紀錄（可能是 bot 升級前的舊訊息）',
                ephemeral=True,
            )
            return
        if status == 'duplicate':
            await interaction.response.send_message(
                '你已經給過這篇讚了喵', ephemeral=True,
            )
            return

        # status == 'recorded'：award 分享者並原地刷新按鈕 label，不發任何訊息
        like_count   = len((entry or {}).get('likers', []))
        report_count = len((entry or {}).get('reporters', []))
        await apply_delta(str(sharer_id), _LIKE_REWARD)
        try:
            await interaction.response.edit_message(view=ReportView(
                report_count=report_count, like_count=like_count,
            ))
        except discord.HTTPException as e:
            print(f'[每日任務] 更新讚數 label 失敗 msg={msg.id}: '
                  f'{type(e).__name__}: {e}')

    async def _on_fav_add(self, interaction: discord.Interaction) -> None:
        msg = interaction.message
        if msg is None:
            await interaction.response.send_message(
                '找不到對應訊息喵', ephemeral=True,
            )
            return
        entry = _parse_share_card(msg)
        if entry is None:
            await interaction.response.send_message(
                '解析不到這張卡片的資料（可能是舊格式）', ephemeral=True,
            )
            return
        ok, err = await _add_user_fav(str(interaction.user.id), entry)
        await interaction.response.send_message(
            f'📚 已加入收藏：**{entry["title"]}**' if ok else f'ℹ️ {err}',
            ephemeral=True,
        )

    async def _on_fav_list(self, interaction: discord.Interaction) -> None:
        uid = str(interaction.user.id)
        view = FavListView(uid=uid, page=0)
        embeds = view.build_embeds()
        await interaction.response.send_message(
            embeds=embeds, view=view, ephemeral=True,
        )


# ── 收藏清單 view（ephemeral，timeout=None）─────────────────────────────
class FavListView(discord.ui.View):
    """ephemeral 列表，每頁 5 個獨立小 embed（含 thumbnail），prev/next + 頁碼 select。"""

    _PAGE_SIZE = 5

    def __init__(self, *, uid: str, page: int):
        super().__init__(timeout=None)
        self.uid = uid
        self.page = page
        self._favs = get_user_favs(uid)
        self.total_pages = max(
            1, (len(self._favs) + self._PAGE_SIZE - 1) // self._PAGE_SIZE
        )
        if self.page >= self.total_pages:
            self.page = max(0, self.total_pages - 1)
        self._build()

    def build_embeds(self) -> list[discord.Embed]:
        if not self._favs:
            return [discord.Embed(
                title='📖 我的收藏',
                description='_(還沒有收藏任何漫畫)_',
                color=discord.Color.greyple(),
            )]
        start = self.page * self._PAGE_SIZE
        slice_ = self._favs[start:start + self._PAGE_SIZE]
        embeds: list[discord.Embed] = []
        # header embed：總數 + 頁碼資訊
        embeds.append(discord.Embed(
            title=f'📖 我的收藏 ({len(self._favs)} 筆)',
            description=f'第 {self.page + 1}/{self.total_pages} 頁',
            color=discord.Color.gold(),
        ))
        for i, fav in enumerate(slice_):
            embed = discord.Embed(
                title=fav.get('title', '(無標題)'),
                url=fav.get('url'),
                color=discord.Color.magenta(),
            )
            thumb = fav.get('thumb')
            if thumb:
                embed.set_thumbnail(url=thumb)
            embed.set_footer(text=f'#{start + i + 1}　加入時間 {fav.get("added_at", "")[:16]}')
            embeds.append(embed)
        return embeds

    def _build(self) -> None:
        self.clear_items()

        # Row 0: 頁碼 select（總頁 >1 才顯示）
        if self.total_pages > 1:
            if self.total_pages <= 25:
                page_values = list(range(self.total_pages))
            else:
                step = self.total_pages / 25
                page_values = sorted({int(i * step) for i in range(25)})[:25]
                if self.page not in page_values:
                    page_values = sorted(set(page_values) | {self.page})[-25:]
            page_options = [
                discord.SelectOption(
                    label=f'第 {p + 1} 頁',
                    value=str(p),
                    default=(p == self.page),
                )
                for p in page_values
            ]
            page_sel = discord.ui.Select(
                placeholder=f'跳到頁數… (共 {self.total_pages} 頁)',
                options=page_options, min_values=1, max_values=1, row=0,
            )
            page_sel.callback = self._jump_page
            self.add_item(page_sel)

        # Row 1: 移除某筆（select）
        start = self.page * self._PAGE_SIZE
        slice_ = self._favs[start:start + self._PAGE_SIZE]
        if slice_:
            rm_options: list[discord.SelectOption] = []
            for i, fav in enumerate(slice_):
                title = fav.get('title', '(無標題)')
                rm_options.append(discord.SelectOption(
                    label=f'#{start + i + 1} {title}'[:100],
                    value=fav.get('url') or '',
                    description='移除此收藏',
                ))
            rm_sel = discord.ui.Select(
                placeholder='移除收藏…（選一筆）',
                options=rm_options, min_values=1, max_values=1, row=1,
            )
            rm_sel.callback = self._on_remove
            self.add_item(rm_sel)

        # Row 2: 上 / 下 / 關閉
        prev_btn = discord.ui.Button(
            label='◀️ 上一頁', style=discord.ButtonStyle.secondary,
            disabled=(self.page <= 0), row=2,
        )
        prev_btn.callback = self._prev
        self.add_item(prev_btn)
        next_btn = discord.ui.Button(
            label='下一頁 ▶️', style=discord.ButtonStyle.secondary,
            disabled=(self.page >= self.total_pages - 1), row=2,
        )
        next_btn.callback = self._next
        self.add_item(next_btn)
        close_btn = discord.ui.Button(
            label='✖️ 關閉', style=discord.ButtonStyle.danger, row=2,
        )
        close_btn.callback = self._close
        self.add_item(close_btn)

    async def _check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message(
                '這不是你的收藏視窗', ephemeral=True,
            )
            return False
        return True

    async def _refresh(self, interaction: discord.Interaction) -> None:
        new = FavListView(uid=self.uid, page=self.page)
        await interaction.response.edit_message(
            embeds=new.build_embeds(), view=new,
        )

    async def _jump_page(self, interaction: discord.Interaction) -> None:
        if not await self._check(interaction):
            return
        try:
            p = int(interaction.data['values'][0])
        except (ValueError, KeyError, IndexError):
            await interaction.response.defer()
            return
        self.page = max(0, min(self.total_pages - 1, p))
        await self._refresh(interaction)

    async def _prev(self, interaction: discord.Interaction) -> None:
        if not await self._check(interaction):
            return
        self.page = max(0, self.page - 1)
        await self._refresh(interaction)

    async def _next(self, interaction: discord.Interaction) -> None:
        if not await self._check(interaction):
            return
        self.page = min(self.total_pages - 1, self.page + 1)
        await self._refresh(interaction)

    async def _on_remove(self, interaction: discord.Interaction) -> None:
        if not await self._check(interaction):
            return
        url = interaction.data['values'][0]
        if not url:
            await interaction.response.defer()
            return
        await _remove_user_fav(self.uid, url)
        await self._refresh(interaction)

    async def _close(self, interaction: discord.Interaction) -> None:
        if not await self._check(interaction):
            return
        try:
            await interaction.response.defer()
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass


class AdminReportView(discord.ui.View):
    """管理員頻道的處置按鈕。頻道本身只開給伺服器管理員看，這裡不再做使用者限制。
    Persistent（固定 custom_id、timeout=None）。"""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def _guard(self, interaction: discord.Interaction) -> int | None:
        sharer_id = _parse_sharer_id_from_message(interaction.message)
        if sharer_id is None:
            await interaction.response.send_message(
                '解析不到被處置的用戶 ID（embed footer 損毀）',
                ephemeral=True,
            )
            return None
        return sharer_id

    async def _append_outcome(self, interaction: discord.Interaction,
                              outcome: str) -> None:
        msg = interaction.message
        if msg is None or not msg.embeds:
            return
        embed = msg.embeds[0]
        ts = int(datetime.now(_TZ).timestamp())
        prev = embed.description or ''
        embed.description = f'{prev}\n\n**📌 處置：** {outcome}（<t:{ts}:f>）'
        try:
            await msg.edit(embed=embed, view=None)
        except discord.HTTPException:
            pass

    @discord.ui.button(
        label='禁用', emoji='⏳',
        style=discord.ButtonStyle.danger,
        custom_id='daily_share:ban3d',
    )
    async def ban_3d_btn(self, interaction: discord.Interaction,
                         _btn: discord.ui.Button) -> None:
        sharer_id = await self._guard(interaction)
        if sharer_id is None:
            return
        await interaction.response.defer()

        ban_entry = await _set_ban(
            str(sharer_id), btype='temp',
            days=_TEMP_BAN_DAYS, by=interaction.user.id,
        )
        until = ban_entry.get('until')
        until_disp = ''
        if until:
            ts = int(datetime.fromisoformat(until).timestamp())
            until_disp = f'，至 <t:{ts}:f>'

        # 「禁用」也算下架未還原 → 同時觸發罰則（penalty_applied 防重扣）
        penalty_note = ''
        admin_msg_id = interaction.message.id if interaction.message else None
        if admin_msg_id is not None:
            entry_key, entry = _find_report_by_admin_msg(admin_msg_id)
            if entry_key is not None and entry is not None:
                applied = await _apply_penalty(
                    interaction.client, entry_key, entry,
                )
                await _mark_status(entry_key, 'deleted')
                if applied:
                    p = entry.get('penalty') or {}
                    penalty_note = (
                        f'\n💸 已收回獎勵 {int(p.get("reward", 0)):,} + '
                        f'罰金 {int(p.get("fine", 0)):,}'
                    )

        await self._append_outcome(
            interaction,
            f'禁用 <@{sharer_id}> {_TEMP_BAN_DAYS} 天{until_disp}{penalty_note}',
        )

    @discord.ui.button(
        label='刪除', emoji='🗑️',
        style=discord.ButtonStyle.secondary,
        custom_id='daily_share:delete',
    )
    async def delete_btn(self, interaction: discord.Interaction,
                         _btn: discord.ui.Button) -> None:
        sharer_id = await self._guard(interaction)
        if sharer_id is None:
            return
        await interaction.response.defer()

        # 原訊息在轉送當下已被撤下，這裡：
        # 1) 套用罰則（扣獎勵 + 罰金 + @通知，penalty_applied 防重）
        # 2) 標記 entry status=deleted（保留歷史，供日後依流水號封禁查得到）
        penalty_note = ''
        admin_msg_id = interaction.message.id if interaction.message else None
        if admin_msg_id is not None:
            entry_key, entry = _find_report_by_admin_msg(admin_msg_id)
            if entry_key is not None and entry is not None:
                applied = await _apply_penalty(
                    interaction.client, entry_key, entry,
                )
                await _mark_status(entry_key, 'deleted')
                if applied:
                    p = entry.get('penalty') or {}
                    penalty_note = (
                        f'\n💸 已收回獎勵 {int(p.get("reward", 0)):,} + '
                        f'罰金 {int(p.get("fine", 0)):,}'
                    )

        await self._append_outcome(
            interaction,
            f'刪除：保留下架，<@{sharer_id}> 的分享不會還原{penalty_note}',
        )

    @discord.ui.button(
        label='放行', emoji='✅',
        style=discord.ButtonStyle.success,
        custom_id='daily_share:release',
    )
    async def release_btn(self, interaction: discord.Interaction,
                          _btn: discord.ui.Button) -> None:
        sharer_id = await self._guard(interaction)
        if sharer_id is None:
            return
        await interaction.response.defer()

        # 1. 由 admin message id 反查當初的分享內容
        admin_msg_id = interaction.message.id if interaction.message else None
        if admin_msg_id is None:
            await interaction.followup.send('找不到管理員訊息', ephemeral=True)
            return
        entry_key, entry = _find_report_by_admin_msg(admin_msg_id)
        if entry is None:
            await interaction.followup.send(
                '找不到對應的分享紀錄（可能已經被處理）', ephemeral=True,
            )
            return

        # 2. 取目標頻道 → 取分享者顯示名
        target_channel = interaction.client.get_channel(_TARGET_CHANNEL_ID)
        if target_channel is None:
            try:
                target_channel = await interaction.client.fetch_channel(_TARGET_CHANNEL_ID)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                await interaction.followup.send(
                    f'找不到還原目標頻道：{type(e).__name__}', ephemeral=True,
                )
                return
        recommender = f'(用戶 {sharer_id})'
        guild = getattr(target_channel, 'guild', None)
        if guild is not None:
            member = guild.get_member(int(sharer_id))
            if member is None:
                try:
                    member = await guild.fetch_member(int(sharer_id))
                except (discord.NotFound, discord.HTTPException):
                    member = None
            if member is not None:
                recommender = member.display_name

        # 3. 重建 embed 並 repost（保留「讚」按鈕，移除「檢舉」按鈕）
        #    JM 需要重新下載 cover bytes 走附件（CDN 不開放 Discord proxy）
        prev_serial = entry.get('serial')
        prev_reward = int(entry.get('reward') or 0)
        restore_kwargs: dict[str, Any] = {
            'view': ReportView(with_report=False),
        }
        embed_thumb = entry.get('thumb')
        if entry.get('site') == 'jm' and entry.get('thumb'):
            cover_bytes = await _download_jm_cover(
                entry['thumb'], entry.get('url') or 'https://18comic.vip/',
            )
            if cover_bytes:
                cover_name = f'cover_{sharer_id}.jpg'
                restore_kwargs['file'] = discord.File(
                    io.BytesIO(cover_bytes), filename=cover_name,
                )
                embed_thumb = f'attachment://{cover_name}'
        restored = _build_share_post_embed(
            recommender=recommender,
            title=entry.get('title') or '(無法取得標題)',
            tag=entry.get('tag') or '',
            rating=entry.get('rating'),
            url=entry.get('url') or '',
            thumb=embed_thumb,
            sharer_id=int(sharer_id),
            serial=int(prev_serial) if prev_serial is not None else None,
        )
        restore_kwargs['embed'] = restored
        try:
            restored_msg = await target_channel.send(**restore_kwargs)
        except discord.HTTPException as e:
            await interaction.followup.send(
                f'還原失敗：{e}', ephemeral=True,
            )
            return

        # 4. 舊紀錄標記 released 並指向新訊息；新訊息建新紀錄繼承同個 serial
        #    （流水號不會因為「放行」而被洗掉，且 likers/penalty 狀態移到新紀錄）
        try:
            await _mark_status(entry_key, 'released',
                               {'restored_message_id': restored_msg.id})
        except Exception as e:
            print(f'[每日任務] 放行標記 released 失敗 key={entry_key}: '
                  f'{type(e).__name__}: {e}')
        try:
            await _register_share_record(
                message_id=restored_msg.id,
                sharer_id=int(sharer_id),
                site=entry.get('site') or '',
                url=entry.get('url') or '',
                title=entry.get('title') or '(無法取得標題)',
                tag=entry.get('tag') or '',
                rating=entry.get('rating'),
                thumb=entry.get('thumb'),
                reward=prev_reward,
                serial=int(prev_serial) if prev_serial is not None else None,
            )
        except Exception as e:
            print(f'[每日任務] 放行還原訊息建新 record 失敗 msg={restored_msg.id}: '
                  f'{type(e).__name__}: {e}')

        # 5. 標註 admin embed
        await self._append_outcome(
            interaction, f'放行：已還原 <@{sharer_id}> 的分享（不再可被檢舉）',
        )


# ─────────────────────────────────────────────────────────────────────────
# 表單流程（首頁 / 表單頁 / Modal）
# ─────────────────────────────────────────────────────────────────────────
class DailyTaskHomeView(discord.ui.View):
    def __init__(self, uid: str, *, opener: discord.Interaction):
        super().__init__(timeout=86400)
        self.uid    = uid
        self.opener = opener

    @discord.ui.button(label='每日分享本子', emoji='📚',
                       style=discord.ButtonStyle.primary)
    async def daily_share_btn(self, interaction: discord.Interaction,
                              _btn: discord.ui.Button):
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message(
                '這不是你的表單喵', ephemeral=True,
            )
            return
        share_view = DailyShareView(self.uid)
        await interaction.response.send_message(
            embed=_share_embed(None),
            view=share_view,
            ephemeral=True,
        )
        share_view.share_interaction = interaction
        try:
            await self.opener.delete_original_response()
        except discord.HTTPException:
            pass


class DailyShareView(discord.ui.View):
    def __init__(self, uid: str):
        super().__init__(timeout=600)
        self.uid: str = uid
        self.site: str | None = None
        self.share_interaction: discord.Interaction | None = None

        self._site_select = discord.ui.Select(
            placeholder='選擇來源網站',
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=cfg['label'], value=key, emoji=cfg['emoji'],
                )
                for key, cfg in _SITES.items()
            ],
        )
        self._site_select.callback = self._on_site_select
        self.add_item(self._site_select)

        self._submit_btn = discord.ui.Button(
            label='填寫內容', emoji='📝',
            style=discord.ButtonStyle.success,
        )
        self._submit_btn.callback = self._on_open_modal
        self.add_item(self._submit_btn)

    async def _on_site_select(self, interaction: discord.Interaction) -> None:
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message(
                '這不是你的表單喵', ephemeral=True,
            )
            return
        self.site = self._site_select.values[0]
        for opt in self._site_select.options:
            opt.default = (opt.value == self.site)
        await interaction.response.edit_message(
            embed=_share_embed(self.site), view=self,
        )

    async def _on_open_modal(self, interaction: discord.Interaction) -> None:
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message(
                '這不是你的表單喵', ephemeral=True,
            )
            return
        if self.site is None:
            await interaction.response.send_message(
                '請先從上方選單選擇來源網站喵！', ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            DailyShareModal(uid=self.uid, site=self.site, parent_view=self),
        )


class DailyShareModal(discord.ui.Modal, title='每日分享本子'):
    link_input = discord.ui.TextInput(
        label='連結或編號（必填）',
        placeholder='貼整段連結，或只填編號數字',
        required=True,
        max_length=300,
    )
    tag_input = discord.ui.TextInput(
        label='Tag 或內容簡介（可留空）',
        placeholder='例：百合 / 巨乳 / 修女',
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=400,
    )
    rating_input = discord.ui.TextInput(
        label='推薦度 1~10（可留空）',
        placeholder='整數 1~10',
        required=False,
        max_length=3,
    )

    def __init__(self, *, uid: str, site: str, parent_view: DailyShareView):
        super().__init__()
        self.uid         = uid
        self.site        = site
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # 0. 禁用檢查（在 defer 之前，方便回錯誤）
        banned, ban_desc = _ban_status(self.uid)
        if banned:
            await interaction.response.send_message(
                f'⛔ 你目前被禁用每日分享本子：{ban_desc}',
                ephemeral=True,
            )
            return

        # 1. 解析連結 / 編號
        resolved = _resolve_url(self.site, str(self.link_input.value))
        if resolved is None:
            await interaction.response.send_message(
                '連結或編號格式錯誤喵！請貼完整連結，或只填編號數字。',
                ephemeral=True,
            )
            return
        url, gid = resolved

        # 2. 解析推薦度
        rating: int | None = None
        rating_raw = (str(self.rating_input.value) or '').strip()
        if rating_raw:
            if not rating_raw.isdigit() or not (1 <= int(rating_raw) <= 10):
                await interaction.response.send_message(
                    '推薦度只能填 1~10 的整數喵！', ephemeral=True,
                )
                return
            rating = int(rating_raw)

        tag_raw = (str(self.tag_input.value) or '').strip()
        optional_filled = (1 if tag_raw else 0) + (1 if rating is not None else 0)

        await interaction.response.defer(ephemeral=True, thinking=True)

        # 3. 找目標頻道
        channel = interaction.client.get_channel(_TARGET_CHANNEL_ID)
        if channel is None:
            try:
                channel = await interaction.client.fetch_channel(_TARGET_CHANNEL_ID)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                await interaction.followup.send(
                    f'找不到目標頻道喵：{type(e).__name__}', ephemeral=True,
                )
                return

        # 4. 爬標題 + 縮圖（JM 會額外回 bytes，需要走附件）
        title, thumb, thumb_bytes = await _fetch_meta(self.site, gid)
        if not title:
            title = '(無法取得標題)'

        # 5. 先結算獎勵（要把 reward 寫進 register，供日後下架罰則用）+ 配流水號
        nth, reward = await _claim_daily_share_reward(self.uid, optional_filled)
        serial = await _allocate_serial()

        # 6. 發 embed 到目標頻道（含「檢舉」按鈕，title 帶 #流水號）
        recommender = (
            interaction.user.display_name
            if isinstance(interaction.user, discord.Member)
            else interaction.user.name
        )
        send_kwargs: dict[str, Any] = {'view': ReportView()}
        if thumb_bytes:
            cover_name = f'cover_{gid}.jpg'
            send_kwargs['file'] = discord.File(
                io.BytesIO(thumb_bytes), filename=cover_name,
            )
            embed_thumb: str | None = f'attachment://{cover_name}'
        else:
            embed_thumb = thumb
        post_embed = _build_share_post_embed(
            recommender=recommender,
            title=title, tag=tag_raw, rating=rating,
            url=url, thumb=embed_thumb,
            sharer_id=interaction.user.id,
            serial=serial,
        )
        send_kwargs['embed'] = post_embed
        try:
            sent_msg = await channel.send(**send_kwargs)
        except discord.HTTPException as e:
            await interaction.followup.send(
                f'發送到目標頻道失敗喵：{e}', ephemeral=True,
            )
            return

        # 7. 記錄分享內容供日後檢舉 / 罰則使用
        try:
            await _register_share_record(
                message_id=sent_msg.id,
                sharer_id=interaction.user.id,
                site=self.site, url=url, title=title,
                tag=tag_raw, rating=rating, thumb=thumb,
                reward=reward, serial=serial,
            )
        except Exception as e:
            print(f'[每日任務] register share record 失敗 msg={sent_msg.id}: '
                  f'{type(e).__name__}: {e}')
        if reward > 0:
            notice = (f'🎁 你完成了每日任務，'
                      f'獲得咕嚕喵碎片 x **{reward:,}**（第 {nth} 次）')
        else:
            notice = f'🎁 你完成了每日任務（第 {nth} 次，今日獎勵已領滿）'
        try:
            await interaction.edit_original_response(content=notice)
        except discord.HTTPException as e:
            print(f'[每日任務] 獎勵通知發送失敗: {e}')

        # 8. 收掉表單頁 ephemeral（Modal 的 original response 留著當通知，不刪）
        share_inter = self.parent_view.share_interaction
        if share_inter is not None:
            try:
                await share_inter.delete_original_response()
            except discord.HTTPException:
                pass


# ─────────────────────────────────────────────────────────────────────────
# /管理員 — 工具面板（主選單 / 解禁面板 / 封禁 Modal）
# ─────────────────────────────────────────────────────────────────────────
# 目前可禁用的「每日任務」只有「每日分享本子」一種；資料結構保留 task 欄位但
# 預設 'share'，未來新增任務時再展開成 Select / 子面板。
_TASK_LABEL_SHARE = '每日分享本子'
_MAX_BAN_DAYS     = 3650   # ~10 年，給管理員手動上限


def _ban_line(uid: str, entry: dict,
              guild: discord.Guild | None) -> str:
    """格式化單一禁用紀錄成一行顯示文字。"""
    member = guild.get_member(int(uid)) if guild else None
    name   = member.display_name if member else f'(離開 / 未在伺服器) {uid}'
    btype  = entry.get('type')
    if btype == 'perm':
        return f'- <@{uid}>（{name}）— 永久禁用'
    until_str = entry.get('until')
    if until_str:
        ts = int(datetime.fromisoformat(until_str).timestamp())
        return f'- <@{uid}>（{name}）— 暫時禁用，至 <t:{ts}:f>（<t:{ts}:R>）'
    return f'- <@{uid}>（{name}）— 暫時禁用（時間不詳）'


def _admin_home_embed() -> discord.Embed:
    return discord.Embed(
        title='管理員工具 — 每日任務',
        description=(
            '🔓 **解禁用戶** — 列出目前禁用中的用戶，挑一位解禁\n'
            f'🚫 **暫時封禁** — 依「用戶 ID」或「流水號」封禁 N 天\n'
            f'⛔ **永久封禁** — 依「用戶 ID」或「流水號」永久封禁\n\n'
            f'_目標任務：**{_TASK_LABEL_SHARE}**_'
        ),
        color=discord.Color.dark_teal(),
    )


def _admin_unban_embed(guild: discord.Guild | None) -> discord.Embed:
    bans = _list_active_bans()
    if not bans:
        body = '_目前沒有任何用戶被禁用每日分享本子。_'
    else:
        body = (
            '**目前禁用中的用戶：**\n'
            + '\n'.join(_ban_line(uid, entry, guild) for uid, entry in bans)
            + '\n\n從下方選單挑一位即可解禁。'
        )
    return discord.Embed(
        title='管理員 — 解禁面板',
        description=body,
        color=discord.Color.dark_teal(),
    )


class AdminPanelHomeView(discord.ui.View):
    """主選單：解禁 / 暫時封禁 / 永久封禁。後二者都是進子面板選輸入方式。"""

    def __init__(self, *, opener: discord.Interaction):
        super().__init__(timeout=86400)
        self.opener = opener

    @discord.ui.button(label='解禁用戶', emoji='🔓',
                       style=discord.ButtonStyle.success, row=0)
    async def unban_btn(self, interaction: discord.Interaction,
                        _btn: discord.ui.Button) -> None:
        view = AdminUnbanView(interaction.guild, opener=self.opener)
        await interaction.response.edit_message(
            embed=_admin_unban_embed(interaction.guild), view=view,
        )

    @discord.ui.button(label='暫時封禁', emoji='🚫',
                       style=discord.ButtonStyle.danger, row=0)
    async def ban_btn(self, interaction: discord.Interaction,
                      _btn: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            embed=_admin_ban_method_embed(perm=False),
            view=AdminBanMethodView(opener=self.opener, perm=False),
        )

    @discord.ui.button(label='永久封禁', emoji='⛔',
                       style=discord.ButtonStyle.danger, row=0)
    async def perm_ban_btn(self, interaction: discord.Interaction,
                           _btn: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            embed=_admin_ban_method_embed(perm=True),
            view=AdminBanMethodView(opener=self.opener, perm=True),
        )


def _admin_ban_method_embed(*, perm: bool) -> discord.Embed:
    label = '永久封禁' if perm else '暫時封禁'
    return discord.Embed(
        title=f'管理員 — {label}',
        description=(
            f'**{label}** — 選擇輸入方式：\n\n'
            '🆔 **依用戶 ID** — 直接輸入 sharer_id\n'
            '🔢 **依流水號** — 輸入該本子的流水號（#XXXX），自動帶出 sharer_id'
        ),
        color=discord.Color.dark_red() if perm else discord.Color.dark_orange(),
    )


class AdminBanMethodView(discord.ui.View):
    """子面板：選擇封禁輸入方式（依用戶 ID / 依流水號）。perm 旗標決定要不要問天數。"""

    def __init__(self, *, opener: discord.Interaction, perm: bool):
        super().__init__(timeout=86400)
        self.opener = opener
        self.perm   = perm

    @discord.ui.button(label='依用戶 ID', emoji='🆔',
                       style=discord.ButtonStyle.primary, row=0)
    async def by_id_btn(self, interaction: discord.Interaction,
                        _btn: discord.ui.Button) -> None:
        await interaction.response.send_modal(AdminBanModal(
            opener=self.opener, perm=self.perm,
        ))

    @discord.ui.button(label='依流水號', emoji='🔢',
                       style=discord.ButtonStyle.primary, row=0)
    async def by_serial_btn(self, interaction: discord.Interaction,
                            _btn: discord.ui.Button) -> None:
        await interaction.response.send_modal(AdminSerialBanModal(
            opener=self.opener, perm=self.perm,
        ))

    @discord.ui.button(label='返回', emoji='↩️',
                       style=discord.ButtonStyle.secondary, row=1)
    async def back_btn(self, interaction: discord.Interaction,
                       _btn: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            embed=_admin_home_embed(),
            view=AdminPanelHomeView(opener=self.opener),
        )


class AdminUnbanView(discord.ui.View):
    """解禁面板：Select 挑要解禁的用戶 + 返回主選單。"""

    def __init__(self, guild: discord.Guild | None,
                 *, opener: discord.Interaction):
        super().__init__(timeout=86400)
        self.guild  = guild
        self.opener = opener

        bans = _list_active_bans()
        if bans:
            options: list[discord.SelectOption] = []
            for uid, entry in bans[:25]:
                member = guild.get_member(int(uid)) if guild else None
                name   = member.display_name if member else f'uid {uid}'
                btype  = entry.get('type')
                if btype == 'perm':
                    desc = '永久禁用'
                else:
                    until_str = entry.get('until')
                    if until_str:
                        until = datetime.fromisoformat(until_str)
                        desc  = f'禁用至 {until.strftime("%Y-%m-%d %H:%M")}'
                    else:
                        desc = '暫時禁用'
                options.append(discord.SelectOption(
                    label=name[:100], value=uid, description=desc[:100],
                ))

            select = discord.ui.Select(
                placeholder='選擇要解禁的用戶',
                min_values=1, max_values=1, options=options, row=0,
            )

            async def _on_pick(inter: discord.Interaction) -> None:
                target_uid = select.values[0]
                removed = await _remove_ban(target_uid)
                msg = (f'✅ 已解禁 <@{target_uid}>' if removed
                       else f'<@{target_uid}> 並無禁用紀錄（可能剛被其他人解禁了）')
                await inter.response.send_message(
                    msg, ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                try:
                    await self.opener.edit_original_response(
                        embed=_admin_unban_embed(self.guild),
                        view=AdminUnbanView(self.guild, opener=self.opener),
                    )
                except discord.HTTPException:
                    pass

            select.callback = _on_pick
            self.add_item(select)

        back_btn = discord.ui.Button(
            label='返回', emoji='↩️',
            style=discord.ButtonStyle.secondary, row=1,
        )

        async def _back(inter: discord.Interaction) -> None:
            await inter.response.edit_message(
                embed=_admin_home_embed(),
                view=AdminPanelHomeView(opener=self.opener),
            )

        back_btn.callback = _back
        self.add_item(back_btn)


class AdminBanModal(discord.ui.Modal):
    """依用戶 ID 封禁。perm=True 不問天數。"""

    def __init__(self, *, opener: discord.Interaction, perm: bool):
        super().__init__(title=f'{"永久" if perm else "暫時"}封禁 — 依用戶 ID')
        self.opener = opener
        self.perm   = perm

        self.uid_input = discord.ui.TextInput(
            label='用戶 ID（sharer_id，純數字）',
            placeholder='例：404111257008865280',
            required=True, min_length=5, max_length=25,
        )
        self.add_item(self.uid_input)

        if not perm:
            self.days_input: discord.ui.TextInput | None = discord.ui.TextInput(
                label='禁用天數（正整數）',
                placeholder=f'1 ~ {_MAX_BAN_DAYS}',
                required=True, max_length=4,
            )
            self.add_item(self.days_input)
        else:
            self.days_input = None

    async def on_submit(self, interaction: discord.Interaction) -> None:
        uid_str = (str(self.uid_input.value) or '').strip()
        if not uid_str.isdigit():
            await interaction.response.send_message(
                '用戶 ID 必須是純數字喵', ephemeral=True,
            )
            return

        days: int | None = None
        if not self.perm:
            days_raw = (str(self.days_input.value) or '').strip() if self.days_input else ''
            if not days_raw.isdigit():
                await interaction.response.send_message(
                    '天數必須是正整數喵', ephemeral=True,
                )
                return
            days = int(days_raw)
            if not 1 <= days <= _MAX_BAN_DAYS:
                await interaction.response.send_message(
                    f'天數範圍是 1 ~ {_MAX_BAN_DAYS} 喵', ephemeral=True,
                )
                return

        btype = 'perm' if self.perm else 'temp'
        ban_entry = await _set_ban(
            uid_str, btype=btype, days=days, by=interaction.user.id,
        )

        if self.perm:
            notice = f'⛔ 已永久封禁 <@{uid_str}> — {_TASK_LABEL_SHARE}'
        else:
            until_str = ban_entry.get('until') or ''
            until_disp = ''
            if until_str:
                ts = int(datetime.fromisoformat(until_str).timestamp())
                until_disp = f'，至 <t:{ts}:f>'
            notice = (f'🚫 已禁用 <@{uid_str}> {days} 天 — '
                      f'{_TASK_LABEL_SHARE}{until_disp}')

        await interaction.response.send_message(
            notice, ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        try:
            await self.opener.edit_original_response(
                embed=_admin_home_embed(),
                view=AdminPanelHomeView(opener=self.opener),
            )
        except discord.HTTPException:
            pass


class AdminSerialBanModal(discord.ui.Modal):
    """依流水號處置 — 一律走檢舉機制（刪原訊息 + 轉送 admin channel review）。
    perm=True 流程順便永久 ban 該 sharer；perm=False 視天數欄決定：
       - 留空：純走檢舉，不 ban 用戶
       - 1~_MAX_BAN_DAYS：同時 temp ban 該天數
    最終由 admin 在 review channel 三按鈕（禁用 / 刪除 / 放行）決定罰則。"""

    def __init__(self, *, opener: discord.Interaction, perm: bool):
        super().__init__(title=f'{"永久" if perm else "流水號"}封禁 — 依流水號')
        self.opener = opener
        self.perm   = perm

        self.serial_input = discord.ui.TextInput(
            label='流水號（純數字，例：42 對應 #0042）',
            placeholder='例：42',
            required=True, min_length=1, max_length=10,
        )
        self.add_item(self.serial_input)

        if not perm:
            self.days_input: discord.ui.TextInput | None = discord.ui.TextInput(
                label='封禁天數（留空 = 只走檢舉、不 ban）',
                placeholder=f'1 ~ {_MAX_BAN_DAYS}（留空不 ban）',
                required=False, max_length=4,
            )
            self.add_item(self.days_input)
        else:
            self.days_input = None

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = (str(self.serial_input.value) or '').strip()
        if not raw.isdigit():
            await interaction.response.send_message(
                '流水號必須是純數字喵', ephemeral=True,
            )
            return
        serial = int(raw)

        # 驗證天數（若有填）— 在 defer 前先擋掉格式錯誤
        days: int | None = None
        if not self.perm and self.days_input is not None:
            days_raw = (str(self.days_input.value) or '').strip()
            if days_raw:
                if not days_raw.isdigit():
                    await interaction.response.send_message(
                        '天數必須是純數字喵', ephemeral=True,
                    )
                    return
                days = int(days_raw)
                if not 1 <= days <= _MAX_BAN_DAYS:
                    await interaction.response.send_message(
                        f'天數範圍是 1 ~ {_MAX_BAN_DAYS} 喵',
                        ephemeral=True,
                    )
                    return

        # 找該 serial 目前 active 的分享紀錄
        entry_key, entry = _resolve_active_share_by_serial(serial)
        if entry_key is None or entry is None:
            await interaction.response.send_message(
                f'找不到流水號 {_format_serial(serial)} 目前 active 的分享'
                f'（可能已被處置 / 流水號不存在）',
                ephemeral=True,
            )
            return
        sharer_id = int(entry.get('sharer_id') or 0)

        await interaction.response.defer(ephemeral=True, thinking=True)

        # 1. 走檢舉機制：刪原訊息 + 轉送 admin
        admin_msg_id = await _forward_to_admin_via_serial(
            interaction.client, entry_key, entry,
            banned_by=interaction.user.id,
        )

        # 2. 視 perm / days 決定是否同時封禁該用戶
        ban_note = ''
        if self.perm:
            await _set_ban(
                str(sharer_id), btype='perm', days=None,
                by=interaction.user.id,
            )
            ban_note = f'\n⛔ 已永久封禁 <@{sharer_id}>'
        elif days is not None:
            ban_entry = await _set_ban(
                str(sharer_id), btype='temp', days=days,
                by=interaction.user.id,
            )
            until_str = ban_entry.get('until') or ''
            until_disp = ''
            if until_str:
                ts = int(datetime.fromisoformat(until_str).timestamp())
                until_disp = f'，至 <t:{ts}:f>'
            ban_note = (f'\n🚫 已封禁 <@{sharer_id}> {days} 天{until_disp}')

        if admin_msg_id is None:
            await interaction.followup.send(
                f'⚠️ 轉送 admin channel 失敗，但流水號 {_format_serial(serial)} '
                f'本子的訊息已嘗試收回{ban_note}',
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.followup.send(
                f'✅ 流水號 {_format_serial(serial)} 已收回原訊息並轉送 '
                f'<#{_ADMIN_CHANNEL_ID}> 審核{ban_note}',
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        try:
            await self.opener.edit_original_response(
                embed=_admin_home_embed(),
                view=AdminPanelHomeView(opener=self.opener),
            )
        except discord.HTTPException:
            pass


# ─────────────────────────────────────────────────────────────────────────
# 入口 / Persistent view 註冊
# ─────────────────────────────────────────────────────────────────────────
def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name='每日任務', description='開啟每日任務表單頁')
    async def slash_daily_task(interaction: discord.Interaction):
        uid  = str(interaction.user.id)
        view = DailyTaskHomeView(uid, opener=interaction)
        await interaction.response.send_message(
            embed=_home_embed(uid),
            view=view,
            ephemeral=True,
        )

    @tree.command(name='管理員',
                  description='管理員工具：每日任務 封禁 / 解禁')
    async def slash_admin(interaction: discord.Interaction):
        if interaction.user.id not in _ADMIN_IDS:
            await interaction.response.send_message(
                '你沒有使用此指令的權限喵', ephemeral=True,
            )
            return
        view = AdminPanelHomeView(opener=interaction)
        await interaction.response.send_message(
            embed=_admin_home_embed(), view=view, ephemeral=True,
        )


def register_persistent_views(client: discord.Client) -> None:
    """on_ready 時呼叫一次：讓 bot 重啟後仍能接舊訊息上的按鈕點擊。"""
    client.add_view(ReportView())
    client.add_view(AdminReportView())
