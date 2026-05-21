"""
PVP 對戰邏輯（被 /賭博 對戰:<user> 派遣呼叫）。

流程：
  1. /賭博 對戰:<user> → 發起者 ephemeral 設定金額
  2. Bot 公開發送邀請 embed，@對手
  3. 對手按按鈕：比大小 / 骰子 / 老虎機 / 輪盤 / 拒絕
  4. 開局：雙方各扣本注，遊戲立即結算
  5. 勝者拿走 2× bet；平手雙方退款
  6. 沒有 house edge（純玩家對玩家）

對外入口：start_setup(interaction, opponent)

互動式 PVP（21點 / 爆點 / 踩地雷）需要雙方各自跑 ephemeral 回合 → 跨訊息
狀態機，下輪做。
"""
from __future__ import annotations

import random

import discord

from commands import blackjack as _bj
from commands import crash as _c
from commands import minesweeper as _m
from commands import roulette as _r
from commands import slot as _s
from commands._setup import insufficient_embed, make_bet_row
from commands._wallet import apply_delta, get_balance, send_smart


_RANKS = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
_SUITS = ['♠', '♥', '♦', '♣']
_RANK_VAL = {r: i for i, r in enumerate(_RANKS, start=2)}  # 2=2, A=14

_DICE_EMOJI = {1: '⚀', 2: '⚁', 3: '⚂', 4: '⚃', 5: '⚄', 6: '⚅'}


def _draw_card() -> tuple[str, int]:
    rank = random.choice(_RANKS)
    suit = random.choice(_SUITS)
    return rank + suit, _RANK_VAL[rank]


def _roll_3d6() -> tuple[int, int, int]:
    return (random.randint(1, 6), random.randint(1, 6), random.randint(1, 6))


def _result_embed(init_user: discord.abc.User, opp_user: discord.abc.User,
                  bet: int, game_name: str,
                  init_show: str, opp_show: str,
                  winner: discord.abc.User | None,
                  mult: float = 1.0,
                  init_balance: int | None = None,
                  opp_balance: int | None = None) -> discord.Embed:
    if winner is None:
        title = f'🤝 PVP {game_name} — 平手'
        body  = f'雙方各退回 {bet} 咕嚕喵碎片（不套用倍率）'
        color = discord.Color.gold()
    else:
        loser = opp_user if winner.id == init_user.id else init_user
        transfer = int(bet * mult)
        title = f'🏆 PVP {game_name} — {winner.display_name} 勝'
        body  = (
            f'最高倍率 **{mult:.2f}x** × 本注 **{bet}** = **{transfer}** 咕嚕喵碎片\n'
            f'{winner.mention} 淨 **+{transfer}**\n'
            f'{loser.mention} 淨 **-{transfer}**'
        )
        color = discord.Color.green()

    embed = discord.Embed(title=title, description=body, color=color)
    embed.add_field(name=f'⚔️ {init_user.display_name}',
                    value=init_show, inline=True)
    embed.add_field(name=f'🛡️ {opp_user.display_name}',
                    value=opp_show,  inline=True)
    if init_balance is not None and opp_balance is not None:
        embed.add_field(
            name='💰 結算後餘額',
            value=(
                f'{init_user.mention}: **{init_balance}** 咕嚕喵碎片\n'
                f'{opp_user.mention}: **{opp_balance}** 咕嚕喵碎片'
            ),
            inline=False,
        )
    return embed


# 每個遊戲類型的「最高倍率」(net win = bet × N) 用於 PVP 結算
# crash / mines 用 winner 實際達到的 cashout 倍率（動態），其他用此表
_PVP_STATIC_MULT = {
    'card':     2,    # 比大小
    'dice':     5,    # 骰子比大小
    'slot':     5,    # 老虎機比大小
    'roulette': 5,    # 輪盤比大小
    'bj':       5,    # 21 點
    # 'crash' / 'mines' 用實際倍率（_compute_pvp_mult 內處理）
}


def _compute_pvp_mult(game_type: str, init_val: float, opp_val: float) -> float:
    """回傳本局 PVP 結算時的「最高倍率」。crash/mines 走實際 cashout，
    其他遊戲用 _PVP_STATIC_MULT 中的靜態值。"""
    if game_type in ('crash', 'mines'):
        return max(1.0, float(max(init_val, opp_val)))
    return float(_PVP_STATIC_MULT.get(game_type, 1))


async def _settle(init_user: discord.abc.User, opp_user: discord.abc.User,
                  bet: int, init_val: float, opp_val: float,
                  game_type: str = 'card',
                  ) -> tuple[discord.abc.User | None, float]:
    """根據比較結果分配獎金。回傳 (winner|None, 倍率)。
    本注於開局時已雙方各扣。輸贏轉移額 = bet × 最高倍率（可使輸家負債）。"""
    mult = _compute_pvp_mult(game_type, init_val, opp_val)
    if init_val == opp_val:
        # 平手：退本，不套用倍率
        await apply_delta(str(init_user.id), bet)
        await apply_delta(str(opp_user.id),  bet)
        return None, mult
    transfer = int(bet * mult)
    if init_val > opp_val:
        winner, loser = init_user, opp_user
    else:
        winner, loser = opp_user, init_user
    # 贏家：退本 + 收取對手 transfer 額
    await apply_delta(str(winner.id), bet + transfer)
    # 輸家：除了開局已扣的 bet，再額外扣 (transfer - bet)；可能造成負餘額
    extra_loss = transfer - bet
    if extra_loss != 0:
        await apply_delta(str(loser.id), -extra_loss)
    return winner, mult


# ── 瞬間結算的 PVP（card/dice/slot/roulette）共用私密 session ──────
class _InstantPVPSession:
    """雙方各按「抽牌」鈕看私訊結果，兩邊都按完才在公開訊息開牌。"""

    GAME_NAME = ''
    GAME_TYPE = ''
    TITLE_EMOJI = ''
    DRAW_LABEL = '抽牌'

    def __init__(self, public_msg, init_user, opp_user, bet):
        self.public_msg = public_msg
        self.init_user  = init_user
        self.opp_user   = opp_user
        self.bet        = bet
        self.joined     = {init_user.id: False, opp_user.id: False}
        self.scores     : dict[int, float | None] = {
            init_user.id: None, opp_user.id: None,
        }
        self.shows      = {init_user.id: '', opp_user.id: ''}
        self.settled    = False

    def _draw(self, uid: int) -> tuple[float, str]:
        """子類覆寫：回傳 (score, 個人顯示文字)。"""
        raise NotImplementedError

    def _public_embed(self) -> discord.Embed:
        lines = [
            f'**{self.GAME_NAME} PVP** — 各下 **{self.bet}** 咕嚕喵碎片',
            '雙方按下方按鈕抽各自的結果（私訊看）。兩邊都按完才公開開牌。',
            '',
        ]
        for u in (self.init_user, self.opp_user):
            mark = '✅ 已抽' if self.joined[u.id] else '⏳ 尚未抽'
            lines.append(f'{u.mention}: {mark}')
        return discord.Embed(
            title=f'{self.TITLE_EMOJI} {self.GAME_NAME} PVP',
            description='\n'.join(lines), color=discord.Color.blurple(),
        )

    async def _update_public(self) -> None:
        try:
            await self.public_msg.edit(
                embed=self._public_embed(),
                view=_InstantPVPPublicView(self),
            )
        except discord.HTTPException:
            pass

    async def join(self, interaction: discord.Interaction, uid: int) -> None:
        if self.joined[uid]:
            await interaction.response.send_message(
                '你已經抽過了', ephemeral=True,
            )
            return
        self.joined[uid] = True
        score, show = self._draw(uid)
        self.scores[uid] = score
        self.shows[uid]  = show
        await interaction.response.send_message(
            embed=discord.Embed(
                title='🤫 你的結果（私訊）',
                description=f'{show}\n\n等對方也抽完才公開開牌。',
                color=discord.Color.gold(),
            ),
            ephemeral=True,
        )
        if all(s is not None for s in self.scores.values()):
            await self._finalize()
        else:
            await self._update_public()

    async def _finalize(self) -> None:
        if self.settled:
            return
        self.settled = True
        init_s = self.scores[self.init_user.id]
        opp_s  = self.scores[self.opp_user.id]
        winner, mult = await _settle(
            self.init_user, self.opp_user, self.bet,
            init_s, opp_s, game_type=self.GAME_TYPE,
        )
        init_bal = get_balance(str(self.init_user.id))
        opp_bal  = get_balance(str(self.opp_user.id))
        embed = _result_embed(
            self.init_user, self.opp_user, self.bet, self.GAME_NAME,
            self.shows[self.init_user.id], self.shows[self.opp_user.id],
            winner, mult=mult,
            init_balance=init_bal, opp_balance=opp_bal,
        )
        try:
            await self.public_msg.edit(
                embed=embed,
                view=_again_view(self.init_user, self.opp_user, self.bet,
                                 self.GAME_TYPE),
            )
        except discord.HTTPException:
            pass


