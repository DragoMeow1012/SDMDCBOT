"""
踩地雷 (Minesweeper)，由 /賭博 派遣呼叫。

設定畫面：
  - 標題與規則說明
  - 下注金額（共用 _setup.make_bet_row：100/500/1000/5000/Custom）
  - 難度：3 / 8 / 20 顆地雷
  - 「開始」按鈕

對局畫面（5 欄 × 4 列 = 20 格 + 第 5 列 1 顆 💰 提現按鈕）：
  - 20 個 ❓ 格子（cell 0..19，4 列）
  - 第 5 列獨佔 💰 提現按鈕
  - 每揭一個 🦺 安全格 → 乘數上升
  - 揭到 💥 地雷 → 結束、輸掉本注
  - 場上隨機放 1 個 🛡️ 護盾格，揭開後可擋下「下一次」地雷
  - 隨時可按 💰 提現鎖定當前乘數

乘數公式（HE=10%）：
  mult(k, mines) = min(10, (1 - HE) × Π (TOTAL-i)/(SAFE-i))
  TOTAL = 20（格數），SAFE = TOTAL − mines − 1（扣掉護盾格）
  任何「在某 k 提現」策略期望 RTP = 90%（未碰上限時）

難度 3 / 8 / 15（小場縮放後對應原 5×5 的 3 / 8 / 20 密度）。

對外入口：start_setup(interaction, bet=500, options=None)
"""
from __future__ import annotations

import random
from typing import Any

import discord

from commands._setup import (
    MIN_BET, EndOfGameView, insufficient_embed, make_bet_row, tier_label,
)
from commands._wallet import (
    apply_delta, get_balance, send_or_edit, send_smart,
    settle_with_streak, streak_line,
)


GAME_NAME = '踩地雷'
RULES_TEXT = (
    '**遊戲規則：**\n'
    '點擊 ❓ 格子來揭開它。\n'
    '每揭開一個 🦺 安全格，您的獎金乘數會增加。\n'
    '如果揭開 💥 地雷，遊戲結束，您將失去賭注。\n'
    '您可以隨時點擊 💰 提現 來領取當前乘數對應的獎金。\n'
    '**特殊格：**🛡️ 抽到可以抵擋一次地雷不會遊戲結束'
)

TOTAL_CELLS    = 20          # 5 欄 × 4 列
CASH_ROW       = 4           # 💰 提現獨佔第 5 列
DIFFICULTIES   = [3, 8, 15]  # 對應原 5×5 的 3/8/20 密度
DEFAULT_MINES  = 8
DEFAULT_BET    = 500
HOUSE_EDGE     = 0.30   # 看起來像 RTP=70%，但護盾會把實際 RTP 拉回 80-100%
MAX_MULT       = 10.0


def mines_mult(safe_revealed: int, mine_count: int) -> float:
    """乘數：扣 10% house edge、上限 10x、揭 0 格 → 1.0。"""
    if safe_revealed <= 0:
        return 1.0
    safe = TOTAL_CELLS - mine_count - 1  # 扣掉護盾格
    if safe <= 0:
        return MAX_MULT
    k = min(safe_revealed, safe)
    m = 1.0
    for i in range(k):
        m *= (TOTAL_CELLS - i) / (safe - i)
    return min(MAX_MULT, round(m * (1 - HOUSE_EDGE), 2))


