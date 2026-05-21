"""
賭博遊戲共用的 Setup / End 元件。

- BET_PRESETS：下注金額預設按鈕（100/500/1000/5000 + Custom）
- CustomBetModal：Custom 按下後跳出的 Modal
- make_bet_row()：給 SetupView 用的下注金額按鈕列（會自動把當前選中的標 ★）
- EndOfGameView：每局結束後的「再來一局 / 調整設定」共用 view
- insufficient_embed()：「你沒有足夠的咕嚕喵碎片」標準錯誤 embed
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

import discord

from commands._wallet import get_balance


BET_PRESETS = [1000, 10_000, 100_000, 1_000_000]
MIN_BET     = 100
MAX_BET     = 100_000_000


def insufficient_embed(need: int, have: int) -> discord.Embed:
    return discord.Embed(
        title='⚠️ 你沒有足夠的咕嚕喵碎片',
        description=f'本局需要 **{need}**，目前餘額 **{have}**',
        color=discord.Color.red(),
    )


# ── 結算 5 段標籤 ────────────────────────────────────────────────────────
def tier_label(payout: int, bet: int) -> tuple[str, discord.Color]:
    """根據 payout/bet 比例分 5 段：賠光 / 小賠 / 沒贏沒輸 / 小贏 / 大贏。

    回傳 (帶 emoji 的標籤, embed 顏色)。所有遊戲的結算 embed 統一用這個。
    """
    if bet <= 0:
        return '🪙 沒贏沒輸', discord.Color.gold()
    ratio = payout / bet
    if payout == 0:
        return '💀 賠光', discord.Color.dark_grey()
    if ratio < 1:
        return '💧 小賠', discord.Color.dark_red()
    if ratio == 1:
        return '🪙 沒贏沒輸', discord.Color.gold()
    if ratio < 2:
        return '🪙 小贏', discord.Color.green()
    return '💰 大贏', discord.Color.brand_green()


# ── Custom 金額 Modal ────────────────────────────────────────────────────
class CustomBetModal(discord.ui.Modal, title='輸入下注金額'):
    amount = discord.ui.TextInput(
        label='金額（咕嚕喵碎片）',
        placeholder=f'整數，最小 {MIN_BET}',
        min_length=1, max_length=10,
    )

    def __init__(self, parent_view: 'SetupViewLike',
                 refresh_fn: Callable[[discord.Interaction], Awaitable[None]]):
        super().__init__()
        self.parent_view = parent_view
        self.refresh_fn  = refresh_fn

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.amount.value.strip()
        if not raw.isdigit():
            await interaction.response.send_message(
                '請輸入整數', ephemeral=True,
            )
            return
        n = int(raw)
        if n < MIN_BET:
            await interaction.response.send_message(
                f'最小下注 {MIN_BET}', ephemeral=True,
            )
            return
        if n > MAX_BET:
            await interaction.response.send_message(
                f'最大下注 {MAX_BET}', ephemeral=True,
            )
            return
        self.parent_view.bet = n
        await self.refresh_fn(interaction)


# ── 下注金額按鈕列 ───────────────────────────────────────────────────────
class SetupViewLike:
    """SetupView 需要實作的最低介面：uid 屬性、bet 屬性。type 註記用。"""
    uid: str
    bet: int


def make_bet_row(parent_view: SetupViewLike,
                 refresh_fn: Callable[[discord.Interaction], Awaitable[None]],
                 row: int = 0) -> list[discord.ui.Button]:
    """產生 5 顆下注金額按鈕。被選中的會變綠色 + ★。Custom 開 Modal。"""
    btns: list[discord.ui.Button] = []
    for amount in BET_PRESETS:
        selected = (parent_view.bet == amount)
        btn = discord.ui.Button(
            label=f'{amount}{" ★" if selected else ""}',
            style=discord.ButtonStyle.success if selected else discord.ButtonStyle.secondary,
            row=row,
        )

        def _make_cb(a: int):
            async def _cb(interaction: discord.Interaction) -> None:
                if str(interaction.user.id) != parent_view.uid:
                    await interaction.response.send_message(
                        '這不是你的賭局', ephemeral=True,
                    )
                    return
                parent_view.bet = a
                await refresh_fn(interaction)
            return _cb

        btn.callback = _make_cb(amount)
        btns.append(btn)

    is_custom    = parent_view.bet not in BET_PRESETS
    custom_label = f'自訂: {parent_view.bet} ★' if is_custom else '自訂'
    custom_btn = discord.ui.Button(
        label=custom_label,
        style=discord.ButtonStyle.success if is_custom else discord.ButtonStyle.secondary,
        row=row,
    )

    async def _custom_cb(interaction: discord.Interaction) -> None:
        if str(interaction.user.id) != parent_view.uid:
            await interaction.response.send_message(
                '這不是你的賭局', ephemeral=True,
            )
            return
        await interaction.response.send_modal(CustomBetModal(parent_view, refresh_fn))

    custom_btn.callback = _custom_cb
    btns.append(custom_btn)
    return btns


# ── 每局結束後的通用 view ────────────────────────────────────────────────
StartFn   = Callable[[discord.Interaction, int, dict[str, Any]], Awaitable[None]]
RoundFn   = Callable[[discord.Interaction, int, dict[str, Any]], Awaitable[None]]


class EndOfGameView(discord.ui.View):
    """所有遊戲結束都掛這個：再來一局（同設定）/ 調整設定（回 setup）。"""

    def __init__(self, uid: str, bet: int, options: dict[str, Any],
                 run_round: RoundFn, start_setup: StartFn):
        super().__init__(timeout=300)
        self.uid          = uid
        self.bet          = bet
        self.options      = options
        self._run_round   = run_round
        self._start_setup = start_setup

    def _check_owner(self, interaction: discord.Interaction) -> bool:
        return str(interaction.user.id) == self.uid

    def _disable_all(self) -> None:
        for c in self.children:
            if isinstance(c, discord.ui.Button):
                c.disabled = True

    @discord.ui.button(label='再來一局', style=discord.ButtonStyle.success,
                       emoji='🔄', row=0)
    async def again(self, interaction: discord.Interaction,
                    button: discord.ui.Button) -> None:
        if not self._check_owner(interaction):
            await interaction.response.send_message(
                '這不是你的賭局', ephemeral=True,
            )
            return
        balance = get_balance(self.uid)
        if balance < self.bet:
            self._disable_all()
            await interaction.response.edit_message(
                embed=insufficient_embed(self.bet, balance), view=self,
            )
            self.stop()
            return
        # 原地修改同一則訊息為新一局
        self.stop()
        await self._run_round(interaction, self.bet, self.options, edit=True)

    @discord.ui.button(label='調整設定', style=discord.ButtonStyle.secondary,
                       emoji='⚙️', row=0)
    async def adjust(self, interaction: discord.Interaction,
                     button: discord.ui.Button) -> None:
        if not self._check_owner(interaction):
            await interaction.response.send_message(
                '這不是你的賭局', ephemeral=True,
            )
            return
        self._disable_all()
        await interaction.response.edit_message(view=self)
        self.stop()
        await self._start_setup(interaction, self.bet, self.options)
