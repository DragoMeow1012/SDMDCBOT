"""
/fishing 釣魚系統 + 釣魚相關物品狀態（釣竿 / 魚餌 / 保溫箱）+ 贈禮系統。

UI 流程：
  /fishing → FishingMainView (ephemeral E_fish)
    │
    ├─ 選釣竿 / 選魚餌 (Select) → 更新裝備並 refresh
    │
    ├─ 開始釣魚 (在白名單頻道) → edit_message → FishingSessionView
    │   ├─ (時長/2) 中段提示 → edit 進度文字
    │   └─ (時長) 完成 → edit → FishingResultView
    │       ├─ 出售 → 賣魚 + edit + 重新甩勾 / 關閉
    │       ├─ 保留 → 進保溫箱 + edit + 重新甩勾 / 關閉
    │       └─ 重新甩勾 → delete + followup 發新主面板
    │
    ├─ 購買釣竿 / 購買魚餌 → 子面板
    ├─ 魚類圖鑑 → DexView (分頁)
    ├─ 設定頻道白名單 → WhitelistView
    └─ 關閉

資料：
  - fishing.json 見 _DEFAULT_USER_REC
  - fishing_whitelist.json -> {<guild_id>: [<channel_id>, ...]}
  - gifts.json 見 GIFT_FILE schema

背景任務：
  - cleanup_active_sessions_on_restart() 在 main.py on_ready 呼叫一次
  - start_gift_expire_task(client) 每 5 分鐘檢查 48h 過期
"""
from __future__ import annotations

import asyncio
import os
import random
import secrets
from datetime import datetime, timedelta
from typing import Any

import discord
from discord import app_commands

from commands._wallet import apply_delta, get_balance
from data.fishing_data import (
    BAIT_SPECS, BASE_TIME_MAX, BASE_TIME_MIN, DAILY_FIRST_BONUS_RATE,
    FISH_BY_RARITY, FISH_POND_CAP, FISH_POND_EXPAND_COST, FISH_POND_EXPAND_STEP,
    FISH_SPECS, MYTHIC_BASE_CHANCE, NEW_SPECIES_BONUS_RATE,
    PROGRESS_TICK, RARITIES, RARITY_COLOR, RARITY_EMOJI, RARITY_LABEL,
    SPECIAL_TAG_COLOR, SPECIAL_TAG_HINT,
    ROD_SPECS, STARTER_ROD, TIME_FLOOR,
    LUCK_POTION_I_PRICE, LUCK_POTION_I_REDUCE,
    LUCK_POTION_II_PRICE, LUCK_POTION_II_LUCK,
    LUCK_POTION_III_PRICE,
    MEOW_BLESSING_MINUTES_BY_RARITY, MEOW_BLESSING_TAG_BONUS_MINUTES,
    MEOW_BLESSING_MAX_MINUTES, MEOW_BLESSING_PRICE_MULT,
    WEIGHT_CLASS_PROBS, WEIGHT_CLASS_PRICE_MULT, WEIGHT_CLASS_LABEL,
    WEIGHT_CLASS_KG_RANGE, RARITY_BASE_WEIGHT_KG,
    RADAR_PRICE, RADAR_UNSEEN_BIAS,
    POTION_DURATION_MIN, POTION_STACK_CAP_MIN,
    SPECIAL_TAGS, SPECIAL_TAG_BASE_CHANCE, SPECIAL_TAG_POTION_CHANCE,
    HOOK_TIMEOUT_SEC,
)
from data.fishing_script import pick_story as _script_pick_story, pick_tag as _script_pick_tag
from utils.json_store import load_json, save_json, save_json_async


_FILE           = os.path.join('data', 'fishing.json')
_WL_FILE        = os.path.join('data', 'fishing_whitelist.json')
_GIFT_FILE      = os.path.join('data', 'gifts.json')

GIFT_EXPIRE_HOURS = 48
_GIFT_CHECK_INTERVAL = 300.0    # 5 分鐘檢查一次過期

# fishing.json 的全局 read-modify-write lock：所有寫入路徑都該 hold 這把鎖，
# 避免 coroutine A load → B load → A save → B save 把 A 的改動覆蓋掉。
# 同 module 內 sync save_json (lock 內呼叫) 不會 yield 控制權，所以絕對 atomic。
_FILE_LOCK = asyncio.Lock()


_DEFAULT_USER_REC: dict[str, Any] = {
    'rods':                [],                  # 不再贈送 T1，需玩家自行購買
    'equipped_rod':        None,
    'baits':               {},                  # bait_key -> count
    'selected_bait':       None,                # 上次選過的 bait key
    'fish':                {},                  # fish_key -> count
    'dex':                 [],                  # 已釣過的 fish_key（含垃圾）
    'first_catch_day':     None,                # 'YYYY-MM-DD'
    # 待結算 bonus (首釣 + 新種)。stored_key -> 金額；保留時計算 + 寫入；賣魚時 pop 全給。
    # 多次保留同 stored_key 會累加（同一條魚 inventory 只能拿一次 bonus，其他無 bonus）。
    'pending_bonus':       {},
    'active_session':      None,                # 見 _start_session_record
    'pond_cap_extra':      0,                   # 已擴充的保溫箱格數（每次 +5）
    'luck_potion_i_until':   None,              # 幸運藥水 I 到期 ISO
    'luck_potion_ii_until':  None,              # 幸運藥水 II 到期 ISO
    'luck_potion_iii_until': None,              # 幸運藥水 III 到期 ISO
    'radar_until':           None,              # 物種雷達 到期 ISO
    'meow_blessing_until':   None,              # 小龍喵祝福 到期 ISO（同時生效三種幸運）
    'meow_feed_day':         None,              # 最後一次投餵的日期 YYYY-MM-DD（每日 1 次）
}


# ── 特殊 tag helpers ───────────────────────────────────────────────────
def _split_stored_key(stored: str) -> tuple[str, str | None, str | None]:
    """保溫箱 key 格式：'fish_key[#tag][#w=class]'，解析回 (fish_key, tag, weight_class)。
    向後相容：舊資料 'fish_key' / 'fish_key#tag' 解析時 weight_class 為 None（= normal）。"""
    parts = stored.split('#')
    fish_key = parts[0]
    tag = None
    weight_class = None
    for p in parts[1:]:
        if p.startswith('w='):
            weight_class = p[2:] or None
        elif tag is None:
            tag = p
    return fish_key, tag, weight_class


def _join_stored_key(fish_key: str, tag: str | None,
                     weight_class: str | None = None) -> str:
    parts = [fish_key]
    if tag and tag in SPECIAL_TAGS:
        parts.append(tag)
    if weight_class and weight_class != 'normal':
        parts.append(f'w={weight_class}')
    return '#'.join(parts)


def get_tagged_price(fish_key: str, tag: str | None,
                     weight_class: str | None = None) -> int:
    base = int(FISH_SPECS[fish_key]['price'])
    if tag and tag in SPECIAL_TAGS:
        base = int(base * SPECIAL_TAGS[tag])
    if weight_class and weight_class in WEIGHT_CLASS_PRICE_MULT:
        base = int(base * WEIGHT_CLASS_PRICE_MULT[weight_class])
    return base


def display_fish_name(fish_key: str, tag: str | None,
                      weight_class: str | None = None) -> str:
    name = FISH_SPECS[fish_key]['name']
    if tag and tag in SPECIAL_TAGS:
        name = f'【{tag}】{name}'
    if weight_class and weight_class in WEIGHT_CLASS_LABEL and WEIGHT_CLASS_LABEL[weight_class]:
        name = f'{WEIGHT_CLASS_LABEL[weight_class]} {name}'
    return name


def _roll_special_tag(*, boost_level: int, rod_bonus: float = 0.0) -> str | None:
    """變體 tag 抽籤：每層藥水 III／祝福 把基礎 1% → 2.5% → ... 線性加成。"""
    step = SPECIAL_TAG_POTION_CHANCE - SPECIAL_TAG_BASE_CHANCE
    chance = SPECIAL_TAG_BASE_CHANCE + step * max(0, boost_level) + max(0.0, rod_bonus)
    if random.random() >= chance:
        return None
    return random.choice(list(SPECIAL_TAGS.keys()))


def _rod_effect_str(spec: dict) -> str:
    """釣竿效果格式化（UI 多處共用）。mythic_bonus 為 0 時不顯示，避免噪音。"""
    parts = [
        f'時長 -{int(spec["time_reduce"]*100)}%',
        f'變體 +{spec["tag_bonus"]*100:g}%',
    ]
    mb = float(spec.get('mythic_bonus', 0.0))
    if mb > 0:
        parts.append(f'神話 +{mb*100:g}%')
    return ' / '.join(parts)


def _buff_status_line(uid: str) -> str:
    """組目前 buff 顯示行。無 buff 回空字串。"""
    parts: list[str] = []
    blessing = _get_active_buff_until(uid, 'meow_blessing_until')
    if blessing:
        parts.append(f'🍀 小龍喵祝福 <t:{int(blessing.timestamp())}:R>')
    p1 = _get_active_buff_until(uid, 'luck_potion_i_until')
    if p1:
        parts.append(f'🧪 幸運Ⅰ <t:{int(p1.timestamp())}:R>')
    p2 = _get_active_buff_until(uid, 'luck_potion_ii_until')
    if p2:
        parts.append(f'🍀 幸運Ⅱ <t:{int(p2.timestamp())}:R>')
    p3 = _get_active_buff_until(uid, 'luck_potion_iii_until')
    if p3:
        parts.append(f'✨ 幸運Ⅲ <t:{int(p3.timestamp())}:R>')
    radar = _get_active_buff_until(uid, 'radar_until')
    if radar:
        parts.append(f'📡 雷達 <t:{int(radar.timestamp())}:R>')
    return ' ｜ '.join(parts)


# ── 釣魚 buff helpers ───────────────────────────────────────────────────
def _get_active_buff_until(uid: str, field: str) -> datetime | None:
    rec = get_user_rec(uid)
    s = rec.get(field)
    if not s:
        return None
    try:
        exp = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    return exp if exp > datetime.now() else None


def meow_blessing_active(uid: str) -> bool:
    return _get_active_buff_until(uid, 'meow_blessing_until') is not None


def get_meow_blessing_until(uid: str) -> datetime | None:
    return _get_active_buff_until(uid, 'meow_blessing_until')


def fed_meow_today(uid: str) -> bool:
    return get_user_rec(uid).get('meow_feed_day') == _today_str()


def potion_i_active(uid: str) -> bool:
    return (meow_blessing_active(uid)
            or _get_active_buff_until(uid, 'luck_potion_i_until') is not None)


def potion_ii_active(uid: str) -> bool:
    return (meow_blessing_active(uid)
            or _get_active_buff_until(uid, 'luck_potion_ii_until') is not None)


def potion_iii_active(uid: str) -> bool:
    return (meow_blessing_active(uid)
            or _get_active_buff_until(uid, 'luck_potion_iii_until') is not None)


# ── stack 計數（祝福與藥水可疊加；mutex 仍只限制三種藥水彼此間）─────────
def potion_i_stack(uid: str) -> int:
    n = 1 if meow_blessing_active(uid) else 0
    if _get_active_buff_until(uid, 'luck_potion_i_until'):
        n += 1
    return n


def potion_ii_stack(uid: str) -> int:
    n = 1 if meow_blessing_active(uid) else 0
    if _get_active_buff_until(uid, 'luck_potion_ii_until'):
        n += 1
    return n


def potion_iii_stack(uid: str) -> int:
    n = 1 if meow_blessing_active(uid) else 0
    if _get_active_buff_until(uid, 'luck_potion_iii_until'):
        n += 1
    return n


def apply_blessing_multiplier(uid: str, price: int) -> int:
    """祝福生效時，售價 ×MEOW_BLESSING_PRICE_MULT。"""
    if meow_blessing_active(uid):
        return int(price * MEOW_BLESSING_PRICE_MULT)
    return price


def radar_active(uid: str) -> bool:
    return _get_active_buff_until(uid, 'radar_until') is not None


def get_potion_i_until(uid: str) -> datetime | None:
    return _get_active_buff_until(uid, 'luck_potion_i_until')


def get_potion_ii_until(uid: str) -> datetime | None:
    return _get_active_buff_until(uid, 'luck_potion_ii_until')


def get_potion_iii_until(uid: str) -> datetime | None:
    return _get_active_buff_until(uid, 'luck_potion_iii_until')


def get_radar_until(uid: str) -> datetime | None:
    return _get_active_buff_until(uid, 'radar_until')


_POTION_FIELDS: dict[str, tuple[str, str]] = {
    'i':   ('luck_potion_i_until',   '幸運藥水 I'),
    'ii':  ('luck_potion_ii_until',  '幸運藥水 II'),
    'iii': ('luck_potion_iii_until', '幸運藥水 III'),
}


def _other_active_potion(uid: str, self_kind: str) -> str | None:
    """回傳目前生效中、且 kind != self_kind 的藥水名稱（用於互斥檢查）。"""
    for k, (field, name) in _POTION_FIELDS.items():
        if k == self_kind:
            continue
        if _get_active_buff_until(uid, field) is not None:
            return name
    return None


async def buy_luck_potion(uid: str, kind: str) -> tuple[bool, str, datetime | None]:
    """kind = 'i' / 'ii' / 'iii'。買新覆蓋舊：任何既有藥水都會被清空，新藥水
    從 now+POTION_DURATION_MIN 起算。三種藥水彼此互不共存（買哪個就只剩哪個）。
    祝福屬獨立系統，不受影響。"""
    if kind not in ('i', 'ii', 'iii'):
        return False, '未知藥水', None
    field = _POTION_FIELDS[kind][0]
    price = (LUCK_POTION_I_PRICE if kind == 'i'
             else LUCK_POTION_II_PRICE if kind == 'ii'
             else LUCK_POTION_III_PRICE)

    cur_balance = get_balance(uid)
    if cur_balance < price:
        return False, f'餘額不足，需要 {price:,}（你有 {cur_balance:,}）', None

    now = datetime.now()
    new_exp = now + timedelta(minutes=POTION_DURATION_MIN)

    await apply_delta(uid, -price)
    async with _FILE_LOCK:
        data = _load_all()
        rec = _get_or_init_rec(data, uid)
        # 買新覆蓋舊：先清空全部三種藥水，再把目標寫入
        for f, _ in _POTION_FIELDS.values():
            rec[f] = None
        rec[field] = new_exp.isoformat()
        save_json(_FILE, data)
    return True, '', new_exp


async def buy_luck_potion_iii(uid: str) -> tuple[bool, str, datetime | None]:
    """向後相容包裝：等價於 buy_luck_potion(uid, 'iii')。"""
    return await buy_luck_potion(uid, 'iii')


def compute_meow_duration_minutes(fish_key: str, tag: str | None,
                                  weight_class: str | None = None) -> int:
    """投餵某條魚的祝福時長（分鐘）。trash 回 0。
    base = 稀有度時長 + tag 加成（固定 +120），再 × 重量倍率（用 WEIGHT_CLASS_PRICE_MULT），cap 360。"""
    spec = FISH_SPECS.get(fish_key)
    if not spec:
        return 0
    base = MEOW_BLESSING_MINUTES_BY_RARITY.get(spec['rarity'], 0)
    if base <= 0:
        return 0
    bonus = MEOW_BLESSING_TAG_BONUS_MINUTES if (tag and tag in SPECIAL_TAGS) else 0
    weight_mult = WEIGHT_CLASS_PRICE_MULT.get(weight_class or 'normal', 1.0)
    total = int((base + bonus) * weight_mult)
    return min(MEOW_BLESSING_MAX_MINUTES, total)


async def feed_meow(
    uid: str, stored_key: str,
) -> tuple[bool, str, datetime | None, int]:
    """投餵小龍喵：消耗 1 條魚，啟動祝福。

    Returns (ok, msg, new_until, duration_minutes)。
    一天一次；trash 不可投餵；保溫箱必須有此魚。
    """
    fish_key, tag, weight_class = _split_stored_key(stored_key)
    if fish_key not in FISH_SPECS:
        return False, '未知魚種', None, 0
    minutes = compute_meow_duration_minutes(fish_key, tag, weight_class)
    if minutes <= 0:
        return False, '🦞 小龍喵不吃這種魚', None, 0

    today = _today_str()
    new_exp = datetime.now() + timedelta(minutes=minutes)

    async with _FILE_LOCK:
        data = _load_all()
        rec = _get_or_init_rec(data, uid)
        if rec.get('meow_feed_day') == today:
            return False, '🦞 今天已經餵過小龍喵了，明天再來', None, 0
        fish = rec.get('fish', {})
        if int(fish.get(stored_key, 0)) <= 0:
            return False, '保溫箱裡沒有這條魚', None, 0
        fish[stored_key] = int(fish[stored_key]) - 1
        if fish[stored_key] == 0:
            fish.pop(stored_key)
        rec['meow_blessing_until'] = new_exp.isoformat()
        rec['meow_feed_day']       = today
        save_json(_FILE, data)
    return True, '', new_exp, minutes


async def buy_radar(uid: str) -> tuple[bool, str, datetime | None]:
    cur_balance = get_balance(uid)
    if cur_balance < RADAR_PRICE:
        return False, f'餘額不足，需要 {RADAR_PRICE:,}（你有 {cur_balance:,}）', None

    now = datetime.now()
    cur_exp = _get_active_buff_until(uid, 'radar_until')
    base = cur_exp if cur_exp else now
    new_exp = base + timedelta(minutes=POTION_DURATION_MIN)
    cap = now + timedelta(minutes=POTION_STACK_CAP_MIN)
    if new_exp > cap:
        return False, f'❌ 已達累計上限 {POTION_STACK_CAP_MIN // 60} 小時', cur_exp

    await apply_delta(uid, -RADAR_PRICE)
    async with _FILE_LOCK:
        data = _load_all()
        rec = _get_or_init_rec(data, uid)
        rec['radar_until'] = new_exp.isoformat()
        save_json(_FILE, data)
    return True, '', new_exp


def get_fish_pond_cap(uid: str) -> int:
    """保溫箱有效容量 = FISH_POND_CAP 基礎 + 已擴充。"""
    extra = int(get_user_rec(uid).get('pond_cap_extra', 0))
    return FISH_POND_CAP + extra


async def expand_fish_pond(uid: str) -> tuple[bool, str, int]:
    """擴充保溫箱：扣 FISH_POND_EXPAND_COST，+ FISH_POND_EXPAND_STEP 格。
    回 (ok, err_msg, new_cap)。"""
    cur_balance = get_balance(uid)
    if cur_balance < FISH_POND_EXPAND_COST:
        return False, (
            f'餘額不足，需要 {FISH_POND_EXPAND_COST:,}（你有 {cur_balance:,}）'
        ), get_fish_pond_cap(uid)
    # 扣錢 + 擴格 在 wallet 和 fishing.json 是不同檔案，先扣後加；任一失敗時撤回
    await apply_delta(uid, -FISH_POND_EXPAND_COST)
    async with _FILE_LOCK:
        data = _load_all()
        rec = _get_or_init_rec(data, uid)
        rec['pond_cap_extra'] = int(rec.get('pond_cap_extra', 0)) + FISH_POND_EXPAND_STEP
        new_cap = FISH_POND_CAP + rec['pond_cap_extra']
        save_json(_FILE, data)
    return True, '', new_cap


