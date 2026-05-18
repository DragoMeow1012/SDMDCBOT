"""
骰子機 (Sic Bo)。/賭博 派遣呼叫。

設定畫面 (DiceSetupView)：
  - row 0：下注金額（共用按鈕列）
  - row 1：押注類型 Select (8 項)
  - row 2：細項 Select（若選了圍骰/點數/數字才出現）
  - row 3：「開骰」按鈕

對局：直接擲骰、結算
結算：EndOfGameView（再來一局 = 同 bet+類型+細項，調整設定 = 回 setup）

賠率（return 上限 10× bet）：
  - 大 / 小 / 單 / 雙       1:1
  - 任意圍骰                5:1
  - 特定圍骰 N              9:1
  - 特定點數 S              4/17→9, 5/16→8, 6/15→7, 7/14→6, 8/13→4, 9~12→3
  - 單一數字 N              1:1 per 次出現
"""
from __future__ import annotations

import random
from typing import Any

import discord

from commands._setup import (
    EndOfGameView, insufficient_embed, make_bet_row, tier_label,
)
from commands._wallet import get_balance, send_or_edit, send_smart, settle_bet


_RESCUE_PROB   = 0.35   # 失敗時 35% 機率拿回部分本金（平衡：稍微提高勝率）
_RESCUE_RATIO  = 0.5    # 拿回 0.5× bet → 屬於「小賠」tier


GAME_NAME   = '骰子 (Sic Bo)'
DEFAULT_BET = 500
RULES_TEXT  = (
    '**遊戲規則：**\n'
    '3 顆骰子一次擲出。選押注類型 + 必要時的細項，按「開骰」即結算。\n'
    '**賠率（return 上限 10×）：** 大/小/單/雙 1:1、任意圍骰 5:1、'
    '特定圍骰 9:1、特定點數 3~9:1、單一數字按出現次數 1~3:1。'
)

# 押注類別
_CAT_BIG          = '大'
_CAT_SMALL        = '小'
_CAT_ODD          = '單'
_CAT_EVEN         = '雙'
_CAT_ANY_TRIPLE   = '任意圍骰'
_CAT_SPEC_TRIPLE  = '特定圍骰'
_CAT_TOTAL        = '特定點數'
_CAT_NUMBER       = '單一數字'
_CAT_PAIR         = '特定雙骰'

_CATEGORIES = [
    (_CAT_BIG,         '大 (11-17, 1:1)'),
    (_CAT_SMALL,       '小 (4-10, 1:1)'),
    (_CAT_ODD,         '單 (奇數總和, 1:1)'),
    (_CAT_EVEN,        '雙 (偶數總和, 1:1)'),
    (_CAT_ANY_TRIPLE,  '任意圍骰 (5:1)'),
    (_CAT_SPEC_TRIPLE, '特定圍骰 (9:1)'),
    (_CAT_TOTAL,       '特定點數 (賠率視點數)'),
    (_CAT_NUMBER,      '單一數字 (1:1 / 次)'),
    (_CAT_PAIR,        '特定雙骰 (≥2 次 9:1)'),
]

_NEEDS_DETAIL = {_CAT_SPEC_TRIPLE, _CAT_TOTAL, _CAT_NUMBER, _CAT_PAIR}

_SUM_PAYOUT: dict[int, int] = {
    4: 9, 5: 8, 6: 7, 7: 6,  8: 4,  9: 3, 10: 3,
    11: 3, 12: 3, 13: 4, 14: 6, 15: 7, 16: 8, 17: 9,
}

_DICE_EMOJI = {1: '⚀', 2: '⚁', 3: '⚂', 4: '⚃', 5: '⚄', 6: '⚅'}


def _roll() -> tuple[int, int, int]:
    return random.randint(1, 6), random.randint(1, 6), random.randint(1, 6)


def _apply_rescue(payout: int, bet: int, desc: str) -> tuple[int, str]:
    """payout==0（賠光）時 25% 機率拿回 0.4× bet → 小賠 tier。"""
    if payout > 0 or bet <= 0:
        return payout, desc
    if random.random() < _RESCUE_PROB:
        rescued = int(bet * _RESCUE_RATIO)
        return rescued, f'{desc}  (但拿回 {rescued} 小賠)'
    return payout, desc


def _compute_payout(category: str, detail: int | None,
                    dice: tuple[int, int, int],
                    bet: int) -> tuple[int, str]:
    a, b, c   = dice
    total     = a + b + c
    is_triple = (a == b == c)

    if category == _CAT_BIG:
        if 11 <= total <= 17 and not is_triple:
            return bet * 2, '✓ 是大'
        return 0, '✗ 不是大（或圍骰）'
    if category == _CAT_SMALL:
        if 4 <= total <= 10 and not is_triple:
            return bet * 2, '✓ 是小'
        return 0, '✗ 不是小（或圍骰）'
    if category == _CAT_ODD:
        if total % 2 == 1 and not is_triple:
            return bet * 2, '✓ 是單'
        return 0, '✗ 不是單（或圍骰）'
    if category == _CAT_EVEN:
        if total % 2 == 0 and not is_triple:
            return bet * 2, '✓ 是雙'
        return 0, '✗ 不是雙（或圍骰）'
    if category == _CAT_ANY_TRIPLE:
        if is_triple:
            return bet * 6, f'✓ 圍骰 {a}-{a}-{a}'
        return 0, '✗ 沒出圍骰'
    if category == _CAT_SPEC_TRIPLE:
        if is_triple and a == detail:
            return bet * 10, f'✓ 圍骰 {a}-{a}-{a}'
        if is_triple:
            return 0, f'✗ 圍骰是 {a}-{a}-{a}，不是 {detail}-{detail}-{detail}'
        return 0, f'✗ 沒出圍骰 {detail}-{detail}-{detail}'
    if category == _CAT_TOTAL:
        if detail is not None and total == detail and not is_triple:
            mult = _SUM_PAYOUT.get(detail, 0)
            return bet * (mult + 1), f'✓ 點數 = {detail}'
        return 0, f'✗ 點數 = {total}，不是 {detail}'
    if category == _CAT_NUMBER:
        cnt = sum(1 for d in dice if d == detail)
        if cnt > 0:
            return bet * (cnt + 1), f'✓ {detail} 出現 {cnt} 次'
        return 0, f'✗ {detail} 沒出現'
    if category == _CAT_PAIR:
        cnt = sum(1 for d in dice if d == detail)
        if cnt >= 2:
            return bet * 10, f'✓ {detail} 出現 {cnt} 次（達雙骰）'
        return 0, f'✗ {detail} 出現 {cnt} 次（要 ≥ 2 次）'
    return 0, '未知押注'


