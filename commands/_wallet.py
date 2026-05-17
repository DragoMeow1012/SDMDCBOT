"""
咕嚕喵碎片錢包共用工具 + 賭博類 View 共用 UI helper。

錢包：賭博類遊戲（slot/dice/roulette/blackjack）都透過這裡存取餘額，
避免每個遊戲重複 load_json/setdefault/save_json_async 樣板。
底層儲存仍是 data/morning_records.json（與 /早安小龍喵 共用）。

UI helper：send_smart() 自動依 interaction 狀態挑 response.send_message
或 followup.send，讓「再來一局」這種「按鈕內再發新訊息」的流程不必各檔重寫。
"""
from __future__ import annotations

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


def get_balance(uid: str) -> int:
    return int(load_json(_FILE).get('users', {}).get(uid, {}).get('balance', 0))


async def apply_delta(uid: str, delta: int) -> int:
    """單一原子操作：balance += delta，回傳新餘額。delta 可正可負。"""
    data  = load_json(_FILE)
    users = data.setdefault('users', {})
    rec   = users.setdefault(uid, dict(_DEFAULT_REC))
    rec['balance'] = int(rec.get('balance', 0)) + delta
    await save_json_async(_FILE, data)
    return int(rec['balance'])


async def settle_bet(uid: str, cost: int, payout: int) -> int:
    """一把賭局結算：扣下注 + 入獎金，回傳新餘額。"""
    return await apply_delta(uid, payout - cost)


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