# ── 共用：取 / 寫整份 fishing.json ──────────────────────────────────────
def _load_all() -> dict:
    data = load_json(_FILE)
    data.setdefault('users', {})
    return data


def _get_or_init_rec(data: dict, uid: str) -> dict:
    users = data.setdefault('users', {})
    rec = users.get(uid)
    if rec is None:
        rec = {k: (v.copy() if isinstance(v, (dict, list)) else v)
               for k, v in _DEFAULT_USER_REC.items()}
        users[uid] = rec
        return rec
    # 缺欄位補齊（舊存檔向前相容）
    for k, v in _DEFAULT_USER_REC.items():
        if k not in rec:
            rec[k] = v.copy() if isinstance(v, (dict, list)) else v
    return rec


def get_user_rec(uid: str) -> dict:
    """唯讀取得用戶 fishing 紀錄；不存在時回 default copy（不會寫檔）。"""
    data = _load_all()
    rec = data.get('users', {}).get(uid)
    if rec is None:
        return {k: (v.copy() if isinstance(v, (dict, list)) else v)
                for k, v in _DEFAULT_USER_REC.items()}
    out = {**_DEFAULT_USER_REC, **rec}
    return out


# ── 公開：物品 inventory helpers（供 shop.py 販售/贈禮使用） ────────────
def get_rods(uid: str) -> list[str]:
    return list(get_user_rec(uid).get('rods', []))


def get_baits(uid: str) -> dict[str, int]:
    return dict(get_user_rec(uid).get('baits', {}))


def get_fish(uid: str) -> dict[str, int]:
    return dict(get_user_rec(uid).get('fish', {}))


def get_equipped_rod(uid: str) -> str | None:
    """玩家當前裝備的釣竿；沒裝備（或不存在於擁有列表）回 None。"""
    rec = get_user_rec(uid)
    eq = rec.get('equipped_rod')
    rods = rec.get('rods', [])
    if eq and eq in rods:
        return eq
    # 自動回退：若有任何擁有的竿，挑第一支
    if rods:
        return rods[0]
    return None


def get_fish_count_total(uid: str) -> int:
    return sum(int(v) for v in get_user_rec(uid).get('fish', {}).values())


def get_dex(uid: str) -> set[str]:
    return set(get_user_rec(uid).get('dex', []))


def get_dex_tags(uid: str) -> dict[str, list[str]]:
    """fish_key -> 已釣過的變體 tag 列表（依首次釣到順序）。"""
    return get_user_rec(uid).get('dex_tags', {}) or {}


def get_dex_max_weight(uid: str) -> dict[str, float]:
    """fish_key -> 史上最重 kg。"""
    raw = get_user_rec(uid).get('dex_max_weight', {}) or {}
    return {k: float(v) for k, v in raw.items()}


def get_catch_history(uid: str, limit: int = 20) -> list[dict]:
    """回傳最近的釣魚紀錄（最近的在最後）。"""
    hist = get_user_rec(uid).get('catch_history', []) or []
    return list(hist[-limit:])


async def adjust_rod(uid: str, key: str, *, add: bool) -> tuple[bool, str]:
    """加入 / 移除一支竿。移除已裝備竿時自動切到剩餘任一支或 None。"""
    if key not in ROD_SPECS:
        return False, f'未知釣竿 {key}'
    async with _FILE_LOCK:
        data = _load_all()
        rec = _get_or_init_rec(data, uid)
        rods = rec['rods']
        if add:
            if key in rods:
                return False, '你已經擁有這支竿'
            rods.append(key)
            # 第一支竿購入時自動裝備
            if rec.get('equipped_rod') is None:
                rec['equipped_rod'] = key
        else:
            if key not in rods:
                return False, '你沒有這支竿'
            rods.remove(key)
            if rec.get('equipped_rod') == key:
                rec['equipped_rod'] = rods[0] if rods else None
        save_json(_FILE, data)
    return True, ''


async def adjust_bait(uid: str, key: str, delta: int) -> tuple[bool, str]:
    """魚餌數量 ±N。delta<0 時若不夠扣回 False。"""
    if key not in BAIT_SPECS:
        return False, f'未知魚餌 {key}'
    async with _FILE_LOCK:
        data = _load_all()
        rec = _get_or_init_rec(data, uid)
        baits = rec['baits']
        cur = int(baits.get(key, 0))
        new = cur + delta
        if new < 0:
            return False, f'魚餌不足（持有 {cur}，要扣 {-delta}）'
        if new == 0:
            baits.pop(key, None)
        else:
            baits[key] = new
        save_json(_FILE, data)
    return True, ''


async def adjust_fish(uid: str, key: str, delta: int) -> tuple[bool, str]:
    """魚數量 ±N。key 可為 'fish_key' 或 'fish_key#tag'（特殊魚）。
    delta>0 時受保溫箱（動態）容量限制。"""
    fish_key, tag, weight_class = _split_stored_key(key)
    if fish_key not in FISH_SPECS:
        return False, f'未知魚種 {fish_key}'
    if tag and tag not in SPECIAL_TAGS:
        return False, f'未知 tag {tag}'
    async with _FILE_LOCK:
        data = _load_all()
        rec = _get_or_init_rec(data, uid)
        fish = rec['fish']
        cur = int(fish.get(key, 0))
        new = cur + delta
        if new < 0:
            return False, f'魚數量不足（持有 {cur}，要扣 {-delta}）'
        if delta > 0:
            cap = FISH_POND_CAP + int(rec.get('pond_cap_extra', 0))
            total_after = sum(int(v) for v in fish.values()) + delta
            if total_after > cap:
                return False, f'保溫箱已滿（上限 {cap} 條，可到商店擴充）'
        if new == 0:
            fish.pop(key, None)
        else:
            fish[key] = new
        save_json(_FILE, data)
    return True, ''


async def set_equipped_rod(uid: str, key: str) -> tuple[bool, str]:
    if key not in ROD_SPECS:
        return False, '未知釣竿'
    async with _FILE_LOCK:
        data = _load_all()
        rec = _get_or_init_rec(data, uid)
        if key not in rec['rods']:
            return False, '你還沒擁有這支竿'
        rec['equipped_rod'] = key
        save_json(_FILE, data)
    return True, ''


async def set_selected_bait(uid: str, key: str | None) -> None:
    async with _FILE_LOCK:
        data = _load_all()
        rec = _get_or_init_rec(data, uid)
        rec['selected_bait'] = key
        save_json(_FILE, data)


# ── 釣魚邏輯 ────────────────────────────────────────────────────────────
_RARITY_BASE_SEC: dict[str, float] = {
    'trash':      8.0,
    'common':    16.0,
    'rare':      28.0,
    'epic':      42.0,
    'legendary': 60.0,
    'mythic':    80.0,
}


def _compute_duration(rod_key: str, bait_key: str, rarity: str,
                      *, potion_i_stack: int = 0) -> float:
    """依稀有度決定基準時長 ± 20% 變化，再套 rod 時長縮短 + bait 平面減秒。
    幸運藥水 I（每層）額外 -20% 時長 — 祝福 + 藥水可疊加。最終 clamp 到 [TIME_FLOOR, 60s]。

    稀有度基準時長：trash 8s / common 16s / rare 28s / epic 42s / legendary 60s。
    """
    base = _RARITY_BASE_SEC.get(rarity, 16.0) * random.uniform(0.8, 1.2)
    rod = ROD_SPECS[rod_key]
    bait = BAIT_SPECS[bait_key]
    total_reduce = rod['time_reduce'] + LUCK_POTION_I_REDUCE * max(0, potion_i_stack)
    after_rod = base * (1.0 - min(total_reduce, 0.85))   # 上限 85% 縮短
    final = after_rod - bait['flat_time_reduce_sec']
    return min(60.0, max(TIME_FLOOR, final))


def _roll_rarity(rod_key: str, bait_key: str,
                 *, potion_ii_stack: int = 0) -> str:
    """套用 bait base 權重 + bait 特效 (+幸運藥水 II × 層數) → 加權隨機 rarity。
    祝福 + 藥水可疊加（每層 +LUCK_POTION_II_LUCK）。
    釣竿不再影響稀有度（改為影響特殊變體 tag 機率，見 _roll_special_tag）。
    神話魚為獨立 roll：在所有 bait 邏輯之前判定；pool 為空時跳過避免空抽。
    """
    rod = ROD_SPECS[rod_key]
    bait = BAIT_SPECS[bait_key]

    # 神話獨立 roll：base + rod mythic_bonus；pool 必須非空才生效
    if FISH_BY_RARITY.get('mythic'):
        mythic_chance = MYTHIC_BASE_CHANCE + float(rod.get('mythic_bonus', 0.0))
        if random.random() < mythic_chance:
            return 'mythic'

    luck = int(bait['extra_luck']) + LUCK_POTION_II_LUCK * max(0, potion_ii_stack)

    # bait 10% 機率直接強制 epic+
    if bait['force_epic_plus_pct'] > 0 and random.random() < bait['force_epic_plus_pct']:
        return 'legendary' if random.random() < 0.3 else 'epic'

    # 套 luck factor（只作用在前 5 級；mythic 走獨立 roll，不在 weights 內）
    weights = list(bait['weights'])
    luck_factor = [
        max(0.1, 1.0 - luck / 100.0),     # trash
        max(0.3, 1.0 - luck / 200.0),     # common
        1.0 + luck / 400.0,                # rare
        1.0 + luck / 200.0,                # epic
        1.0 + luck / 100.0,                # legendary
    ]
    final = [max(0.0, weights[i] * luck_factor[i]) for i in range(5)]

    # bait 「保底罕見以上」→ 把 trash/common 設 0
    if bait['force_rare_plus']:
        final[0] = 0.0
        final[1] = 0.0

    total = sum(final)
    if total <= 0:
        return 'common'   # 不該發生的 fallback
    r = random.random() * total
    cum = 0.0
    for i, w in enumerate(final):
        cum += w
        if r <= cum:
            return RARITIES[i]
    return 'legendary'   # 對應 weights 最後一格


def _roll_species(rarity: str, *, dex: set[str] | None = None,
                  radar_active: bool = False) -> str:
    """從 rarity 對應的魚種池抽一隻；雷達生效時 50% 機率從「沒釣過」子池抽。"""
    pool = FISH_BY_RARITY.get(rarity) or FISH_BY_RARITY['common']
    if radar_active and dex is not None:
        unseen = [k for k in pool if k not in dex]
        if unseen and random.random() < RADAR_UNSEEN_BIAS:
            return random.choice(unseen)
    return random.choice(pool)


def _roll_weight(rarity: str) -> tuple[str, float]:
    """抽一個重量類別 + 取樣 kg。回 (weight_class, weight_kg)。"""
    r = random.random()
    cum = 0.0
    chosen = 'normal'
    for cls, p in WEIGHT_CLASS_PROBS:
        cum += p
        if r <= cum:
            chosen = cls
            break
    base_kg = float(RARITY_BASE_WEIGHT_KG.get(rarity, 1.0))
    lo, hi = WEIGHT_CLASS_KG_RANGE.get(chosen, (0.85, 1.20))
    kg = base_kg * random.uniform(lo, hi)
    return chosen, round(kg, 2)


def _roll_price(fish_key: str) -> int:
    # 每種魚固定售價（與 rarity 等級關係 fix 在 FISH_SPECS 內）
    return int(FISH_SPECS[fish_key]['price'])


# ── Whitelist ───────────────────────────────────────────────────────────
def _load_wl() -> dict:
    return load_json(_WL_FILE) or {}


async def _save_wl(data: dict) -> None:
    await save_json_async(_WL_FILE, data)


def get_whitelist(guild_id: int) -> list[int]:
    return [int(c) for c in (_load_wl().get(str(guild_id)) or [])]


def is_channel_whitelisted(guild_id: int, channel_id: int) -> bool:
    """白名單為空 = 禁止所有頻道（需先設定才能釣魚）；非空 = 只准白名單頻道。"""
    wl = get_whitelist(guild_id)
    if not wl:
        return False
    return int(channel_id) in wl


async def add_whitelist(guild_id: int, channel_id: int) -> bool:
    data = _load_wl()
    arr = data.setdefault(str(guild_id), [])
    if int(channel_id) in [int(c) for c in arr]:
        return False
    arr.append(int(channel_id))
    await _save_wl(data)
    return True


async def remove_whitelist(guild_id: int, channel_id: int) -> bool:
    data = _load_wl()
    arr = data.get(str(guild_id)) or []
    cid_int = int(channel_id)
    new_arr = [int(c) for c in arr if int(c) != cid_int]
    if len(new_arr) == len(arr):
        return False
    if new_arr:
        data[str(guild_id)] = new_arr
    else:
        data.pop(str(guild_id), None)
    await _save_wl(data)
    return True


# ── Active session helpers ──────────────────────────────────────────────
def _today_str() -> str:
    return datetime.now().date().isoformat()


async def _start_session_record(uid: str, *, rod_key: str, bait_key: str,
                                channel_id: int, duration_s: float,
                                predetermined_fish: str | None = None,
                                story_tag: str | None = None) -> None:
    """寫入 active_session（已先扣餌）。釣魚結束會 set pending_catch；
    出售/保留後 clear。預先決定的魚種與劇本 tag 也在這裡寫入。"""
    async with _FILE_LOCK:
        data = _load_all()
        rec = _get_or_init_rec(data, uid)
        now = datetime.now()
        rec['active_session'] = {
            'rod_key':            rod_key,
            'bait_key':           bait_key,
            'started_at':         now.isoformat(),
            'expected_finish':    (now + timedelta(seconds=duration_s)).isoformat(),
            'channel_id':         int(channel_id),
            'pending_catch':      None,
            'predetermined_fish': predetermined_fish,
            'story_tag':          story_tag,
        }
        save_json(_FILE, data)


async def _finish_session_record(uid: str, *, fish_key: str, price: int,
                                 is_new_species: bool, is_daily_first: bool,
                                 tag: str | None = None,
                                 weight_class: str | None = None,
                                 weight_kg: float = 0.0) -> None:
    """釣魚物理結束後寫 pending_catch（不 clear active_session）。tag 為特殊 tag、
    weight_class/weight_kg 為新加的重量資訊（normal class 也應傳入便於後續紀錄）。

    新魚種圖鑑獎勵 + 每日首釣獎勵：在此立即結算並 +錢包，不再走 sell/keep 結算路徑。
    保溫箱只負責保管魚與顯示。
    """
    # 算「新魚種圖鑑」與「每日首釣」獎勵：tag × weight 倍率先吃進 base，再 × bonus rate × 祝福
    def _bonus_after_mults(rate: float) -> int:
        b = int(price)
        if tag and tag in SPECIAL_TAGS:
            b = int(b * SPECIAL_TAGS[tag])
        if weight_class and weight_class in WEIGHT_CLASS_PRICE_MULT:
            b = int(b * WEIGHT_CLASS_PRICE_MULT[weight_class])
        out = int(b * rate)
        if meow_blessing_active(uid):
            out = int(out * MEOW_BLESSING_PRICE_MULT)
        return out

    is_real_fish = FISH_SPECS[fish_key]['rarity'] != 'trash'
    new_species_bonus = _bonus_after_mults(NEW_SPECIES_BONUS_RATE) if (is_new_species and is_real_fish) else 0
    daily_first_bonus = _bonus_after_mults(DAILY_FIRST_BONUS_RATE) if (is_daily_first and is_real_fish) else 0
    total_immediate_bonus = new_species_bonus + daily_first_bonus

    async with _FILE_LOCK:
        data = _load_all()
        rec = _get_or_init_rec(data, uid)
        sess = rec.get('active_session') or {}
        sess['pending_catch'] = {
            'fish_key':       fish_key,
            'tag':            tag,
            'weight_class':   weight_class,
            'weight_kg':      float(weight_kg),
            'price':          int(price),
            'is_new_species': bool(is_new_species),
            'is_daily_first': bool(is_daily_first),
            'new_species_bonus_paid': int(new_species_bonus),
            'daily_first_bonus_paid': int(daily_first_bonus),
        }
        rec['active_session'] = sess
        # 同時把 dex 紀錄 + 首釣日期更新（這部分玩家無論出售/保留都要記）
        dex = rec.setdefault('dex', [])
        if fish_key not in dex:
            dex.append(fish_key)
        # 變體 tag 圖鑑：記錄此 fish_key 釣過哪些 special tag（依首次出現順序）
        if tag and tag in SPECIAL_TAGS:
            dex_tags = rec.setdefault('dex_tags', {})
            tag_list = dex_tags.setdefault(fish_key, [])
            if tag not in tag_list:
                tag_list.append(tag)
        # 最大重量紀錄：保留每種 fish_key 史上最重
        if weight_kg > 0:
            dex_max_weight = rec.setdefault('dex_max_weight', {})
            cur_max = float(dex_max_weight.get(fish_key, 0.0))
            if weight_kg > cur_max:
                dex_max_weight[fish_key] = round(float(weight_kg), 2)
        # 釣魚歷史：最近 50 條
        history = rec.setdefault('catch_history', [])
        history.append({
            'fish_key':     fish_key,
            'tag':          tag,
            'weight_class': weight_class,
            'weight_kg':    round(float(weight_kg), 2) if weight_kg else 0.0,
            'price':        int(price),
            'ts':           datetime.now().isoformat(timespec='seconds'),
        })
        if len(history) > 50:
            del history[:len(history) - 50]
        rec['first_catch_day'] = _today_str()
        save_json(_FILE, data)

    # 新魚種圖鑑 + 每日首釣獎勵：立即入帳（鎖外做，與 fishing.json 寫入分離）
    if total_immediate_bonus > 0:
        await apply_delta(uid, total_immediate_bonus)


async def _clear_session(uid: str) -> None:
    async with _FILE_LOCK:
        data = _load_all()
        rec = _get_or_init_rec(data, uid)
        rec['active_session'] = None
        save_json(_FILE, data)


def get_active_session(uid: str) -> dict | None:
    rec = get_user_rec(uid)
    return rec.get('active_session')


# ── 「保留 / 出售」結算 ─────────────────────────────────────────────────
async def keep_pending(uid: str) -> tuple[bool, str]:
    """把 active_session.pending_catch 加入 fish inventory + clear session。
    垃圾理論上不會走到這（task 內已自動賣），但保險起見也做容錯：垃圾改為自動賣。
    若帶 tag，存成 'fish_key#tag[#w=class]'。

    新魚種 + 每日首釣獎勵已在 _finish_session_record 立即入帳，這裡不再算 pending bonus。
    """
    sess = get_active_session(uid)
    if not sess or not sess.get('pending_catch'):
        return False, '沒有待處理的釣獲'
    pc = sess['pending_catch']
    fish_key = pc['fish_key']
    tag = pc.get('tag')
    weight_class = pc.get('weight_class')
    if FISH_SPECS[fish_key]['rarity'] == 'trash':
        ok, msg, paid = await sell_pending(uid)
        return ok, msg

    stored = _join_stored_key(fish_key, tag, weight_class)
    async with _FILE_LOCK:
        data = _load_all()
        rec = _get_or_init_rec(data, uid)
        rec['fish'][stored] = int(rec['fish'].get(stored, 0)) + 1
        rec['active_session'] = None
        save_json(_FILE, data)
    return True, ''