def _dice_str(dice: tuple[int, int, int]) -> str:
    a, b, c = dice
    return f'{_DICE_EMOJI[a]} {_DICE_EMOJI[b]} {_DICE_EMOJI[c]}'


def _bet_desc(category: str, detail: int | None) -> str:
    if category in _NEEDS_DETAIL:
        if category == _CAT_SPEC_TRIPLE:
            return f'圍骰 {detail}-{detail}-{detail}'
        if category == _CAT_TOTAL:
            return f'點數 {detail}'
        if category == _CAT_NUMBER:
            return f'單一數字 {detail}'
        if category == _CAT_PAIR:
            return f'特定雙骰 {detail}-{detail}'
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
        if category in _NEEDS_DETAIL:
            body.append(
                f'細項：**{_bet_desc(category, detail)}**' if detail is not None
                else '細項：_(請選)_'
            )
    body += ['', '選好後按下方 🎲「開骰」']
    embed = discord.Embed(
        title=f'🎲 {GAME_NAME} — 開局設定',
        description='\n'.join(body), color=discord.Color.blurple(),
    )
    embed.set_footer(text=user.display_name)
    return embed


def _result_embed(user: discord.abc.User, bet: int,
                  category: str, detail: int | None,
                  dice: tuple[int, int, int], payout: int,
                  result_desc: str, balance: int) -> discord.Embed:
    _, color = tier_label(payout, bet)
    net  = payout - bet
    sign = f'+{net}' if net >= 0 else str(net)
    total = sum(dice)
    desc = [
        f'{_dice_str(dice)}　總和 **{total}**', '',
        f'押注：**{_bet_desc(category, detail)}**（{bet} 咕嚕喵碎片）',
        result_desc, '',
        f'淨損益 **{sign}**　|　餘額 **{balance}** 咕嚕喵碎片',
    ]
    embed = discord.Embed(
        title=f'🎲 {GAME_NAME}　{sign}',
        description='\n'.join(desc), color=color,
    )
    embed.set_footer(text=user.display_name)
    return embed


# ── Setup view ──────────────────────────────────────────────────────────
class DiceSetupView(discord.ui.View):
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
        if self.category in _NEEDS_DETAIL:
            self.add_item(self._detail_select())
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

    def _detail_select(self) -> discord.ui.Select:
        if self.category == _CAT_SPEC_TRIPLE:
            options = [
                discord.SelectOption(label=f'圍骰 {i}-{i}-{i}', value=str(i))
                for i in range(1, 7)
            ]
            ph = '選圍骰數字 (9:1)'
        elif self.category == _CAT_TOTAL:
            options = [
                discord.SelectOption(
                    label=f'點數 {s}（{_SUM_PAYOUT[s]}:1）', value=str(s),
                )
                for s in sorted(_SUM_PAYOUT)
            ]
            ph = '選點數'
        elif self.category == _CAT_NUMBER:
            options = [
                discord.SelectOption(label=f'數字 {i}', value=str(i))
                for i in range(1, 7)
            ]
            ph = '選數字 (1:1 / 次)'
        elif self.category == _CAT_PAIR:
            options = [
                discord.SelectOption(label=f'雙骰 {i}-{i}', value=str(i))
                for i in range(1, 7)
            ]
            ph = '選雙骰數字 (≥2 次 9:1)'
        else:
            options = [discord.SelectOption(label='—', value='0')]
            ph = ''

        for o in options:
            if self.detail is not None and o.value == str(self.detail):
                o.default = True

        sel = discord.ui.Select(placeholder=ph, options=options, row=2)

        async def _cb(interaction: discord.Interaction) -> None:
            if not self._check_owner(interaction):
                await self._deny(interaction); return
            self.detail = int(sel.values[0])
            await self._refresh(interaction)

        sel.callback = _cb
        return sel

    def _go_button(self) -> discord.ui.Button:
        ready = self.category is not None and (
            self.category not in _NEEDS_DETAIL or self.detail is not None
        )
        btn = discord.ui.Button(
            label='開骰', emoji='🎲',
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
    view    = DiceSetupView(
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
    uid       = str(interaction.user.id)
    category  = options.get('category')
    detail    = options.get('detail')
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

    dice = _roll()
    payout, result_desc = _compute_payout(category, detail, dice, bet)
    payout, result_desc = _apply_rescue(payout, bet, result_desc)
    new_balance = await settle_bet(uid, bet, payout)
    embed = _result_embed(
        interaction.user, bet, category, detail,
        dice, payout, result_desc, new_balance,
    )
    end_view = EndOfGameView(
        uid, bet, {'category': category, 'detail': detail},
        run_round, start_setup,
    )
    await send_or_edit(interaction, edit=edit, embed=embed, view=end_view)