class _InstantPVPPublicView(discord.ui.View):
    def __init__(self, session: _InstantPVPSession):
        super().__init__(timeout=900)
        self.session = session
        self._build()

    def _build(self) -> None:
        self.clear_items()
        for user in (self.session.init_user, self.session.opp_user):
            done = self.session.joined[user.id]
            btn = discord.ui.Button(
                label=f'{user.display_name[:10]} {self.session.DRAW_LABEL}',
                emoji=('✅' if done else '🎴'),
                style=(discord.ButtonStyle.secondary if done
                       else discord.ButtonStyle.primary),
                disabled=done,
            )
            btn.callback = self._cb(user.id)
            self.add_item(btn)

    def _cb(self, owner_id: int):
        async def _do(interaction: discord.Interaction) -> None:
            if interaction.user.id != owner_id:
                await interaction.response.send_message(
                    '這不是你的按鈕', ephemeral=True,
                )
                return
            await self.session.join(interaction, owner_id)
        return _do


class _CardPVPSession(_InstantPVPSession):
    GAME_NAME = '比大小'
    GAME_TYPE = 'card'
    TITLE_EMOJI = '🃏'
    DRAW_LABEL = '抽牌'

    def _draw(self, uid: int) -> tuple[float, str]:
        card, val = _draw_card()
        return float(val), f'`{card}`\n值 **{val}**'


class _DicePVPSession(_InstantPVPSession):
    GAME_NAME = '骰子比大小'
    GAME_TYPE = 'dice'
    TITLE_EMOJI = '🎲'
    DRAW_LABEL = '擲骰'

    def _draw(self, uid: int) -> tuple[float, str]:
        d = _roll_3d6()
        s = sum(d)
        return float(s), f"{' '.join(_DICE_EMOJI[x] for x in d)}\n總和 **{s}**"


class _SlotPVPSession(_InstantPVPSession):
    GAME_NAME = '老虎機比大小'
    GAME_TYPE = 'slot'
    TITLE_EMOJI = '🎰'
    DRAW_LABEL = '旋轉'

    def _draw(self, uid: int) -> tuple[float, str]:
        grid, score, hits = _slot_spin_and_score(self.bet)
        show = (
            _slot_grid_text(grid) + '\n'
            + (f"中：{', '.join(hits)}\n" if hits else '無中獎線\n')
            + f'得分 **{score}**'
        )
        return float(score), show


class _RoulettePVPSession(_InstantPVPSession):
    GAME_NAME = '輪盤比大小'
    GAME_TYPE = 'roulette'
    TITLE_EMOJI = '🎡'
    DRAW_LABEL = '旋盤'

    def _draw(self, uid: int) -> tuple[float, str]:
        n = _r._spin()
        return float(n), f'開出 {_r._number_color(n)} **{n}**'


async def _play_compare_card(interaction: discord.Interaction,
                             init_user, opp_user, bet: int) -> None:
    session = _CardPVPSession(interaction.message, init_user, opp_user, bet)
    await _smart_edit(
        interaction, content=None, embed=session._public_embed(),
        view=_InstantPVPPublicView(session),
    )


async def _play_compare_dice(interaction: discord.Interaction,
                             init_user, opp_user, bet: int) -> None:
    session = _DicePVPSession(interaction.message, init_user, opp_user, bet)
    await _smart_edit(
        interaction, content=None, embed=session._public_embed(),
        view=_InstantPVPPublicView(session),
    )


def _slot_spin_and_score(bet: int) -> tuple[list[str], int, list[str]]:
    """PVP 老虎機：跑一次 slot._spin + 計分（不套用 rescue）。"""
    grid = _s._spin()
    line_bet = bet // 5
    total = 0
    hits: list[str] = []
    for (a, b, c), name in _s._LINES:
        if grid[a] == grid[b] == grid[c]:
            mult = _s._EMOJI_TO_MULT[grid[a]]
            pay  = line_bet * mult
            total += pay
            hits.append(f'{name} {grid[a]}×3')
    return grid, min(total, bet * 10), hits


def _slot_grid_text(grid: list[str]) -> str:
    return '\n'.join(' | '.join(grid[i*3:i*3+3]) for i in range(3))


async def _play_compare_slot(interaction: discord.Interaction,
                             init_user, opp_user, bet: int) -> None:
    session = _SlotPVPSession(interaction.message, init_user, opp_user, bet)
    await _smart_edit(
        interaction, content=None, embed=session._public_embed(),
        view=_InstantPVPPublicView(session),
    )


async def _play_compare_roulette(interaction: discord.Interaction,
                                 init_user, opp_user, bet: int) -> None:
    session = _RoulettePVPSession(interaction.message, init_user, opp_user, bet)
    await _smart_edit(
        interaction, content=None, embed=session._public_embed(),
        view=_InstantPVPPublicView(session),
    )


# ─────────────────────────────────────────────────────────────────────
# 互動式 PVP（21點 / 爆點 / 踩地雷）
# 共同模式：雙方各 2 顆按鈕在同一則公開訊息，gated by owner
# 兩邊都結束 → 自動結算 + 退回 EndOfGameView 樣式（不再來一局）
# ─────────────────────────────────────────────────────────────────────

async def _smart_edit(interaction: discord.Interaction, **kwargs) -> None:
    """interaction.response 已用過 → 直接 message.edit；
    否則用 response.edit_message。讓深處 helper 不必管 defer 狀態。
    對 deferred component interaction，message.edit 比 edit_original_response 穩定。"""
    if interaction.response.is_done():
        try:
            await interaction.message.edit(**kwargs)
        except Exception as e:
            print(f'[PVP] message.edit fail: {e!r}, fallback to edit_original_response')
            try:
                await interaction.edit_original_response(**kwargs)
            except Exception as e2:
                print(f'[PVP] edit_original_response also fail: {e2!r}')
                raise
    else:
        await interaction.response.edit_message(**kwargs)


