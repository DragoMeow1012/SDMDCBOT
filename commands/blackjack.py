"""
21 點（Blackjack），由 /賭博 派遣呼叫。

規則：
  - 6 副牌 shoe，每局重洗（不做牌算）
  - 莊家 stand on 17（含 soft 17）
  - 玩家動作：Hit / Stand / Double / Split
  - Double：僅初始 2 張時可選，加倍下注、發 1 張、自動停牌
  - Split：初始 2 張同點數（10/J/Q/K 算同）可拆。最多拆 1 次（無 re-split）
  - Split A：每手再發 1 張、自動停牌、不算 Blackjack（但 21 點仍算 21）
  - Blackjack（初始 A + 10 點）3:2 賠付；split 後拿到 21 不算 BJ
  - Bust：立即輸；Push：本金歸還
  - 五龍：玩家手 5 張且不爆牌 → 自動勝（無視莊家），2:1 賠付（取回 3× bet）

UI：
  - 玩家階段：Hit / Stand / Double / Split 4 個按鈕（不能用的會 disable）
  - 結算：莊家掀牌→補牌→顯示每手結果與淨損益→「再來一局」按鈕

對外入口：start_setup(interaction, bet=500, options=None)
"""
from __future__ import annotations

import random
from typing import Any

import discord

from commands._setup import (
    EndOfGameView, insufficient_embed, make_bet_row, tier_label,
)
from commands._wallet import (
    apply_delta, get_balance, send_or_edit, send_smart,
    settle_with_streak, streak_line,
)


GAME_NAME   = '21 點 (Blackjack)'
RULES_TEXT  = (
    '**遊戲規則：**\n'
    '6 副牌、莊家 stand on 17。\n'
    '玩家動作：要牌 / 停牌 / 加倍 / 分牌 / 投降。\n'
    'Blackjack（初始 A + 10/J/Q/K）3:2；分牌後拿到 21 不算 BJ。\n'
    '**特殊牌型**：🐲 五小 (5 張全 2-5) 5:1、🐉 五龍 (5 張不爆) 2:1、'
    '🎰 三七 (3 張全 7) 3:1。\n'
    '**投降**：初始 2 張可投降退一半；'
    '**保險**：莊家明牌 A 可下半本，命中 2:1。\n'
    '**21+3 邊注**（可選，半本）：莊家首張 + 玩家前 2 張組成撲克牌型 → '
    '順 10:1 / 同花 5:1 / 三條 25:1 / 同花順 25:1 / 同花三條 50:1。'
)
DEFAULT_BET = 500


_RANKS     = ['A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K']
_TEN_RANKS = {'10', 'J', 'Q', 'K'}
_SUITS     = ['♠', '♥', '♦', '♣']
_DECKS     = 6


def _rank(card: str) -> str:
    """從 'A♠' / '10♥' 取出 rank 字串。"""
    return card[:-1]


def _suit(card: str) -> str:
    return card[-1]


def _fresh_shoe() -> list[str]:
    shoe = [r + s for r in _RANKS for s in _SUITS] * _DECKS
    random.shuffle(shoe)
    return shoe


def _hand_value(cards: list[str]) -> int:
    """回傳不爆牌前提下最大點數；若無法不爆，回傳最小爆牌點數。"""
    total = 0
    aces  = 0
    for c in cards:
        r = _rank(c)
        if r == 'A':
            aces  += 1
            total += 11
        elif r in _TEN_RANKS:
            total += 10
        else:
            total += int(r)
    while total > 21 and aces:
        total -= 10
        aces  -= 1
    return total


def _is_bust(cards: list[str]) -> bool:
    return _hand_value(cards) > 21


def _is_blackjack(cards: list[str]) -> bool:
    return len(cards) == 2 and _hand_value(cards) == 21


def _cards_str(cards: list[str], hide_first: bool = False) -> str:
    """`A♠` `10♥` `K♦` 這樣，每張牌一個 inline code box。"""
    shown = ['🂠'] + cards[1:] if hide_first else cards
    return ' '.join(f'`{c}`' for c in shown)