class MinesGame:
    def __init__(self, bet: int, mine_count: int):
        self.bet        = bet
        self.mine_count = mine_count

        all_cells = list(range(TOTAL_CELLS))
        self.mines: set[int] = set(random.sample(all_cells, mine_count))

        safe_pool = [c for c in all_cells if c not in self.mines]
        # 場上至少有一個安全格才放護盾（mine_count = 23 才會沒有，這裡用不到）
        self.shield_cell: int = random.choice(safe_pool) if safe_pool else -1

        self.revealed       : set[int] = set()
        self.absorbed       : set[int] = set()  # 被護盾擋掉的地雷
        self.shield_picked  : bool = False      # 護盾格是否被揭
        self.shield_active  : bool = False      # 護盾就緒（拿到後尚未用）
        self.crashed        : bool = False
        self.cashed         : bool = False

    @property
    def safe_revealed(self) -> int:
        """🦺 一般安全格被揭的數量（不含護盾格、不含被擋掉的地雷）。"""
        return sum(
            1 for i in self.revealed
            if i != self.shield_cell and i not in self.absorbed
        )

    @property
    def ended(self) -> bool:
        return self.crashed or self.cashed

    def current_mult(self) -> float:
        return mines_mult(self.safe_revealed, self.mine_count)

    def reveal(self, idx: int) -> str:
        """揭一格。回傳 'safe' / 'shield' / 'mine_absorbed' / 'mine' / 'noop'。"""
        if idx in self.revealed or self.ended:
            return 'noop'
        if idx in self.mines:
            if self.shield_active:
                self.shield_active = False
                self.absorbed.add(idx)
                self.mines.discard(idx)
                self.revealed.add(idx)
                return 'mine_absorbed'
            self.revealed.add(idx)
            self.crashed = True
            return 'mine'
        if idx == self.shield_cell and not self.shield_picked:
            self.shield_picked = True
            self.shield_active = True
            self.revealed.add(idx)
            return 'shield'
        # 一般安全格
        self.revealed.add(idx)
        return 'safe'


# ── 顯示用 ────────────────────────────────────────────────────────────────
def _cell_text(game: MinesGame, idx: int, *, reveal_all: bool) -> str:
    """embed 文字格子。reveal_all=True 用於結算時整盤亮給玩家看。
    💢 = 被護盾擋掉的地雷（與護盾 🛡️ 區分，場上頂多 1 個 🛡️）。"""
    if idx in game.absorbed:
        return '💢'
    if idx in game.revealed:
        if idx in game.mines:
            return '💥'
        if idx == game.shield_cell:
            return '🛡️'
        return '🦺'
    if reveal_all:
        if idx in game.mines:
            return '💥'
        if idx == game.shield_cell:
            return '🛡️'
        return '🦺'
    return '❓'


def _grid_text(game: MinesGame, *, reveal_all: bool = False) -> str:
    rows: list[str] = []
    for r in range(4):
        row: list[str] = []
        for c in range(5):
            idx = r * 5 + c
            row.append(_cell_text(game, idx, reveal_all=reveal_all))
        rows.append(' '.join(row))
    return '\n'.join(rows)


def _running_embed(user: discord.abc.User, game: MinesGame) -> discord.Embed:
    mult      = game.current_mult()
    potential = int(game.bet * mult)
    net       = potential - game.bet
    safe_total = TOTAL_CELLS - game.mine_count - 1
    shield_str = (
        '🛡️ 護盾就緒（可擋下一次地雷）' if game.shield_active
        else ('🛡️ 護盾已用' if game.shield_picked else '🛡️ 護盾尚未抽到')
    )

    desc = [
        _grid_text(game),
        '',
        f'地雷：**{game.mine_count}** 顆　|　已揭 **{game.safe_revealed} / {safe_total}** 安全格',
        f'當前乘數：**{mult:.2f}x**',
        f'若現在提現：**{potential}** (+{net})',
        shield_str,
        '',
        '點 ❓ 揭格 | 點 💰 提現',
    ]
    embed = discord.Embed(
        title=f'💣 {GAME_NAME} — {game.mine_count} 顆地雷',
        description='\n'.join(desc),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text=user.display_name)
    return embed


def _cashed_embed(user: discord.abc.User, game: MinesGame,
                  balance: int, streak: int = 0,
                  bonus: int = 0) -> discord.Embed:
    mult     = game.current_mult()
    payout   = int(game.bet * mult)
    net      = payout - game.bet + bonus
    sign     = f'+{net}' if net >= 0 else str(net)
    _, color = tier_label(payout, game.bet)
    desc = [
        _grid_text(game, reveal_all=True),
        '',
        f'💰 提現 @ **{mult:.2f}x**',
        f'投注 {game.bet}　取回 **{payout}**　淨 **{sign}**',
    ]
    sl = streak_line(streak, bonus)
    if sl:
        desc.append(sl)
    desc += ['', f'餘額：**{balance}** 咕嚕喵碎片']
    embed = discord.Embed(
        title=f'💣 {GAME_NAME}　{sign}',
        description='\n'.join(desc), color=color,
    )
    embed.set_footer(text=user.display_name)
    return embed


