"""
輪盤（歐式單零 0-36）。/賭博 派遣呼叫。

設定畫面（RouletteSetupView）：
  - row 0：下注金額（共用按鈕列）
  - row 1：押注類型 Select（9 項）
  - row 2：細項 — 打 / 列 用 Select，單號用「輸入號碼」按鈕 + Modal
  - row 3：「開盤」按鈕

賠率（return 上限 10×）：
  紅/黑/單/雙/小/大 1:1、打/列 2:1、單號 9:1。
"""
from __future__ import annotations

import random
from typing import Any

import discord

from commands._setup import (
    EndOfGameView, insufficient_embed, make_bet_row, tier_label,
)
from commands._wallet import get_balance, send_or_edit, send_smart, settle_bet


_RESCUE_PROB   = 0.35
_RESCUE_RATIO  = 0.5


def _apply_rescue(payout: int, bet: int, desc: str) -> tuple[int, str]:
    """payout==0（賠光）時 25% 機率拿回 0.4× bet → 小賠 tier。"""
    if payout > 0 or bet <= 0:
        return payout, desc
    if random.random() < _RESCUE_PROB:
        rescued = int(bet * _RESCUE_RATIO)
        return rescued, f'{desc}  (但拿回 {rescued} 小賠)'
    return payout, desc


GAME_NAME   = '輪盤 (Roulette)'
DEFAULT_BET = 500
RULES_TEXT  = (
    '**遊戲規則：**\n'
    '歐式輪盤（0~36，單零）。選押注類型 + 必要時的細項，按「開盤」。\n'
    '**賠率（return 上限 10×）：** 紅/黑/單/雙/小/大 1:1、打 / 列 2:1、單號 9:1。'
)

# 紅色號碼
_RED = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}

# 押注類別
_CAT_RED      = 'red'
_CAT_BLACK    = 'black'
_CAT_ODD      = 'odd'
_CAT_EVEN     = 'even'
_CAT_LOW      = 'low'
_CAT_HIGH     = 'high'
_CAT_DOZEN    = 'dozen'
_CAT_COLUMN   = 'column'
_CAT_STRAIGHT = 'straight'

_CATEGORIES = [
    (_CAT_RED,      '紅 (1:1)'),
    (_CAT_BLACK,    '黑 (1:1)'),
    (_CAT_ODD,      '單 (1:1, 0 不算)'),
    (_CAT_EVEN,     '雙 (1:1, 0 不算)'),
    (_CAT_LOW,      '小 1-18 (1:1)'),
    (_CAT_HIGH,     '大 19-36 (1:1)'),
    (_CAT_DOZEN,    '打 12 個一段 (2:1)'),
    (_CAT_COLUMN,   '列 (2:1)'),
    (_CAT_STRAIGHT, '單號 0-36 (9:1)'),
]

_DOZEN_LABELS  = {1: '第 1 打 (1-12)', 2: '第 2 打 (13-24)', 3: '第 3 打 (25-36)'}
_COLUMN_LABELS = {
    1: '第 1 列 (1,4,7,...,34)',
    2: '第 2 列 (2,5,8,...,35)',
    3: '第 3 列 (3,6,9,...,36)',
}

_NEEDS_DOZEN_COL = {_CAT_DOZEN, _CAT_COLUMN}
_NEEDS_STRAIGHT  = {_CAT_STRAIGHT}


def _spin() -> int:
    return random.randint(0, 36)


def _number_color(n: int) -> str:
    if n == 0:
        return '🟢'
    return '🔴' if n in _RED else '⚫'


def _dozen_of(n: int) -> int:
    if 1 <= n <= 12:  return 1
    if 13 <= n <= 24: return 2
    if 25 <= n <= 36: return 3
    return 0


def _column_of(n: int) -> int:
    if n == 0:
        return 0
    return 3 if n % 3 == 0 else n % 3


def _compute_payout(category: str, detail: int | None, n: int,
                    bet: int) -> tuple[int, str]:
    if category == _CAT_RED:
        if n in _RED:
            return bet * 2, '✓ 紅'
        return 0, '✗ 不是紅' + ('（0 綠）' if n == 0 else '')
    if category == _CAT_BLACK:
        if n != 0 and n not in _RED:
            return bet * 2, '✓ 黑'
        return 0, '✗ 不是黑' + ('（0 綠）' if n == 0 else '')
    if category == _CAT_ODD:
        if n != 0 and n % 2 == 1:
            return bet * 2, '✓ 單'
        return 0, '✗ 不是單'
    if category == _CAT_EVEN:
        if n != 0 and n % 2 == 0:
            return bet * 2, '✓ 雙'
        return 0, '✗ 不是雙'
    if category == _CAT_LOW:
        if 1 <= n <= 18:
            return bet * 2, '✓ 1-18'
        return 0, '✗ 不在 1-18'
    if category == _CAT_HIGH:
        if 19 <= n <= 36:
            return bet * 2, '✓ 19-36'
        return 0, '✗ 不在 19-36'
    if category == _CAT_DOZEN:
        if _dozen_of(n) == detail:
            return bet * 3, f'✓ 第 {detail} 打'
        return 0, f'✗ 點數落在第 {_dozen_of(n) or 0} 打'
    if category == _CAT_COLUMN:
        if _column_of(n) == detail:
            return bet * 3, f'✓ 第 {detail} 列'
        return 0, f'✗ 點數落在第 {_column_of(n) or 0} 列'
    if category == _CAT_STRAIGHT:
        if detail is not None and n == detail:
            return bet * 10, f'✓ 中單號 {n}'
        return 0, f'✗ 號碼是 {n}，押的是 {detail}'
    return 0, '未知押注'


