"""
爆點 (Crash)。/賭博 派遣呼叫。

設定：下注金額 + 「起飛」按鈕
對局：手動加注 — 玩家按「🚀 加注」推升倍率，隨時可按「💰 提現」鎖定。
      每按一次加注：current ×= 1.12；若超過預定爆炸點就 BOOM。
結算：EndOfGameView (再來一局 / 調整設定)

爆炸點公式（HE=20%）：
  crash = clamp(0.8 / (1 - U), 1.05, 10)
  10% 機率直接 1.05x；其餘任何提現策略期望 RTP = 80%。

對外入口：start_setup(interaction, bet=500, options=None)
"""
from __future__ import annotations

import asyncio
import random
from typing import Any

import discord

from commands._setup import (
    EndOfGameView, insufficient_embed, make_bet_row, tier_label,
)
from commands._wallet import apply_delta, get_balance, send_or_edit, send_smart


GAME_NAME   = '爆點 (Crash)'
DEFAULT_BET = 500
RULES_TEXT  = (
    '**遊戲規則：**\n'
    '🚀 火箭從 1.00x 起飛。\n'
    '按「🚀 加注」每按一次倍率 × 1.12，隨時可能爆炸。\n'
    '按「💰 提現」鎖定當前倍率、贏 bet × 倍率。\n'
    '若按到爆炸點，本注歸零。\n'
    '上限 10×；前 10% 機率會在最低點就爆，請小心。'
)
_GROWTH    = 1.12
_CAP       = 10.0
_MIN_CRASH = 1.05
_INSTANT_P = 0.10


def _generate_crash() -> float:
    u = random.random()
    if u < _INSTANT_P:
        return _MIN_CRASH
    # HE 從 0.20 砍到 0.15，RTP 80% → 85%
    raw = 0.85 / (1.0 - u)
    return round(max(_MIN_CRASH, min(_CAP, raw)), 2)


class CrashGame:
    def __init__(self, bet: int):
        self.bet       = bet
        self.crash_at  = _generate_crash()
        self.current   = 1.00
        self.cashed_at : float | None = None
        self.crashed   = False
        self._lock     = asyncio.Lock()

    async def tick(self) -> bool:
        async with self._lock:
            if self.cashed_at is not None or self.crashed:
                return False
            new = round(self.current * _GROWTH, 2)
            if new >= self.crash_at:
                self.current = self.crash_at
                self.crashed = True
                return False
            self.current = min(_CAP, new)
            return True

    async def cash_out(self) -> float | None:
        async with self._lock:
            if self.cashed_at is not None or self.crashed:
                return None
            self.cashed_at = self.current
            return self.cashed_at


def _rocket_visual(m: float) -> str:
    trail = max(1, min(6, int((m - 1) * 1.2) + 1))
    return '🚀\n' + '\n'.join(['💨'] * trail)


def _running_embed(user: discord.abc.User, bet: int,
                   current: float) -> discord.Embed:
    potential = int(bet * current)
    net       = potential - bet
    desc = [
        _rocket_visual(current), '',
        f'# **{current:.2f}x**', '',
        f'投注 {bet}　提領可得 **{potential}** (+{net})', '',
        '按「🚀 加注」推升倍率（× 1.12，可能爆炸） | 按「💰 提現」鎖定',
    ]
    embed = discord.Embed(
        title=f'🚀 {GAME_NAME} — 飛行中',
        description='\n'.join(desc), color=discord.Color.blurple(),
    )
    embed.set_footer(text=user.display_name)
    return embed


def _cashed_embed(user: discord.abc.User, bet: int, mult: float,
                  balance: int) -> discord.Embed:
    payout   = int(bet * mult)
    net      = payout - bet
    sign     = f'+{net}' if net >= 0 else str(net)
    _, color = tier_label(payout, bet)
    desc = [
        '🚀💨💨💨', '',
        f'# 💰 提現 @ **{mult:.2f}x**', '',
        f'投注 {bet}　取回 **{payout}**　淨 **{sign}**', '',
        f'餘額：**{balance}** 咕嚕喵碎片',
    ]
    embed = discord.Embed(
        title=f'🚀 {GAME_NAME}　{sign}',
        description='\n'.join(desc), color=color,
    )
    embed.set_footer(text=user.display_name)
    return embed


def _crashed_embed(user: discord.abc.User, bet: int, crash_at: float,
                   balance: int) -> discord.Embed:
    _, color = tier_label(0, bet)
    desc = [
        '💥💥💥', '🚀➰', '',
        f'# 💥 BOOM @ **{crash_at:.2f}x**', '',
        f'投注 **{bet}** 全部歸零... ／(•ㅿ•)＼', '',
        f'餘額：**{balance}** 咕嚕喵碎片',
    ]
    embed = discord.Embed(
        title=f'💥 {GAME_NAME}　-{bet}',
        description='\n'.join(desc), color=color,
    )
    embed.set_footer(text=user.display_name)
    return embed