def _crashed_embed(user: discord.abc.User, game: MinesGame,
                   balance: int) -> discord.Embed:
    _, color = tier_label(0, game.bet)
    desc = [
        _grid_text(game, reveal_all=True),
        '',
        f'💥 BOOM @ {game.safe_revealed} 個安全格',
        f'投注 **{game.bet}** 全部歸零... ／(•ㅿ•)＼',
        '',
        f'餘額：**{balance}** 咕嚕喵碎片',
    ]
    embed = discord.Embed(
        title=f'💣 {GAME_NAME}　-{game.bet}',
        description='\n'.join(desc), color=color,
    )
    embed.set_footer(text=user.display_name)
    return embed


# ── 對局 view ────────────────────────────────────────────────────────────
class MinesGameView(discord.ui.View):
    def __init__(self, uid: str, game: MinesGame):
        super().__init__(timeout=86400)
        self.uid  = uid
        self.game = game
        self._build()

    def _build(self) -> None:
        self.clear_items()
        for idx in range(TOTAL_CELLS):
            row = idx // 5
            self.add_item(self._cell_button(idx, row))
        cash = discord.ui.Button(
            label='提現', emoji='💰',
            style=discord.ButtonStyle.success, row=CASH_ROW,
        )
        cash.callback = self._cash_out_cb
        self.add_item(cash)

    def _cell_button(self, idx: int, row: int) -> discord.ui.Button:
        if idx in self.game.absorbed:
            btn = discord.ui.Button(
                emoji='🛡️', style=discord.ButtonStyle.primary,
                row=row, disabled=True,
            )
            return btn
        if idx in self.game.revealed:
            if idx in self.game.mines:
                btn = discord.ui.Button(
                    emoji='💥', style=discord.ButtonStyle.danger,
                    row=row, disabled=True,
                )
            elif idx == self.game.shield_cell:
                btn = discord.ui.Button(
                    emoji='🛡️', style=discord.ButtonStyle.primary,
                    row=row, disabled=True,
                )
            else:
                btn = discord.ui.Button(
                    emoji='🦺', style=discord.ButtonStyle.success,
                    row=row, disabled=True,
                )
            return btn
        btn = discord.ui.Button(
            emoji='❓', style=discord.ButtonStyle.secondary, row=row,
        )
        btn.callback = self._make_cell_cb(idx)
        return btn

    def _make_cell_cb(self, idx: int):
        async def _cb(interaction: discord.Interaction) -> None:
            if str(interaction.user.id) != self.uid:
                await interaction.response.send_message(
                    '這不是你的牌局', ephemeral=True,
                )
                return
            result = self.game.reveal(idx)
            if result == 'noop':
                await interaction.response.defer()
                return
            if self.game.crashed:
                await self._end(interaction)
                return
            # 還能繼續玩
            self._build()
            embed = _running_embed(interaction.user, self.game)
            await interaction.response.edit_message(embed=embed, view=self)
        return _cb

    async def _cash_out_cb(self, interaction: discord.Interaction) -> None:
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message(
                '這不是你的牌局', ephemeral=True,
            )
            return
        if self.game.ended:
            await interaction.response.defer()
            return
        self.game.cashed = True
        await self._end(interaction)

    async def _end(self, interaction: discord.Interaction) -> None:
        if self.game.cashed:
            mult   = self.game.current_mult()
            payout = int(self.game.bet * mult)
            balance, streak, bonus = await settle_with_streak(
                self.uid, self.game.bet, payout, deducted=True,
            )
            embed = _cashed_embed(interaction.user, self.game, balance,
                                  streak, bonus)
        else:
            # 踩雷：本金已扣，payout=0，streak 歸 0
            balance, _, _ = await settle_with_streak(
                self.uid, self.game.bet, 0, deducted=True,
            )
            embed = _crashed_embed(interaction.user, self.game, balance)

        end_view = EndOfGameView(
            self.uid, self.game.bet,
            {'mines': self.game.mine_count},
            run_round, start_setup,
        )
        await interaction.response.edit_message(embed=embed, view=end_view)
        self.stop()