def _bet_label(category: str | None, detail: int | None) -> str:
    if category is None:
        return '—'
    name = {
        _CAT_RED: '紅', _CAT_BLACK: '黑', _CAT_ODD: '單', _CAT_EVEN: '雙',
        _CAT_LOW: '小 1-18', _CAT_HIGH: '大 19-36',
    }.get(category)
    if name:
        return name
    if category == _CAT_DOZEN:
        return _DOZEN_LABELS.get(detail or 0, '打 ?')
    if category == _CAT_COLUMN:
        return _COLUMN_LABELS.get(detail or 0, '列 ?')
    if category == _CAT_STRAIGHT:
        return f'單號 {detail}' if detail is not None else '單號 ?'
    return category


# ── Embeds ──────────────────────────────────────────────────────────────
def _setup_embed(user: discord.abc.User, bet: int,
                 category: str | None, detail: int | None) -> discord.Embed:
    body = [RULES_TEXT, '', f'目前下注：**{bet}** 咕嚕喵碎片']
    if category is None:
        body.append('押注類型：_(請選)_')
    else:
        cat_label = next(
            (label for v, label in _CATEGORIES if v == category), category,
        )
        body.append(f'押注類型：**{cat_label}**')
        if category in (_NEEDS_DOZEN_COL | _NEEDS_STRAIGHT):
            body.append(
                f'細項：**{_bet_label(category, detail)}**' if detail is not None
                else '細項：_(請選)_'
            )
    body += ['', '選好後按下方 🎡「開盤」']
    embed = discord.Embed(
        title=f'🎡 {GAME_NAME} — 開局設定',
        description='\n'.join(body), color=discord.Color.blurple(),
    )
    embed.set_footer(text=user.display_name)
    return embed


def _result_embed(user: discord.abc.User, bet: int,
                  category: str, detail: int | None,
                  n: int, payout: int, desc: str,
                  balance: int) -> discord.Embed:
    _, color = tier_label(payout, bet)
    net  = payout - bet
    sign = f'+{net}' if net >= 0 else str(net)
    body = [
        f'開出 {_number_color(n)} **{n}**', '',
        f'押注：**{_bet_label(category, detail)}**（{bet} 咕嚕喵碎片）',
        desc, '',
        f'淨損益 **{sign}**　|　餘額 **{balance}** 咕嚕喵碎片',
    ]
    embed = discord.Embed(
        title=f'🎡 {GAME_NAME}　{sign}',
        description='\n'.join(body), color=color,
    )
    embed.set_footer(text=user.display_name)
    return embed


# ── Modal for straight bet ──────────────────────────────────────────────
class StraightModal(discord.ui.Modal, title='選單號 (0-36)'):
    number = discord.ui.TextInput(
        label='號碼', placeholder='輸入 0 ~ 36 的整數',
        min_length=1, max_length=2,
    )

    def __init__(self, parent: 'RouletteSetupView'):
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.number.value.strip()
        if not raw.isdigit():
            await interaction.response.send_message(
                '請輸入整數', ephemeral=True,
            )
            return
        n = int(raw)
        if n < 0 or n > 36:
            await interaction.response.send_message(
                '範圍 0 ~ 36', ephemeral=True,
            )
            return
        self.parent.detail = n
        self.parent._rebuild()
        embed = _setup_embed(
            interaction.user, self.parent.bet,
            self.parent.category, self.parent.detail,
        )
        await interaction.response.edit_message(embed=embed, view=self.parent)