# ── 21+3 邊注（莊家首張 + 玩家前兩張 = 3 張撲克組合） ─────────────────
_RANK_ORDER = {'A': 1, '2': 2, '3': 3, '4': 4, '5': 5, '6': 6, '7': 7,
               '8': 8, '9': 9, '10': 10, 'J': 11, 'Q': 12, 'K': 13}


def _detect_21_3(player_cards: list[str],
                 dealer_first: str) -> tuple[str, int] | None:
    """偵測 21+3 牌型。回傳 (牌型名, 賠率)；無 → None。
    payout 採取「X:1 即 win = X * side_bet」的 X 值。
    """
    cards = [dealer_first, player_cards[0], player_cards[1]]
    ranks = [_rank(c) for c in cards]
    suits = [_suit(c) for c in cards]
    same_suit = len(set(suits)) == 1
    same_rank = len(set(ranks)) == 1

    vals = sorted(_RANK_ORDER[r] for r in ranks)
    is_straight = vals[1] == vals[0] + 1 and vals[2] == vals[1] + 1
    # A 可當 14：Q-K-A → 12,13,14
    if 'A' in ranks:
        vals_h = sorted(14 if r == 'A' else _RANK_ORDER[r] for r in ranks)
        if vals_h[1] == vals_h[0] + 1 and vals_h[2] == vals_h[1] + 1:
            is_straight = True

    if same_suit and same_rank:
        return ('🌟 同花三條', 50)
    if same_suit and is_straight:
        return ('💎 同花順', 25)
    if same_rank:
        return ('🎴 三條', 25)
    if same_suit:
        return ('🟦 同花', 5)
    if is_straight:
        return ('🔢 順', 10)
    return None