# ── Setup view ───────────────────────────────────────────────────────────
def _setup_embed(user: discord.abc.User, bet: int,
                 mine_count: int) -> discord.Embed:
    body = [
        RULES_TEXT,
        '',
        f'目前下注：**{bet}** 咕嚕喵碎片',
        f'地雷數：**{mine_count}** 顆',
        '',
        '按下方 💣 「開始」開始遊戲。',
    ]
    embed = discord.Embed(
        title=f'💣 {GAME_NAME} — 模式選擇',
        description='\n'.join(body),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text=user.display_name)
    return embed


class MinesSetupView(discord.ui.View):
    def __init__(self, uid: str, bet: int = DEFAULT_BET,
                 mines: int = DEFAULT_MINES):
        super().__init__(timeout=86400)
        self.uid   = uid
        self.bet   = bet
        self.mines = mines
        self._build()

    def _build(self) -> None:
        self.clear_items()
        # row 0：下注金額
        for btn in make_bet_row(self, self._redraw, row=0):
            self.add_item(btn)
        # row 1：難度
        for n in DIFFICULTIES:
            selected = (self.mines == n)
            btn = discord.ui.Button(
                label=f'{n} 顆地雷{" ★" if selected else ""}',
                style=(discord.ButtonStyle.success if selected
                       else discord.ButtonStyle.secondary),
                row=1,
            )
            btn.callback = self._make_diff_cb(n)
            self.add_item(btn)
        # row 2：開始
        start = discord.ui.Button(
            label='開始', emoji='💣',
            style=discord.ButtonStyle.primary, row=2,
        )
        start.callback = self._start_cb
        self.add_item(start)

    def _make_diff_cb(self, n: int):
        async def _cb(interaction: discord.Interaction) -> None:
            if str(interaction.user.id) != self.uid:
                await interaction.response.send_message(
                    '這不是你的賭局', ephemeral=True,
                )
                return
            self.mines = n
            await self._redraw(interaction)
        return _cb

    async def _redraw(self, interaction: discord.Interaction) -> None:
        self._build()
        embed = _setup_embed(interaction.user, self.bet, self.mines)
        await interaction.response.edit_message(embed=embed, view=self)

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
        await run_round(interaction, self.bet, {'mines': self.mines})


# ── 對外入口 ─────────────────────────────────────────────────────────────
async def start_setup(interaction: discord.Interaction,
                      bet: int = DEFAULT_BET,
                      options: dict[str, Any] | None = None) -> None:
    options = options or {}
    mines   = int(options.get('mines', DEFAULT_MINES))
    bet     = max(MIN_BET, int(bet))
    view    = MinesSetupView(str(interaction.user.id), bet=bet, mines=mines)
    embed   = _setup_embed(interaction.user, bet, mines)
    await send_smart(interaction, embed=embed, view=view, ephemeral=True)


async def run_round(interaction: discord.Interaction, bet: int,
                    options: dict[str, Any], *, edit: bool = False) -> None:
    uid     = str(interaction.user.id)
    mines   = int(options.get('mines', DEFAULT_MINES))
    balance = get_balance(uid)
    if balance < bet:
        await send_or_edit(
            interaction, edit=edit,
            embed=insufficient_embed(bet, balance),
            **({'view': None} if edit else {}),
        )
        return
    await apply_delta(uid, -bet)
    game  = MinesGame(bet, mines)
    view  = MinesGameView(uid, game)
    embed = _running_embed(interaction.user, game)
    await send_or_edit(interaction, edit=edit, embed=embed, view=view)