# ── Setup view ──────────────────────────────────────────────────────────
class RouletteSetupView(discord.ui.View):
    def __init__(self, uid: str, bet: int = DEFAULT_BET,
                 category: str | None = None, detail: int | None = None):
        super().__init__(timeout=300)
        self.uid      = uid
        self.bet      = bet
        self.category = category
        self.detail   = detail
        self._rebuild()

    def _check_owner(self, interaction: discord.Interaction) -> bool:
        return str(interaction.user.id) == self.uid

    async def _deny(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            '這不是你的賭局', ephemeral=True,
        )

    def _rebuild(self) -> None:
        self.clear_items()
        for btn in make_bet_row(self, self._refresh, row=0):
            self.add_item(btn)
        self.add_item(self._cat_select())
        if self.category in _NEEDS_DOZEN_COL:
            self.add_item(self._dozen_col_select())
        if self.category in _NEEDS_STRAIGHT:
            self.add_item(self._straight_button())
        self.add_item(self._go_button())

    def _cat_select(self) -> discord.ui.Select:
        options = [
            discord.SelectOption(
                label=label, value=value, default=(value == self.category),
            )
            for value, label in _CATEGORIES
        ]
        sel = discord.ui.Select(placeholder='選押注類型', options=options, row=1)

        async def _cb(interaction: discord.Interaction) -> None:
            if not self._check_owner(interaction):
                await self._deny(interaction); return
            self.category = sel.values[0]
            self.detail   = None
            await self._refresh(interaction)

        sel.callback = _cb
        return sel

    def _dozen_col_select(self) -> discord.ui.Select:
        labels = _DOZEN_LABELS if self.category == _CAT_DOZEN else _COLUMN_LABELS
        options = [
            discord.SelectOption(
                label=labels[i], value=str(i), default=(self.detail == i),
            )
            for i in (1, 2, 3)
        ]
        ph = '選打 (2:1)' if self.category == _CAT_DOZEN else '選列 (2:1)'
        sel = discord.ui.Select(placeholder=ph, options=options, row=2)

        async def _cb(interaction: discord.Interaction) -> None:
            if not self._check_owner(interaction):
                await self._deny(interaction); return
            self.detail = int(sel.values[0])
            await self._refresh(interaction)

        sel.callback = _cb
        return sel

    def _straight_button(self) -> discord.ui.Button:
        label = (f'已選 {self.detail}（換一個）' if self.detail is not None
                 else '輸入號碼')
        btn = discord.ui.Button(
            label=label, emoji='🔢',
            style=discord.ButtonStyle.secondary, row=2,
        )

        async def _cb(interaction: discord.Interaction) -> None:
            if not self._check_owner(interaction):
                await self._deny(interaction); return
            await interaction.response.send_modal(StraightModal(self))

        btn.callback = _cb
        return btn

    def _go_button(self) -> discord.ui.Button:
        ready = self.category is not None and (
            self.category not in (_NEEDS_DOZEN_COL | _NEEDS_STRAIGHT)
            or self.detail is not None
        )
        btn = discord.ui.Button(
            label='開盤', emoji='🎡',
            style=discord.ButtonStyle.primary, disabled=not ready, row=3,
        )
        btn.callback = self._start_cb
        return btn

    async def _refresh(self, interaction: discord.Interaction) -> None:
        self._rebuild()
        embed = _setup_embed(
            interaction.user, self.bet, self.category, self.detail,
        )
        await interaction.response.edit_message(embed=embed, view=self)

    async def _start_cb(self, interaction: discord.Interaction) -> None:
        if not self._check_owner(interaction):
            await self._deny(interaction); return
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
        await run_round(interaction, self.bet, {
            'category': self.category, 'detail': self.detail,
        })


# ── 對外入口 ─────────────────────────────────────────────────────────────
async def start_setup(interaction: discord.Interaction,
                      bet: int = DEFAULT_BET,
                      options: dict[str, Any] | None = None) -> None:
    options = options or {}
    bet     = max(100, int(bet))
    view    = RouletteSetupView(
        str(interaction.user.id), bet=bet,
        category=options.get('category'), detail=options.get('detail'),
    )
    embed = _setup_embed(
        interaction.user, bet,
        options.get('category'), options.get('detail'),
    )
    await send_smart(interaction, embed=embed, view=view, ephemeral=True)


async def run_round(interaction: discord.Interaction, bet: int,
                    options: dict[str, Any], *, edit: bool = False) -> None:
    uid      = str(interaction.user.id)
    category = options.get('category')
    detail   = options.get('detail')
    if category is None:
        await send_or_edit(
            interaction, edit=edit,
            embed=discord.Embed(
                title='⚠️ 未選押注類型',
                description='請使用「調整設定」重新選擇押注。',
                color=discord.Color.red(),
            ),
            **({'view': None} if edit else {}),
        )
        return
    balance = get_balance(uid)
    if balance < bet:
        await send_or_edit(
            interaction, edit=edit,
            embed=insufficient_embed(bet, balance),
            **({'view': None} if edit else {}),
        )
        return

    n = _spin()
    payout, desc = _compute_payout(category, detail, n, bet)
    payout, desc = _apply_rescue(payout, bet, desc)
    new_balance  = await settle_bet(uid, bet, payout)
    embed = _result_embed(
        interaction.user, bet, category, detail,
        n, payout, desc, new_balance,
    )
    end_view = EndOfGameView(
        uid, bet, {'category': category, 'detail': detail},
        run_round, start_setup,
    )
    await send_or_edit(interaction, edit=edit, embed=embed, view=end_view)
