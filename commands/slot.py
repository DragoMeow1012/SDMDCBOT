"""
老虎機（slot），由 commands/gamble.py 的 /賭博 派遣呼叫。

設定畫面：下注金額選擇 + 「開轉」按鈕
對局：直接旋轉，結果 embed 後接 EndOfGameView (再來一局 / 調整設定)

規則：
  - 3×3 盤面、5 條中獎線（3 橫 + 2 斜）
  - line_bet = bet // 5
  - 任何一線三格相同符號 → 中獎，賠率 = line_bet × 符號倍率
  - 全盤獎金硬上限 10× bet
"""
from __future__ import annotations

import random
from typing import Any

import discord

from commands._setup import (
    EndOfGameView, insufficient_embed, make_bet_row, tier_label,
)
from commands._wallet import get_balance, send_or_edit, send_smart, settle_bet


GAME_NAME   = '老虎機 3×3'
RULES_TEXT  = (
    '**遊戲規則：**\n'
    '3×3 盤面，5 條中獎線（上排 / 中排 / 下排 + 兩條斜線）。\n'
    '任一線三格相同符號就中獎，符號越稀有倍率越高。\n'
    '下注會均分到 5 條線（line_bet = bet ÷ 5）。'
)
DEFAULT_BET = 500

_SYMBOLS: list[tuple[str, int, int]] = [
    ('🍒',   40,  5),
    ('🍋',   25, 12),
    ('🍇',   15, 20),
    ('🔔',   10, 30),
    ('⭐',    5, 40),
    ('💎',    3, 45),
    ('7️⃣',   2, 50),
]
_EMOJI_LIST    : list[str]      = [s for s, _, _ in _SYMBOLS]
_WEIGHTS       : list[int]      = [w for _, w, _ in _SYMBOLS]
_EMOJI_TO_MULT : dict[str, int] = {s: m for s, _, m in _SYMBOLS}

_LINES: list[tuple[tuple[int, int, int], str]] = [
    ((0, 1, 2), '上排'),
    ((3, 4, 5), '中排'),
    ((6, 7, 8), '下排'),
    ((0, 4, 8), '左斜'),
    ((2, 4, 6), '右斜'),
]


def _spin() -> list[str]:
    return random.choices(_EMOJI_LIST, weights=_WEIGHTS, k=9)


_RESCUE_PROB   = 0.30  # 沒中任何線時 30% 機率退回部分本金（小賠 tier）
_RESCUE_RATIO  = 0.5


def _evaluate(grid: list[str], bet: int) -> tuple[list[tuple[str, str, int, int]], int]:
    line_bet = bet // 5
    hits: list[tuple[str, str, int, int]] = []
    total = 0
    for (a, b, c), name in _LINES:
        if grid[a] == grid[b] == grid[c]:
            mult = _EMOJI_TO_MULT[grid[a]]
            pay  = line_bet * mult
            hits.append((name, grid[a], mult, pay))
            total += pay
    if not hits and random.random() < _RESCUE_PROB:
        total = int(bet * _RESCUE_RATIO)
    return hits, min(total, bet * 10)


def _render_grid(grid: list[str]) -> str:
    return '\n'.join(' | '.join(grid[i*3:i*3+3]) for i in range(3))


def _result_embed(user: discord.abc.User, grid: list[str],
                  hits: list[tuple[str, str, int, int]],
                  bet: int, payout: int, balance: int) -> discord.Embed:
    _, color = tier_label(payout, bet)
    net  = payout - bet
    sign = f'+{net}' if net >= 0 else str(net)
    desc: list[str] = [_render_grid(grid), '']
    if hits:
        desc.append(f'**中獎線**（每線下注 {bet // 5}）：')
        for name, sym, mult, pay in hits:
            desc.append(f'• {name} {sym}×3 → {mult}x = **{pay}**')
        desc.append(f'\n總贏得 **{payout}** ／ 投注 {bet}')
    else:
        desc.append('沒中任何一線... ／(•ㅿ•)＼')
        desc.append(f'投注 {bet}')
    desc.append(f'\n淨損益 **{sign}**　|　餘額 **{balance}** 咕嚕喵碎片')

    embed = discord.Embed(
        title=f'🎰 {GAME_NAME}　{sign}',
        description='\n'.join(desc), color=color,
    )
    embed.set_footer(text=user.display_name)
    return embed


def _setup_embed(user: discord.abc.User, bet: int) -> discord.Embed:
    body = [
        RULES_TEXT,
        '',
        f'目前下注：**{bet}** 咕嚕喵碎片',
        '',
        '按下方 🎰「開轉」開始遊戲。',
    ]
    embed = discord.Embed(
        title=f'🎰 {GAME_NAME} — 開局設定',
        description='\n'.join(body),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text=user.display_name)
    return embed


class SlotSetupView(discord.ui.View):
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
            label='開轉', emoji='🎰',
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
    view = SlotSetupView(str(interaction.user.id), bet=bet)
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
    grid         = _spin()
    hits, payout = _evaluate(grid, bet)
    new_balance  = await settle_bet(uid, bet, payout)
    embed = _result_embed(interaction.user, grid, hits, bet,
                          payout, new_balance)
    end_view = EndOfGameView(uid, bet, {}, run_round, start_setup)
    await send_or_edit(interaction, edit=edit, embed=embed, view=end_view)