class BlackjackGame:
    def __init__(self, bet: int, side_21_3: int = 0):
        self.shoe   = _fresh_shoe()
        self.dealer = [self.shoe.pop(), self.shoe.pop()]
        self.hands  = [{
            'cards':   [self.shoe.pop(), self.shoe.pop()],
            'bet':     bet,
            'stood':   False,
            'doubled': False,
            'split':   False,   # 是否來自拆牌
        }]
        self.active : int = 0
        self.phase  : str = 'player'   # 'player' | 'done'

        # 保險 / 投降
        self.insurance_taken : bool = False
        self.insurance_bet   : int  = 0
        self.surrendered     : bool = False
        self._peeked         : bool = False  # 莊家是否已掀洞牌

        # 21+3 邊注（開局決定後立即結算，與主局獨立）
        self.side_21_3_bet : int = side_21_3
        self.side_21_3_hit : tuple[str, int] | None = (
            _detect_21_3(self.hands[0]['cards'], self.dealer[0])
            if side_21_3 > 0 else None
        )

        # 規則：莊家明牌是 A 要先讓玩家決定保險再掀牌；
        # 明牌非 A 時若任一方 BJ 直接結算（標準規則）。
        if self.dealer[0] != 'A':
            self._peek_for_bj()

    @property
    def cur(self) -> dict:
        return self.hands[self.active]

    # ── 可選動作判斷 ────────────────────────
    def can_hit(self) -> bool:
        h = self.cur
        return not h['stood'] and not h['doubled']

    def can_stand(self) -> bool:
        return self.can_hit()

    def can_double(self, balance: int, base_bet: int) -> bool:
        h = self.cur
        return (
            not h['stood'] and not h['doubled']
            and len(h['cards']) == 2
            and balance >= h['bet']    # 還要再墊一注 == 當前 hand bet
        )

    def can_split(self, balance: int, base_bet: int) -> bool:
        if len(self.hands) > 1:    # 已拆過
            return False
        h = self.cur
        if len(h['cards']) != 2:
            return False
        if balance < base_bet:
            return False
        ra, rb = _rank(h['cards'][0]), _rank(h['cards'][1])
        if ra in _TEN_RANKS and rb in _TEN_RANKS:
            return True
        return ra == rb

    def can_surrender(self) -> bool:
        # 初始 2 張、第一手、未動作過才能投降
        h = self.cur
        return (
            self.phase == 'player'
            and self.active == 0
            and len(self.hands) == 1
            and len(h['cards']) == 2
            and not h['stood']
            and not h['doubled']
            and not self.surrendered
        )

    def can_insurance(self, balance: int, base_bet: int) -> bool:
        # 莊家明牌是 A、未買過保險、未動作過、餘額夠（半本）
        h = self.cur
        return (
            self.dealer[0] == 'A'
            and not self.insurance_taken
            and not self.surrendered
            and self.phase == 'player'
            and self.active == 0
            and len(self.hands) == 1
            and len(h['cards']) == 2
            and not h['stood']
            and not h['doubled']
            and balance >= base_bet // 2
        )

    # ── 玩家動作 ─────────────────────────────
    def _peek_for_bj(self) -> None:
        """掀莊家洞牌，若任一方 BJ 直接結算。動作前都要呼叫一次。"""
        if self._peeked:
            return
        self._peeked = True
        if _is_blackjack(self.hands[0]['cards']) or _is_blackjack(self.dealer):
            self.hands[0]['stood'] = True
            self._advance()

    def hit(self) -> None:
        self._peek_for_bj()
        if self.phase == 'done':
            return
        self.cur['cards'].append(self.shoe.pop())
        if _is_bust(self.cur['cards']):
            self.cur['stood'] = True
            self._advance()
            return
        if len(self.cur['cards']) >= 5:
            # 五龍：5 張不爆牌自動停牌（結算時無視莊家自動勝）
            self.cur['stood'] = True
            self._advance()

    def stand(self) -> None:
        self._peek_for_bj()
        if self.phase == 'done':
            return
        self.cur['stood'] = True
        self._advance()

    def double(self) -> int:
        """回傳此次再加扣的注額。"""
        self._peek_for_bj()
        if self.phase == 'done':
            return 0
        extra = self.cur['bet']
        self.cur['bet']    *= 2
        self.cur['doubled'] = True
        self.cur['cards'].append(self.shoe.pop())
        self.cur['stood']   = True
        self._advance()
        return extra

    def split(self, base_bet: int) -> int:
        """回傳此次再加扣的注額（= base_bet）。"""
        self._peek_for_bj()
        if self.phase == 'done':
            return 0
        h  = self.cur
        c2 = h['cards'].pop()
        h['cards'].append(self.shoe.pop())
        h['split']  = True
        hand2 = {
            'cards':   [c2, self.shoe.pop()],
            'bet':     base_bet,
            'stood':   False,
            'doubled': False,
            'split':   True,
        }
        self.hands.append(hand2)
        # split A：兩手各發 1 張後自動停牌
        if _rank(c2) == 'A':
            h['stood']     = True
            hand2['stood'] = True
            self._advance()
        return base_bet

    def surrender(self) -> int:
        """投降：所有手停牌、進入結算。回傳本注（給 view 算退一半用）。"""
        self.surrendered = True
        for h in self.hands:
            h['stood'] = True
        self.phase = 'done'
        # 不掀莊家牌（投降直接結算）
        return self.hands[0]['bet']

    def take_insurance(self, base_bet: int) -> int:
        """買保險：扣半本，並立即掀莊家洞牌結算 BJ。回傳保險注額。"""
        self.insurance_bet   = base_bet // 2
        self.insurance_taken = True
        self._peek_for_bj()
        return self.insurance_bet

    # ── 流程控制 ─────────────────────────────
    def _advance(self) -> None:
        while self.active < len(self.hands) and self.hands[self.active]['stood']:
            self.active += 1
        if self.active >= len(self.hands):
            self.active = len(self.hands) - 1
            self.phase  = 'done'
            self._play_dealer()

    def _play_dealer(self) -> None:
        # 全部玩家爆 → 莊家不用補牌
        if all(_is_bust(h['cards']) for h in self.hands):
            return
        # Dealer hits to >=17 (stand on soft 17 為簡化)
        while _hand_value(self.dealer) < 17:
            self.dealer.append(self.shoe.pop())

    # ── 結算 ─────────────────────────────────
    def results(self) -> list[tuple[str, int]]:
        """每手 (描述, 總取回 = 含本金)。輸 → 0。
        若投降 → 每手退半本；若有保險 → 額外結算一筆保險項。"""
        out: list[tuple[str, int]] = []

        # 投降：每手退半本，不掀莊家
        if self.surrendered:
            for h in self.hands:
                out.append(('🏳️ 投降', h['bet'] // 2))
            self._settle_insurance(out)
            self._settle_21_3(out)
            return out

        d_val  = _hand_value(self.dealer)
        d_bj   = _is_blackjack(self.dealer)
        d_bust = _is_bust(self.dealer)
        for h in self.hands:
            p_val       = _hand_value(h['cards'])
            p_bj        = _is_blackjack(h['cards']) and not h['split']
            five_dragon = len(h['cards']) >= 5 and not _is_bust(h['cards'])
            five_small  = (
                len(h['cards']) == 5
                and not _is_bust(h['cards'])
                and all(_rank(c) in {'2', '3', '4', '5'} for c in h['cards'])
            )
            three_sevens = (
                len(h['cards']) == 3
                and all(_rank(c) == '7' for c in h['cards'])
            )
            bet = h['bet']

            if _is_bust(h['cards']):
                out.append(('爆牌', 0))
            elif five_small:
                # 五小：5 張全 2-5 不爆 → 5:1
                out.append(('🐲 五小 5:1', bet * 6))
            elif five_dragon:
                # 五龍：5 張不爆 → 2:1
                out.append(('🐉 五龍 2:1', bet * 3))
            elif three_sevens:
                # 三七：3 張全 7（= 21 點）→ 3:1
                out.append(('🎰 三七 3:1', bet * 4))
            elif p_bj and not d_bj:
                out.append(('Blackjack! 3:2', bet + bet * 3 // 2))
            elif p_bj and d_bj:
                out.append(('雙方 Blackjack → 和', bet))
            elif d_bj:
                out.append(('莊家 Blackjack', 0))
            elif d_bust:
                out.append(('莊家爆牌', bet * 2))
            elif p_val > d_val:
                out.append(('贏', bet * 2))
            elif p_val == d_val:
                out.append(('和', bet))
            else:
                out.append(('輸', 0))

        self._settle_insurance(out)
        self._settle_21_3(out)
        return out

    def _settle_insurance(self, out: list[tuple[str, int]]) -> None:
        if not self.insurance_taken:
            return
        if _is_blackjack(self.dealer):
            # 保險 2:1 → 取回 3× 保險注額
            out.append(('🛡️ 保險命中 2:1', self.insurance_bet * 3))
        else:
            out.append(('🛡️ 保險落空', 0))

    def _settle_21_3(self, out: list[tuple[str, int]]) -> None:
        if self.side_21_3_bet <= 0:
            return
        if self.side_21_3_hit is None:
            out.append(('🃏 21+3 落空', 0))
            return
        name, mult = self.side_21_3_hit
        # X:1 賠付 → 取回 (X+1) × side_bet
        out.append((f'🃏 21+3 {name} {mult}:1', self.side_21_3_bet * (mult + 1)))


def _build_embed(user: discord.abc.User, game: BlackjackGame,
                 committed: int, payout: int | None,
                 results: list[tuple[str, int]] | None,
                 balance: int, streak: int = 0,
                 bonus: int = 0) -> discord.Embed:
    hide = (game.phase == 'player')
    dealer_str = _cards_str(game.dealer, hide_first=hide)
    dealer_val = '?' if hide else str(_hand_value(game.dealer))

    lines = [f'**莊家**：{dealer_str}　（{dealer_val} 點）', '']
    for i, h in enumerate(game.hands):
        prefix = '➡️ ' if game.phase == 'player' and i == game.active else '　'
        tags = []
        if h['doubled']: tags.append('加倍')
        if h['split']:   tags.append('分牌')
        if len(h['cards']) >= 5 and not _is_bust(h['cards']):
            tags.append('🐉 五龍')
        tag_str = f'　[{" / ".join(tags)}]' if tags else ''
        val = _hand_value(h['cards'])
        bust = '（爆）' if _is_bust(h['cards']) else f'（{val} 點）'
        lines.append(
            f"{prefix}**玩家手 {i+1}**：{_cards_str(h['cards'])}　{bust}"
            f"　下注 {h['bet']}{tag_str}"
        )
        if results is not None and i < len(results):
            desc, take = results[i]
            net = take - h['bet']
            sign = f'+{net}' if net > 0 else str(net)
            lines.append(f'　　→ {desc} (`{sign}`)')

    # 旁注狀態（保險、21+3）
    extra_lines = []
    if game.insurance_taken:
        extra_lines.append(f'🛡️ 保險已下：{game.insurance_bet}')
    if game.side_21_3_bet > 0:
        s21 = ('未中' if game.side_21_3_hit is None
               else f'命中 {game.side_21_3_hit[0]} ({game.side_21_3_hit[1]}:1)')
        extra_lines.append(f'🃏 21+3 邊注：{game.side_21_3_bet}（{s21}）')
    if extra_lines:
        lines += ['', *extra_lines]

    # 額外結果行（保險 / 21+3 → 出現在 results 末段）
    main_hand_count = len(game.hands)
    if results is not None and len(results) > main_hand_count:
        for desc, take in results[main_hand_count:]:
            sign = f'+{take}' if take > 0 else '0'
            lines.append(f'　　→ {desc} (`{sign}`)')

    if game.phase == 'player':
        lines += ['', f'目前已下：**{committed}** 咕嚕喵碎片']
        title  = '🃏 21 點 — 出牌中'
        color  = discord.Color.blurple()
    else:
        assert payout is not None
        _, color = tier_label(payout, committed)
        net  = payout - committed + bonus
        sign = f'+{net}' if net >= 0 else str(net)
        title = f'🃏 21 點　{sign}'
        lines += ['', f'總投注 {committed}　取回 {payout}　淨 **{sign}**']
        sl = streak_line(streak, bonus)
        if sl:
            lines.append(sl)
        lines.append(f'餘額：**{balance}** 咕嚕喵碎片')

    embed = discord.Embed(title=title, description='\n'.join(lines), color=color)
    embed.set_footer(text=user.display_name)
    return embed


class BlackjackView(discord.ui.View):
    """21 點互動 View：Hit / Stand / Double / Split 四個按鈕，依狀態 disable。"""

    def __init__(self, uid: str, base_bet: int, game: BlackjackGame,
                 committed: int):
        super().__init__(timeout=86400)
        self.uid       = uid
        self.base_bet  = base_bet     # 起手下注（split / double 都加扣這個基準）
        self.game      = game
        self.committed = committed    # 累積已扣的注額
        self._rebuild()

    def _check_owner(self, interaction: discord.Interaction) -> bool:
        return str(interaction.user.id) == self.uid

    async def _deny(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            '這不是你的牌局', ephemeral=True,
        )

    def _rebuild(self) -> None:
        self.clear_items()
        if self.game.phase != 'player':
            return
        bal = get_balance(self.uid)
        # row 0：主要動作（最多 5 顆）
        row0 = [
            ('要牌',  '🃏', self._hit,       not self.game.can_hit()),
            ('停牌',  '✋', self._stand,     not self.game.can_stand()),
            ('加倍',  '⏫', self._double,    not self.game.can_double(bal, self.base_bet)),
            ('分牌',  '✂️', self._split,     not self.game.can_split(bal, self.base_bet)),
            ('投降',  '🏳️', self._surrender, not self.game.can_surrender()),
        ]
        for label, emoji, cb, disabled in row0:
            btn = discord.ui.Button(
                label=label, emoji=emoji,
                style=discord.ButtonStyle.primary,
                disabled=disabled, row=0,
            )
            btn.callback = self._wrap(cb)
            self.add_item(btn)
        # row 1：保險（莊家明牌是 A 才出現）
        if self.game.can_insurance(bal, self.base_bet):
            ins_amount = self.base_bet // 2
            ins_btn = discord.ui.Button(
                label=f'保險 ({ins_amount})', emoji='🛡️',
                style=discord.ButtonStyle.secondary, row=1,
            )
            ins_btn.callback = self._wrap(self._insurance)
            self.add_item(ins_btn)

    def _wrap(self, fn):
        async def _w(interaction: discord.Interaction) -> None:
            if not self._check_owner(interaction):
                await self._deny(interaction)
                return
            await fn(interaction)
        return _w

    async def _hit(self, interaction: discord.Interaction) -> None:
        self.game.hit()
        await self._redraw(interaction)

    async def _stand(self, interaction: discord.Interaction) -> None:
        self.game.stand()
        await self._redraw(interaction)

    async def _double(self, interaction: discord.Interaction) -> None:
        extra = self.game.double()
        await apply_delta(self.uid, -extra)
        self.committed += extra
        await self._redraw(interaction)

    async def _split(self, interaction: discord.Interaction) -> None:
        extra = self.game.split(self.base_bet)
        await apply_delta(self.uid, -extra)
        self.committed += extra
        await self._redraw(interaction)

    async def _surrender(self, interaction: discord.Interaction) -> None:
        self.game.surrender()
        await self._redraw(interaction)

    async def _insurance(self, interaction: discord.Interaction) -> None:
        amount = self.game.take_insurance(self.base_bet)
        await apply_delta(self.uid, -amount)
        self.committed += amount
        # 買完保險後莊家就掀牌（take_insurance 內已 peek）
        await self._redraw(interaction)

    async def _redraw(self, interaction: discord.Interaction) -> None:
        if self.game.phase == 'done':
            results = self.game.results()
            payout  = sum(p for _, p in results)
            # 本金已扣（committed），payout 為總取回。連勝以 committed 為 bet 比較。
            balance, streak, bonus = await settle_with_streak(
                self.uid, self.committed, payout, deducted=True,
            )
            embed = _build_embed(
                interaction.user, self.game, self.committed,
                payout, results, balance, streak, bonus,
            )
            end_view = EndOfGameView(
                self.uid, self.base_bet,
                {'side_21_3': self.game.side_21_3_bet > 0},
                run_round, start_setup,
            )
            await interaction.response.edit_message(embed=embed, view=end_view)
            self.stop()
        else:
            self._rebuild()
            embed = _build_embed(
                interaction.user, self.game, self.committed,
                None, None, get_balance(self.uid),
            )
            await interaction.response.edit_message(embed=embed, view=self)


def _setup_embed(user: discord.abc.User, bet: int,
                 side_21_3: bool) -> discord.Embed:
    side_21_3_amount = bet // 2 if side_21_3 else 0
    body = [
        RULES_TEXT, '',
        f'目前下注：**{bet}** 咕嚕喵碎片',
        f'21+3 邊注：**{"已下 " + str(side_21_3_amount) if side_21_3 else "未下"}**',
        '',
        '按下方 🃏「發牌」開始遊戲。',
    ]
    embed = discord.Embed(
        title=f'🃏 {GAME_NAME} — 開局設定',
        description='\n'.join(body), color=discord.Color.blurple(),
    )
    embed.set_footer(text=user.display_name)
    return embed


class BlackjackSetupView(discord.ui.View):
    def __init__(self, uid: str, bet: int = DEFAULT_BET,
                 side_21_3: bool = False):
        super().__init__(timeout=86400)
        self.uid       = uid
        self.bet       = bet
        self.side_21_3 = side_21_3
        self._build()

    def _build(self) -> None:
        self.clear_items()
        for btn in make_bet_row(self, self._redraw, row=0):
            self.add_item(btn)
        # 21+3 邊注切換
        toggle = discord.ui.Button(
            label=f'21+3 邊注 ({"ON" if self.side_21_3 else "OFF"})',
            emoji='🃏', row=1,
            style=(discord.ButtonStyle.success if self.side_21_3
                   else discord.ButtonStyle.secondary),
        )
        toggle.callback = self._toggle_cb
        self.add_item(toggle)
        # 發牌
        start = discord.ui.Button(
            label='發牌', emoji='🎴',
            style=discord.ButtonStyle.primary, row=1,
        )
        start.callback = self._start_cb
        self.add_item(start)

    async def _toggle_cb(self, interaction: discord.Interaction) -> None:
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message(
                '這不是你的賭局', ephemeral=True,
            )
            return
        self.side_21_3 = not self.side_21_3
        await self._redraw(interaction)

    async def _redraw(self, interaction: discord.Interaction) -> None:
        self._build()
        await interaction.response.edit_message(
            embed=_setup_embed(interaction.user, self.bet, self.side_21_3),
            view=self,
        )

    async def _start_cb(self, interaction: discord.Interaction) -> None:
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message(
                '這不是你的賭局', ephemeral=True,
            )
            return
        # 主注 + 21+3 邊注（若有）= 開局總扣費
        side = self.bet // 2 if self.side_21_3 else 0
        total_need = self.bet + side
        balance = get_balance(self.uid)
        if balance < total_need:
            await interaction.response.edit_message(
                embed=insufficient_embed(total_need, balance), view=None,
            )
            self.stop()
            return
        await interaction.response.defer()
        await interaction.delete_original_response()
        self.stop()
        await run_round(
            interaction, self.bet,
            {'side_21_3': self.side_21_3},
        )


# ── 對外入口 ─────────────────────────────────────────────────────────────
async def start_setup(interaction: discord.Interaction,
                      bet: int = DEFAULT_BET,
                      options: dict[str, Any] | None = None) -> None:
    options   = options or {}
    bet       = max(100, int(bet))
    side_21_3 = bool(options.get('side_21_3', False))
    view = BlackjackSetupView(
        str(interaction.user.id), bet=bet, side_21_3=side_21_3,
    )
    await send_smart(
        interaction,
        embed=_setup_embed(interaction.user, bet, side_21_3),
        view=view, ephemeral=True,
    )


async def run_round(interaction: discord.Interaction, bet: int,
                    options: dict[str, Any], *, edit: bool = False) -> None:
    uid       = str(interaction.user.id)
    side_21_3 = bool(options.get('side_21_3', False))
    side_bet  = bet // 2 if side_21_3 else 0
    total     = bet + side_bet
    balance   = get_balance(uid)
    if balance < total:
        await send_or_edit(
            interaction, edit=edit,
            embed=insufficient_embed(total, balance),
            **({'view': None} if edit else {}),
        )
        return
    await apply_delta(uid, -total)
    game      = BlackjackGame(bet, side_21_3=side_bet)
    committed = total

    if game.phase == 'done':
        results = game.results()
        payout  = sum(p for _, p in results)
        balance, streak, bonus = await settle_with_streak(
            uid, committed, payout, deducted=True,
        )
        embed = _build_embed(
            interaction.user, game, committed, payout, results, balance,
            streak, bonus,
        )
        view = EndOfGameView(
            uid, bet, {'side_21_3': side_21_3},
            run_round, start_setup,
        )
    else:
        embed = _build_embed(
            interaction.user, game, committed, None, None,
            get_balance(uid),
        )
        view = BlackjackView(uid, bet, game, committed)

    await send_or_edit(interaction, edit=edit, embed=embed, view=view)