class CrashGameView(discord.ui.View):
    def __init__(self, uid: str, bet: int, game: CrashGame):
        super().__init__(timeout=300)
        self.uid  = uid
        self.bet  = bet
        self.game = game

    @discord.ui.button(label='加注', style=discord.ButtonStyle.primary,
                       emoji='🚀', row=0)
    async def step_up(self, interaction: discord.Interaction,
                      button: discord.ui.Button) -> None:
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message(
                '這不是你的火箭', ephemeral=True,
            )
            return
        still_alive = await self.game.tick()
        if not still_alive:
            balance = get_balance(self.uid)
            embed   = _crashed_embed(
                interaction.user, self.bet, self.game.current, balance,
            )
            end_view = EndOfGameView(
                self.uid, self.bet, {}, run_round, start_setup,
            )
            await interaction.response.edit_message(embed=embed, view=end_view)
            self.stop()
            return
        embed = _running_embed(interaction.user, self.bet, self.game.current)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label='提現', style=discord.ButtonStyle.success,
                       emoji='💰', row=0)
    async def cash_out(self, interaction: discord.Interaction,
                       button: discord.ui.Button) -> None:
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message(
                '這不是你的火箭', ephemeral=True,
            )
            return
        m = await self.game.cash_out()
        if m is None:
            await interaction.response.send_message(
                '已經結算過了', ephemeral=True,
            )
            return
        payout = int(self.bet * m)
        if payout > 0:
            await apply_delta(self.uid, payout)
        balance  = get_balance(self.uid)
        embed    = _cashed_embed(interaction.user, self.bet, m, balance)
        end_view = EndOfGameView(
            self.uid, self.bet, {}, run_round, start_setup,
        )
        await interaction.response.edit_message(embed=embed, view=end_view)
        self.stop()


# ── Setup view ──────────────────────────────────────────────────────────
def _setup_embed(user: discord.abc.User, bet: int) -> discord.Embed:
    body = [
        RULES_TEXT, '',
        f'目前下注：**{bet}** 咕嚕喵碎片', '',
        '按下方 🚀「起飛」開始遊戲。',
    ]
    embed = discord.Embed(
        title=f'🚀 {GAME_NAME} — 開局設定',
        description='\n'.join(body), color=discord.Color.blurple(),
    )
    embed.set_footer(text=user.display_name)
    return embed


class CrashSetupView(discord.ui.View):
    def __init__(self, uid: str, bet: int = DEFAULT_BET):
        super().__init__(timeout=300)
        self.uid = uid
        self.bet = bet
        self._build()

    def _build(self) -> None:
        self.clear_items()
        for btn in make_bet_row(self, self._refresh, row=0):
            self.add_item(btn)
        start = discord.ui.Button(
            label='起飛', emoji='🚀',
            style=discord.ButtonStyle.primary, row=1,
        )
        start.callback = self._start_cb
        self.add_item(start)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        self._build()
        await interaction.response.edit_message(
            embed=_setup_embed(interaction.user, self.bet), view=self,
        )

    async def _start_cb(self, interaction: discord.Interaction) -> None:
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message(
                '這不是你的賭局', ephemeral=True,
            )
            return
        balance = get_balance(self.uid)
        if balance < self.bet:
            await interaction.response.edit_message(
                embed=insufficient_embed(self.bet, balance), view=None,
            )
            self.stop()
            return
        await interaction.response.defer()
        await interaction.delete_original_response()
        self.stop()
        await run_round(interaction, self.bet, {})


# ── 對外入口 ─────────────────────────────────────────────────────────────
async def start_setup(interaction: discord.Interaction,
                      bet: int = DEFAULT_BET,
                      options: dict[str, Any] | None = None) -> None:
    bet  = max(100, int(bet))
    view = CrashSetupView(str(interaction.user.id), bet=bet)
    await send_smart(interaction, embed=_setup_embed(interaction.user, bet),
                     view=view, ephemeral=True)


async def run_round(interaction: discord.Interaction, bet: int,
                    options: dict[str, Any], *, edit: bool = False) -> None:
    uid     = str(interaction.user.id)
    balance = get_balance(uid)
    if balance < bet:
        await send_or_edit(
            interaction, edit=edit,
            embed=insufficient_embed(bet, balance),
            **({'view': None} if edit else {}),
        )
        return
    await apply_delta(uid, -bet)
    game  = CrashGame(bet)
    view  = CrashGameView(uid, bet, game)
    embed = _running_embed(interaction.user, bet, game.current)
    await send_or_edit(interaction, edit=edit, embed=embed, view=view)