async def sell_pending(uid: str) -> tuple[bool, str, int]:
    """賣掉 pending_catch（含首釣 / 新種獎勵 + 特殊 tag 倍率）並 clear session。
    回 (ok, msg, paid)。"""
    async with _FILE_LOCK:
        data = _load_all()
        rec = _get_or_init_rec(data, uid)
        sess = rec.get('active_session') or {}
        pc = sess.get('pending_catch')
        if not pc:
            return False, '沒有待處理的釣獲', 0
        fish_key = pc['fish_key']
        tag = pc.get('tag')
        weight_class = pc.get('weight_class')
        base = int(pc['price'])
        # 特殊 tag + 重量倍率（在 base 上）
        if tag and tag in SPECIAL_TAGS:
            base = int(base * SPECIAL_TAGS[tag])
        if weight_class and weight_class in WEIGHT_CLASS_PRICE_MULT:
            base = int(base * WEIGHT_CLASS_PRICE_MULT[weight_class])
        # 新魚種 / 每日首釣獎勵已在 _finish_session_record 立即入帳，這裡不再計入
        paid = max(0, base)
        rec['active_session'] = None
        save_json(_FILE, data)
    paid = apply_blessing_multiplier(uid, paid)
    if paid > 0:
        await apply_delta(uid, paid)
    return True, '', paid


# ── 賣魚（從保溫箱賣，給 shop.py 販售面板用） ──────────────────────────
def _consume_pending_bonus(rec: dict, stored_key: str) -> int:
    """賣魚時把待結算 bonus 全部拿掉（玩家賣 1 條也拿全部 bonus — 簡化）。
    必須在 _FILE_LOCK 內 + 來自 fresh _load_all 的 rec 上呼叫，本身不寫檔。"""
    pb = rec.get('pending_bonus') or {}
    bonus = int(pb.pop(stored_key, 0))
    if bonus > 0:
        rec['pending_bonus'] = pb
    return bonus


async def sell_fish_from_pond(uid: str, stored_key: str, qty: int = 1) -> tuple[bool, str, int]:
    """賣保溫箱裡的魚。stored_key 可帶 tag（'fish_key#tag'），單價以 tag 倍率計。
    若該 stored_key 有 pending_bonus，賣時一次拿完。"""
    if qty <= 0:
        return False, '數量必須為正', 0
    fish_key, tag, weight_class = _split_stored_key(stored_key)
    if fish_key not in FISH_SPECS:
        return False, '未知魚種', 0
    unit_price = get_tagged_price(fish_key, tag, weight_class)
    total_price = unit_price * qty

    async with _FILE_LOCK:
        data = _load_all()
        rec = _get_or_init_rec(data, uid)
        cur = int(rec['fish'].get(stored_key, 0))
        if cur < qty:
            return False, f'數量不足（持有 {cur}）', 0
        new_n = cur - qty
        if new_n > 0:
            rec['fish'][stored_key] = new_n
        else:
            rec['fish'].pop(stored_key, None)
        bonus = _consume_pending_bonus(rec, stored_key)
        save_json(_FILE, data)

    paid = apply_blessing_multiplier(uid, total_price + bonus)
    await apply_delta(uid, paid)
    return True, '', paid


async def sell_all_fish_by_rarity(uid: str, rarity: str) -> tuple[int, int]:
    """把指定稀有度的魚全部賣掉（含 tag 倍率 + 該 stored_key 累積 bonus）。"""
    async with _FILE_LOCK:
        data = _load_all()
        rec = _get_or_init_rec(data, uid)
        total_count = 0
        total_price = 0
        for stored, n in list(rec['fish'].items()):
            fk, tag, wc = _split_stored_key(stored)
            if fk not in FISH_SPECS:
                continue
            if FISH_SPECS[fk]['rarity'] != rarity:
                continue
            n = int(n)
            if n <= 0:
                continue
            gain = get_tagged_price(fk, tag, wc) * n
            gain += _consume_pending_bonus(rec, stored)
            rec['fish'].pop(stored, None)
            total_count += n
            total_price += gain
        save_json(_FILE, data)
    total_price = apply_blessing_multiplier(uid, total_price)
    if total_price != 0:
        await apply_delta(uid, total_price)
    return total_count, total_price


async def sell_all_fish(uid: str) -> tuple[int, int]:
    async with _FILE_LOCK:
        data = _load_all()
        rec = _get_or_init_rec(data, uid)
        total_count = 0
        total_price = 0
        for stored, n in list(rec['fish'].items()):
            fk, tag, wc = _split_stored_key(stored)
            if fk not in FISH_SPECS:
                continue
            n = int(n)
            if n <= 0:
                continue
            gain = get_tagged_price(fk, tag, wc) * n
            gain += _consume_pending_bonus(rec, stored)
            rec['fish'].pop(stored, None)
            total_count += n
            total_price += gain
        save_json(_FILE, data)
    total_price = apply_blessing_multiplier(uid, total_price)
    if total_price != 0:
        await apply_delta(uid, total_price)
    return total_count, total_price


# ── 賣釣竿 / 賣魚餌 (半價) ──────────────────────────────────────────────
def rod_sell_price(rod_key: str) -> int:
    return ROD_SPECS[rod_key]['price'] // 2


