"""
咕嚕喵碎片錢包共用工具 + 賭博類 View 共用 UI helper。

錢包：賭博類遊戲（slot/dice/roulette/blackjack）都透過這裡存取餘額，
避免每個遊戲重複 load_json/setdefault/save_json_async 樣板。
底層儲存仍是 data/morning_records.json（與 /早安小龍喵 共用）。

UI helper：send_smart() 自動依 interaction 狀態挑 response.send_message
或 followup.send，讓「再來一局」這種「按鈕內再發新訊息」的流程不必各檔重寫。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

import discord

from utils.json_store import load_json, save_json_async


_FILE = os.path.join('data', 'morning_records.json')

_DEFAULT_REC = {
    'balance':    0,
    'total_days': 0,
    'streak':     0,
    'last_day':   None,
}

# 全域 read-modify-write lock：morning_records.json 的所有寫入路徑都必須持有這把鎖
# 才能 load + 修改 + save。避免 coroutine A load → B load → A save → B save 互相覆蓋。
# shop.py / morning.py / stock.py / daily_task.py / _wallet.py 內部都統一用這把鎖。
WALLET_LOCK = asyncio.Lock()


def get_balance(uid: str) -> int:
    return int(load_json(_FILE).get('users', {}).get(uid, {}).get('balance', 0))


async def apply_delta(uid: str, delta: int) -> int:
    """單一原子操作：balance += delta，回傳新餘額。delta 可正可負。"""
    async with WALLET_LOCK:
        data  = load_json(_FILE)
        users = data.setdefault('users', {})
        rec   = users.setdefault(uid, dict(_DEFAULT_REC))
        rec['balance'] = int(rec.get('balance', 0)) + delta
        await save_json_async(_FILE, data)
        return int(rec['balance'])


async def settle_bet(uid: str, cost: int, payout: int) -> int:
    """一把賭局結算：扣下注 + 入獎金，回傳新餘額。"""
    return await apply_delta(uid, payout - cost)


# ── 連勝倍率（gamble_streak）────────────────────────────────────────────
# 每連勝 N 把（N>=2），淨贏額外 ×min(0.05*(N-1), 0.30)。輸歸 0、平手不變。
STREAK_BONUS_PER_STEP = 0.05
STREAK_BONUS_CAP      = 0.30


def get_streak(uid: str) -> int:
    return int(load_json(_FILE).get('users', {}).get(uid, {}).get('gamble_streak', 0))


async def settle_with_streak(
    uid: str, bet: int, payout: int, *, deducted: bool = False,
) -> tuple[int, int, int]:
    """賭局結算 + 連勝倍率。

    bet:    本金（>0）。
    payout: 該回回收金額（含本金）。0=全輸、==bet 平手、>bet 淨贏。
    deducted: False → 餘額 += (payout-bet)（本金尚未扣）；
              True  → 餘額 += payout（本金已先扣）。

    Returns (new_balance, streak, bonus).
    """
    won  = payout > bet
    lost = payout < bet

    async with WALLET_LOCK:
        data  = load_json(_FILE)
        users = data.setdefault('users', {})
        rec   = users.setdefault(uid, dict(_DEFAULT_REC))

        streak = int(rec.get('gamble_streak', 0))
        bonus  = 0
        if won:
            streak += 1
            pct = min(STREAK_BONUS_PER_STEP * (streak - 1), STREAK_BONUS_CAP)
            bonus = int((payout - bet) * pct)
        elif lost:
            streak = 0

        delta = (payout if deducted else (payout - bet)) + bonus
        rec['balance']       = int(rec.get('balance', 0)) + delta
        rec['gamble_streak'] = streak
        await save_json_async(_FILE, data)
        return int(rec['balance']), streak, bonus


def streak_line(streak: int, bonus: int) -> str | None:
    """賭場結算 embed 用的連勝紅利顯示行。streak<2 或 bonus<=0 回 None。"""
    if streak < 2 or bonus <= 0:
        return None
    return f'🔥 連勝 **{streak}** 把，紅利 **+{bonus:,}**'


async def send_smart(interaction: discord.Interaction, **kwargs: Any) -> Any:
    """同一個 interaction：第一次 → response.send_message；
    response 已用過（例如按鈕已 edit_message） → followup.send。
    一律回傳 Message 物件，讓需要後續 edit() 的呼叫者（例如 crash 動畫）
    不必各自 if/else。
    """
    if interaction.response.is_done():
        return await interaction.followup.send(**kwargs)
    await interaction.response.send_message(**kwargs)
    return await interaction.original_response()


async def send_or_edit(interaction: discord.Interaction, *,
                       edit: bool, **kwargs: Any) -> Any:
    """edit=True → 原地修改當前 interaction message；False → 走 send_smart 發新。
    用在「再來一局」這種要回收同一則訊息的流程。"""
    if edit:
        return await interaction.response.edit_message(**kwargs)
    return await send_smart(interaction, **kwargs)