class PVPAgainView(discord.ui.View):
    """PVP 結算後的「再來一局」按鈕。雙方各一顆，都按下後直接同訊息開新一局。"""

    def __init__(self, init_user, opp_user, bet: int, game_type: str,
                 extra: dict | None = None):
        super().__init__(timeout=600)
        self.init_user  = init_user
        self.opp_user   = opp_user
        self.bet        = bet
        self.game_type  = game_type
        self.extra      = extra or {}
        self.init_ready = False
        self.opp_ready  = False
        self._building  = False
        self._build()

    def _build(self) -> None:
        self.clear_items()
        for user in (self.init_user, self.opp_user):
            ready = (self.init_ready if user.id == self.init_user.id
                     else self.opp_ready)
            btn = discord.ui.Button(
                label=f'{user.display_name[:10]} 再來一局'
                      + (' ✓' if ready else ''),
                emoji='🔄',
                style=(discord.ButtonStyle.success if ready
                       else discord.ButtonStyle.primary),
                disabled=ready,
            )
            btn.callback = self._cb(user.id)
            self.add_item(btn)

    def _cb(self, owner_id: int):
        async def _do(interaction: discord.Interaction) -> None:
            if interaction.user.id != owner_id:
                await interaction.response.send_message(
                    '這不是你的按鈕', ephemeral=True,
                )
                return
            bal = get_balance(str(owner_id))
            if bal < self.bet:
                await interaction.response.send_message(
                    embed=insufficient_embed(self.bet, bal), ephemeral=True,
                )
                return
            # 標記 ready
            if owner_id == self.init_user.id:
                self.init_ready = True
            else:
                self.opp_ready = True

            if self.init_ready and self.opp_ready:
                # 兩邊都按 → 雙方再驗餘額後扣費 → 同訊息開新一局
                # defer 以免 apply_delta x2 + dispatch 超過 3 秒
                await interaction.response.defer()
                init_bal = get_balance(str(self.init_user.id))
                opp_bal  = get_balance(str(self.opp_user.id))
                if init_bal < self.bet or opp_bal < self.bet:
                    short_uid = (self.init_user.mention if init_bal < self.bet
                                 else self.opp_user.mention)
                    await _smart_edit(
                        interaction,
                        embed=discord.Embed(
                            title='⚠️ 餘額不足',
                            description=f'{short_uid} 沒有足夠的咕嚕喵碎片',
                            color=discord.Color.red(),
                        ),
                        view=None,
                    )
                    self.stop()
                    return
                await apply_delta(str(self.init_user.id), -self.bet)
                await apply_delta(str(self.opp_user.id),  -self.bet)
                self.stop()
                await _dispatch_game(
                    interaction, self.game_type,
                    self.init_user, self.opp_user, self.bet,
                    extra=self.extra,
                )
            else:
                self._build()
                await interaction.response.edit_message(view=self)
        return _do


def _again_view(init_user, opp_user, bet: int,
                game_type: str,
                extra: dict | None = None) -> PVPAgainView:
    return PVPAgainView(init_user, opp_user, bet, game_type, extra)


async def _dispatch_game(interaction: discord.Interaction, game_type: str,
                         init_user, opp_user, bet: int,
                         extra: dict | None = None) -> None:
    """根據 game_type 派遣到對應的 PVP 對局函式（雙方均已扣費）。
    extra 用於傳遞遊戲特定設定（例如 mines 的 mine_count）。"""
    extra = extra or {}
    if game_type == 'card':
        await _play_compare_card(interaction, init_user, opp_user, bet)
    elif game_type == 'dice':
        await _play_compare_dice(interaction, init_user, opp_user, bet)
    elif game_type == 'slot':
        await _play_compare_slot(interaction, init_user, opp_user, bet)
    elif game_type == 'roulette':
        await _play_compare_roulette(interaction, init_user, opp_user, bet)
    elif game_type == 'bj':
        await _play_pvp_blackjack(interaction, init_user, opp_user, bet)
    elif game_type == 'crash':
        await _play_pvp_crash(interaction, init_user, opp_user, bet)
    elif game_type == 'mines':
        # rematch 走這條：跳過難度選擇，直接用 extra 帶的 mine_count
        mc = int(extra.get('mine_count', 3))
        await _start_pvp_mines(interaction, init_user, opp_user, bet, mc)


# ── 21 點 PVP（私密手牌） ──────────────────────────────────────────
class PVP21Game:
    def __init__(self, init_id: int, opp_id: int):
        self.shoe   = _bj._fresh_shoe()
        self.hands  = {
            init_id: [self.shoe.pop(), self.shoe.pop()],
            opp_id:  [self.shoe.pop(), self.shoe.pop()],
        }
        self.stood  = {init_id: False, opp_id: False}

    def hit(self, uid: int) -> None:
        if self.stood[uid]:
            return
        self.hands[uid].append(self.shoe.pop())
        if _bj._is_bust(self.hands[uid]):
            self.stood[uid] = True

    def stand(self, uid: int) -> None:
        self.stood[uid] = True

    def all_done(self) -> bool:
        return all(self.stood.values())

    def score(self, uid: int) -> int:
        v = _bj._hand_value(self.hands[uid])
        return 0 if v > 21 else v