def bait_sell_price(bait_key: str) -> int:
    return max(1, BAIT_SPECS[bait_key]['price'] // 2)


async def sell_rod(uid: str, rod_key: str) -> tuple[bool, str, int]:
    if rod_key not in ROD_SPECS:
        return False, '未知釣竿', 0
    if rod_key not in get_rods(uid):
        return False, '你沒有這支竿', 0
    price = rod_sell_price(rod_key)
    ok, err = await adjust_rod(uid, rod_key, add=False)
    if not ok:
        return False, err, 0
    await apply_delta(uid, price)
    return True, '', price


async def sell_bait(uid: str, bait_key: str, qty: int = 1) -> tuple[bool, str, int]:
    if qty <= 0:
        return False, '數量必須為正', 0
    if bait_key not in BAIT_SPECS:
        return False, '未知魚餌', 0
    cur = int(get_baits(uid).get(bait_key, 0))
    if cur < qty:
        return False, f'魚餌不足（持有 {cur}）', 0
    price_per = bait_sell_price(bait_key)
    total = price_per * qty
    ok, err = await adjust_bait(uid, bait_key, -qty)
    if not ok:
        return False, err, 0
    await apply_delta(uid, total)
    return True, '', total


# ── 購買魚餌 ────────────────────────────────────────────────────────────
async def buy_bait(uid: str, bait_key: str, qty: int = 1) -> tuple[bool, str, int]:
    """從錢包扣 price*qty + 魚餌庫存 +qty。"""
    if qty <= 0:
        return False, '數量必須為正', 0
    if bait_key not in BAIT_SPECS:
        return False, '未知魚餌', 0
    price_per = BAIT_SPECS[bait_key]['price']
    cost = price_per * qty
    cur_balance = get_balance(uid)
    if cur_balance < cost:
        return False, f'餘額不足（需 {cost:,}，你有 {cur_balance:,}）', 0
    # 扣錢 + 加餌都不會失敗，分兩步寫但 fishing.json 跟 wallet 是不同檔案
    # 先扣錢失敗就不會加餌；加餌不可能失敗（沒上限）
    await apply_delta(uid, -cost)
    ok, err = await adjust_bait(uid, bait_key, +qty)
    if not ok:
        await apply_delta(uid, +cost)   # 退款
        return False, err, 0
    return True, '', cost


async def buy_rod(uid: str, rod_key: str) -> tuple[bool, str]:
    if rod_key not in ROD_SPECS:
        return False, '未知釣竿'
    if rod_key in get_rods(uid):
        return False, '你已經擁有這支竿'
    price = ROD_SPECS[rod_key]['price']
    if price <= 0:
        return False, '此釣竿不可購買'
    cur = get_balance(uid)
    if cur < price:
        return False, f'餘額不足（需 {price:,}，你有 {cur:,}）'
    await apply_delta(uid, -price)
    ok, err = await adjust_rod(uid, rod_key, add=True)
    if not ok:
        await apply_delta(uid, +price)
        return False, err
    return True, ''


# ── 贈禮系統 ────────────────────────────────────────────────────────────
def _load_gifts() -> dict:
    return load_json(_GIFT_FILE) or {}


async def _save_gifts(data: dict) -> None:
    await save_json_async(_GIFT_FILE, data)


async def create_gift(*, from_uid: str, to_uid: str, category: str, key: str,
                      qty: int, guild_id: int, channel_id: int,
                      is_purchase: bool = False,
                      paid_amount: int = 0) -> tuple[bool, str, str | None]:
    """寫 gifts.json。回 (ok, msg, gift_id)。

    is_purchase=False（魚）：從 sender 扣物品。
    is_purchase=True（其他類別 — 從商店買來送）：呼叫端已先扣錢，這裡只負責寫入 pending。
    """
    if from_uid == to_uid:
        return False, '不能送禮給自己', None
    if qty <= 0:
        return False, '數量必須為正', None

    # 非購買贈禮 → 從 sender 扣物品
    if not is_purchase:
        if category == 'rod':
            ok, err = await adjust_rod(from_uid, key, add=False)
        elif category == 'bait':
            ok, err = await adjust_bait(from_uid, key, -qty)
        elif category == 'fish':
            ok, err = await adjust_fish(from_uid, key, -qty)
        else:
            return False, f'不支援的贈禮類別：{category}', None
        if not ok:
            return False, err, None

    gift_id = secrets.token_hex(8)
    data = _load_gifts()
    data.setdefault('gifts', {})[gift_id] = {
        'id':           gift_id,
        'from_uid':     from_uid,
        'to_uid':       to_uid,
        'category':     category,
        'key':          key,
        'qty':          int(qty),
        'guild_id':     int(guild_id),
        'channel_id':   int(channel_id),
        'is_purchase':  bool(is_purchase),
        'paid_amount':  int(paid_amount),
        'expires_at':   (datetime.now() + timedelta(hours=GIFT_EXPIRE_HOURS)).isoformat(),
    }
    await _save_gifts(data)
    return True, '', gift_id


async def claim_gift(gift_id: str, claimer_uid: str) -> tuple[bool, str]:
    """收禮人接收：物品入庫 + 從 gifts.json 移除。"""
    data = _load_gifts()
    g = (data.get('gifts') or {}).get(gift_id)
    if g is None:
        return False, '此贈禮已失效或已被收下'
    if g['to_uid'] != claimer_uid:
        return False, '這不是給你的贈禮'

    cat = g['category']
    key = g['key']
    qty = int(g['qty'])
    if cat == 'rod':
        if key in get_rods(claimer_uid):
            return False, '你已經有這支竿，無法接收（請通知贈禮者退回）'
        ok, err = await adjust_rod(claimer_uid, key, add=True)
    elif cat == 'bait':
        ok, err = await adjust_bait(claimer_uid, key, +qty)
    elif cat == 'fish':
        ok, err = await adjust_fish(claimer_uid, key, +qty)
    elif cat == 'reverse':
        from commands.shop import _atomic_add_reverse
        ok, err = await _atomic_add_reverse(claimer_uid, qty)
    elif cat == 'miner':
        from commands.shop import _atomic_add_miner
        ok, err = await _atomic_add_miner(claimer_uid, qty)
    else:
        return False, '不支援的贈禮類別'
    if not ok:
        return False, err

    data['gifts'].pop(gift_id, None)
    await _save_gifts(data)
    return True, ''


async def refund_gift(gift_id: str) -> tuple[bool, str]:
    """把物品 / 金額退還給原贈禮者（用於拒收 / 過期 / claim 失敗）。"""
    data = _load_gifts()
    g = (data.get('gifts') or {}).get(gift_id)
    if g is None:
        return False, '此贈禮不存在'

    sender = g['from_uid']

    # 購買贈禮 → 退錢
    if g.get('is_purchase'):
        amt = int(g.get('paid_amount', 0))
        if amt > 0:
            await apply_delta(sender, amt)
        data['gifts'].pop(gift_id, None)
        await _save_gifts(data)
        return True, ''

    cat = g['category']
    key = g['key']
    qty = int(g['qty'])
    if cat == 'rod':
        if key in get_rods(sender):
            # 贈禮者已經有同一支竿（不可能擁有兩支）→ 補錢
            await apply_delta(sender, rod_sell_price(key))
        else:
            await adjust_rod(sender, key, add=True)
    elif cat == 'bait':
        await adjust_bait(sender, key, +qty)
    elif cat == 'fish':
        await adjust_fish(sender, key, +qty)

    data['gifts'].pop(gift_id, None)
    await _save_gifts(data)
    return True, ''


# ── 釣魚 session：重啟後清理 ────────────────────────────────────────────
async def cleanup_active_sessions_on_restart() -> None:
    """bot 重啟時：
      - pending_catch 為一般魚 → 自動保留進保溫箱
      - pending_catch 為垃圾 → 自動賣出（不入保溫箱）
      - 還沒釣完 → 退回魚餌
    一律 clear active_session。"""
    data = _load_all()
    users = data.get('users', {})
    restored = 0
    autosold = 0
    refunded = 0
    for uid, rec in list(users.items()):
        sess = rec.get('active_session')
        if not sess:
            continue
        pc = sess.get('pending_catch')
        if pc:
            fk = pc.get('fish_key')
            if fk and fk in FISH_SPECS:
                if FISH_SPECS[fk]['rarity'] == 'trash':
                    # 直接賣出（低價回收）— 新魚種/首釣獎勵已在 catch 時立即入帳
                    price = int(pc.get('price', 0))
                    if price > 0:
                        await apply_delta(uid, price)
                    autosold += 1
                else:
                    fish = rec.setdefault('fish', {})
                    cap = FISH_POND_CAP + int(rec.get('pond_cap_extra', 0))
                    total_after = sum(int(v) for v in fish.values()) + 1
                    if total_after <= cap:
                        fish[fk] = int(fish.get(fk, 0)) + 1
                        restored += 1
        else:
            bk = sess.get('bait_key')
            if bk and bk in BAIT_SPECS:
                baits = rec.setdefault('baits', {})
                baits[bk] = int(baits.get(bk, 0)) + 1
                refunded += 1
        rec['active_session'] = None
    if restored or autosold or refunded:
        await save_json_async(_FILE, data)
    print(f'[FISHING] 重啟清理：保留 {restored} 條魚，賣出 {autosold} 垃圾，退回 {refunded} 個餌')


# ── 贈禮過期背景任務 ────────────────────────────────────────────────────
async def _gift_expire_once(client: discord.Client) -> None:
    data = _load_gifts()
    gifts = data.get('gifts') or {}
    if not gifts:
        return
    now = datetime.now()
    expired_ids: list[str] = []
    for gid, g in gifts.items():
        try:
            exp = datetime.fromisoformat(g['expires_at'])
        except (KeyError, ValueError):
            expired_ids.append(gid)
            continue
        if exp <= now:
            expired_ids.append(gid)
    for gid in expired_ids:
        g = gifts.get(gid)
        if g is None:
            continue
        # 嘗試在原頻道留訊息
        try:
            channel = client.get_channel(int(g['channel_id']))
            if channel is not None:
                await channel.send(
                    f'⏰ 贈禮過期：<@{g["from_uid"]}> 給 <@{g["to_uid"]}> 的禮物已退回。',
                    allowed_mentions=discord.AllowedMentions(users=[
                        discord.Object(id=int(g['from_uid'])),
                    ]),
                )
        except (discord.HTTPException, ValueError):
            pass
        await refund_gift(gid)
    if expired_ids:
        print(f'[FISHING] 贈禮過期退回 {len(expired_ids)} 件')


async def _gift_expire_loop(client: discord.Client) -> None:
    while True:
        await asyncio.sleep(_GIFT_CHECK_INTERVAL)
        try:
            await _gift_expire_once(client)
        except Exception as e:
            print(f'[FISHING] gift expire loop error: {e}')


def start_gift_expire_task(client: discord.Client) -> asyncio.Task:
    return asyncio.create_task(_gift_expire_loop(client))


# =====================================================================
# UI 區段
# =====================================================================

def _short(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + '…'


def _fmt_price(p: int) -> str:
    sign = '-' if p < 0 else ''
    return f'{sign}{abs(p):,}'


# ── 主面板 embed ────────────────────────────────────────────────────────
def _main_embed(user: discord.abc.User, *, message: str | None = None) -> discord.Embed:
    uid = str(user.id)
    rec = get_user_rec(uid)
    balance = get_balance(uid)
    rod_key = get_equipped_rod(uid)
    rod = ROD_SPECS[rod_key] if rod_key else None
    bait_key = rec.get('selected_bait')
    fish_total = sum(int(v) for v in rec.get('fish', {}).values())

    body = [
        f'💰 餘額：**{balance:,}** 咕嚕喵碎片',
    ]
    if rod_key is None:
        body.append('🎣 已裝備釣竿：_(尚未擁有任何釣竿；請至「購買釣竿」買 T1)_')
    else:
        body.append(
            f'🎣 已裝備釣竿：**{rod["name"]}** (T{rod["tier"]}, '
            f'{_rod_effect_str(rod)})'
        )
    if bait_key and bait_key in BAIT_SPECS:
        bs = BAIT_SPECS[bait_key]
        owned = int(rec.get('baits', {}).get(bait_key, 0))
        body.append(f'🪱 已選魚餌：**{bs["name"]}** × {owned}')
    else:
        body.append('🪱 已選魚餌：_(尚未選擇)_')
    body.append(f'🐟 保溫箱：**{fish_total}** / {get_fish_pond_cap(uid)} 條')
    buff = _buff_status_line(uid)
    if buff:
        body.append(f'✨ Buff：{buff}')

    sess = rec.get('active_session')
    if sess and sess.get('pending_catch') is None:
        try:
            exp = datetime.fromisoformat(sess['expected_finish'])
            body.append(
                f'⏳ 你目前正在釣魚，剩餘 <t:{int(exp.timestamp())}:R>'
            )
        except (KeyError, ValueError):
            pass
    elif sess and sess.get('pending_catch'):
        fk = sess['pending_catch']['fish_key']
        body.append(
            f'📌 上次釣到的 **{FISH_SPECS[fk]["name"]}** 還沒處理 — 重新甩勾會自動保留'
        )

    if message:
        body.insert(0, message)
        body.insert(1, '')

    embed = discord.Embed(
        title='🎣 釣魚 — 主面板',
        description='\n'.join(body),
        color=discord.Color.teal(),
    )
    embed.set_footer(text=user.display_name)
    return embed


# ── 主面板 View ──────────────────────────────────────────────────────────
class FishingMainView(discord.ui.View):
    def __init__(self, uid: str, root_interaction: discord.Interaction,
                 *, message: str | None = None):
        super().__init__(timeout=600)
        self.uid = uid
        self.root_interaction = root_interaction
        self.message_hint = message
        self._build()

    def _build(self) -> None:
        self.clear_items()
        rec = get_user_rec(self.uid)

        # Row 0: rod select（沒擁有任何竿時顯示 disabled placeholder）
        rod_options: list[discord.SelectOption] = []
        eq = get_equipped_rod(self.uid)
        for rk in rec.get('rods', []):
            spec = ROD_SPECS[rk]
            rod_options.append(discord.SelectOption(
                label=f'T{spec["tier"]} {spec["name"]}',
                value=rk,
                description=_rod_effect_str(spec),
                default=(rk == eq),
            ))
        if rod_options:
            rod_select = discord.ui.Select(
                placeholder='選擇釣竿',
                options=rod_options[:25],
                min_values=1, max_values=1, row=0,
            )
            rod_select.callback = self._on_rod_select
        else:
            rod_select = discord.ui.Select(
                placeholder='尚未擁有任何釣竿 — 請先「購買釣竿」',
                options=[discord.SelectOption(label='（沒有可用釣竿）', value='_none')],
                min_values=1, max_values=1, row=0, disabled=True,
            )
        self.add_item(rod_select)

        # Row 1: bait select (只列已擁有 + 數量 > 0)
        bait_options: list[discord.SelectOption] = []
        cur_bait = rec.get('selected_bait')
        baits = rec.get('baits', {})
        for bk, n in sorted(baits.items(), key=lambda kv: BAIT_SPECS.get(kv[0], {}).get('price', 0)):
            if int(n) <= 0 or bk not in BAIT_SPECS:
                continue
            bs = BAIT_SPECS[bk]
            bait_options.append(discord.SelectOption(
                label=f'{_short(bs["name"], 18)} × {n}',
                value=bk,
                description=_short(bs['note'], 90),
                default=(bk == cur_bait),
            ))
        if bait_options:
            bait_select = discord.ui.Select(
                placeholder='選擇魚餌',
                options=bait_options[:25],
                min_values=1, max_values=1, row=1,
            )
            bait_select.callback = self._on_bait_select
        else:
            bait_select = discord.ui.Select(
                placeholder='尚未擁有任何魚餌 — 請先「購買魚餌」',
                options=[discord.SelectOption(label='（沒有可用魚餌）', value='_none')],
                min_values=1, max_values=1, row=1, disabled=True,
            )
        self.add_item(bait_select)

        # Row 2: 開始 + 圖鑑
        sess = rec.get('active_session')
        # 有未處理的 pending_catch → 進結果頁
        has_pending = bool(sess and sess.get('pending_catch'))
        in_session  = bool(sess and not has_pending)

        start_btn = discord.ui.Button(
            label='🎣 開始釣魚' if not has_pending else '🎣 處理上次釣獲',
            style=discord.ButtonStyle.success,
            disabled=(
                in_session
                or (eq is None and not has_pending)        # 沒釣竿
                or (cur_bait is None and not has_pending)  # 沒選餌
            ),
            row=2,
        )
        start_btn.callback = self._on_start
        self.add_item(start_btn)

        dex_btn = discord.ui.Button(
            label='📓 魚類圖鑑', style=discord.ButtonStyle.secondary, row=2,
        )
        dex_btn.callback = self._on_dex
        self.add_item(dex_btn)

        pond_btn = discord.ui.Button(
            label='📦 查看保溫箱', style=discord.ButtonStyle.secondary, row=2,
        )
        pond_btn.callback = self._on_view_pond
        self.add_item(pond_btn)

        # Row 3: 釣魚用品 + 白名單 + 投餵小龍喵
        buy_equip_btn = discord.ui.Button(
            label='🛒 購買釣魚用品', style=discord.ButtonStyle.primary, row=3,
        )
        buy_equip_btn.callback = self._on_buy_equip
        self.add_item(buy_equip_btn)

        feed_btn = discord.ui.Button(
            label='🦞 投餵小龍喵' if not fed_meow_today(self.uid)
                  else '🦞 今天餵過了',
            style=discord.ButtonStyle.success if not fed_meow_today(self.uid)
                  else discord.ButtonStyle.secondary,
            disabled=fed_meow_today(self.uid),
            row=3,
        )
        feed_btn.callback = self._on_feed_meow
        self.add_item(feed_btn)

        wl_btn = discord.ui.Button(
            label='📋 頻道白名單', style=discord.ButtonStyle.secondary, row=3,
        )
        wl_btn.callback = self._on_whitelist
        self.add_item(wl_btn)

        # Row 4: 前往商店 + 緊急收竿 + 關閉
        shop_btn = discord.ui.Button(
            label='🛒 前往商店', style=discord.ButtonStyle.secondary, row=4,
        )
        shop_btn.callback = self._on_goto_shop
        self.add_item(shop_btn)

        reset_btn = discord.ui.Button(
            label='🆘 遇到 bug? 點此收竿!',
            style=discord.ButtonStyle.secondary, row=4,
        )
        reset_btn.callback = self._on_force_reset
        self.add_item(reset_btn)

        close_btn = discord.ui.Button(
            label='✖️ 關閉', style=discord.ButtonStyle.danger, row=4,
        )
        close_btn.callback = self._on_close
        self.add_item(close_btn)

    async def _check_owner(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message('這不是你的釣魚面板', ephemeral=True)
            return False
        return True

    async def _redraw(self, interaction: discord.Interaction,
                       *, message: str | None = None) -> None:
        new_view = FishingMainView(self.uid, self.root_interaction, message=message)
        embed = _main_embed(interaction.user, message=message)
        if not interaction.response.is_done():
            try:
                await interaction.response.edit_message(embed=embed, view=new_view)
                return
            except discord.HTTPException:
                pass
        try:
            await self.root_interaction.edit_original_response(embed=embed, view=new_view)
        except discord.HTTPException:
            pass

    async def _on_rod_select(self, interaction: discord.Interaction) -> None:
        if not await self._check_owner(interaction):
            return
        val = interaction.data['values'][0]
        ok, err = await set_equipped_rod(self.uid, val)
        if not ok:
            await interaction.response.send_message(err, ephemeral=True)
            return
        await self._redraw(interaction)

    async def _on_bait_select(self, interaction: discord.Interaction) -> None:
        if not await self._check_owner(interaction):
            return
        val = interaction.data['values'][0]
        if val == '_none':
            await interaction.response.defer()
            return
        await set_selected_bait(self.uid, val)
        await self._redraw(interaction)

    async def _on_start(self, interaction: discord.Interaction) -> None:
        if not await self._check_owner(interaction):
            return
        rec = get_user_rec(self.uid)
        sess = rec.get('active_session')

        # 已經在釣（仍在計時，沒拿到 pending_catch）→ 拒絕
        if sess and not sess.get('pending_catch'):
            await interaction.response.send_message('你正在釣魚中，請等結束', ephemeral=True)
            return

        # 有未處理 pending_catch → 自動保留再繼續
        if sess and sess.get('pending_catch'):
            await keep_pending(self.uid)

        # 白名單檢查（DM 不檢查，只在 guild 內檢查；空白名單 = 禁釣）
        guild = interaction.guild
        if guild is not None:
            if not is_channel_whitelisted(guild.id, interaction.channel_id):
                wl = get_whitelist(guild.id)
                if not wl:
                    msg = '❌ 此伺服器尚未設定任何釣魚白名單頻道，先用「📋 頻道白名單」加入頻道才能釣魚'
                else:
                    msg = '❌ 此頻道不在釣魚白名單內，請切換到允許的頻道'
                await interaction.response.send_message(msg, ephemeral=True)
                return

        bait_key = rec.get('selected_bait')
        rod_key = get_equipped_rod(self.uid)
        if not rod_key:
            await interaction.response.send_message(
                '請先到「🛒 購買釣魚用品」買一支竿', ephemeral=True,
            )
            return
        if not bait_key or bait_key not in BAIT_SPECS:
            await interaction.response.send_message('請先選擇魚餌', ephemeral=True)
            return
        if int(rec.get('baits', {}).get(bait_key, 0)) <= 0:
            await interaction.response.send_message('此魚餌庫存不足', ephemeral=True)
            return
        # 保溫箱已滿 → 擋
        if get_fish_count_total(self.uid) >= get_fish_pond_cap(self.uid):
            await interaction.response.send_message(
                '❌ 保溫箱已滿，請先賣魚或擴充保溫箱才能繼續拋勾',
                ephemeral=True,
            )
            return

        # 扣餌 + 預先 roll 魚（依稀有度決定時長）+ 抽劇本 + 寫 session
        ok, err = await adjust_bait(self.uid, bait_key, -1)
        if not ok:
            await interaction.response.send_message(err, ephemeral=True)
            return
        p1_stack = potion_i_stack(self.uid)
        p2_stack = potion_ii_stack(self.uid)
        radar_on = radar_active(self.uid)
        rarity   = _roll_rarity(rod_key, bait_key, potion_ii_stack=p2_stack)
        fish_key = _roll_species(rarity, dex=get_dex(self.uid), radar_active=radar_on)
        duration = _compute_duration(rod_key, bait_key, rarity, potion_i_stack=p1_stack)
        story_tag, s1, s2, s3 = _script_pick_story(
            name='小龍喵',
            rod=ROD_SPECS[rod_key]['name'],
            bait=BAIT_SPECS[bait_key]['name'],
        )
        story_full = f'{s1}\n\n{s2}\n\n{s3}'
        await _start_session_record(
            self.uid, rod_key=rod_key, bait_key=bait_key,
            channel_id=int(interaction.channel_id or 0), duration_s=duration,
            predetermined_fish=fish_key, story_tag=story_tag,
        )
        # 在當前頻道發**公開**訊息（所有人看得到誰在釣）— 全劇本一次顯示在拋線階段
        progress_view = FishingProgressView(self.uid)
        embed = _progress_embed(interaction.user, rod_key, bait_key,
                                stage='cast', story_line=story_full)
        try:
            public_msg = await interaction.channel.send(
                content=f'🎣 {interaction.user.mention} 開始釣魚！',
                embed=embed, view=progress_view,
                allowed_mentions=discord.AllowedMentions(users=False),
            )
        except discord.HTTPException as e:
            # 發訊息失敗：退回魚餌 + 清 session
            await adjust_bait(self.uid, bait_key, +1)
            await _clear_session(self.uid)
            await interaction.response.send_message(
                f'❌ 在此頻道發訊息失敗：{e}（已退回魚餌）', ephemeral=True,
            )
            return

        # 關閉 ephemeral 主面板（已開始釣，無須再顯示）
        # 用 interaction.delete_original_response() 比 root_interaction 穩定
        # —— button click 的 original 就是這條 ephemeral message
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        for target in (interaction, self.root_interaction):
            try:
                await target.delete_original_response()
                break
            except discord.HTTPException:
                continue

        # 啟動釣魚 task（魚種已預先決定 + 帶劇本全文，每階段都顯示）
        asyncio.create_task(_run_fishing_task(
            uid=self.uid, public_msg=public_msg, fisher=interaction.user,
            rod_key=rod_key, bait_key=bait_key, duration=duration,
            predetermined_fish=fish_key, story_full=story_full,
        ))

    async def _on_dex(self, interaction: discord.Interaction) -> None:
        if not await self._check_owner(interaction):
            return
        view = DexView(self.uid, self.root_interaction, page=0)
        await interaction.response.edit_message(embed=view.build_embed(interaction.user), view=view)

    async def _on_view_pond(self, interaction: discord.Interaction) -> None:
        """主面板上看保溫箱 — 直接 edit 同個 ephemeral 切到 PondInventoryView。
        PondInventoryView 拿到 root_interaction 後，返回鈕會切回主面板。"""
        if not await self._check_owner(interaction):
            return
        view = PondInventoryView(self.uid, page=0,
                                 root_interaction=self.root_interaction)
        await interaction.response.edit_message(
            embed=view.build_embed(interaction.user), view=view,
        )

    async def _on_buy_equip(self, interaction: discord.Interaction) -> None:
        """打開「釣魚用品」面板（與商店同步），返回鈕回到本主面板。"""
        if not await self._check_owner(interaction):
            return
        from commands.shop import ShopFishingEquipView
        view = ShopFishingEquipView(self.uid, root_interaction=self.root_interaction,
                                    back_to_fishing_main=True)
        await interaction.response.edit_message(
            embed=view.build_embed(interaction.user), view=view,
        )

    async def _on_feed_meow(self, interaction: discord.Interaction) -> None:
        """打開投餵小龍喵選單。"""
        if not await self._check_owner(interaction):
            return
        view = FeedMeowView(self.uid, root_interaction=self.root_interaction, page=0)
        await interaction.response.edit_message(
            embed=view.build_embed(interaction.user), view=view,
        )

    async def _on_goto_shop(self, interaction: discord.Interaction) -> None:
        if not await self._check_owner(interaction):
            return
        # lazy import 避免循環
        from commands.shop import ShopMainView, _main_shop_embed
        view = ShopMainView(self.uid, self.root_interaction.guild,
                            root_interaction=self.root_interaction)
        await interaction.response.edit_message(
            embed=_main_shop_embed(interaction.user), view=view,
        )

    async def _on_force_reset(self, interaction: discord.Interaction) -> None:
        """緊急收竿：清掉 active_session、退魚餌、重抓魚塘狀態。
        當 task 卡死/訊息消失/狀態錯亂時用。"""
        if not await self._check_owner(interaction):
            return
        sess = get_active_session(self.uid)
        refunded = 0
        kept_pending = False
        if sess:
            pc = sess.get('pending_catch')
            if pc:
                # 已有釣獲但卡在沒處理 → 直接保留進保溫箱（垃圾走 sell）
                ok, _ = await keep_pending(self.uid)
                kept_pending = ok
            else:
                # 還沒釣完 → 退魚餌
                bk = sess.get('bait_key')
                if bk and bk in BAIT_SPECS:
                    await adjust_bait(self.uid, bk, +1)
                    refunded = 1
                await _clear_session(self.uid)
        # 刷新主面板
        new_view = FishingMainView(self.uid, self.root_interaction)
        await interaction.response.edit_message(
            embed=_main_embed(interaction.user), view=new_view,
        )
        parts: list[str] = []
        if refunded:
            parts.append(f'退回 {refunded} 個魚餌')
        if kept_pending:
            parts.append('未處理的釣獲已收入保溫箱')
        if not parts:
            parts.append('無進行中的 session')
        try:
            await interaction.followup.send(
                f'✅ 已強制收竿：{"、".join(parts)}',
                ephemeral=True,
            )
        except discord.HTTPException:
            pass

    async def _on_whitelist(self, interaction: discord.Interaction) -> None:
        if not await self._check_owner(interaction):
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                '白名單只能在伺服器內設定', ephemeral=True,
            )
            return
        view = WhitelistView(self.uid, self.root_interaction)
        await interaction.response.edit_message(embed=view.build_embed(interaction.user), view=view)

    async def _on_close(self, interaction: discord.Interaction) -> None:
        if not await self._check_owner(interaction):
            return
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        for target in (interaction, self.root_interaction):
            try:
                await target.delete_original_response()
                return
            except discord.HTTPException:
                continue


# ── 釣魚進行中 view + embed ─────────────────────────────────────────────
def _progress_embed(user: discord.abc.User, rod_key: str, bait_key: str,
                    *, stage: str, story_line: str = '') -> discord.Embed:
    """釣魚進行中的 embed。不顯示時間倒數（保持懸念感）。
    story_line 為 data/fishing_script.py 抽出的劇本句子，加在描述下方。

    stage:
      - 'cast':         拋線後等待咬勾
      - 'tick_gentle':  魚標微微抖動
      - 'tick_strong':  魚標劇烈晃動
    """
    rod = ROD_SPECS[rod_key]
    bait = BAIT_SPECS[bait_key]
    if stage == 'cast':
        title = '🎣 等待魚兒上鉤中…'
        head = (
            f'**{user.display_name}** 用 **{rod["name"]}** 掛上 **{bait["name"]}**，'
            f'魚線沉入水中。'
        )
        color = discord.Color.blue()
    elif stage == 'tick_gentle':
        title = '🐟 魚標微微抖動…'
        head = f'**{user.display_name}** 的魚線傳來細微的觸感，水面下似乎有什麼正在靠近魚餌。'
        color = discord.Color.teal()
    elif stage == 'tick_strong':
        title = '🌊 魚標劇烈晃動！！'
        head = f'**{user.display_name}** 的魚線猛烈拉動，水花四濺！一定是條大傢伙！'
        color = discord.Color.orange()
    else:
        title = '🎣 釣魚中'
        head = ''
        color = discord.Color.blue()
    parts = [head]
    buff = _buff_status_line(str(user.id))
    if buff:
        parts.append('')
        parts.append(f'**Buff:** {buff}')
    if story_line:
        parts.append('')
        parts.append('**🎬 環境小事件:**')
        parts.append('')
        parts.append(story_line)
    embed = discord.Embed(title=title, description='\n'.join(parts),
                          color=color)
    embed.set_footer(text=user.display_name)
    return embed


class FishingProgressView(discord.ui.View):
    """釣魚等待中的 view — 沒有可互動按鈕，避免玩家中途亂按。"""

    def __init__(self, uid: str):
        super().__init__(timeout=86400)
        self.uid = uid
        wait_btn = discord.ui.Button(
            label='等待中…',
            style=discord.ButtonStyle.secondary,
            disabled=True, row=0,
        )
        self.add_item(wait_btn)


# ── 釣魚實際 task（後台跑時間） ─────────────────────────────────────────
async def _run_fishing_task(*, uid: str, public_msg: discord.Message,
                            fisher: discord.abc.User, rod_key: str,
                            bait_key: str, duration: float,
                            predetermined_fish: str | None = None,
                            story_full: str = '') -> None:
    """背景任務：sleep 對應時長 + 中段提示 + 最終 roll + 寫 pending_catch + edit 公開訊息。

    時長 5~10 秒內（且不超過 duration*0.8）切換到「魚標抖動／晃動」提示，
    隨機在「微微抖動」與「劇烈晃動」之間擇一。
    predetermined_fish 非 None 時使用該魚種（不重抽，搭配 rarity-based duration）。
    story_s2 / story_s3 用於中段 / 結果頁的劇本句子。
    """
    try:
        # 中段提示：5~10 秒之間隨機，且需在 duration 的 80% 內，留點時間給結果
        tick_at = min(random.uniform(5.0, 10.0), duration * 0.8)
        # 還要確保 tick 後有至少 PROGRESS_TICK 秒能等
        if tick_at >= PROGRESS_TICK and (duration - tick_at) >= PROGRESS_TICK:
            await asyncio.sleep(tick_at)
            tick_stage = 'tick_strong' if random.random() < 0.5 else 'tick_gentle'
            try:
                await public_msg.edit(
                    embed=_progress_embed(fisher, rod_key, bait_key,
                                          stage=tick_stage, story_line=story_full),
                )
            except discord.HTTPException:
                pass
            await asyncio.sleep(max(0.0, duration - tick_at))
        else:
            await asyncio.sleep(duration)

        # 使用預先決定的魚種；若無（舊資料）則 fallback roll
        if predetermined_fish and predetermined_fish in FISH_SPECS:
            fish_key = predetermined_fish
        else:
            rarity = _roll_rarity(rod_key, bait_key)
            fish_key = _roll_species(rarity)
        price = _roll_price(fish_key)

        # 特殊 tag：垃圾不會帶 tag；其餘看 base 機率（吃幸運藥水 III + 釣竿 tag_bonus 提升）
        tag: str | None = None
        if FISH_SPECS[fish_key]['rarity'] != 'trash':
            rod_bonus = float(ROD_SPECS.get(rod_key, {}).get('tag_bonus', 0.0))
            tag = _roll_special_tag(boost_level=potion_iii_stack(uid), rod_bonus=rod_bonus)

        rarity_now = FISH_SPECS[fish_key]['rarity']
        # 重量 roll：按 WEIGHT_CLASS_PROBS 抽 class，再按 rarity base kg × class 倍率取樣
        weight_class, weight_kg = _roll_weight(rarity_now)
        # 上鉤頁：玩家需在 HOOK_TIMEOUT_SEC[rarity] 內按「收竿」；超時魚跑掉 + 餌已扣不退
        timeout_sec = float(HOOK_TIMEOUT_SEC.get(rarity_now, 30.0))
        view = HookedView(
            uid=uid, public_msg=public_msg, fisher=fisher,
            rod_key=rod_key, bait_key=bait_key,
            fish_key=fish_key, tag=tag, price=price,
            weight_class=weight_class, weight_kg=weight_kg,
            story_full=story_full, timeout_sec=timeout_sec,
        )
        embed = _hooked_embed(fisher, fish_key=fish_key, tag=tag,
                              timeout_sec=timeout_sec, story_line=story_full)
        try:
            await public_msg.edit(content=f'🎣 {fisher.mention} 上鉤了！',
                                  embed=embed, view=view,
                                  allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException:
            pass

    except Exception as e:
        print(f'[FISHING] task error uid={uid}: {e}')


# ── 上鉤頁 embed + view ─────────────────────────────────────────────────
def _hooked_embed(user: discord.abc.User, *, fish_key: str, tag: str | None,
                  timeout_sec: float, story_line: str = '') -> discord.Embed:
    """上鉤後等玩家收竿的 embed。不洩漏實際魚種（保留神祕感），只用稀有度顏色暗示。
    若帶 tag，會額外加一行 hint 提示玩家「這條不簡單」 — tag 名仍藏起來。
    """
    rarity = FISH_SPECS[fish_key]['rarity']
    deadline = int((datetime.now() + timedelta(seconds=timeout_sec)).timestamp())
    lines: list[str] = [
        f'**{user.display_name}** 的浮標被狠狠拉下，水中有什麼正在掙扎！',
        '',
        f'⏳ 必須在 <t:{deadline}:R> 內按下「收竿」，否則魚會掙脫並把餌吃掉！',
    ]
    # tag hint：暗示有變體但不洩漏 tag 名（保留結算頁的揭曉感）
    if tag and tag in SPECIAL_TAG_HINT:
        lines += ['', SPECIAL_TAG_HINT[tag]]
    buff = _buff_status_line(str(user.id))
    if buff:
        lines += ['', f'**Buff:** {buff}']
    if story_line:
        lines += ['', '**🎬 環境小事件:**', '', story_line]
    # 帶 tag 時，標題加一點異樣感（不直接寫 tag 名）
    title = '✨ 上鉤了！竿尖異常劇烈彎曲…！' if tag else '🎣 上鉤了！竿尖劇烈彎曲！'
    embed = discord.Embed(
        title=title,
        description='\n'.join(lines),
        color=discord.Color(RARITY_COLOR[rarity]),
    )
    embed.set_footer(text=user.display_name)
    return embed


class HookedView(discord.ui.View):
    """上鉤頁：玩家必須在 timeout_sec 內按 收竿，否則魚會跑掉（已扣的餌不退）。"""

    def __init__(self, *, uid: str, public_msg: discord.Message,
                 fisher: discord.abc.User, rod_key: str, bait_key: str,
                 fish_key: str, tag: str | None, price: int,
                 weight_class: str, weight_kg: float,
                 story_full: str, timeout_sec: float):
        super().__init__(timeout=timeout_sec)
        self.uid = uid
        self.public_msg = public_msg
        self.fisher = fisher
        self.rod_key = rod_key
        self.bait_key = bait_key
        self.fish_key = fish_key
        self.tag = tag
        self.price = price
        self.weight_class = weight_class
        self.weight_kg = float(weight_kg)
        self.story_full = story_full
        self.resolved = False   # True 表示玩家已按 收竿 或 超時處理完畢

        reel_btn = discord.ui.Button(
            label='🪝 收竿！', style=discord.ButtonStyle.success, row=0,
        )
        reel_btn.callback = self._on_reel
        self.add_item(reel_btn)

    async def _on_reel(self, interaction: discord.Interaction) -> None:
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message(
                '這不是你的釣魚場次，請開自己的 /fishing', ephemeral=True,
            )
            return
        if self.resolved:
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass
            return
        self.resolved = True
        self.stop()

        # 寫 pending_catch + dex
        dex_before = get_dex(self.uid)
        is_new = self.fish_key not in dex_before
        prev_day = get_user_rec(self.uid).get('first_catch_day')
        is_daily_first = (prev_day != _today_str())
        await _finish_session_record(
            self.uid, fish_key=self.fish_key, price=self.price,
            is_new_species=is_new, is_daily_first=is_daily_first,
            tag=self.tag,
            weight_class=self.weight_class, weight_kg=self.weight_kg,
        )

        rarity = FISH_SPECS[self.fish_key]['rarity']

        # 垃圾自動賣出
        if rarity == 'trash':
            ok, _, paid = await sell_pending(self.uid)
            embed = _trash_autosell_embed(
                self.fisher, self.fish_key, paid if ok else 0,
                story_line=self.story_full,
            )
            view = FishingResultView(
                self.uid, self.public_msg, fisher=self.fisher,
                last_rod=self.rod_key, last_bait=self.bait_key,
                auto_sold=paid if ok else 0, story_s3=self.story_full,
            )
            try:
                await interaction.response.edit_message(
                    content=f'🎣 {self.fisher.mention} 撈到了一些雜物。',
                    embed=embed, view=view,
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
            except discord.HTTPException:
                pass
            return

        # 一般魚：進結算頁
        view = FishingResultView(
            self.uid, self.public_msg, fisher=self.fisher,
            last_rod=self.rod_key, last_bait=self.bait_key,
            story_s3=self.story_full,
        )
        embed = _result_embed(self.fisher, self.uid, story_line=self.story_full)
        try:
            await interaction.response.edit_message(
                content=f'🎣 {self.fisher.mention} 釣到了！',
                embed=embed, view=view,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.HTTPException:
            pass

    async def on_timeout(self) -> None:
        if self.resolved:
            return
        self.resolved = True
        self.stop()
        # 魚跑掉：餌已被扣（不退），清掉 session
        await _clear_session(self.uid)
        bait_name = BAIT_SPECS.get(self.bait_key, {}).get('name', '?')
        escape_embed = discord.Embed(
            title='💨 魚跑了！',
            description=(
                f'**{self.fisher.display_name}** 反應慢了一拍，'
                f'魚兒掙脫魚鉤、順便把 **{bait_name}** 也吃掉了。\n\n'
                '_（魚餌已消耗，未退回）_'
            ),
            color=discord.Color.dark_grey(),
        )
        if self.story_full:
            escape_embed.add_field(
                name='🎬 環境小事件',
                value=self.story_full[:1024],
                inline=False,
            )
        escape_embed.set_footer(text=self.fisher.display_name)
        # 失手後仍提供按鈕：再試一次（同竿同餌） / 返回設定 / 關閉
        escaped_view = FishingEscapedView(
            uid=self.uid, public_msg=self.public_msg, fisher=self.fisher,
            last_rod=self.rod_key, last_bait=self.bait_key,
        )
        try:
            await self.public_msg.edit(
                content=f'🎣 {self.fisher.mention} 失手了！',
                embed=escape_embed, view=escaped_view,
                allowed_mentions=discord.AllowedMentions(users=False),
            )
        except discord.HTTPException:
            pass


class FishingEscapedView(discord.ui.View):
    """魚跑掉後的善後 view：提供「再試一次」「返回設定」「關閉」三個按鈕，
    避免玩家被卡在沒按鈕的頁面（必須重開 /fishing 才能繼續）。"""

    def __init__(self, *, uid: str, public_msg: discord.Message,
                 fisher: discord.abc.User, last_rod: str, last_bait: str):
        super().__init__(timeout=86400)
        self.uid = uid
        self.public_msg = public_msg
        self.fisher = fisher
        self.last_rod = last_rod
        self.last_bait = last_bait

        retry_btn = discord.ui.Button(
            label='🎣 再試一次', style=discord.ButtonStyle.success, row=0,
        )
        retry_btn.callback = self._on_retry
        self.add_item(retry_btn)

        back_btn = discord.ui.Button(
            label='🔧 返回設定', style=discord.ButtonStyle.primary, row=0,
        )
        back_btn.callback = self._on_back
        self.add_item(back_btn)

        close_btn = discord.ui.Button(
            label='✖️ 關閉', style=discord.ButtonStyle.danger, row=0,
        )
        close_btn.callback = self._on_close
        self.add_item(close_btn)

    async def _check_owner(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message(
                '這不是你的釣魚場次', ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        # 超時：disable 所有按鈕，避免後續點擊出錯
        for child in self.children:
            child.disabled = True
        try:
            await self.public_msg.edit(view=self)
        except discord.HTTPException:
            pass

    async def _on_retry(self, interaction: discord.Interaction) -> None:
        """再試一次：同竿同餌再起一場（共用 FishingResultView._on_recast 的邏輯）。"""
        if not await self._check_owner(interaction):
            return
        self.stop()
        # 借 FishingResultView 的 recast 路徑（傳一個臨時 view 進去呼叫）
        proxy = FishingResultView(
            self.uid, self.public_msg, fisher=self.fisher,
            last_rod=self.last_rod, last_bait=self.last_bait,
            sold=0,   # 標 sold 表示「已處理」，跳過 keep_pending 自動保留
        )
        await proxy._on_recast(interaction)

    async def _on_back(self, interaction: discord.Interaction) -> None:
        """返回設定面板（刪掉公開失手頁、開新的 ephemeral 主面板）。"""
        if not await self._check_owner(interaction):
            return
        self.stop()
        try:
            await interaction.response.defer(ephemeral=True, thinking=False)
        except discord.HTTPException:
            pass
        try:
            await self.public_msg.delete()
        except discord.HTTPException:
            pass
        try:
            await interaction.followup.send(
                embed=_main_embed(self.fisher),
                view=FishingMainView(self.uid, interaction),
                ephemeral=True,
            )
        except discord.HTTPException:
            pass

    async def _on_close(self, interaction: discord.Interaction) -> None:
        if not await self._check_owner(interaction):
            return
        self.stop()
        try:
            await interaction.response.defer()
            await self.public_msg.delete()
        except discord.HTTPException:
            pass


# ── 結果 view + embed ───────────────────────────────────────────────────
def _result_embed(user: discord.abc.User, uid: str,
                  *, sold: int | None = None, kept: bool = False,
                  story_line: str = '') -> discord.Embed:
    """sold != None 表示已賣（金額=sold）；kept=True 表示已保留。否則顯示尚未決定。
    story_line 是劇本 Stage 3 的句子（驚醒與收線高潮）。"""
    sess = get_active_session(uid)
    if not sess or not sess.get('pending_catch'):
        return discord.Embed(
            title='🎣 釣魚結果',
            description='（沒有可顯示的釣獲）',
            color=discord.Color.greyple(),
        )
    pc = sess['pending_catch']
    fk = pc['fish_key']
    tag = pc.get('tag')
    weight_class = pc.get('weight_class')
    weight_kg = float(pc.get('weight_kg') or 0.0)
    spec = FISH_SPECS[fk]
    rarity = spec['rarity']
    base = int(pc['price'])
    if tag and tag in SPECIAL_TAGS:
        base = int(base * SPECIAL_TAGS[tag])
    if weight_class and weight_class in WEIGHT_CLASS_PRICE_MULT:
        base = int(base * WEIGHT_CLASS_PRICE_MULT[weight_class])

    disp_name = display_fish_name(fk, tag, weight_class)
    kg_suffix = f' ({weight_kg:.2f} KG)' if weight_kg > 0 else ''
    lines: list[str] = [
        f'{RARITY_EMOJI[rarity]} 恭喜 **{user.display_name}** 釣到了一條 '
        f'**[{RARITY_LABEL[rarity]}] {disp_name}**{kg_suffix}',
    ]
    if tag and tag in SPECIAL_TAGS:
        lines.append(f'✨ 特殊變體：**【{tag}】** （售價 ×{SPECIAL_TAGS[tag]}）')
    buff = _buff_status_line(str(user.id))
    if buff:
        lines += ['', f'**Buff:** {buff}']
    if story_line:
        lines += ['', '**🎬 環境小事件:**', '', story_line]

    # 新魚種 / 每日首釣獎勵已立即入帳；不論 sell/keep 都顯示
    new_sp_bonus = int(pc.get('new_species_bonus_paid') or 0)
    df_bonus = int(pc.get('daily_first_bonus_paid') or 0)
    if new_sp_bonus > 0:
        lines += ['', f'🆕 新魚種圖鑑獎勵 **+{_fmt_price(new_sp_bonus)}** 碎片（已入帳）']
    if df_bonus > 0:
        lines.append(f'🌅 每日首釣獎勵 **+{_fmt_price(df_bonus)}** 碎片（已入帳）')

    if sold is not None:
        sign = '💸 罰款' if sold < 0 else '💰 售出'
        extras: list[str] = []
        if weight_class and weight_class in WEIGHT_CLASS_PRICE_MULT and weight_class != 'normal':
            extras.append(f'重量加成: ×{WEIGHT_CLASS_PRICE_MULT[weight_class]}')
        suffix = f'（含：{"；".join(extras)}）' if extras else ''
        lines += [
            '',
            f'{sign}：**{_fmt_price(sold)}** 碎片 {suffix}'.rstrip(),
        ]
    elif kept:
        if rarity == 'trash':
            lines += ['', '🚯 此為垃圾，已丟回水裡（不入保溫箱）']
        else:
            lines += ['', '📦 已保留進保溫箱，可日後到 /shop → 販售 賣出']
    else:
        if rarity == 'trash':
            lines += ['', f'_預設售價：{_fmt_price(base)}（垃圾選保留會直接丟掉）_']
        else:
            lines += ['', f'_出售可得：**{_fmt_price(base)}** 碎片_']

    # 帶 tag 時：改 title 強調、用 tag 顏色覆蓋稀有度色（讓變體在訊息列就顯眼）
    if tag and tag in SPECIAL_TAG_COLOR:
        title = f'✨ 釣到了【{tag}】變體！'
        color = discord.Color(SPECIAL_TAG_COLOR[tag])
    else:
        title = '🎣 釣魚結果'
        color = discord.Color(RARITY_COLOR[rarity])
    embed = discord.Embed(
        title=title,
        description='\n'.join(lines),
        color=color,
    )
    embed.set_footer(text=user.display_name)
    return embed


class FishingResultView(discord.ui.View):
    """attach 在公開訊息上。只有釣魚者本人能按按鈕。
    auto_sold 為 int 時表示這次釣到垃圾已自動賣出，只給「重新甩勾 / 關閉」。"""

    def __init__(self, uid: str, public_msg: discord.Message,
                 *, fisher: discord.abc.User, last_rod: str, last_bait: str,
                 sold: int | None = None, kept: bool = False,
                 auto_sold: int | None = None,
                 story_s3: str = ''):
        super().__init__(timeout=600)
        self.uid = uid
        self.public_msg = public_msg
        self.fisher = fisher
        self.last_rod = last_rod
        self.last_bait = last_bait
        self.sold = sold
        self.kept = kept
        self.auto_sold = auto_sold
        # 這次釣魚抽到的劇本 Stage 3 句子（給結算頁顯示）；recast 會重抽
        self.story_s3 = story_s3
        self._build()

    def _build(self) -> None:
        self.clear_items()
        resolved = (self.sold is not None) or self.kept or (self.auto_sold is not None)

        # 自動賣出垃圾 → 不顯示 出售 / 保留
        if self.auto_sold is None:
            sell_btn = discord.ui.Button(
                label='💰 出售', style=discord.ButtonStyle.success,
                disabled=resolved, row=0,
            )
            sell_btn.callback = self._on_sell
            self.add_item(sell_btn)

            keep_btn = discord.ui.Button(
                label='📦 保留', style=discord.ButtonStyle.primary,
                disabled=resolved, row=0,
            )
            keep_btn.callback = self._on_keep
            self.add_item(keep_btn)

        # 重新甩勾：用同竿同餌再釣（若餌不足會跳訊息）
        recast_btn = discord.ui.Button(
            label='🎣 重新甩勾', style=discord.ButtonStyle.secondary, row=1,
        )
        recast_btn.callback = self._on_recast
        self.add_item(recast_btn)

        pond_btn = discord.ui.Button(
            label='📦 查看保溫箱', style=discord.ButtonStyle.secondary, row=1,
        )
        pond_btn.callback = self._on_view_pond
        self.add_item(pond_btn)

        dex_btn = discord.ui.Button(
            label='📓 查看圖鑑', style=discord.ButtonStyle.secondary, row=1,
        )
        dex_btn.callback = self._on_view_dex
        self.add_item(dex_btn)

        back_btn = discord.ui.Button(
            label='🔧 返回設定', style=discord.ButtonStyle.primary, row=2,
        )
        back_btn.callback = self._on_back_to_settings
        self.add_item(back_btn)

        close_btn = discord.ui.Button(
            label='✖️ 關閉', style=discord.ButtonStyle.danger, row=2,
        )
        close_btn.callback = self._on_close
        self.add_item(close_btn)

    async def _check_owner(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message(
                '這不是你的釣魚結果，請開自己的 /fishing', ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        # 自動保留 — 不放任 pending_catch 卡住下次釣魚
        sess = get_active_session(self.uid)
        if sess and sess.get('pending_catch'):
            await keep_pending(self.uid)
        # 把按鈕全 disabled
        for child in self.children:
            child.disabled = True
        try:
            await self.public_msg.edit(view=self)
        except discord.HTTPException:
            pass

    async def _on_sell(self, interaction: discord.Interaction) -> None:
        if not await self._check_owner(interaction):
            return
        # 已處理過（自己重複點 / 同時點了出售與保留 / 已超時自動保留）→ silent ack
        if self.sold is not None or self.kept or self.auto_sold is not None:
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass
            return
        ok, err, paid = await sell_pending(self.uid)
        if not ok:
            # race：session 已被另一個 click 清掉 → 視為已處理，silent ack
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass
            return
        self.sold = paid   # 標記避免後續 click 再進來
        new_view = FishingResultView(
            self.uid, self.public_msg, fisher=self.fisher,
            last_rod=self.last_rod, last_bait=self.last_bait, sold=paid,
            story_s3=self.story_s3,
        )
        embed = _post_resolve_embed(self.fisher, sold=paid,
                                    story_line=self.story_s3)
        await interaction.response.edit_message(embed=embed, view=new_view)

    async def _on_keep(self, interaction: discord.Interaction) -> None:
        if not await self._check_owner(interaction):
            return
        # 已處理過 → silent ack
        if self.sold is not None or self.kept or self.auto_sold is not None:
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass
            return
        sess_before = get_active_session(self.uid)
        pc_before = (sess_before or {}).get('pending_catch', {}) or {}
        fk = pc_before.get('fish_key')
        tag_before = pc_before.get('tag')
        rarity_before = FISH_SPECS[fk]['rarity'] if fk else 'common'
        ok, msg = await keep_pending(self.uid)
        if not ok:
            # race or 保溫箱滿等其他錯誤
            if '沒有待處理' in msg:
                # session 已被別處清掉 → silent ack
                try:
                    await interaction.response.defer()
                except discord.HTTPException:
                    pass
                return
            # 其他真實錯誤（保溫箱滿等）→ 仍然提示
            await interaction.response.send_message(msg, ephemeral=True)
            return
        self.kept = True
        new_view = FishingResultView(
            self.uid, self.public_msg, fisher=self.fisher,
            last_rod=self.last_rod, last_bait=self.last_bait, kept=True,
            story_s3=self.story_s3,
        )
        embed = _post_resolve_embed(self.fisher, kept=True,
                                    rarity_was_trash=(rarity_before == 'trash'),
                                    fish_name=(display_fish_name(fk, tag_before) if fk else ''),
                                    story_line=self.story_s3)
        await interaction.response.edit_message(embed=embed, view=new_view)

    async def _on_recast(self, interaction: discord.Interaction) -> None:
        if not await self._check_owner(interaction):
            return
        self.stop()   # 切換 view 後不需要 timeout 再 fire
        # 結束前若有未處理的 pending → 自動保留
        sess = get_active_session(self.uid)
        if sess and sess.get('pending_catch') and self.sold is None and not self.kept:
            await keep_pending(self.uid)

        # 用同竿同餌再釣
        bait_key = self.last_bait
        rod_key = self.last_rod
        if int(get_baits(self.uid).get(bait_key, 0)) <= 0:
            await interaction.response.send_message(
                f'此魚餌「{BAIT_SPECS[bait_key]["name"]}」已用完，請開 /fishing 重新選餌',
                ephemeral=True,
            )
            return

        if interaction.guild is not None:
            if not is_channel_whitelisted(interaction.guild.id, interaction.channel_id):
                await interaction.response.send_message(
                    '❌ 此頻道不在白名單', ephemeral=True,
                )
                return

        # 保溫箱已滿 → 擋
        if get_fish_count_total(self.uid) >= get_fish_pond_cap(self.uid):
            await interaction.response.send_message(
                '❌ 保溫箱已滿，請先賣魚或擴充保溫箱才能繼續拋勾',
                ephemeral=True,
            )
            return

        ok, err = await adjust_bait(self.uid, bait_key, -1)
        if not ok:
            await interaction.response.send_message(err, ephemeral=True)
            return

        # 預先 roll 魚 + 重抽劇本（每次甩勾都換，全文一次顯示）
        p1_stack = potion_i_stack(self.uid)
        p2_stack = potion_ii_stack(self.uid)
        radar_on = radar_active(self.uid)
        new_rarity   = _roll_rarity(rod_key, bait_key, potion_ii_stack=p2_stack)
        new_fish_key = _roll_species(new_rarity, dex=get_dex(self.uid),
                                     radar_active=radar_on)
        duration = _compute_duration(rod_key, bait_key, new_rarity,
                                     potion_i_stack=p1_stack)
        new_tag, new_s1, new_s2, new_s3 = _script_pick_story(
            name='小龍喵',
            rod=ROD_SPECS[rod_key]['name'],
            bait=BAIT_SPECS[bait_key]['name'],
        )
        new_story_full = f'{new_s1}\n\n{new_s2}\n\n{new_s3}'
        await _start_session_record(
            self.uid, rod_key=rod_key, bait_key=bait_key,
            channel_id=int(interaction.channel_id or 0), duration_s=duration,
            predetermined_fish=new_fish_key, story_tag=new_tag,
        )
        embed = _progress_embed(self.fisher, rod_key, bait_key,
                                stage='cast', story_line=new_story_full)
        progress_view = FishingProgressView(self.uid)

        # 編輯原公開訊息（bot 自己的訊息，無時間限制）
        try:
            await interaction.response.edit_message(
                content=f'🎣 {self.fisher.mention} 再次甩勾！',
                embed=embed, view=progress_view,
                allowed_mentions=discord.AllowedMentions(users=False),
            )
        except discord.HTTPException as e:
            await adjust_bait(self.uid, bait_key, +1)
            await _clear_session(self.uid)
            try:
                await interaction.followup.send(
                    f'❌ 編輯失敗：{e}（已退回魚餌）', ephemeral=True,
                )
            except discord.HTTPException:
                pass
            return

        asyncio.create_task(_run_fishing_task(
            uid=self.uid, public_msg=self.public_msg, fisher=self.fisher,
            rod_key=rod_key, bait_key=bait_key, duration=duration,
            predetermined_fish=new_fish_key, story_full=new_story_full,
        ))

    async def _on_close(self, interaction: discord.Interaction) -> None:
        if not await self._check_owner(interaction):
            return
        self.stop()
        sess = get_active_session(self.uid)
        if sess and sess.get('pending_catch') and self.sold is None and not self.kept:
            await keep_pending(self.uid)
        try:
            await interaction.response.defer()
            await self.public_msg.delete()
        except discord.HTTPException:
            pass

    async def _on_view_pond(self, interaction: discord.Interaction) -> None:
        """送出 ephemeral PondInventoryView。若尚未按出售/保留，先自動保留再開
        （這樣剛釣的魚會出現在保溫箱裡）。"""
        if not await self._check_owner(interaction):
            return

        # 自動保留 pending（讓剛釣的魚顯示在保溫箱）
        sess = get_active_session(self.uid)
        is_unresolved = (
            sess and sess.get('pending_catch') is not None
            and self.sold is None and not self.kept and self.auto_sold is None
        )
        if is_unresolved:
            pc = sess['pending_catch']
            fk = pc['fish_key']
            tag = pc.get('tag')
            spec = FISH_SPECS[fk]
            rarity = spec['rarity']
            ok, _msg = await keep_pending(self.uid)
            if ok:
                self.kept = True
                # 把公開結算頁更新成已保留狀態
                try:
                    new_view = FishingResultView(
                        self.uid, self.public_msg, fisher=self.fisher,
                        last_rod=self.last_rod, last_bait=self.last_bait,
                        kept=True, story_s3=self.story_s3,
                    )
                    new_embed = _post_resolve_embed(
                        self.fisher, kept=True,
                        rarity_was_trash=(rarity == 'trash'),
                        fish_name=display_fish_name(fk, tag),
                        story_line=self.story_s3,
                    )
                    await self.public_msg.edit(embed=new_embed, view=new_view)
                except discord.HTTPException:
                    pass

        view = PondInventoryView(self.uid, page=0)
        await interaction.response.send_message(
            embed=view.build_embed(interaction.user), view=view, ephemeral=True,
        )

    async def _on_view_dex(self, interaction: discord.Interaction) -> None:
        """送出 ephemeral DexView（圖鑑），✖️ 關閉 後消失，不影響公開結算頁。"""
        if not await self._check_owner(interaction):
            return
        view = DexView(self.uid, interaction, page=0, close_on_back=True)
        await interaction.response.send_message(
            embed=view.build_embed(interaction.user), view=view, ephemeral=True,
        )

    async def _on_back_to_settings(self, interaction: discord.Interaction) -> None:
        """刪掉公開結算頁面，並送出新的 ephemeral 設定面板。"""
        if not await self._check_owner(interaction):
            return
        self.stop()
        sess = get_active_session(self.uid)
        if sess and sess.get('pending_catch') and self.sold is None and not self.kept and self.auto_sold is None:
            await keep_pending(self.uid)

        # 先 defer，再刪除公開訊息，再發新 ephemeral
        try:
            await interaction.response.defer(ephemeral=True, thinking=False)
        except discord.HTTPException:
            pass
        try:
            await self.public_msg.delete()
        except discord.HTTPException:
            pass

        # 初始化使用者 record（理應已存在）+ 開 ephemeral 主面板
        async with _FILE_LOCK:
            data = _load_all()
            _get_or_init_rec(data, self.uid)
            save_json(_FILE, data)

        try:
            await interaction.followup.send(
                embed=_main_embed(self.fisher),
                view=FishingMainView(self.uid, interaction),
                ephemeral=True,
            )
        except discord.HTTPException:
            pass


def _post_resolve_embed(user: discord.abc.User, *, sold: int | None = None,
                        kept: bool = False, rarity_was_trash: bool = False,
                        fish_name: str = '', story_line: str = '') -> discord.Embed:
    if sold is not None:
        body = [
            f'💰 售出：**{_fmt_price(sold)}** 咕嚕喵碎片',
            f'💼 目前餘額：**{get_balance(str(user.id)):,}** 碎片',
        ]
        color = discord.Color.gold()
    elif kept:
        body = [f'📦 已保留 **{fish_name}** 進保溫箱']
        color = discord.Color.green()
    else:
        body = ['（無變化）']
        color = discord.Color.greyple()
    lines: list[str] = list(body)
    if story_line:
        lines += ['', '**🎬 環境小事件:**', '', story_line]
    embed = discord.Embed(title='🎣 釣魚結算', description='\n'.join(lines), color=color)
    embed.set_footer(text=user.display_name)
    return embed


def _trash_autosell_embed(user: discord.abc.User, fish_key: str, paid: int,
                          *, story_line: str = '') -> discord.Embed:
    spec = FISH_SPECS[fish_key]
    lines: list[str] = [
        f'{RARITY_EMOJI["trash"]} 撈到一些雜物：**[{RARITY_LABEL["trash"]}] {spec["name"]}**',
        '',
        f'🚮 已自動賣出，回收價 **+{paid}** 碎片',
        f'💼 目前餘額：**{get_balance(str(user.id)):,}** 碎片',
    ]
    buff = _buff_status_line(str(user.id))
    if buff:
        lines += ['', f'**Buff:** {buff}']
    if story_line:
        lines += ['', '**🎬 環境小事件:**', '', story_line]
    embed = discord.Embed(
        title='🎣 釣魚結果',
        description='\n'.join(lines),
        color=discord.Color(RARITY_COLOR['trash']),
    )
    embed.set_footer(text=user.display_name)
    return embed


# ── 魚類圖鑑 View ───────────────────────────────────────────────────────
_DEX_PER_PAGE = 25


class DexView(discord.ui.View):
    def __init__(self, uid: str, root_interaction: discord.Interaction, *, page: int,
                 close_on_back: bool = False):
        super().__init__(timeout=86400)
        self.uid = uid
        self.root_interaction = root_interaction
        self.close_on_back = close_on_back
        self.page = page
        self.species_by_rarity = [
            (r, [k for k in FISH_BY_RARITY[r]]) for r in RARITIES
        ]
        # flatten in rarity order
        self.flat: list[tuple[str, str]] = []
        for r, keys in self.species_by_rarity:
            for k in keys:
                self.flat.append((r, k))
        self.total_pages = max(1, (len(self.flat) + _DEX_PER_PAGE - 1) // _DEX_PER_PAGE)
        self._build()

    def build_embed(self, user: discord.abc.User) -> discord.Embed:
        dex = get_dex(self.uid)
        dex_tags = get_dex_tags(self.uid)
        dex_max_w = get_dex_max_weight(self.uid)
        fish = get_fish(self.uid)
        start = self.page * _DEX_PER_PAGE
        end = start + _DEX_PER_PAGE
        slice_ = self.flat[start:end]

        lines: list[str] = []
        cur_rarity = None
        for r, k in slice_:
            if r != cur_rarity:
                cur_rarity = r
                lines.append(f'\n**{RARITY_EMOJI[r]} {RARITY_LABEL[r]}**')
            mark = '✅' if k in dex else '⬜'
            owned = int(fish.get(k, 0))
            owned_txt = f' × {owned}' if owned > 0 else ''
            spec = FISH_SPECS[k]
            line = f'　{mark} {spec["name"]}{owned_txt} `${spec["price"]:,}`'
            max_w = dex_max_w.get(k)
            if max_w:
                line += f'　⚖️ {max_w:.2f}kg'
            caught_tags = dex_tags.get(k) or []
            if caught_tags:
                line += '　✨ ' + ' '.join(f'【{t}】' for t in caught_tags)
            lines.append(line)

        total = len(self.flat)
        caught = len([k for _, k in self.flat if k in dex])
        embed = discord.Embed(
            title=f'📓 魚類圖鑑 ({caught}/{total})',
            description='\n'.join(lines) or '（無資料）',
            color=discord.Color.dark_teal(),
        )
        embed.set_footer(text=f'頁碼 {self.page + 1}/{self.total_pages}')
        return embed

    def build_history_embed(self, user: discord.abc.User) -> discord.Embed:
        """釣魚歷史：最近 20 條（新到舊）。"""
        hist = list(reversed(get_catch_history(self.uid, limit=20)))
        if not hist:
            desc = '_(還沒有釣魚紀錄)_'
        else:
            lines: list[str] = []
            for entry in hist:
                fk = entry.get('fish_key')
                if fk not in FISH_SPECS:
                    continue
                tag = entry.get('tag')
                wc = entry.get('weight_class')
                kg = float(entry.get('weight_kg') or 0.0)
                price = int(entry.get('price') or 0)
                ts = entry.get('ts', '')
                spec = FISH_SPECS[fk]
                disp = display_fish_name(fk, tag, wc)
                kg_txt = f' {kg:.2f}kg' if kg > 0 else ''
                # ISO ts → Discord 相對時間
                try:
                    dt = datetime.fromisoformat(ts)
                    when = f'<t:{int(dt.timestamp())}:R>'
                except Exception:
                    when = ts
                lines.append(
                    f'{RARITY_EMOJI[spec["rarity"]]} **{disp}**{kg_txt}'
                    f' `${price:,}` {when}'
                )
            desc = '\n'.join(lines)
        embed = discord.Embed(
            title='🎣 釣魚歷史（最近 20 筆）',
            description=desc,
            color=discord.Color.dark_teal(),
        )
        embed.set_footer(text=user.display_name)
        return embed

    def _rarity_first_page(self, rarity: str) -> int:
        """回該稀有度第一條的頁碼（0-indexed）。找不到回 0。"""
        for idx, (r, _) in enumerate(self.flat):
            if r == rarity:
                return idx // _DEX_PER_PAGE
        return 0

    def _page_rarity_range(self) -> tuple[str, str]:
        """目前頁的起 / 訖稀有度（給 select default 用）。"""
        start = self.page * _DEX_PER_PAGE
        end = min(start + _DEX_PER_PAGE - 1, len(self.flat) - 1) if self.flat else 0
        first_r = self.flat[start][0] if self.flat else 'common'
        last_r = self.flat[end][0] if self.flat else 'common'
        return first_r, last_r

    def _build(self) -> None:
        self.clear_items()

        # Row 0: 稀有度跳轉 select
        first_r, _ = self._page_rarity_range()
        rarity_options: list[discord.SelectOption] = []
        for r in RARITIES:
            count = len(FISH_BY_RARITY.get(r) or [])
            if count == 0:
                continue
            rarity_options.append(discord.SelectOption(
                label=f'{RARITY_EMOJI[r]} 跳到 {RARITY_LABEL[r]} ({count})',
                value=r,
                default=(r == first_r),
            ))
        if rarity_options:
            rarity_sel = discord.ui.Select(
                placeholder='跳到稀有度…',
                options=rarity_options, min_values=1, max_values=1, row=0,
            )
            rarity_sel.callback = self._jump_rarity
            self.add_item(rarity_sel)

        # Row 1: 頁數跳轉 select（最多 25 個分頁，總頁多時等距取樣）
        if self.total_pages > 1:
            if self.total_pages <= 25:
                page_values = list(range(self.total_pages))
            else:
                step = self.total_pages / 25
                page_values = sorted({int(i * step) for i in range(25)})[:25]
                if self.page not in page_values:
                    # 把當前頁塞進去取代最近的
                    page_values = sorted(set(page_values) | {self.page})[-25:]
            page_options: list[discord.SelectOption] = []
            for p in page_values:
                page_options.append(discord.SelectOption(
                    label=f'跳到第 {p + 1} 頁',
                    value=str(p),
                    default=(p == self.page),
                ))
            page_sel = discord.ui.Select(
                placeholder=f'跳到頁數… (共 {self.total_pages} 頁)',
                options=page_options, min_values=1, max_values=1, row=1,
            )
            page_sel.callback = self._jump_page
            self.add_item(page_sel)

        # Row 2: 上/下頁 + 返回
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

        history_btn = discord.ui.Button(
            label='📜 釣魚歷史', style=discord.ButtonStyle.secondary, row=2,
        )
        history_btn.callback = self._on_history
        self.add_item(history_btn)

        back_btn = discord.ui.Button(
            label='✖️ 關閉' if self.close_on_back else '⬅️ 返回主面板',
            style=discord.ButtonStyle.danger if self.close_on_back
            else discord.ButtonStyle.primary,
            row=2,
        )
        back_btn.callback = self._back
        self.add_item(back_btn)

    async def _on_history(self, interaction: discord.Interaction) -> None:
        if not await self._check_owner(interaction):
            return
        # 同一個 view 但顯示歷史 embed；按下「關閉」走原回程
        await interaction.response.edit_message(
            embed=self.build_history_embed(interaction.user), view=self,
        )

    async def _check_owner(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message('這不是你的釣魚面板', ephemeral=True)
            return False
        return True

    async def _jump_rarity(self, interaction: discord.Interaction) -> None:
        if not await self._check_owner(interaction):
            return
        rarity = interaction.data['values'][0]
        new_page = self._rarity_first_page(rarity)
        new = DexView(self.uid, self.root_interaction, page=new_page,
                      close_on_back=self.close_on_back)
        await interaction.response.edit_message(
            embed=new.build_embed(interaction.user), view=new,
        )

    async def _jump_page(self, interaction: discord.Interaction) -> None:
        if not await self._check_owner(interaction):
            return
        try:
            new_page = max(0, min(self.total_pages - 1, int(interaction.data['values'][0])))
        except (ValueError, KeyError):
            new_page = 0
        new = DexView(self.uid, self.root_interaction, page=new_page,
                      close_on_back=self.close_on_back)
        await interaction.response.edit_message(
            embed=new.build_embed(interaction.user), view=new,
        )

    async def _prev(self, interaction: discord.Interaction) -> None:
        if not await self._check_owner(interaction):
            return
        self.page = max(0, self.page - 1)
        new = DexView(self.uid, self.root_interaction, page=self.page,
                      close_on_back=self.close_on_back)
        await interaction.response.edit_message(embed=new.build_embed(interaction.user), view=new)

    async def _next(self, interaction: discord.Interaction) -> None:
        if not await self._check_owner(interaction):
            return
        self.page = min(self.total_pages - 1, self.page + 1)
        new = DexView(self.uid, self.root_interaction, page=self.page,
                      close_on_back=self.close_on_back)
        await interaction.response.edit_message(embed=new.build_embed(interaction.user), view=new)

    async def _back(self, interaction: discord.Interaction) -> None:
        if not await self._check_owner(interaction):
            return
        if self.close_on_back:
            try:
                await interaction.response.defer()
                await interaction.delete_original_response()
            except discord.HTTPException:
                pass
            return
        main = FishingMainView(self.uid, self.root_interaction)
        await interaction.response.edit_message(
            embed=_main_embed(interaction.user), view=main,
        )


# ── 保溫箱庫存（從釣魚結算頁開啟，可順手賣）─────────────────────────────
class PondInventoryView(discord.ui.View):
    """ephemeral 視窗：顯示玩家保溫箱現有的魚 + 可從這賣（單選或批量）。
    與 SellFishView / DexView 完全獨立，避免互相影響。

    root_interaction:
      - 非 None（從 FishingMainView 進來）→ 返回鈕回主面板
      - None（從 FishingResultView 進來）→ 返回鈕關掉本 ephemeral
    """

    _PAGE_SIZE = 20

    def __init__(self, uid: str, *, page: int = 0,
                 root_interaction: discord.Interaction | None = None):
        super().__init__(timeout=86400)
        self.uid = uid
        self.page = page
        self.root_interaction = root_interaction
        def _sort_key(kv):
            fk, _, _ = _split_stored_key(kv[0])
            return (list(RARITIES).index(FISH_SPECS[fk]['rarity']), kv[0])
        self._owned: list[tuple[str, int]] = sorted(
            ((k, int(v)) for k, v in get_fish(self.uid).items()
             if int(v) > 0 and _split_stored_key(k)[0] in FISH_SPECS),
            key=_sort_key,
        )
        self.total_pages = max(
            1, (len(self._owned) + self._PAGE_SIZE - 1) // self._PAGE_SIZE
        )
        self._build()

    def build_embed(self, user: discord.abc.User) -> discord.Embed:
        cap = get_fish_pond_cap(self.uid)
        total_count = sum(n for _, n in self._owned)
        rec = get_user_rec(self.uid)
        pb = rec.get('pending_bonus') or {}
        blessing_on = meow_blessing_active(self.uid)
        mult = MEOW_BLESSING_PRICE_MULT if blessing_on else 1.0

        def _final_unit(fk, tag, wc):
            return int(get_tagged_price(fk, tag, wc) * mult)

        total_value = 0
        for k, n in self._owned:
            fk, tag, wc = _split_stored_key(k)
            line_total = _final_unit(fk, tag, wc) * n + int(pb.get(k, 0) * mult)
            total_value += line_total
        if not self._owned:
            desc = '_(保溫箱裡沒有任何魚)_'
        else:
            start = self.page * self._PAGE_SIZE
            slice_ = self._owned[start:start + self._PAGE_SIZE]
            lines: list[str] = []
            for k, n in slice_:
                fk, tag, wc = _split_stored_key(k)
                spec = FISH_SPECS[fk]
                disp = display_fish_name(fk, tag, wc)
                unit_price = _final_unit(fk, tag, wc)
                bonus = int(pb.get(k, 0) * mult)
                bonus_txt = f'　🌅 每日首釣 +{bonus:,}' if bonus else ''
                lines.append(
                    f'{RARITY_EMOJI[spec["rarity"]]} **{disp}** × {n}'
                    f' `${unit_price:,}/條`{bonus_txt}'
                )
            desc = '\n'.join(lines)
        footer = (
            f'\n\n💰 全賣可得：**{total_value:,}** 碎片'
            f'\n💼 目前餘額：**{get_balance(self.uid):,}** 碎片'
        )
        if blessing_on:
            footer += (f'\n🍀 小龍喵祝福加成中（售價 ×{MEOW_BLESSING_PRICE_MULT}）')
        return discord.Embed(
            title=f'📦 我的保溫箱 ({total_count}/{cap})'
                  f' — 第 {self.page + 1}/{self.total_pages} 頁',
            description=desc + footer,
            color=discord.Color.dark_teal(),
        )

    def _build(self) -> None:
        start = self.page * self._PAGE_SIZE
        slice_ = self._owned[start:start + self._PAGE_SIZE]

        # Row 0: Select 選某一品種
        if slice_:
            blessing_on = meow_blessing_active(self.uid)
            mult = MEOW_BLESSING_PRICE_MULT if blessing_on else 1.0
            options: list[discord.SelectOption] = []
            for k, n in slice_[:25]:
                fk, tag, wc = _split_stored_key(k)
                spec = FISH_SPECS[fk]
                disp = display_fish_name(fk, tag, wc)
                unit = int(get_tagged_price(fk, tag, wc) * mult)
                options.append(discord.SelectOption(
                    label=f'{disp} × {n}'[:100],
                    description=f'{RARITY_LABEL[spec["rarity"]]} | ${unit:,}/條',
                    value=k,
                ))
            sel = discord.ui.Select(
                placeholder='選一個品種來賣（會跳出數量輸入）',
                options=options, min_values=1, max_values=1, row=0,
            )
            sel.callback = self._on_pick
            self.add_item(sel)

        # Row 1: 全賣 trash/common/rare
        for label, rarity in [('全賣垃圾', 'trash'),
                              ('全賣普通', 'common'),
                              ('全賣罕見', 'rare')]:
            b = discord.ui.Button(label=label, style=discord.ButtonStyle.danger, row=1)
            b.callback = self._make_bulk(rarity)
            self.add_item(b)

        # Row 2: 全賣 epic/legendary/全部
        for label, rarity in [('全賣史詩', 'epic'),
                              ('全賣傳說', 'legendary')]:
            b = discord.ui.Button(label=label, style=discord.ButtonStyle.danger, row=2)
            b.callback = self._make_bulk(rarity)
            self.add_item(b)
        all_btn = discord.ui.Button(label='💸 全賣全部',
                                    style=discord.ButtonStyle.danger, row=2)
        all_btn.callback = self._sell_all
        self.add_item(all_btn)

        # Row 3: 分頁 + 關閉
        if self.total_pages > 1:
            prev_btn = discord.ui.Button(
                label='◀️', style=discord.ButtonStyle.secondary,
                disabled=(self.page <= 0), row=3,
            )
            prev_btn.callback = self._prev
            self.add_item(prev_btn)
            next_btn = discord.ui.Button(
                label='▶️', style=discord.ButtonStyle.secondary,
                disabled=(self.page >= self.total_pages - 1), row=3,
            )
            next_btn.callback = self._next
            self.add_item(next_btn)
        back_label = ('⬅️ 返回主面板' if self.root_interaction is not None
                      else '🎣 返回釣魚')
        close_btn = discord.ui.Button(label=back_label,
                                      style=discord.ButtonStyle.primary, row=3)
        close_btn.callback = self._close
        self.add_item(close_btn)

    async def _check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message(
                '這不是你的保溫箱', ephemeral=True,
            )
            return False
        return True

    async def _redraw(self, interaction: discord.Interaction) -> None:
        new = PondInventoryView(self.uid, page=0,
                                root_interaction=self.root_interaction)
        await interaction.response.edit_message(
            embed=new.build_embed(interaction.user), view=new,
        )

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        if not await self._check(interaction):
            return
        key = interaction.data['values'][0]
        await interaction.response.send_modal(_PondSellQtyModal(self.uid, key))

    def _make_bulk(self, rarity: str):
        async def _cb(interaction: discord.Interaction) -> None:
            if not await self._check(interaction):
                return
            count, total = await sell_all_fish_by_rarity(self.uid, rarity)
            await self._redraw(interaction)
            try:
                if count > 0:
                    await interaction.followup.send(
                        f'✅ 已賣出 **{count}** 條【{RARITY_LABEL[rarity]}】魚，'
                        f'獲得 `{total:,}` 碎片', ephemeral=True,
                    )
                else:
                    await interaction.followup.send(
                        f'目前沒有【{RARITY_LABEL[rarity]}】魚可賣', ephemeral=True,
                    )
            except discord.HTTPException:
                pass
        return _cb

    async def _sell_all(self, interaction: discord.Interaction) -> None:
        if not await self._check(interaction):
            return
        count, total = await sell_all_fish(self.uid)
        await self._redraw(interaction)
        try:
            await interaction.followup.send(
                f'✅ 清空保溫箱：賣出 **{count}** 條，獲得 `{total:,}` 碎片',
                ephemeral=True,
            )
        except discord.HTTPException:
            pass

    async def _prev(self, interaction: discord.Interaction) -> None:
        if not await self._check(interaction):
            return
        new = PondInventoryView(self.uid, page=max(0, self.page - 1),
                                root_interaction=self.root_interaction)
        await interaction.response.edit_message(
            embed=new.build_embed(interaction.user), view=new,
        )

    async def _next(self, interaction: discord.Interaction) -> None:
        if not await self._check(interaction):
            return
        new = PondInventoryView(self.uid,
                                page=min(self.total_pages - 1, self.page + 1),
                                root_interaction=self.root_interaction)
        await interaction.response.edit_message(
            embed=new.build_embed(interaction.user), view=new,
        )

    async def _close(self, interaction: discord.Interaction) -> None:
        if not await self._check(interaction):
            return
        if self.root_interaction is not None:
            # 從主面板進來 → 切回主面板
            main = FishingMainView(self.uid, self.root_interaction)
            await interaction.response.edit_message(
                embed=_main_embed(interaction.user), view=main,
            )
        else:
            # 從結算頁進來 → 關掉 ephemeral，公開結算頁仍可見
            try:
                await interaction.response.defer()
                await interaction.delete_original_response()
            except discord.HTTPException:
                pass


class _PondSellQtyModal(discord.ui.Modal, title='保溫箱賣魚 — 數量'):
    qty_input = discord.ui.TextInput(
        label='數量', placeholder='輸入要賣幾條（1 ~ 持有數）',
        required=True, min_length=1, max_length=4, default='1',
    )

    def __init__(self, uid: str, fish_key: str):
        super().__init__()
        self.uid = uid
        self.fish_key = fish_key

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            qty = int(str(self.qty_input.value).strip())
        except ValueError:
            await interaction.response.send_message('數量必須是整數', ephemeral=True)
            return
        if qty <= 0:
            await interaction.response.send_message('數量必須為正', ephemeral=True)
            return
        ok, err, gain = await sell_fish_from_pond(self.uid, self.fish_key, qty)
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        # 重建並 edit 原 ephemeral
        new = PondInventoryView(self.uid, page=0)
        try:
            await interaction.edit_original_response(
                embed=new.build_embed(interaction.user), view=new,
            )
        except discord.HTTPException:
            pass
        fk, tag, wc = _split_stored_key(self.fish_key)
        name = display_fish_name(fk, tag, wc)
        msg = (f'✅ 已賣出 **{name}** × {qty}，獲得 `{gain:,}` 碎片'
               if ok else f'❌ {err}')
        try:
            await interaction.followup.send(msg, ephemeral=True)
        except discord.HTTPException:
            pass


# ── 投餵小龍喵 picker ──────────────────────────────────────────────────
class FeedMeowView(discord.ui.View):
    """從保溫箱挑一條魚投餵小龍喵：啟動全幸運祝福（一天 1 次）。"""

    _PAGE_SIZE = 25  # Discord Select 單頁上限

    def __init__(self, uid: str, *, page: int,
                 root_interaction: discord.Interaction):
        super().__init__(timeout=86400)
        self.uid = uid
        self.page = page
        self.root_interaction = root_interaction

        def _sort_key(kv):
            fk, _, _ = _split_stored_key(kv[0])
            return (list(RARITIES).index(FISH_SPECS[fk]['rarity']), kv[0])
        # 過濾掉 trash（小龍喵不吃）
        self._owned: list[tuple[str, int]] = sorted(
            ((k, int(v)) for k, v in get_fish(self.uid).items()
             if int(v) > 0
             and _split_stored_key(k)[0] in FISH_SPECS
             and FISH_SPECS[_split_stored_key(k)[0]]['rarity'] != 'trash'),
            key=_sort_key,
        )
        self.total_pages = max(
            1, (len(self._owned) + self._PAGE_SIZE - 1) // self._PAGE_SIZE
        )
        self._build()

    def build_embed(self, user: discord.abc.User) -> discord.Embed:
        already = fed_meow_today(self.uid)
        cur_until = get_meow_blessing_until(self.uid)
        header = [
            '**🦞 投餵小龍喵**',
            '',
            '挑一條魚當作貢品，小龍喵會給你祝福：',
            '幸運Ⅰ（時長 -20%）/ 幸運Ⅱ（+運氣）/ 幸運Ⅲ（變體機率 ↑）三種**同時生效**。',
            f'💰 祝福期間售魚價格 **×{MEOW_BLESSING_PRICE_MULT}**。',
            '與花錢買的幸運藥水**可疊加**（藥水之間仍互斥）。',
            '',
            '時長：稀有度 + 變體 tag (+120) 再 × 重量倍率（light 0.75 / normal 1 / heavy 1.4 / super 2.5），上限 6h。',
            f'└ common 30m / rare 60m / epic 120m / legendary 180m / mythic 240m',
            '',
            '每天只能投餵 1 次。',
        ]
        if cur_until:
            header.append(f'\n🍀 目前祝福剩餘到 <t:{int(cur_until.timestamp())}:R>')
        if already:
            header.append('⏳ 今天已經投餵過了，明天 00:00 後可再投餵')

        if not self._owned:
            body = '_(保溫箱裡沒有可投餵的魚)_'
        else:
            start = self.page * self._PAGE_SIZE
            slice_ = self._owned[start:start + self._PAGE_SIZE]
            lines = []
            for k, n in slice_:
                fk, tag, wc = _split_stored_key(k)
                spec = FISH_SPECS[fk]
                disp = display_fish_name(fk, tag, wc)
                mins = compute_meow_duration_minutes(fk, tag, wc)
                lines.append(
                    f'{RARITY_EMOJI[spec["rarity"]]} **{disp}** × {n}'
                    f' → 🍀 {mins} 分鐘'
                )
            body = '\n'.join(lines)

        return discord.Embed(
            title=f'🦞 投餵小龍喵 — 第 {self.page + 1}/{self.total_pages} 頁',
            description='\n'.join(header) + '\n\n' + body,
            color=discord.Color.from_rgb(255, 215, 0),
        )

    def _build(self) -> None:
        self.clear_items()
        already = fed_meow_today(self.uid)
        start = self.page * self._PAGE_SIZE
        slice_ = self._owned[start:start + self._PAGE_SIZE]

        # Row 0: Select 選魚
        if slice_ and not already:
            options: list[discord.SelectOption] = []
            for k, n in slice_:
                fk, tag, wc = _split_stored_key(k)
                spec = FISH_SPECS[fk]
                disp = display_fish_name(fk, tag, wc)
                mins = compute_meow_duration_minutes(fk, tag, wc)
                options.append(discord.SelectOption(
                    label=f'{disp} × {n}'[:100],
                    description=f'{RARITY_LABEL[spec["rarity"]]} | 祝福 {mins} 分鐘',
                    value=k,
                ))
            sel = discord.ui.Select(
                placeholder='選一條魚投餵小龍喵',
                options=options, min_values=1, max_values=1, row=0,
            )
            sel.callback = self._on_pick
            self.add_item(sel)
        elif not slice_:
            sel = discord.ui.Select(
                placeholder='保溫箱沒有可投餵的魚（trash 不能餵）',
                options=[discord.SelectOption(label='（空）', value='_none')],
                min_values=1, max_values=1, row=0, disabled=True,
            )
            self.add_item(sel)
        else:
            sel = discord.ui.Select(
                placeholder='今天已經投餵過了',
                options=[discord.SelectOption(label='（明天再來）', value='_none')],
                min_values=1, max_values=1, row=0, disabled=True,
            )
            self.add_item(sel)

        # Row 1: 分頁 + 返回
        if self.total_pages > 1:
            prev_btn = discord.ui.Button(
                label='◀️', style=discord.ButtonStyle.secondary,
                disabled=(self.page <= 0), row=1,
            )
            prev_btn.callback = self._prev
            self.add_item(prev_btn)
            next_btn = discord.ui.Button(
                label='▶️', style=discord.ButtonStyle.secondary,
                disabled=(self.page >= self.total_pages - 1), row=1,
            )
            next_btn.callback = self._next
            self.add_item(next_btn)
        back_btn = discord.ui.Button(
            label='⬅️ 返回主面板', style=discord.ButtonStyle.primary, row=1,
        )
        back_btn.callback = self._back
        self.add_item(back_btn)

    async def _check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message(
                '這不是你的釣魚面板', ephemeral=True,
            )
            return False
        return True

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        if not await self._check(interaction):
            return
        key = interaction.data['values'][0]
        ok, err, new_exp, minutes = await feed_meow(self.uid, key)
        if not ok:
            await interaction.response.send_message(f'❌ {err}', ephemeral=True)
            return
        fk, tag, wc = _split_stored_key(key)
        name = display_fish_name(fk, tag, wc)
        msg = (
            f'🦞 小龍喵心滿意足地吞下了 **{name}**！\n'
            f'✨ 獲得祝福 **{minutes} 分鐘**'
            f'（幸運Ⅰ + Ⅱ + Ⅲ 同時生效）\n'
            f'⏰ 到期 <t:{int(new_exp.timestamp())}:R>'
        )
        # 重建本 view（已餵 → 禁用）並 edit
        new = FeedMeowView(self.uid, page=self.page,
                           root_interaction=self.root_interaction)
        await interaction.response.edit_message(
            embed=new.build_embed(interaction.user), view=new,
        )
        try:
            await interaction.followup.send(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    async def _prev(self, interaction: discord.Interaction) -> None:
        if not await self._check(interaction):
            return
        new = FeedMeowView(self.uid, page=max(0, self.page - 1),
                           root_interaction=self.root_interaction)
        await interaction.response.edit_message(
            embed=new.build_embed(interaction.user), view=new,
        )

    async def _next(self, interaction: discord.Interaction) -> None:
        if not await self._check(interaction):
            return
        new = FeedMeowView(self.uid, page=min(self.total_pages - 1, self.page + 1),
                           root_interaction=self.root_interaction)
        await interaction.response.edit_message(
            embed=new.build_embed(interaction.user), view=new,
        )

    async def _back(self, interaction: discord.Interaction) -> None:
        if not await self._check(interaction):
            return
        main = FishingMainView(self.uid, self.root_interaction)
        await interaction.response.edit_message(
            embed=_main_embed(interaction.user), view=main,
        )


# ── 釣竿購買子面板 ──────────────────────────────────────────────────────
class RodShopView(discord.ui.View):
    def __init__(self, uid: str, root_interaction: discord.Interaction,
                 *, back_to_shop_equip: bool = False,
                 back_to_fishing_main: bool = False):
        super().__init__(timeout=86400)
        self.uid = uid
        self.root_interaction = root_interaction
        self.back_to_shop_equip = back_to_shop_equip
        self.back_to_fishing_main = back_to_fishing_main
        self._build()

    def build_embed(self, user: discord.abc.User) -> discord.Embed:
        owned = set(get_rods(self.uid))
        balance = get_balance(self.uid)
        lines = [f'💰 餘額：**{balance:,}** 咕嚕喵碎片', '']
        if not owned:
            lines.append('_(目前沒有任何釣竿；買 T1 來開始釣魚)_')
            lines.append('')
        for k in sorted(ROD_SPECS.keys(), key=lambda x: ROD_SPECS[x]['tier']):
            spec = ROD_SPECS[k]
            owned_mark = '✅ 已擁有' if k in owned else f'`{spec["price"]:,}`'
            lines.append(
                f'**T{spec["tier"]} {spec["name"]}** — {owned_mark}\n'
                f'　{_rod_effect_str(spec)}　{spec["note"]}'
            )
        embed = discord.Embed(
            title='🎣 購買釣竿',
            description='\n'.join(lines)[:4000],
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=user.display_name)
        return embed

    def _build(self) -> None:
        self.clear_items()
        owned = set(get_rods(self.uid))
        balance = get_balance(self.uid)
        options: list[discord.SelectOption] = []
        for k in sorted(ROD_SPECS.keys(), key=lambda x: ROD_SPECS[x]['tier']):
            spec = ROD_SPECS[k]
            if spec['price'] <= 0:
                continue
            if k in owned:
                continue
            options.append(discord.SelectOption(
                label=f'T{spec["tier"]} {spec["name"]} ({spec["price"]:,})',
                value=k,
                description=_rod_effect_str(spec),
            ))
        if options:
            sel = discord.ui.Select(
                placeholder='選擇要購買的釣竿',
                options=options, min_values=1, max_values=1, row=0,
            )
            sel.callback = self._on_buy
            self.add_item(sel)

        back = discord.ui.Button(
            label='⬅️ 返回釣魚用品' if self.back_to_shop_equip else '⬅️ 返回主面板',
            style=discord.ButtonStyle.primary, row=1,
        )
        back.callback = self._back
        self.add_item(back)

        go_fishing = discord.ui.Button(
            label='🎣 前往釣魚', style=discord.ButtonStyle.success, row=1,
        )
        go_fishing.callback = self._go_fishing
        self.add_item(go_fishing)
        _ = balance   # 留用避免被 linter 砍

    async def _check_owner(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message('這不是你的釣魚面板', ephemeral=True)
            return False
        return True

    async def _on_buy(self, interaction: discord.Interaction) -> None:
        if not await self._check_owner(interaction):
            return
        val = interaction.data['values'][0]
        ok, err = await buy_rod(self.uid, val)
        msg = f'✅ 已購買 **{ROD_SPECS[val]["name"]}**' if ok else f'❌ {err}'
        new = RodShopView(self.uid, self.root_interaction,
                          back_to_shop_equip=self.back_to_shop_equip,
                          back_to_fishing_main=self.back_to_fishing_main)
        await interaction.response.edit_message(embed=new.build_embed(interaction.user), view=new)
        try:
            await interaction.followup.send(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    async def _back(self, interaction: discord.Interaction) -> None:
        if not await self._check_owner(interaction):
            return
        if self.back_to_shop_equip:
            from commands.shop import ShopFishingEquipView
            view = ShopFishingEquipView(
                self.uid, root_interaction=self.root_interaction,
                back_to_fishing_main=self.back_to_fishing_main,
            )
            await interaction.response.edit_message(
                embed=view.build_embed(interaction.user), view=view,
            )
        else:
            await interaction.response.edit_message(
                embed=_main_embed(interaction.user),
                view=FishingMainView(self.uid, self.root_interaction),
            )

    async def _go_fishing(self, interaction: discord.Interaction) -> None:
        """無論 entry 是商店或 /fishing，都把面板切到釣魚主面板。"""
        if not await self._check_owner(interaction):
            return
        await interaction.response.edit_message(
            embed=_main_embed(interaction.user),
            view=FishingMainView(self.uid, self.root_interaction),
        )


# ── 魚餌購買子面板 ──────────────────────────────────────────────────────
_BAITS_SORTED = sorted(BAIT_SPECS.keys(), key=lambda k: BAIT_SPECS[k]['price'])
_BAIT_PAGE_SIZE = 10


class BaitShopView(discord.ui.View):
    def __init__(self, uid: str, root_interaction: discord.Interaction, *, page: int,
                 back_to_shop_equip: bool = False,
                 back_to_fishing_main: bool = False):
        super().__init__(timeout=86400)
        self.uid = uid
        self.root_interaction = root_interaction
        self.page = page
        self.back_to_shop_equip = back_to_shop_equip
        self.back_to_fishing_main = back_to_fishing_main
        self.total_pages = max(1, (len(_BAITS_SORTED) + _BAIT_PAGE_SIZE - 1) // _BAIT_PAGE_SIZE)
        self._build()

    def build_embed(self, user: discord.abc.User) -> discord.Embed:
        baits_owned = get_baits(self.uid)
        balance = get_balance(self.uid)
        start = self.page * _BAIT_PAGE_SIZE
        end = start + _BAIT_PAGE_SIZE
        page_keys = _BAITS_SORTED[start:end]
        lines = [f'💰 餘額：**{balance:,}** 咕嚕喵碎片', '']
        for k in page_keys:
            bs = BAIT_SPECS[k]
            owned = int(baits_owned.get(k, 0))
            lines.append(
                f'🪱 **{bs["name"]}** — `{bs["price"]:,}` / 個（持有 {owned}）\n'
                f'　{bs["note"]}'
            )
        embed = discord.Embed(
            title='🪱 購買魚餌',
            description='\n'.join(lines)[:4000],
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f'頁碼 {self.page + 1}/{self.total_pages}')
        return embed

    def _build(self) -> None:
        self.clear_items()
        start = self.page * _BAIT_PAGE_SIZE
        end = start + _BAIT_PAGE_SIZE
        page_keys = _BAITS_SORTED[start:end]
        options: list[discord.SelectOption] = []
        for k in page_keys:
            bs = BAIT_SPECS[k]
            options.append(discord.SelectOption(
                label=f'{_short(bs["name"], 18)} ({bs["price"]:,})',
                value=k,
                description=_short(bs['note'], 90),
            ))
        if options:
            sel = discord.ui.Select(
                placeholder='選擇要購買的魚餌',
                options=options, min_values=1, max_values=1, row=0,
            )
            sel.callback = self._on_pick
            self.add_item(sel)

        prev_btn = discord.ui.Button(
            label='◀️', style=discord.ButtonStyle.secondary,
            disabled=(self.page <= 0), row=1,
        )
        prev_btn.callback = self._prev
        self.add_item(prev_btn)
        next_btn = discord.ui.Button(
            label='▶️', style=discord.ButtonStyle.secondary,
            disabled=(self.page >= self.total_pages - 1), row=1,
        )
        next_btn.callback = self._next
        self.add_item(next_btn)
        back = discord.ui.Button(
            label='⬅️ 返回釣魚用品' if self.back_to_shop_equip else '⬅️ 返回主面板',
            style=discord.ButtonStyle.primary, row=1,
        )
        back.callback = self._back
        self.add_item(back)

        go_fishing = discord.ui.Button(
            label='🎣 前往釣魚', style=discord.ButtonStyle.success, row=1,
        )
        go_fishing.callback = self._go_fishing
        self.add_item(go_fishing)

    async def _check_owner(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message('這不是你的釣魚面板', ephemeral=True)
            return False
        return True

    async def _go_fishing(self, interaction: discord.Interaction) -> None:
        if not await self._check_owner(interaction):
            return
        await interaction.response.edit_message(
            embed=_main_embed(interaction.user),
            view=FishingMainView(self.uid, self.root_interaction),
        )

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        if not await self._check_owner(interaction):
            return
        val = interaction.data['values'][0]
        await interaction.response.send_modal(BaitBuyModal(
            self.uid, self.root_interaction, val, self.page,
            back_to_shop_equip=self.back_to_shop_equip,
            back_to_fishing_main=self.back_to_fishing_main,
        ))

    async def _prev(self, interaction: discord.Interaction) -> None:
        if not await self._check_owner(interaction):
            return
        self.page = max(0, self.page - 1)
        new = BaitShopView(self.uid, self.root_interaction, page=self.page,
                           back_to_shop_equip=self.back_to_shop_equip,
                           back_to_fishing_main=self.back_to_fishing_main)
        await interaction.response.edit_message(embed=new.build_embed(interaction.user), view=new)

    async def _next(self, interaction: discord.Interaction) -> None:
        if not await self._check_owner(interaction):
            return
        self.page = min(self.total_pages - 1, self.page + 1)
        new = BaitShopView(self.uid, self.root_interaction, page=self.page,
                           back_to_shop_equip=self.back_to_shop_equip,
                           back_to_fishing_main=self.back_to_fishing_main)
        await interaction.response.edit_message(embed=new.build_embed(interaction.user), view=new)

    async def _back(self, interaction: discord.Interaction) -> None:
        if not await self._check_owner(interaction):
            return
        if self.back_to_shop_equip:
            from commands.shop import ShopFishingEquipView
            view = ShopFishingEquipView(
                self.uid, root_interaction=self.root_interaction,
                back_to_fishing_main=self.back_to_fishing_main,
            )
            await interaction.response.edit_message(
                embed=view.build_embed(interaction.user), view=view,
            )
        else:
            await interaction.response.edit_message(
                embed=_main_embed(interaction.user),
                view=FishingMainView(self.uid, self.root_interaction),
            )


class BaitBuyModal(discord.ui.Modal, title='購買魚餌'):
    qty_input = discord.ui.TextInput(
        label='數量', placeholder='輸入要買幾個', min_length=1, max_length=4,
        required=True, default='1',
    )

    def __init__(self, uid: str, root_interaction: discord.Interaction,
                 bait_key: str, page: int, *, back_to_shop_equip: bool = False,
                 back_to_fishing_main: bool = False):
        super().__init__()
        self.uid = uid
        self.root_interaction = root_interaction
        self.bait_key = bait_key
        self.page = page
        self.back_to_shop_equip = back_to_shop_equip
        self.back_to_fishing_main = back_to_fishing_main

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            qty = int(str(self.qty_input.value).strip())
        except ValueError:
            await interaction.response.send_message('數量必須是整數', ephemeral=True)
            return
        if qty <= 0:
            await interaction.response.send_message('數量必須為正', ephemeral=True)
            return
        ok, err, cost = await buy_bait(self.uid, self.bait_key, qty)
        if not ok:
            await interaction.response.send_message(err, ephemeral=True)
            return
        # 顯示成功訊息（followup）+ 刷子面板
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        new = BaitShopView(self.uid, self.root_interaction, page=self.page,
                           back_to_shop_equip=self.back_to_shop_equip,
                           back_to_fishing_main=self.back_to_fishing_main)
        try:
            await self.root_interaction.edit_original_response(
                embed=new.build_embed(interaction.user), view=new,
            )
        except discord.HTTPException:
            pass
        try:
            await interaction.followup.send(
                f'✅ 已購買 {BAIT_SPECS[self.bait_key]["name"]} × {qty}，'
                f'扣 `{cost:,}` 碎片', ephemeral=True,
            )
        except discord.HTTPException:
            pass


# ── 頻道白名單 View ─────────────────────────────────────────────────────
class WhitelistView(discord.ui.View):
    def __init__(self, uid: str, root_interaction: discord.Interaction):
        super().__init__(timeout=86400)
        self.uid = uid
        self.root_interaction = root_interaction
        self._build()

    def build_embed(self, user: discord.abc.User) -> discord.Embed:
        gid = self.root_interaction.guild.id if self.root_interaction.guild else 0
        wl = get_whitelist(gid) if gid else []
        if not wl:
            current = '_(未設定 — 所有頻道皆可釣魚)_'
        else:
            current = '\n'.join(f'<#{c}>' for c in wl)
        lines = [
            '**📋 釣魚頻道白名單**',
            '',
            '此白名單作用於整個伺服器：清單為空時 = 所有頻道可釣魚；',
            '清單非空時 = 只有清單內的頻道能釣魚。',
            '',
            f'**目前白名單：**\n{current}',
            '',
            f'**目前所在頻道：** <#{self.root_interaction.channel_id}>',
        ]
        embed = discord.Embed(
            title='🎣 頻道白名單', description='\n'.join(lines),
            color=discord.Color.teal(),
        )
        return embed

    def _build(self) -> None:
        self.clear_items()
        gid = self.root_interaction.guild.id if self.root_interaction.guild else 0
        cid = self.root_interaction.channel_id
        in_wl = bool(cid and gid and cid in get_whitelist(gid))

        add_btn = discord.ui.Button(
            label='➕ 加入目前頻道', style=discord.ButtonStyle.success,
            disabled=in_wl or (gid == 0), row=0,
        )
        add_btn.callback = self._on_add
        self.add_item(add_btn)

        rm_btn = discord.ui.Button(
            label='➖ 從白名單移除', style=discord.ButtonStyle.danger,
            disabled=(not in_wl), row=0,
        )
        rm_btn.callback = self._on_remove
        self.add_item(rm_btn)

        back = discord.ui.Button(
            label='⬅️ 返回主面板', style=discord.ButtonStyle.primary, row=1,
        )
        back.callback = self._back
        self.add_item(back)

    async def _on_add(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message('只能在伺服器內設定', ephemeral=True)
            return
        ok = await add_whitelist(interaction.guild.id, interaction.channel_id)
        msg = '✅ 已加入白名單' if ok else '此頻道已在白名單內'
        new = WhitelistView(self.uid, self.root_interaction)
        await interaction.response.edit_message(embed=new.build_embed(interaction.user), view=new)
        try:
            await interaction.followup.send(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    async def _on_remove(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message('只能在伺服器內設定', ephemeral=True)
            return
        ok = await remove_whitelist(interaction.guild.id, interaction.channel_id)
        msg = '✅ 已從白名單移除' if ok else '此頻道不在白名單內'
        new = WhitelistView(self.uid, self.root_interaction)
        await interaction.response.edit_message(embed=new.build_embed(interaction.user), view=new)
        try:
            await interaction.followup.send(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    async def _back(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            embed=_main_embed(interaction.user),
            view=FishingMainView(self.uid, self.root_interaction),
        )


# ── 贈禮接收訊息 View（給 shop.py 贈禮流程發訊息用） ────────────────────
# persistent view：timeout=None + 按鈕 custom_id 內嵌 gift_id。
# bot 重啟時要在 on_ready 透過 register_persistent_gift_views(client) 重新註冊。
_GIFT_ACCEPT_RE = 'gift_accept:'
_GIFT_REJECT_RE = 'gift_reject:'


async def _gift_accept_handler(interaction: discord.Interaction, gift_id: str) -> None:
    data = _load_gifts()
    g = (data.get('gifts') or {}).get(gift_id)
    if g is None:
        try:
            await interaction.response.edit_message(
                content='此贈禮已失效或已被處理', view=None, embed=None,
            )
        except discord.HTTPException:
            pass
        return
    if str(interaction.user.id) != g['to_uid']:
        await interaction.response.send_message('這不是給你的贈禮', ephemeral=True)
        return
    ok, err = await claim_gift(gift_id, g['to_uid'])
    if not ok:
        await interaction.response.send_message(err, ephemeral=True)
        return
    await interaction.response.edit_message(
        content=f'🎉 <@{g["to_uid"]}> 已接收 <@{g["from_uid"]}> 的贈禮！',
        embed=None, view=None,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def _gift_reject_handler(interaction: discord.Interaction, gift_id: str) -> None:
    data = _load_gifts()
    g = (data.get('gifts') or {}).get(gift_id)
    if g is None:
        try:
            await interaction.response.edit_message(
                content='此贈禮已失效或已被處理', view=None, embed=None,
            )
        except discord.HTTPException:
            pass
        return
    if str(interaction.user.id) != g['to_uid']:
        await interaction.response.send_message('這不是給你的贈禮', ephemeral=True)
        return
    await refund_gift(gift_id)
    await interaction.response.edit_message(
        content=f'↩️ <@{g["to_uid"]}> 拒絕了 <@{g["from_uid"]}> 的贈禮，物品已退回',
        embed=None, view=None,
        allowed_mentions=discord.AllowedMentions.none(),
    )


class GiftAcceptView(discord.ui.View):
    """persistent view (timeout=None) — bot 重啟後仍可運作，前提是 client.add_view
    時被重新註冊。"""

    def __init__(self, gift_id: str | None = None):
        super().__init__(timeout=None)
        # gift_id=None 時為註冊用 prototype（add_view 時用），實際按鈕 custom_id
        # 仍需要每個 instance 各自帶 gift_id；因此 persistent 採取 listener 模式：
        # 用 on_interaction 攔 custom_id 比較好。
        # 為了相容既有呼叫端，仍保留按鈕生成；註冊則透過 on_interaction 全域處理。
        if gift_id is not None:
            accept = discord.ui.Button(
                label='🎁 接收', style=discord.ButtonStyle.success,
                custom_id=f'{_GIFT_ACCEPT_RE}{gift_id}',
            )
            self.add_item(accept)
            reject = discord.ui.Button(
                label='❌ 拒絕', style=discord.ButtonStyle.secondary,
                custom_id=f'{_GIFT_REJECT_RE}{gift_id}',
            )
            self.add_item(reject)


async def dispatch_gift_interaction(interaction: discord.Interaction) -> bool:
    """供 main.py on_interaction 攔截 — 處理「重啟後 view instance 已不在」的情況。
    回 True 表示已處理，呼叫端不要再 dispatch。"""
    if interaction.type != discord.InteractionType.component:
        return False
    cid = (interaction.data or {}).get('custom_id') or ''
    if cid.startswith(_GIFT_ACCEPT_RE):
        await _gift_accept_handler(interaction, cid[len(_GIFT_ACCEPT_RE):])
        return True
    if cid.startswith(_GIFT_REJECT_RE):
        await _gift_reject_handler(interaction, cid[len(_GIFT_REJECT_RE):])
        return True
    return False


# ── slash command setup ────────────────────────────────────────────────
def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name='fishing', description='開啟釣魚主面板（選擇釣竿、魚餌、開始釣魚）')
    async def slash_fishing(interaction: discord.Interaction):
        # 確保用戶 record 存在（新玩家為空清單，需自行購買釣竿與魚餌）
        async with _FILE_LOCK:
            data = _load_all()
            _get_or_init_rec(data, str(interaction.user.id))
            save_json(_FILE, data)

        uid = str(interaction.user.id)
        view = FishingMainView(uid, interaction)
        await interaction.response.send_message(
            embed=_main_embed(interaction.user),
            view=view,
            ephemeral=True,
        )