class PVP21Session:
    """同訊息公開狀態 + 每人各自 ephemeral 私密手牌的協調器。"""

    def __init__(self, public_msg: discord.Message,
                 init_user, opp_user, bet: int):
        self.public_msg  = public_msg
        self.init_user   = init_user
        self.opp_user    = opp_user
        self.bet         = bet
        self.game        = PVP21Game(init_user.id, opp_user.id)
        self.joined      = {init_user.id: False, opp_user.id: False}
        self.ephemerals  : dict[int, discord.Message] = {}
        self.settled     = False

    # ── 公開訊息 ──────────────────────────────────────────────────
    def _public_embed(self) -> discord.Embed:
        lines = [
            f'**21 點 PVP** — 各下 **{self.bet}** 咕嚕喵碎片',
            '雙方各自按下方「我的牌」按鈕查看私人牌局',
            '',
        ]
        for u in (self.init_user, self.opp_user):
            joined = '✅ 已加入' if self.joined[u.id] else '⏳ 尚未加入'
            status = '✅ 已停牌' if self.game.stood[u.id] else '➡️ 出牌中'
            n      = len(self.game.hands[u.id])
            lines.append(f'{u.mention}: {joined} / {status} (手牌 {n} 張)')
        return discord.Embed(
            title='🃏 21 點 PVP — 進行中',
            description='\n'.join(lines), color=discord.Color.blurple(),
        )

    async def update_public(self) -> None:
        try:
            await self.public_msg.edit(
                embed=self._public_embed(),
                view=PVP21PublicView(self),
            )
        except discord.HTTPException:
            pass

    # ── 個人 ephemeral ────────────────────────────────────────────
    def _eph_embed(self, uid: int, *, finished: bool = False) -> discord.Embed:
        cards = self.game.hands[uid]
        v     = _bj._hand_value(cards)
        tag   = '（爆）' if v > 21 else f'（{v} 點）'
        if finished:
            title = '🃏 你的牌（本局結束）'
            desc  = (
                f'{_bj._cards_str(cards)} {tag}\n\n'
                '本局已結算，請看公開訊息。'
            )
            color = discord.Color.dark_grey()
        elif self.game.stood[uid]:
            title = '🃏 你的牌 — 已停牌'
            desc  = (
                f'{_bj._cards_str(cards)} {tag}\n\n'
                '已停牌，等待對方出牌完。'
            )
            color = discord.Color.gold()
        else:
            title = '🃏 你的牌 — 你的回合'
            desc  = (
                f'{_bj._cards_str(cards)} {tag}\n\n'
                '按「要牌」抽一張，「停牌」結束。\n'
                '5 張不爆牌自動停牌（爭五龍）。'
            )
            color = discord.Color.blurple()
        return discord.Embed(title=title, description=desc, color=color)

    async def join(self, interaction: discord.Interaction, uid: int) -> None:
        """玩家首次點「我的牌」→ 送出 ephemeral 私訊。"""
        if self.joined[uid]:
            await interaction.response.send_message(
                '你已經開啟過了，看剛剛的私訊', ephemeral=True,
            )
            return
        self.joined[uid] = True
        await interaction.response.send_message(
            embed=self._eph_embed(uid),
            view=PVP21EphView(self, uid), ephemeral=True,
        )
        try:
            self.ephemerals[uid] = await interaction.original_response()
        except discord.HTTPException:
            pass
        await self.update_public()

    async def action(self, interaction: discord.Interaction,
                     uid: int, what: str) -> None:
        """ephemeral 上的 要牌/停牌：更新 game + 自己 ephemeral + 公開狀態。"""
        if self.settled:
            await interaction.response.defer()
            return
        if what == 'hit':
            self.game.hit(uid)
        else:
            self.game.stand(uid)

        if self.game.all_done():
            await self._finalize(interaction, uid)
            return

        # 自己 ephemeral 刷新
        await interaction.response.edit_message(
            embed=self._eph_embed(uid),
            view=PVP21EphView(self, uid),
        )
        # 公開狀態跟著更新
        await self.update_public()

    async def _finalize(self, interaction: discord.Interaction,
                        last_uid: int) -> None:
        self.settled = True
        init_s = self.game.score(self.init_user.id)
        opp_s  = self.game.score(self.opp_user.id)
        # 開牌組 result embed（這時候才把雙方手牌公開）
        init_show = self._format_revealed(self.init_user.id)
        opp_show  = self._format_revealed(self.opp_user.id)
        winner, mult = await _settle(
            self.init_user, self.opp_user, self.bet,
            init_s, opp_s, game_type='bj',
        )
        init_bal = get_balance(str(self.init_user.id))
        opp_bal  = get_balance(str(self.opp_user.id))
        result_embed = _result_embed(
            self.init_user, self.opp_user, self.bet, '21 點 PVP',
            init_show, opp_show, winner, mult=mult,
            init_balance=init_bal, opp_balance=opp_bal,
        )
        # 觸發結算者的 ephemeral 收尾
        await interaction.response.edit_message(
            embed=self._eph_embed(last_uid, finished=True), view=None,
        )
        # 另一邊的 ephemeral 也要更新（如果有）
        other_uid = (self.opp_user.id if last_uid == self.init_user.id
                     else self.init_user.id)
        other_msg = self.ephemerals.get(other_uid)
        if other_msg is not None:
            try:
                await other_msg.edit(
                    embed=self._eph_embed(other_uid, finished=True),
                    view=None,
                )
            except discord.HTTPException:
                pass
        # 公開訊息：開牌 + 再來一局
        try:
            await self.public_msg.edit(
                embed=result_embed,
                view=_again_view(self.init_user, self.opp_user, self.bet, 'bj'),
            )
        except discord.HTTPException:
            pass

    def _format_revealed(self, uid: int) -> str:
        cards = self.game.hands[uid]
        v = _bj._hand_value(cards)
        tag = '（爆）' if v > 21 else f'（{v} 點）'
        return f'{_bj._cards_str(cards)} {tag}'


class PVP21PublicView(discord.ui.View):
    """公開訊息上的「我的牌」按鈕（2 顆，各自門禁）。"""

    def __init__(self, session: PVP21Session):
        super().__init__(timeout=900)
        self.session = session
        self._build()

    def _build(self) -> None:
        self.clear_items()
        for user in (self.session.init_user, self.session.opp_user):
            joined = self.session.joined[user.id]
            btn = discord.ui.Button(
                label=f'{user.display_name[:10]} 我的牌',
                emoji=('✅' if joined else '🃏'),
                style=(discord.ButtonStyle.secondary if joined
                       else discord.ButtonStyle.primary),
                disabled=joined,
            )
            btn.callback = self._cb(user.id)
            self.add_item(btn)

    def _cb(self, owner_id: int):
        async def _do(interaction: discord.Interaction) -> None:
            if interaction.user.id != owner_id:
                await interaction.response.send_message(
                    '這不是你的牌', ephemeral=True,
                )
                return
            await self.session.join(interaction, owner_id)
        return _do


class PVP21EphView(discord.ui.View):
    """個人 ephemeral 的要牌/停牌按鈕。"""

    def __init__(self, session: PVP21Session, owner_id: int):
        super().__init__(timeout=900)
        self.session  = session
        self.owner_id = owner_id
        self._build()

    def _build(self) -> None:
        self.clear_items()
        done = self.session.game.stood[self.owner_id]
        hit = discord.ui.Button(
            label='要牌', emoji='🃏',
            style=discord.ButtonStyle.primary, disabled=done,
        )
        hit.callback = self._cb('hit')
        self.add_item(hit)
        stand = discord.ui.Button(
            label='停牌', emoji='✋',
            style=discord.ButtonStyle.secondary, disabled=done,
        )
        stand.callback = self._cb('stand')
        self.add_item(stand)

    def _cb(self, action: str):
        async def _do(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message(
                    '這不是你的牌', ephemeral=True,
                )
                return
            await self.session.action(interaction, self.owner_id, action)
        return _do


async def _play_pvp_blackjack(interaction: discord.Interaction,
                              init_user, opp_user, bet: int) -> None:
    # 把當前訊息轉為 PVP 21 點公開狀態
    session = PVP21Session(interaction.message, init_user, opp_user, bet)
    await _smart_edit(
        interaction, content=None,
        embed=session._public_embed(),
        view=PVP21PublicView(session),
    )


# ── 爆點 PVP ──────────────────────────────────────────────────────────
class PVPCrashGame:
    def __init__(self, init_id: int, opp_id: int):
        self.crash_at  = {init_id: _c._generate_crash(),
                          opp_id:  _c._generate_crash()}
        self.current   = {init_id: 1.00, opp_id: 1.00}
        self.cashed_at : dict[int, float | None] = {init_id: None, opp_id: None}
        self.crashed   = {init_id: False, opp_id: False}

    def step_up(self, uid: int) -> bool:
        """加注一格。回傳是否爆炸。"""
        if self.crashed[uid] or self.cashed_at[uid] is not None:
            return False
        new = round(self.current[uid] * _c._GROWTH, 2)
        if new >= self.crash_at[uid]:
            self.current[uid] = self.crash_at[uid]
            self.crashed[uid] = True
            return True
        self.current[uid] = min(_c._CAP, new)
        return False

    def cash_out(self, uid: int) -> None:
        if self.crashed[uid] or self.cashed_at[uid] is not None:
            return
        self.cashed_at[uid] = self.current[uid]

    def is_done(self, uid: int) -> bool:
        return self.crashed[uid] or self.cashed_at[uid] is not None

    def all_done(self) -> bool:
        return all(self.is_done(uid) for uid in self.current)

    def score(self, uid: int) -> float:
        return self.cashed_at[uid] if self.cashed_at[uid] is not None else 0.0


class _PVPCrashSession:
    def __init__(self, public_msg, init_user, opp_user, bet):
        self.public_msg = public_msg
        self.init_user  = init_user
        self.opp_user   = opp_user
        self.bet        = bet
        self.game       = PVPCrashGame(init_user.id, opp_user.id)
        self.joined     = {init_user.id: False, opp_user.id: False}
        self.ephemerals : dict[int, discord.Message] = {}
        self.settled    = False

    def _public_embed(self) -> discord.Embed:
        lines = [
            f'**爆點 PVP** — 各下 **{self.bet}** 咕嚕喵碎片',
            '雙方各按下方按鈕「加入」，自己的火箭/倍率只有自己看得到',
            '',
        ]
        for u in (self.init_user, self.opp_user):
            mark = '✅ 已加入' if self.joined[u.id] else '⏳ 尚未加入'
            if self.joined[u.id]:
                if self.game.crashed[u.id]:
                    status = '💥 已 BOOM'
                elif self.game.cashed_at[u.id] is not None:
                    status = '💰 已提現'
                else:
                    status = '🚀 飛行中'
                lines.append(f'{u.mention}: {mark} / {status}')
            else:
                lines.append(f'{u.mention}: {mark}')
        return discord.Embed(
            title='🚀 爆點 PVP — 進行中',
            description='\n'.join(lines), color=discord.Color.blurple(),
        )

    async def _update_public(self) -> None:
        try:
            await self.public_msg.edit(
                embed=self._public_embed(),
                view=_PVPCrashPublicView(self),
            )
        except discord.HTTPException:
            pass

    def _eph_embed(self, uid: int, *, finished: bool = False) -> discord.Embed:
        if self.game.crashed[uid]:
            return discord.Embed(
                title='🚀 你的火箭 — ' + ('結束' if finished else 'BOOM'),
                description=(
                    f'💥 BOOM @ **{self.game.current[uid]:.2f}x**\n\n'
                    + ('本局結算請看公開訊息。' if finished else '等待對方完成...')
                ),
                color=discord.Color.dark_grey(),
            )
        if self.game.cashed_at[uid] is not None:
            return discord.Embed(
                title='🚀 你的火箭 — ' + ('結束' if finished else '已提現'),
                description=(
                    f'💰 提現 @ **{self.game.cashed_at[uid]:.2f}x**\n\n'
                    + ('本局結算請看公開訊息。' if finished else '等待對方完成...')
                ),
                color=discord.Color.green(),
            )
        cur = self.game.current[uid]
        return discord.Embed(
            title='🚀 你的火箭 — 飛行中',
            description=(
                f'當前倍率：**{cur:.2f}x**\n'
                f'投注 {self.bet}　提現可得 **{int(self.bet * cur)}**\n\n'
                '按 🚀「加注」推升倍率（可能爆炸） | 按 💰「提現」鎖定'
            ),
            color=discord.Color.blurple(),
        )

    async def join(self, interaction: discord.Interaction, uid: int) -> None:
        if self.joined[uid]:
            await interaction.response.send_message(
                '你已經加入了', ephemeral=True,
            )
            return
        self.joined[uid] = True
        await interaction.response.send_message(
            embed=self._eph_embed(uid),
            view=_PVPCrashEphView(self, uid), ephemeral=True,
        )
        try:
            self.ephemerals[uid] = await interaction.original_response()
        except discord.HTTPException:
            pass
        await self._update_public()

    async def action(self, interaction: discord.Interaction,
                     uid: int, what: str) -> None:
        if self.settled:
            await interaction.response.defer()
            return
        if what == 'step':
            self.game.step_up(uid)
        else:
            self.game.cash_out(uid)

        if self.game.all_done():
            await self._finalize(interaction, uid)
            return
        await interaction.response.edit_message(
            embed=self._eph_embed(uid),
            view=_PVPCrashEphView(self, uid),
        )
        await self._update_public()

    def _reveal_str(self, uid: int) -> str:
        if self.game.crashed[uid]:
            return f'💥 BOOM @ **{self.game.current[uid]:.2f}x**'
        if self.game.cashed_at[uid] is not None:
            return f'💰 提現 @ **{self.game.cashed_at[uid]:.2f}x**'
        return '—'

    async def _finalize(self, interaction: discord.Interaction,
                        last_uid: int) -> None:
        self.settled = True
        init_s = self.game.score(self.init_user.id)
        opp_s  = self.game.score(self.opp_user.id)
        winner, mult = await _settle(
            self.init_user, self.opp_user, self.bet,
            init_s, opp_s, game_type='crash',
        )
        init_bal = get_balance(str(self.init_user.id))
        opp_bal  = get_balance(str(self.opp_user.id))
        result = _result_embed(
            self.init_user, self.opp_user, self.bet, '爆點 PVP',
            self._reveal_str(self.init_user.id),
            self._reveal_str(self.opp_user.id), winner, mult=mult,
            init_balance=init_bal, opp_balance=opp_bal,
        )
        await interaction.response.edit_message(
            embed=self._eph_embed(last_uid, finished=True), view=None,
        )
        other_uid = (self.opp_user.id if last_uid == self.init_user.id
                     else self.init_user.id)
        other_msg = self.ephemerals.get(other_uid)
        if other_msg is not None:
            try:
                await other_msg.edit(
                    embed=self._eph_embed(other_uid, finished=True),
                    view=None,
                )
            except discord.HTTPException:
                pass
        try:
            await self.public_msg.edit(
                embed=result,
                view=_again_view(self.init_user, self.opp_user, self.bet, 'crash'),
            )
        except discord.HTTPException:
            pass


class _PVPCrashPublicView(discord.ui.View):
    def __init__(self, session: _PVPCrashSession):
        super().__init__(timeout=900)
        self.session = session
        self._build()

    def _build(self) -> None:
        self.clear_items()
        for user in (self.session.init_user, self.session.opp_user):
            joined = self.session.joined[user.id]
            btn = discord.ui.Button(
                label=f'{user.display_name[:10]} 我的火箭',
                emoji=('✅' if joined else '🚀'),
                style=(discord.ButtonStyle.secondary if joined
                       else discord.ButtonStyle.primary),
                disabled=joined,
            )
            btn.callback = self._cb(user.id)
            self.add_item(btn)

    def _cb(self, owner_id: int):
        async def _do(interaction: discord.Interaction) -> None:
            if interaction.user.id != owner_id:
                await interaction.response.send_message(
                    '這不是你的火箭', ephemeral=True,
                )
                return
            await self.session.join(interaction, owner_id)
        return _do


class _PVPCrashEphView(discord.ui.View):
    def __init__(self, session: _PVPCrashSession, owner_id: int):
        super().__init__(timeout=900)
        self.session  = session
        self.owner_id = owner_id
        self._build()

    def _build(self) -> None:
        self.clear_items()
        done = self.session.game.is_done(self.owner_id)
        step = discord.ui.Button(
            label='加注', emoji='🚀',
            style=discord.ButtonStyle.primary, disabled=done,
        )
        step.callback = self._cb('step')
        self.add_item(step)
        cash = discord.ui.Button(
            label='提現', emoji='💰',
            style=discord.ButtonStyle.success, disabled=done,
        )
        cash.callback = self._cb('cash')
        self.add_item(cash)

    def _cb(self, action: str):
        async def _do(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message(
                    '這不是你的火箭', ephemeral=True,
                )
                return
            await self.session.action(interaction, self.owner_id, action)
        return _do


async def _play_pvp_crash(interaction: discord.Interaction,
                          init_user, opp_user, bet: int) -> None:
    session = _PVPCrashSession(interaction.message, init_user, opp_user, bet)
    await _smart_edit(
        interaction, content=None, embed=session._public_embed(),
        view=_PVPCrashPublicView(session),
    )


# ── 踩地雷 PVP ────────────────────────────────────────────────────────
class PVPMinesGame:
    """每位玩家各自 5×4=20 格場（地雷數可選 3/8/15），ephemeral 上手動選格揭。"""
    CELLS = 20

    def __init__(self, init_id: int, opp_id: int, mine_count: int = 3):
        self.mine_count = mine_count
        self.mines    : dict[int, set[int]] = {}
        self.revealed : dict[int, set[int]] = {}
        self.crashed  : dict[int, bool]     = {}
        self.cashed   : dict[int, bool]     = {}
        for uid in (init_id, opp_id):
            cells = list(range(self.CELLS))
            self.mines[uid]    = set(random.sample(cells, mine_count))
            self.revealed[uid] = set()
            self.crashed[uid]  = False
            self.cashed[uid]   = False

    def reveal(self, uid: int, idx: int) -> str:
        """揭指定格。回傳 'safe' / 'mine' / 'noop'。"""
        if self.is_done(uid) or idx in self.revealed[uid]:
            return 'noop'
        self.revealed[uid].add(idx)
        if idx in self.mines[uid]:
            self.crashed[uid] = True
            return 'mine'
        return 'safe'

    def safe_count(self, uid: int) -> int:
        return sum(1 for i in self.revealed[uid] if i not in self.mines[uid])

    def cash_out(self, uid: int) -> None:
        if not self.is_done(uid):
            self.cashed[uid] = True

    def is_done(self, uid: int) -> bool:
        return self.crashed[uid] or self.cashed[uid]

    def all_done(self) -> bool:
        return all(self.is_done(uid) for uid in self.mines)

    def mult(self, uid: int) -> float:
        return _m.mines_mult(self.safe_count(uid), self.mine_count)

    def score(self, uid: int) -> float:
        return self.mult(uid) if self.cashed[uid] else 0.0


class _PVPMinesSession:
    def __init__(self, public_msg, init_user, opp_user, bet, mine_count=3):
        self.public_msg = public_msg
        self.init_user  = init_user
        self.opp_user   = opp_user
        self.bet        = bet
        self.game       = PVPMinesGame(init_user.id, opp_user.id, mine_count)
        self.joined     = {init_user.id: False, opp_user.id: False}
        self.ephemerals : dict[int, discord.Message] = {}
        self.settled    = False

    def _public_embed(self) -> discord.Embed:
        lines = [
            f'**踩地雷 PVP** — 各下 **{self.bet}** 咕嚕喵碎片',
            f'地雷 {self.game.mine_count} 顆 / 20 格手動選格揭，每人自己的場只有自己看得到',
            '',
        ]
        for u in (self.init_user, self.opp_user):
            mark = '✅ 已加入' if self.joined[u.id] else '⏳ 尚未加入'
            if self.joined[u.id]:
                if self.game.crashed[u.id]:
                    status = '💥 已踩雷'
                elif self.game.cashed[u.id]:
                    status = '💰 已提現'
                else:
                    status = '❓ 揭格中'
                lines.append(f'{u.mention}: {mark} / {status}')
            else:
                lines.append(f'{u.mention}: {mark}')
        return discord.Embed(
            title='💣 踩地雷 PVP — 進行中',
            description='\n'.join(lines), color=discord.Color.blurple(),
        )

    async def _update_public(self) -> None:
        try:
            await self.public_msg.edit(
                embed=self._public_embed(),
                view=_PVPMinesPublicView(self),
            )
        except discord.HTTPException:
            pass

    def _eph_embed(self, uid: int, *, finished: bool = False) -> discord.Embed:
        n    = self.game.safe_count(uid)
        mult = self.game.mult(uid)
        if self.game.crashed[uid]:
            return discord.Embed(
                title='💣 你的場 — ' + ('結束' if finished else 'BOOM'),
                description=(
                    f'💥 BOOM（揭了 {n} 安全格 → 0x）\n\n'
                    + ('本局結算請看公開訊息。' if finished else '等待對方完成...')
                ),
                color=discord.Color.dark_grey(),
            )
        if self.game.cashed[uid]:
            return discord.Embed(
                title='💣 你的場 — ' + ('結束' if finished else '已提現'),
                description=(
                    f'💰 提現 @ **{mult:.2f}x**（{n} 安全格）\n\n'
                    + ('本局結算請看公開訊息。' if finished else '等待對方完成...')
                ),
                color=discord.Color.green(),
            )
        return discord.Embed(
            title='💣 你的場 — 揭格中',
            description=(
                f'已揭 {n} 安全格　|　當前倍率 **{mult:.2f}x**\n'
                f'投注 {self.bet}　提現可得 **{int(self.bet * mult)}**\n\n'
                '點 ❓ 揭一格 | 點 💰 提現鎖定'
            ),
            color=discord.Color.blurple(),
        )

    async def join(self, interaction: discord.Interaction, uid: int) -> None:
        if self.joined[uid]:
            await interaction.response.send_message(
                '你已經加入了', ephemeral=True,
            )
            return
        self.joined[uid] = True
        await interaction.response.send_message(
            embed=self._eph_embed(uid),
            view=_PVPMinesEphView(self, uid), ephemeral=True,
        )
        try:
            self.ephemerals[uid] = await interaction.original_response()
        except discord.HTTPException:
            pass
        await self._update_public()

    async def action(self, interaction: discord.Interaction,
                     uid: int, what: str, idx: int | None = None) -> None:
        if self.settled:
            await interaction.response.defer()
            return
        if what == 'reveal' and idx is not None:
            self.game.reveal(uid, idx)
        elif what == 'cash':
            self.game.cash_out(uid)

        if self.game.all_done():
            await self._finalize(interaction, uid)
            return
        await interaction.response.edit_message(
            embed=self._eph_embed(uid),
            view=_PVPMinesEphView(self, uid),
        )
        await self._update_public()

    def _grid_text(self, uid: int) -> str:
        """揭完整盤面（包含尚未揭開的）。"""
        game = self.game
        mines = game.mines[uid]
        revealed = game.revealed[uid]
        rows = []
        for r in range(4):
            row = []
            for c in range(5):
                idx = r * 5 + c
                if idx in revealed:
                    row.append('💥' if idx in mines else '🦺')
                else:
                    row.append('💣' if idx in mines else '⬜')
            rows.append(' '.join(row))
        return '\n'.join(rows)

    def _reveal_str(self, uid: int) -> str:
        n    = self.game.safe_count(uid)
        mult = self.game.mult(uid)
        if self.game.crashed[uid]:
            head = f'💥 BOOM（{n} 安全格 → 0x）'
        elif self.game.cashed[uid]:
            head = f'💰 提現 @ **{mult:.2f}x**（{n} 安全格）'
        else:
            head = '—'
        return f'{head}\n{self._grid_text(uid)}'

    async def _finalize(self, interaction: discord.Interaction,
                        last_uid: int) -> None:
        self.settled = True
        init_s = self.game.score(self.init_user.id)
        opp_s  = self.game.score(self.opp_user.id)
        winner, mult = await _settle(
            self.init_user, self.opp_user, self.bet,
            init_s, opp_s, game_type='mines',
        )
        init_bal = get_balance(str(self.init_user.id))
        opp_bal  = get_balance(str(self.opp_user.id))
        result = _result_embed(
            self.init_user, self.opp_user, self.bet, '踩地雷 PVP',
            self._reveal_str(self.init_user.id),
            self._reveal_str(self.opp_user.id), winner, mult=mult,
            init_balance=init_bal, opp_balance=opp_bal,
        )
        await interaction.response.edit_message(
            embed=self._eph_embed(last_uid, finished=True), view=None,
        )
        other_uid = (self.opp_user.id if last_uid == self.init_user.id
                     else self.init_user.id)
        other_msg = self.ephemerals.get(other_uid)
        if other_msg is not None:
            try:
                await other_msg.edit(
                    embed=self._eph_embed(other_uid, finished=True),
                    view=None,
                )
            except discord.HTTPException:
                pass
        try:
            await self.public_msg.edit(
                embed=result,
                view=_again_view(
                    self.init_user, self.opp_user, self.bet, 'mines',
                    extra={'mine_count': self.game.mine_count},
                ),
            )
        except discord.HTTPException:
            pass


class _PVPMinesPublicView(discord.ui.View):
    def __init__(self, session: _PVPMinesSession):
        super().__init__(timeout=900)
        self.session = session
        self._build()

    def _build(self) -> None:
        self.clear_items()
        for user in (self.session.init_user, self.session.opp_user):
            joined = self.session.joined[user.id]
            btn = discord.ui.Button(
                label=f'{user.display_name[:10]} 我的場',
                emoji=('✅' if joined else '💣'),
                style=(discord.ButtonStyle.secondary if joined
                       else discord.ButtonStyle.primary),
                disabled=joined,
            )
            btn.callback = self._cb(user.id)
            self.add_item(btn)

    def _cb(self, owner_id: int):
        async def _do(interaction: discord.Interaction) -> None:
            if interaction.user.id != owner_id:
                await interaction.response.send_message(
                    '這不是你的場', ephemeral=True,
                )
                return
            await self.session.join(interaction, owner_id)
        return _do


class _PVPMinesEphView(discord.ui.View):
    """私訊：5×4 = 20 個格子（row 0~3）+ 第 5 列 1 顆 💰 提現。"""

    def __init__(self, session: _PVPMinesSession, owner_id: int):
        super().__init__(timeout=900)
        self.session  = session
        self.owner_id = owner_id
        self._build()

    def _build(self) -> None:
        self.clear_items()
        game = self.session.game
        done = game.is_done(self.owner_id)
        revealed = game.revealed[self.owner_id]
        mines    = game.mines[self.owner_id]
        for idx in range(PVPMinesGame.CELLS):
            row = idx // 5
            if idx in revealed:
                if idx in mines:
                    btn = discord.ui.Button(
                        emoji='💥', style=discord.ButtonStyle.danger,
                        row=row, disabled=True,
                    )
                else:
                    btn = discord.ui.Button(
                        emoji='🦺', style=discord.ButtonStyle.success,
                        row=row, disabled=True,
                    )
            else:
                btn = discord.ui.Button(
                    emoji='❓', style=discord.ButtonStyle.secondary,
                    row=row, disabled=done,
                )
                btn.callback = self._reveal_cb(idx)
            self.add_item(btn)
        # row 4：提現
        cash = discord.ui.Button(
            label='提現', emoji='💰',
            style=discord.ButtonStyle.success,
            row=4, disabled=done,
        )
        cash.callback = self._cash_cb()
        self.add_item(cash)

    def _reveal_cb(self, idx: int):
        async def _do(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message(
                    '這不是你的場', ephemeral=True,
                )
                return
            await self.session.action(
                interaction, self.owner_id, 'reveal', idx,
            )
        return _do

    def _cash_cb(self):
        async def _do(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message(
                    '這不是你的場', ephemeral=True,
                )
                return
            await self.session.action(
                interaction, self.owner_id, 'cash',
            )
        return _do


_PVP_MINES_DIFFICULTIES = (3, 8, 15)


class _PVPMinesDifficultyView(discord.ui.View):
    """對手選擇地雷數（3/8/15）後才正式開局。"""

    def __init__(self, init_user, opp_user, bet: int):
        super().__init__(timeout=300)
        self.init_user = init_user
        self.opp_user  = opp_user
        self.bet       = bet
        for n in _PVP_MINES_DIFFICULTIES:
            btn = discord.ui.Button(
                label=f'{n} 顆地雷', emoji='💣',
                style=discord.ButtonStyle.primary,
            )
            btn.callback = self._cb(n)
            self.add_item(btn)

    def _cb(self, mine_count: int):
        async def _do(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.init_user.id:
                await interaction.response.send_message(
                    '由發起者選擇地雷數', ephemeral=True,
                )
                return
            await _start_pvp_mines(
                interaction, self.init_user, self.opp_user,
                self.bet, mine_count,
            )
            self.stop()
        return _do


async def _start_pvp_mines(interaction: discord.Interaction,
                           init_user, opp_user, bet: int,
                           mine_count: int) -> None:
    """正式開 PVP 踩地雷局（不再顯示難度選擇）。"""
    session = _PVPMinesSession(
        interaction.message, init_user, opp_user, bet, mine_count,
    )
    await _smart_edit(
        interaction, content=None, embed=session._public_embed(),
        view=_PVPMinesPublicView(session),
    )


async def _play_pvp_minesweeper(interaction: discord.Interaction,
                                init_user, opp_user, bet: int) -> None:
    """對手點「踩地雷」後跳到難度選擇頁。"""
    embed = discord.Embed(
        title='💣 踩地雷 PVP — 選擇難度',
        description=(
            f'**對戰**：{init_user.mention} vs {opp_user.mention}\n'
            f'下注：**{bet}** 咕嚕喵碎片（雙方已扣）\n\n'
            f'{init_user.mention}（發起者）請選擇本局地雷數量：'
        ),
        color=discord.Color.blurple(),
    )
    view = _PVPMinesDifficultyView(init_user, opp_user, bet)
    await _smart_edit(interaction, content=None, embed=embed, view=view)


class PVPInviteView(discord.ui.View):
    """對手按按鈕決定遊戲類型或拒絕。"""

    def __init__(self, init_user: discord.abc.User,
                 opp_user: discord.abc.User, bet: int):
        super().__init__(timeout=600)  # 10 分鐘給對手反應
        self.init_user = init_user
        self.opp_user  = opp_user
        self.bet       = bet
        self.settled   = False

    def _disable_all(self) -> None:
        for c in self.children:
            if isinstance(c, discord.ui.Button):
                c.disabled = True

    async def _gate(self, interaction: discord.Interaction) -> bool:
        if self.settled:
            await interaction.response.send_message(
                '本局已結算', ephemeral=True,
            )
            return False
        if interaction.user.id != self.opp_user.id:
            await interaction.response.send_message(
                '這不是給你的挑戰', ephemeral=True,
            )
            return False
        return True

    async def _accept_check_balance(self, interaction: discord.Interaction) -> bool:
        opp_bal = get_balance(str(self.opp_user.id))
        if opp_bal < self.bet:
            # 對手餘額不夠，退發起者本金
            await apply_delta(str(self.init_user.id), self.bet)
            self._disable_all()
            self.settled = True
            await _smart_edit(
                interaction, content=None,
                embed=discord.Embed(
                    title='⚠️ 對手沒有足夠的咕嚕喵碎片',
                    description=(
                        f'{self.opp_user.mention} 餘額 {opp_bal} < {self.bet}\n'
                        f'{self.init_user.mention} 的本注已退回'
                    ),
                    color=discord.Color.red(),
                ),
                view=self,
            )
            self.stop()
            return False
        # 扣對手本注
        await apply_delta(str(self.opp_user.id), -self.bet)
        self.settled = True
        return True

    @discord.ui.button(label='比大小', emoji='🃏',
                       style=discord.ButtonStyle.success, row=0)
    async def card_btn(self, interaction: discord.Interaction,
                       button: discord.ui.Button) -> None:
        if not await self._gate(interaction):
            return
        await interaction.response.defer()
        if not await self._accept_check_balance(interaction):
            return
        await _play_compare_card(
            interaction, self.init_user, self.opp_user, self.bet,
        )
        self.stop()

    @discord.ui.button(label='骰子', emoji='🎲',
                       style=discord.ButtonStyle.success, row=0)
    async def dice_btn(self, interaction: discord.Interaction,
                       button: discord.ui.Button) -> None:
        if not await self._gate(interaction):
            return
        await interaction.response.defer()
        if not await self._accept_check_balance(interaction):
            return
        await _play_compare_dice(
            interaction, self.init_user, self.opp_user, self.bet,
        )
        self.stop()

    @discord.ui.button(label='老虎機', emoji='🎰',
                       style=discord.ButtonStyle.success, row=0)
    async def slot_btn(self, interaction: discord.Interaction,
                       button: discord.ui.Button) -> None:
        if not await self._gate(interaction):
            return
        await interaction.response.defer()
        if not await self._accept_check_balance(interaction):
            return
        await _play_compare_slot(
            interaction, self.init_user, self.opp_user, self.bet,
        )
        self.stop()

    @discord.ui.button(label='輪盤', emoji='🎡',
                       style=discord.ButtonStyle.success, row=0)
    async def roulette_btn(self, interaction: discord.Interaction,
                           button: discord.ui.Button) -> None:
        if not await self._gate(interaction):
            return
        await interaction.response.defer()
        if not await self._accept_check_balance(interaction):
            return
        await _play_compare_roulette(
            interaction, self.init_user, self.opp_user, self.bet,
        )
        self.stop()

    @discord.ui.button(label='21 點', emoji='🃏',
                       style=discord.ButtonStyle.primary, row=1)
    async def bj_btn(self, interaction: discord.Interaction,
                     button: discord.ui.Button) -> None:
        if not await self._gate(interaction):
            return
        await interaction.response.defer()
        if not await self._accept_check_balance(interaction):
            return
        await _play_pvp_blackjack(
            interaction, self.init_user, self.opp_user, self.bet,
        )
        self.stop()

    @discord.ui.button(label='爆點', emoji='🚀',
                       style=discord.ButtonStyle.primary, row=1)
    async def crash_btn(self, interaction: discord.Interaction,
                        button: discord.ui.Button) -> None:
        if not await self._gate(interaction):
            return
        await interaction.response.defer()
        if not await self._accept_check_balance(interaction):
            return
        await _play_pvp_crash(
            interaction, self.init_user, self.opp_user, self.bet,
        )
        self.stop()

    @discord.ui.button(label='踩地雷', emoji='💣',
                       style=discord.ButtonStyle.primary, row=1)
    async def mines_btn(self, interaction: discord.Interaction,
                        button: discord.ui.Button) -> None:
        if not await self._gate(interaction):
            return
        await interaction.response.defer()
        if not await self._accept_check_balance(interaction):
            return
        await _play_pvp_minesweeper(
            interaction, self.init_user, self.opp_user, self.bet,
        )
        self.stop()

    @discord.ui.button(label='拒絕', emoji='❌',
                       style=discord.ButtonStyle.danger, row=2)
    async def reject_btn(self, interaction: discord.Interaction,
                         button: discord.ui.Button) -> None:
        if not await self._gate(interaction):
            return
        await interaction.response.defer()
        # 退發起者本注
        await apply_delta(str(self.init_user.id), self.bet)
        self.settled = True
        self._disable_all()
        await _smart_edit(
            interaction, content=None,
            embed=discord.Embed(
                title='❌ 對手拒絕挑戰',
                description=(
                    f'{self.opp_user.mention} 拒絕了 PVP\n'
                    f'{self.init_user.mention} 的 {self.bet} 咕嚕喵碎片已退回'
                ),
                color=discord.Color.dark_grey(),
            ),
            view=self,
        )
        self.stop()

    async def on_timeout(self) -> None:
        # 對手沒回應：退發起者本金（不寫 message，因為 view 已停）
        if not self.settled:
            await apply_delta(str(self.init_user.id), self.bet)


DEFAULT_BET = 500


def _setup_embed(user: discord.abc.User, opponent: discord.Member,
                 bet: int) -> discord.Embed:
    body = [
        f'**PVP 對戰**：你要向 {opponent.mention} 發起挑戰',
        '',
        f'下注金額：**{bet}** 咕嚕喵碎片（雙方各下）',
        f'勝者拿 **{bet * 2}**，平手雙方退本，對手拒絕退本。',
        '',
        '選好金額後按下方 ⚔️「發起挑戰」送出邀請。',
    ]
    embed = discord.Embed(
        title='⚔️ PVP 對戰 — 開局設定',
        description='\n'.join(body),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text=user.display_name)
    return embed


class PVPSetupView(discord.ui.View):
    def __init__(self, uid: str, opponent: discord.Member,
                 bet: int = DEFAULT_BET):
        super().__init__(timeout=300)
        self.uid      = uid
        self.opponent = opponent
        self.bet      = bet
        self._build()

    def _build(self) -> None:
        self.clear_items()
        for btn in make_bet_row(self, self._redraw, row=0):
            self.add_item(btn)
        start = discord.ui.Button(
            label='發起挑戰', emoji='⚔️',
            style=discord.ButtonStyle.primary, row=1,
        )
        start.callback = self._start_cb
        self.add_item(start)

    async def _redraw(self, interaction: discord.Interaction) -> None:
        self._build()
        await interaction.response.edit_message(
            embed=_setup_embed(interaction.user, self.opponent, self.bet),
            view=self,
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
        # 收掉 ephemeral 設定畫面，再公開送出邀請
        await interaction.response.defer()
        await interaction.delete_original_response()
        self.stop()
        await _send_invite(interaction, self.opponent, self.bet)


async def _send_invite(interaction: discord.Interaction,
                       opponent: discord.Member, bet: int) -> None:
    """公開發送 PVP 邀請給對手，並預扣發起者本注。"""
    init_uid = str(interaction.user.id)
    # 二次驗證餘額（setup 結束到送出之間可能變化）
    bal = get_balance(init_uid)
    if bal < bet:
        await send_smart(
            interaction, embed=insufficient_embed(bet, bal),
        )
        return
    await apply_delta(init_uid, -bet)

    embed = discord.Embed(
        title='⚔️ PVP 賭盤邀請',
        description=(
            f'{interaction.user.mention} 向 {opponent.mention} 發起挑戰！\n\n'
            f'下注金額：**{bet}** 咕嚕喵碎片（雙方各下）\n'
            f'勝者拿 **{bet * 2}**，平手雙方退本。\n\n'
            f'{opponent.mention}，請選擇遊戲：'
        ),
        color=discord.Color.gold(),
    )
    view = PVPInviteView(interaction.user, opponent, bet)
    await send_smart(
        interaction, content=opponent.mention,
        embed=embed, view=view,
    )


async def start_setup(interaction: discord.Interaction,
                      opponent: discord.Member) -> None:
    """由 /賭博 對戰:user 派遣呼叫。送 ephemeral 設定畫面給發起者。"""
    if interaction.guild is None:
        await interaction.response.send_message(
            '此指令只能在伺服器中使用', ephemeral=True,
        )
        return
    if opponent.bot:
        await interaction.response.send_message(
            '不能對戰機器人', ephemeral=True,
        )
        return
    if opponent.id == interaction.user.id:
        await interaction.response.send_message(
            '不能對戰自己', ephemeral=True,
        )
        return

    view = PVPSetupView(str(interaction.user.id), opponent)
    await interaction.response.send_message(
        embed=_setup_embed(interaction.user, opponent, DEFAULT_BET),
        view=view, ephemeral=True,
    )
