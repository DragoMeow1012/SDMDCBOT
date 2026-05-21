"""
/電子銀行股市：模擬股市交易 + 銀行存款（共用咕嚕喵碎片錢包）。

- 即時報價：透過 yfinance 抓 Yahoo Finance（台股代碼後綴 .TW 或 .TWO，美股直接輸入代碼）
- 共用錢包：data/morning_records.json（與 /早安小龍喵、/賭博 共用咕嚕喵碎片）
- 個人資料：data/bank_stock.json，存銀行存款 + 股票倉位
- 匯率：1 咕嚕喵碎片 = 1 新台幣（美股股價以 yfinance USD=>TWD 即時換算）
- 銀行利息：每天 5% 複利（以台灣時區 00:00 為日界）
- 原子寫入：透過 utils/json_store 的 save_json_async / load_json
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ui import Button, Modal, TextInput, View

import yfinance as yf

from commands import _wallet
from utils.json_store import load_json, save_json_async


# ==========================================
# 設定與基本結構
# ==========================================
_DB_FILE = os.path.join('data', 'bank_stock.json')
_LEGACY_FILE = os.path.join('data', 'stock_portfolios.json')
_DAILY_INTEREST = 0.05               # 銀行每日複利 5%
_TZ = timezone(timedelta(hours=8))   # 台灣時間
_db_lock = asyncio.Lock()


# ==========================================
# E_main 同步機制（從 /shop 進銀行時）
# ==========================================
# 從 /shop 跳進銀行時，shop 的 ephemeral E_main 上會顯示銀行 view。
# Modal submit 後 bot 想刷新 E_main 為新 bank view（target=root_interaction
# 編輯 E_main），但用戶可能同時點「返回商店」想把 E_main 變成 shop main。
# 兩個 PATCH 並發會 race，後到的覆蓋先到的 → 用戶看到「返回失效」。
#
# 用 per-user lock 序列化兩邊；flag 表示用戶當前是否仍在銀行 view：
#   - 進銀行：set_user_in_bank(uid, True)
#   - 返回商店 callback：先 defer ack（避免 3s 超時），再 acquire lock
#     → set False + edit shop main
#   - Modal refresh：acquire lock → 若 flag=False 則 skip（用戶已返回，不要
#     再覆蓋 shop main 變回 bank view）
# 任何 acquire 順序都導致最後 E_main 是用戶期望的狀態。
_e_main_locks: dict[int, asyncio.Lock] = {}
_user_in_bank: dict[int, bool]         = {}


def get_e_main_lock(uid: int) -> asyncio.Lock:
    lock = _e_main_locks.get(uid)
    if lock is None:
        lock = asyncio.Lock()
        _e_main_locks[uid] = lock
    return lock


def set_user_in_bank(uid: int, val: bool) -> None:
    _user_in_bank[uid] = val


def is_user_in_bank(uid: int) -> bool:
    return _user_in_bank.get(uid, False)

# USD/TWD 匯率快取（避免每次操作都打 yfinance）
_fx_cache: dict[str, tuple[float, datetime]] = {}
_FX_TTL = timedelta(minutes=10)

# 報價快取（margin 監控 loop + 多倉位 build_margin_embed 共用，避免重打 yfinance）
_quote_cache: dict[str, tuple[tuple[float, str], datetime]] = {}
_QUOTE_TTL = timedelta(seconds=60)


# ==========================================
# 幣別與換算
# ==========================================
def _guess_currency(ticker: str) -> str:
    """無法呼叫 yfinance.fast_info 時，用 ticker 字尾猜幣別。"""
    if ticker.endswith('.TW') or ticker.endswith('.TWO'):
        return 'TWD'
    return 'USD'


_fx_lock = asyncio.Lock()


async def _get_usd_twd_rate() -> float | None:
    """1 USD = ? TWD，10 分鐘快取一次。

    用 _fx_lock 防止 cache miss 時多個並行呼叫者同時觸發 fetch
    （build_account_pages 改 gather 後，多個 _to_coin 會同時跑）。
    """
    now = datetime.now(_TZ)
    cached = _fx_cache.get('usd_twd')
    if cached and now - cached[1] < _FX_TTL:
        return cached[0]

    def _fetch() -> float | None:
        try:
            fx = yf.Ticker('TWD=X').history(period='1d')
            if fx.empty:
                return None
            return float(fx['Close'].iloc[-1])
        except Exception:
            return None

    async with _fx_lock:
        # 進 lock 後再檢一次，第一個拿 lock 的會 fetch，後續直接吃 cache
        cached = _fx_cache.get('usd_twd')
        if cached and now - cached[1] < _FX_TTL:
            return cached[0]
        rate = await asyncio.to_thread(_fetch)
        if rate and rate > 0:
            _fx_cache['usd_twd'] = (rate, now)
            return rate
        return None


async def _to_coin(raw_price: float, currency: str) -> int | None:
    """原幣計價金額 → 咕嚕喵碎片（=台幣等值）。USD 透過匯率換算；TWD 直接 1:1。"""
    if currency == 'TWD':
        return int(round(raw_price))
    rate = await _get_usd_twd_rate()
    if rate is None or rate <= 0:
        return None
    return int(round(raw_price * rate))


def _format_price(raw: float, currency: str, coin: int | None) -> str:
    """單股 / 總額顯示字串。台股直接 X 碎片；美股顯示 $X 美金 (≈ Y 碎片)。"""
    if currency == 'TWD':
        return f'`{int(round(raw)):,}` 碎片'
    if coin is None:
        return f'`${raw:,.2f}` 美金 (匯率暫無)'
    return f'`${raw:,.2f}` 美金 (≈ `{coin:,}` 碎片)'


# ==========================================
# 報價：(原幣價, 幣別)
# ==========================================
async def _fetch_quote(ticker_code: str) -> tuple[float, str] | None:
    """抓即時收盤價 + 幣別。yfinance 內 fast_info 拿不到 currency 時用字尾猜。

    60 秒記憶體快取：spot 多檔同時計價、margin liquidation loop 重複掃同
    一檔時都受益。yfinance.Ticker().history(period='1d') 的真實 latency
    ~1-2s，60s TTL 對使用者感受上是即時的。
    """
    now = datetime.now(_TZ)
    cached = _quote_cache.get(ticker_code)
    if cached and now - cached[1] < _QUOTE_TTL:
        return cached[0]

    def _fetch() -> tuple[float, str] | None:
        try:
            ticker = yf.Ticker(ticker_code)
            hist = ticker.history(period='1d')
            if hist.empty:
                return None
            price = round(float(hist['Close'].iloc[-1]), 2)
            try:
                currency = (ticker.fast_info.get('currency') or '').upper()
            except Exception:
                currency = ''
            if not currency:
                currency = _guess_currency(ticker_code)
            return (price, currency)
        except Exception:
            return None
    quote = await asyncio.to_thread(_fetch)
    if quote is not None:
        _quote_cache[ticker_code] = (quote, now)
    return quote


# ==========================================
# 資料庫核心輔助函式
# ==========================================
def _today_key() -> str:
    return datetime.now(_TZ).date().isoformat()


def _migrate_legacy(db: dict[str, Any]) -> None:
    """從舊 stock_portfolios.json 一次性匯入；無舊檔直接 return。"""
    if not os.path.exists(_LEGACY_FILE):
        return
    legacy = load_json(_LEGACY_FILE)
    for uid, portfolio in legacy.items():
        if uid in db or not isinstance(portfolio, dict):
            continue
        converted: dict[str, Any] = {}
        for ticker, info in portfolio.items():
            if not isinstance(info, dict) or 'shares' not in info:
                continue
            raw = float(info.get('average_price', 0))
            converted[ticker] = {
                'shares':        int(info['shares']),
                'average_price': raw,
                'currency':      _guess_currency(ticker),
            }
        db[uid] = {
            'portfolio': converted,
            'bank':      {'balance': 0, 'last_interest': None},
        }


def _load_db() -> dict[str, Any]:
    db = load_json(_DB_FILE)
    if not db and not os.path.exists(_DB_FILE):
        _migrate_legacy(db)
    return db


async def calc_net_worth(uid: int) -> int:
    """淨資產 = 錢包 + 銀行存款 + 股票持倉現值 + 槓桿倉位 equity（碎片計）。
    報價或匯率取不到的股票退回成本價 (average_price × shares) 概算；
    槓桿倉位則退回 margin_locked 作為保守估值。
    給外部模組（daily_task 罰則等）使用。"""
    wallet = _wallet.get_balance(str(uid))
    async with _db_lock:
        db  = _load_db()
        acc = _get_user_account(uid, db)
    bank      = int(acc['bank'].get('balance') or 0)
    portfolio = acc.get('portfolio') or {}
    margin_positions = (acc.get('margin') or {}).get('positions') or {}

    stock_value = 0
    if portfolio:
        tickers = list(portfolio.keys())
        quotes  = await asyncio.gather(
            *(_fetch_quote(t) for t in tickers),
            return_exceptions=True,
        )
        for ticker, q in zip(tickers, quotes):
            data     = portfolio[ticker]
            shares   = int(data.get('shares') or 0)
            avg_raw  = float(data.get('average_price') or 0.0)
            currency = data.get('currency') or _guess_currency(ticker)
            if shares <= 0:
                continue
            # 報價取不到或拋例外 → 退回成本價估值
            if isinstance(q, BaseException) or q is None:
                cur_raw, cur_currency = avg_raw, currency
            else:
                cur_raw, cur_currency = q
            coin = await _to_coin(cur_raw * shares, cur_currency)
            if coin is not None:
                stock_value += coin

    margin_value = 0
    if margin_positions:
        m_tickers = list(margin_positions.keys())
        m_quotes  = await asyncio.gather(
            *(_fetch_quote(t) for t in m_tickers),
            return_exceptions=True,
        )
        for ticker, q in zip(m_tickers, m_quotes):
            pos = margin_positions[ticker]
            currency = pos.get('currency') or _guess_currency(ticker)
            if isinstance(q, BaseException) or q is None:
                # 報價失敗 → 保守用 margin_locked
                margin_value += int(pos.get('margin_locked', 0))
                continue
            cur_raw, cur_currency = q
            equity = await _compute_position_equity(pos, cur_raw, cur_currency)
            if equity is None:
                margin_value += int(pos.get('margin_locked', 0))
            else:
                margin_value += equity
    return int(wallet + bank + stock_value + margin_value)


def _get_user_account(user_id: int, db: dict[str, Any]) -> dict[str, Any]:
    uid = str(user_id)
    acc = db.get(uid)
    if not acc:
        acc = {
            'portfolio': {},
            'bank':      {'balance': 0, 'last_interest': None},
            'margin':    {'positions': {}},
        }
        db[uid] = acc
        return acc
    acc.setdefault('portfolio', {})
    acc.setdefault('bank', {'balance': 0, 'last_interest': None})
    acc.setdefault('margin', {'positions': {}})
    acc['margin'].setdefault('positions', {})
    return acc


# ==========================================
# 槓桿（保證金）核心參數
# ==========================================
_MARGIN_LEVERAGE_MIN = 2
_MARGIN_LEVERAGE_MAX = 10
# 強制平倉門檻：權益 / 初始保證金 ≤ 此值即觸發
_MARGIN_LIQUIDATION_RATIO = 0.15
# 背景監控週期：SL/TP + 強制平倉
_MARGIN_MONITOR_INTERVAL_SEC = 120


async def _compute_position_equity(
    pos: dict[str, Any], current_raw: float, cur_currency: str,
) -> int | None:
    """以當前 raw 價計算單一倉位的「可領回淨額（碎片）」。

    - long  : equity = 現值 - 借款
    - short : equity = (進場價值 + 保證金) - 現值（已平掉買回成本）
    equity < 0 時 clamp 到 0（不負債到錢包，由強制平倉攔截）。
    """
    shares = int(pos.get('shares', 0))
    margin_locked = int(pos.get('margin_locked', 0))
    borrowed = int(pos.get('borrowed_funds', 0))
    current_value = await _to_coin(current_raw * shares, cur_currency)
    if current_value is None:
        return None
    pos_type = pos.get('type', 'long')
    if pos_type == 'long':
        equity = current_value - borrowed
    else:
        equity = (margin_locked + borrowed) - current_value
    return max(0, equity)


def _format_raw_price(raw: float, currency: str) -> str:
    """槓桿子面板用：短一點，只顯示原幣 raw 值。"""
    if currency == 'TWD':
        return f'{int(round(raw)):,}'
    return f'${raw:,.2f}'


def _check_sl_tp_trigger(pos: dict[str, Any], current_raw: float) -> str | None:
    """回傳觸發類型 'tp' / 'sl' / None。"""
    pos_type = pos.get('type', 'long')
    tp, sl = pos.get('tp'), pos.get('sl')
    if pos_type == 'long':
        if tp is not None and current_raw >= float(tp): return 'tp'
        if sl is not None and current_raw <= float(sl): return 'sl'
    else:
        if tp is not None and current_raw <= float(tp): return 'tp'
        if sl is not None and current_raw >= float(sl): return 'sl'
    return None


def _apply_interest(bank: dict[str, Any]) -> int:
    """套用到今日的銀行複利，回傳本次新增的利息（碎片）。in-place 更新 bank。"""
    balance = int(bank.get('balance', 0))
    last = bank.get('last_interest')
    today = _today_key()

    if balance <= 0 or not last:
        bank['last_interest'] = today
        return 0

    days = (date.fromisoformat(today) - date.fromisoformat(last)).days
    if days <= 0:
        return 0

    new_balance = int(balance * ((1 + _DAILY_INTEREST) ** days))
    interest = new_balance - balance
    bank['balance'] = new_balance
    bank['last_interest'] = today
    return interest


# ==========================================
# Modal: 買入股票
# ==========================================
class BuyStockModal(Modal, title='買入股票'):
    stock_code: TextInput = TextInput(
        label='股票代碼',
        placeholder='台股請加 .TW (如: 2330.TW)，美股直接輸入 (如: AAPL)',
        required=True,
        max_length=15,
    )
    shares: TextInput = TextInput(
        label='買入股數',
        placeholder='請輸入正整數 (如: 1000)',
        required=True,
        max_length=10,
    )

    def __init__(self, *, back_to_shop=None, refresh_target=None) -> None:
        super().__init__()
        self.back_to_shop   = back_to_shop
        self.refresh_target = refresh_target

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            qty = int(self.shares.value)
            if qty <= 0:
                raise ValueError
        except ValueError:
            await interaction.followup.send('❌ 錯誤：買入股數必須為正整數！', ephemeral=True)
            return

        ticker = self.stock_code.value.upper().strip()
        uid = str(interaction.user.id)

        async with _db_lock:
            quote = await _fetch_quote(ticker)
            if quote is None:
                await interaction.followup.send(
                    f'❌ 找不到股票代碼 `{ticker}` 或無法取得即時報價。',
                    ephemeral=True,
                )
                return
            price_raw, currency = quote

            total_cost_coin = await _to_coin(price_raw * qty, currency)
            if total_cost_coin is None:
                await interaction.followup.send(
                    '❌ 暫時無法取得 USD/TWD 匯率，請稍後再試。',
                    ephemeral=True,
                )
                return
            if total_cost_coin <= 0:
                await interaction.followup.send(
                    '❌ 此次交易金額不足 1 碎片，無法成交。', ephemeral=True,
                )
                return

            balance = _wallet.get_balance(uid)
            if balance < total_cost_coin:
                await interaction.followup.send(
                    f'❌ 餘額不足！\n'
                    f'所需資金: `{total_cost_coin:,}` 咕嚕喵碎片\n'
                    f'錢包餘額: `{balance:,}` 咕嚕喵碎片',
                    ephemeral=True,
                )
                return

            new_balance = await _wallet.apply_delta(uid, -total_cost_coin)

            db = _load_db()
            acc = _get_user_account(interaction.user.id, db)
            portfolio = acc['portfolio']

            if ticker in portfolio:
                old = portfolio[ticker]
                old_qty = int(old['shares'])
                old_raw = float(old['average_price'])
                new_qty = old_qty + qty
                new_raw = round(
                    (old_raw * old_qty + price_raw * qty) / new_qty, 2
                )
                old['shares'] = new_qty
                old['average_price'] = new_raw
                old['currency'] = currency
            else:
                portfolio[ticker] = {
                    'shares':        qty,
                    'average_price': price_raw,
                    'currency':      currency,
                }

            await save_json_async(_DB_FILE, db)

        unit_coin = await _to_coin(price_raw, currency)
        price_text = _format_price(price_raw, currency, unit_coin)
        await interaction.followup.send(
            f'✅ **買入成功！**\n'
            f'代碼：`{ticker}`（{currency}）\n股數：`{qty:,}` 股\n'
            f'成交價：{price_text} / 股\n'
            f'總計花費：`{total_cost_coin:,}` 咕嚕喵碎片\n'
            f'錢包餘額：`{new_balance:,}` 咕嚕喵碎片',
            ephemeral=True,
        )
        await _refresh_main_menu(
            interaction,
            back_to_shop=self.back_to_shop,
            refresh_target=self.refresh_target,
        )


# ==========================================
# Modal: 賣出股票
# ==========================================
class SellStockModal(Modal, title='賣出股票'):
    stock_code: TextInput = TextInput(
        label='股票代碼', placeholder='例如: 2330.TW 或 AAPL',
        required=True, max_length=15,
    )
    shares: TextInput = TextInput(
        label='賣出股數', placeholder='請輸入正整數',
        required=True, max_length=10,
    )

    def __init__(self, *, back_to_shop=None, refresh_target=None) -> None:
        super().__init__()
        self.back_to_shop   = back_to_shop
        self.refresh_target = refresh_target

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            qty = int(self.shares.value)
            if qty <= 0:
                raise ValueError
        except ValueError:
            await interaction.followup.send('❌ 錯誤：賣出股數必須為正整數！', ephemeral=True)
            return

        ticker = self.stock_code.value.upper().strip()
        uid = str(interaction.user.id)

        async with _db_lock:
            db = _load_db()
            acc = _get_user_account(interaction.user.id, db)
            portfolio = acc['portfolio']

            if ticker not in portfolio:
                await interaction.followup.send(
                    f'❌ 您並未持有 `{ticker}` 這檔股票！',
                    ephemeral=True,
                )
                return

            old = portfolio[ticker]
            current_qty = int(old['shares'])
            if qty > current_qty:
                await interaction.followup.send(
                    f'❌ 持股不足！您目前僅持有 `{ticker}` 共 `{current_qty:,}` 股。',
                    ephemeral=True,
                )
                return

            quote = await _fetch_quote(ticker)
            if quote is None:
                await interaction.followup.send(
                    f'❌ 無法取得 `{ticker}` 的即時報價，請稍後再試。',
                    ephemeral=True,
                )
                return
            price_raw, currency = quote

            revenue_coin = await _to_coin(price_raw * qty, currency)
            avg_raw = float(old['average_price'])
            old_currency = old.get('currency', currency)
            cost_basis_coin = await _to_coin(avg_raw * qty, old_currency)
            if revenue_coin is None or cost_basis_coin is None:
                await interaction.followup.send(
                    '❌ 暫時無法取得 USD/TWD 匯率，請稍後再試。',
                    ephemeral=True,
                )
                return
            realized_coin = revenue_coin - cost_basis_coin

            old['shares'] = current_qty - qty
            if old['shares'] == 0:
                del portfolio[ticker]
            else:
                # 賣出不改均價，但更新最新幣別
                old['currency'] = currency

            await save_json_async(_DB_FILE, db)
            new_balance = await _wallet.apply_delta(uid, revenue_coin)

        sign = '+' if realized_coin >= 0 else ''
        unit_coin = await _to_coin(price_raw, currency)
        price_text = _format_price(price_raw, currency, unit_coin)
        await interaction.followup.send(
            f'✅ **賣出成功！**\n'
            f'代碼：`{ticker}`（{currency}）\n股數：`{qty:,}` 股\n'
            f'成交價：{price_text} / 股\n'
            f'獲得資金：`{revenue_coin:,}` 咕嚕喵碎片\n'
            f'本次實現損益：`{sign}{realized_coin:,}` 咕嚕喵碎片\n'
            f'錢包餘額：`{new_balance:,}` 咕嚕喵碎片',
            ephemeral=True,
        )
        await _refresh_main_menu(
            interaction,
            back_to_shop=self.back_to_shop,
            refresh_target=self.refresh_target,
        )


# ==========================================
# Modal: 存款 / 提款
# ==========================================
class DepositModal(Modal, title='銀行存款'):
    amount: TextInput = TextInput(
        label='存入碎片數量', placeholder='請輸入正整數',
        required=True, max_length=12,
    )

    def __init__(self, *, back_to_shop=None, refresh_target=None) -> None:
        super().__init__()
        self.back_to_shop   = back_to_shop
        self.refresh_target = refresh_target

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            amt = int(self.amount.value)
            if amt <= 0:
                raise ValueError
        except ValueError:
            await interaction.followup.send('❌ 錯誤：存款金額必須為正整數！', ephemeral=True)
            return

        uid = str(interaction.user.id)
        async with _db_lock:
            balance = _wallet.get_balance(uid)
            if balance < amt:
                await interaction.followup.send(
                    f'❌ 錢包餘額不足！\n'
                    f'欲存入: `{amt:,}` 碎片\n'
                    f'錢包餘額: `{balance:,}` 碎片',
                    ephemeral=True,
                )
                return

            db = _load_db()
            acc = _get_user_account(interaction.user.id, db)
            bank = acc['bank']
            interest = _apply_interest(bank)  # 先結算過往利息
            bank['balance'] = int(bank['balance']) + amt
            await save_json_async(_DB_FILE, db)
            new_wallet = await _wallet.apply_delta(uid, -amt)

        msg = (
            f'✅ **存款成功！**\n'
            f'存入金額：`{amt:,}` 咕嚕喵碎片\n'
            f'銀行餘額：`{bank["balance"]:,}` 咕嚕喵碎片\n'
            f'錢包餘額：`{new_wallet:,}` 咕嚕喵碎片'
        )
        if interest > 0:
            msg += f'\n💡 結算今日前累計利息：`+{interest:,}` 碎片'
        await interaction.followup.send(msg, ephemeral=True)
        await _refresh_main_menu(
            interaction,
            back_to_shop=self.back_to_shop,
            refresh_target=self.refresh_target,
        )


class WithdrawModal(Modal, title='銀行提款'):
    amount: TextInput = TextInput(
        label='提取碎片數量', placeholder='請輸入正整數',
        required=True, max_length=12,
    )

    def __init__(self, *, back_to_shop=None, refresh_target=None) -> None:
        super().__init__()
        self.back_to_shop   = back_to_shop
        self.refresh_target = refresh_target

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            amt = int(self.amount.value)
            if amt <= 0:
                raise ValueError
        except ValueError:
            await interaction.followup.send('❌ 錯誤：提款金額必須為正整數！', ephemeral=True)
            return

        uid = str(interaction.user.id)
        async with _db_lock:
            db = _load_db()
            acc = _get_user_account(interaction.user.id, db)
            bank = acc['bank']
            interest = _apply_interest(bank)
            bank_balance = int(bank['balance'])

            if bank_balance < amt:
                if interest > 0:
                    await save_json_async(_DB_FILE, db)  # 至少把利息結算保存
                await interaction.followup.send(
                    f'❌ 銀行餘額不足！\n'
                    f'欲提取: `{amt:,}` 碎片\n'
                    f'銀行餘額: `{bank_balance:,}` 碎片',
                    ephemeral=True,
                )
                return

            bank['balance'] = bank_balance - amt
            await save_json_async(_DB_FILE, db)
            new_wallet = await _wallet.apply_delta(uid, amt)

        msg = (
            f'✅ **提款成功！**\n'
            f'提取金額：`{amt:,}` 咕嚕喵碎片\n'
            f'銀行餘額：`{bank["balance"]:,}` 咕嚕喵碎片\n'
            f'錢包餘額：`{new_wallet:,}` 咕嚕喵碎片'
        )
        if interest > 0:
            msg += f'\n💡 結算今日前累計利息：`+{interest:,}` 碎片'
        await interaction.followup.send(msg, ephemeral=True)
        await _refresh_main_menu(
            interaction,
            back_to_shop=self.back_to_shop,
            refresh_target=self.refresh_target,
        )


# ==========================================
# View: 主面板按鈕列（含分頁）
# ==========================================
def _add_pager_buttons(view: View, pages: list[discord.Embed],
                       page_idx: int, *, row: int) -> None:
    """共用：把 ◀ 上一頁 / 下一頁 ▶ 兩顆按鈕加進 view 同一 row。
    callback 透過 interaction.response.edit_message 切換到對應頁。
    """
    total = len(pages)

    async def _go(inter: discord.Interaction, new_idx: int) -> None:
        view.page_idx = new_idx  # type: ignore[attr-defined]
        # 重建按鈕（enable/disable 狀態跟著變）
        view.clear_items()
        if isinstance(view, StockSystemView):
            view._build(pages, new_idx)
        elif isinstance(view, AccountPagerView):
            view._build(pages, new_idx)
        await inter.response.edit_message(embed=pages[new_idx], view=view)

    prev_btn = Button(label='◀ 上一頁',
                      style=discord.ButtonStyle.secondary,
                      disabled=(page_idx <= 0), row=row)
    prev_btn.callback = lambda i: _go(i, page_idx - 1)
    view.add_item(prev_btn)

    next_btn = Button(label='下一頁 ▶',
                      style=discord.ButtonStyle.secondary,
                      disabled=(page_idx >= total - 1), row=row)
    next_btn.callback = lambda i: _go(i, page_idx + 1)
    view.add_item(next_btn)


class StockSystemView(View):
    """銀行+股市主面板。Page state per instance；不再 persistent。

    `back_to_shop`：若由 /shop 跳轉進來，傳入一個 async callback(inter)，
    第三列尾端的「關閉」會換成「返回商店」並改觸發此 callback。
    `refresh_target`：交易/刷新後要編輯的 interaction（其 @original 即是商店
    主訊息 E_main）。從 /shop 進來時傳入 root_interaction，這樣 modal submit
    後也能正確刷新 E_main，而不是創一個重複的 ephemeral。
    """

    def __init__(self, pages: list[discord.Embed] | None = None,
                 page_idx: int = 0,
                 *, back_to_shop=None, refresh_target=None) -> None:
        super().__init__(timeout=300)
        self.pages          = pages or []
        self.page_idx       = page_idx
        self.back_to_shop   = back_to_shop
        self.refresh_target = refresh_target
        if self.pages:
            self._build(self.pages, self.page_idx)

    def _build(self, pages: list[discord.Embed], page_idx: int) -> None:
        back = self.back_to_shop
        rt   = self.refresh_target
        # row 0: 交易（現貨買賣 + 槓桿入口）
        buy = Button(label='買入股票 📈', style=discord.ButtonStyle.success, row=0)
        buy.callback = lambda i: i.response.send_modal(
            BuyStockModal(back_to_shop=back, refresh_target=rt))
        self.add_item(buy)

        sell = Button(label='賣出股票 📉', style=discord.ButtonStyle.danger, row=0)
        sell.callback = lambda i: i.response.send_modal(
            SellStockModal(back_to_shop=back, refresh_target=rt))
        self.add_item(sell)

        margin_btn = Button(label='槓桿交易 ⚡',
                            style=discord.ButtonStyle.primary, row=0)

        async def _open_margin(inter: discord.Interaction) -> None:
            embed = await build_margin_embed(inter.user.id, inter.user.display_name)
            await inter.response.edit_message(
                embed=embed,
                view=MarginSystemView(back_to_shop=back, refresh_target=rt),
            )
        margin_btn.callback = _open_margin
        self.add_item(margin_btn)

        # row 1: 銀行
        dep = Button(label='存款 🏦', style=discord.ButtonStyle.success, row=1)
        dep.callback = lambda i: i.response.send_modal(
            DepositModal(back_to_shop=back, refresh_target=rt))
        self.add_item(dep)

        wd = Button(label='提款 💵', style=discord.ButtonStyle.danger, row=1)
        wd.callback = lambda i: i.response.send_modal(
            WithdrawModal(back_to_shop=back, refresh_target=rt))
        self.add_item(wd)

        # row 2: 刷新 / 關閉（或返回商店）
        refresh = Button(label='刷新資產 🔄',
                         style=discord.ButtonStyle.primary, row=2)
        async def _redraw(inter: discord.Interaction) -> None:
            await inter.response.defer()
            await _refresh_main_menu(inter, back_to_shop=back, refresh_target=rt)
        refresh.callback = _redraw
        self.add_item(refresh)

        if self.back_to_shop is not None:
            back = Button(label='返回商店 🛒',
                          style=discord.ButtonStyle.secondary, row=2)
            back.callback = self.back_to_shop
            self.add_item(back)
        else:
            close = Button(label='關閉 ✖️',
                           style=discord.ButtonStyle.secondary, row=2)
            async def _close(inter: discord.Interaction) -> None:
                try:
                    await inter.response.defer()
                    await inter.delete_original_response()
                except discord.HTTPException:
                    pass
            close.callback = _close
            self.add_item(close)

        # row 3: 分頁
        if len(pages) > 1:
            _add_pager_buttons(self, pages, page_idx, row=3)


class AccountPagerView(View):
    """/帳戶總覽 用的唯讀分頁 view。
    owner_uid 非 None 時加上「使用道具」按鈕（自己看自己時才有）。"""

    def __init__(self, pages: list[discord.Embed],
                 page_idx: int = 0,
                 *, owner_uid: str | None = None) -> None:
        super().__init__(timeout=300)
        self.pages     = pages
        self.page_idx  = page_idx
        self.owner_uid = owner_uid
        self._build(pages, page_idx)

    def _build(self, pages: list[discord.Embed], page_idx: int) -> None:
        if len(pages) > 1:
            _add_pager_buttons(self, pages, page_idx, row=0)

        if self.owner_uid is not None:
            use_btn = Button(label='🎒 使用道具',
                             style=discord.ButtonStyle.primary, row=0)

            async def _open_use(inter: discord.Interaction) -> None:
                if str(inter.user.id) != self.owner_uid:
                    await inter.response.send_message('這不是你的帳戶總覽', ephemeral=True)
                    return
                view = UseItemView(self.owner_uid, root_interaction=inter,
                                   pages=self.pages, page_idx=self.page_idx)
                await inter.response.edit_message(
                    embed=view.build_embed(inter.user), view=view,
                )
            use_btn.callback = _open_use
            self.add_item(use_btn)

        close = Button(label='關閉 ✖️',
                       style=discord.ButtonStyle.secondary, row=0)

        async def _close(inter: discord.Interaction) -> None:
            try:
                await inter.response.defer()
                await inter.delete_original_response()
            except discord.HTTPException:
                pass

        close.callback = _close
        self.add_item(close)


class UseItemView(View):
    """帳戶總覽的「使用道具」面板。

    目前所有 count 型道具都不需要玩家手動 use：
      - 反轉牌：被攻擊時自動觸發
      - 魚餌：釣魚時自動消耗（請至 /fishing）
      - 釣竿：在 /fishing 內裝備使用
      - 礦工：每整點自動產出
      - 藥水：購買後自動激活

    這個面板列出當前持有清單與每種道具的「使用方式」，
    保留未來新增主動使用道具的擴充點。
    """

    def __init__(self, uid: str, *, root_interaction: discord.Interaction,
                 pages: list[discord.Embed], page_idx: int):
        super().__init__(timeout=300)
        self.uid = uid
        self.root_interaction = root_interaction
        self.pages = pages
        self.page_idx = page_idx

        back = Button(label='⬅️ 返回帳戶總覽',
                      style=discord.ButtonStyle.secondary, row=0)

        async def _back(inter: discord.Interaction) -> None:
            if str(inter.user.id) != self.uid:
                await inter.response.send_message('這不是你的帳戶總覽', ephemeral=True)
                return
            new_view = AccountPagerView(self.pages, page_idx=self.page_idx,
                                        owner_uid=self.uid)
            await inter.response.edit_message(
                embed=self.pages[self.page_idx], view=new_view,
            )
        back.callback = _back
        self.add_item(back)

    def build_embed(self, user: discord.abc.User) -> discord.Embed:
        # 收集現有 count 型道具持有量
        from commands.shop import _get_miners, get_reverse_cards, MINER_CAP, REVERSE_CARD_MAX
        from commands import fishing as F
        miners = _get_miners(self.uid)
        rev = get_reverse_cards(self.uid)
        rods = list(F.get_rods(self.uid))
        baits = F.get_baits(self.uid)
        fish_total = F.get_fish_count_total(self.uid)

        lines = [
            '**🎒 你的道具清單與使用方式**',
            '',
            f'⛏️ 礦工 × {miners}/{MINER_CAP}　_(每整點自動產出，不需手動使用)_',
            f'🪞 反轉牌 × {rev}/{REVERSE_CARD_MAX}　_(被整蠱時自動觸發)_',
            f'🎣 額外釣竿 × {len(rods)}　_(請至 /fishing 主面板裝備使用)_',
            f'🪱 魚餌總數 × {sum(int(v) for v in baits.values())}　_(請至 /fishing 選餌釣魚自動消耗)_',
            f'🐟 保溫箱 × {fish_total} / {F.get_fish_pond_cap(self.uid)}　_(可至商店「販售」或「贈禮」處理)_',
            '',
            '_目前沒有需要在這裡主動使用的道具；此面板會在未來新道具加入時擴充。_',
        ]
        return discord.Embed(
            title='🎒 使用道具',
            description='\n'.join(lines),
            color=discord.Color.dark_teal(),
        )


# ==========================================
# 渲染：資產 + 銀行 Embed
# ==========================================
_DATA_SOURCE_FOOTER = (
    '資料來源：Yahoo Finance（台股報價約延遲 15-20 分鐘）｜'
    '匯率：1 NT$ = 1 咕嚕喵碎片'
)


async def build_account_pages(
    user_id: int,
    username: str,
    *,
    title: str | None = None,
    extra_fields: list[tuple[str, str, bool]] | None = None,
) -> list[discord.Embed]:
    """組分頁版的帳戶 embed 列表。

    page 1: 錢包 / 銀行 / 股票市值 / 淨資產
    page 2: extra_fields（打卡 / 礦工 / 道具）— 沒傳 extras 就跳過此頁
    page 3: 總未實現損益 + 持股詳細清單（+ 資料來源 footer）

    每頁 footer 結尾自動加「頁碼 N/total」。
    """
    # 結算銀行利息（即使只是看 embed 也順便更新到今日）
    async with _db_lock:
        db = _load_db()
        acc = _get_user_account(user_id, db)
        interest = _apply_interest(acc['bank'])
        if interest > 0:
            await save_json_async(_DB_FILE, db)

    wallet = _wallet.get_balance(str(user_id))
    portfolio = acc['portfolio']
    bank_balance = int(acc['bank']['balance'])

    total_stock_coin = 0
    total_pl_coin = 0
    total_cost_coin = 0

    if not portfolio:
        details = '*目前無任何持股紀錄，趕快點擊下方按鈕開始交易吧！*'
    else:
        # 一次抓全部 ticker 的報價（N 支股票本來 N×~1.5s 變單次 ~1.5s）
        tickers = list(portfolio.keys())
        quotes = await asyncio.gather(
            *(_fetch_quote(t) for t in tickers),
            return_exceptions=False,
        )

        lines: list[str] = []
        for ticker, quote in zip(tickers, quotes):
            data     = portfolio[ticker]
            shares   = int(data['shares'])
            avg_raw  = float(data['average_price'])
            currency = data.get('currency') or _guess_currency(ticker)

            cur_raw, cur_currency = (avg_raw, currency) if quote is None else quote

            # 每支股票要 4 個 _to_coin（市值/成本/單股均價/單股現價）
            # 匯率 cache + lock 保護，並行不會觸發多次 fetch
            cur_total_coin, cost_total_coin, unit_avg_coin, unit_cur_coin = await asyncio.gather(
                _to_coin(cur_raw * shares, cur_currency),
                _to_coin(avg_raw * shares, currency),
                _to_coin(avg_raw, currency),
                _to_coin(cur_raw, cur_currency),
            )
            if cur_total_coin is None or cost_total_coin is None:
                lines.append(
                    f'**🔹 `{ticker}`** ({currency}) 持股 `{shares:,}` — 匯率暫無，無法計價'
                )
                continue

            pl = cur_total_coin - cost_total_coin
            roi = (pl / cost_total_coin) * 100 if cost_total_coin > 0 else 0.0

            total_stock_coin += cur_total_coin
            total_pl_coin += pl
            total_cost_coin += cost_total_coin

            avg_text = _format_price(avg_raw, currency, unit_avg_coin)
            cur_text = _format_price(cur_raw, cur_currency, unit_cur_coin)

            sign = '+' if pl >= 0 else ''
            lines.append(
                f'**🔹 `{ticker}`**（{cur_currency}）\n'
                f' └ 持股: `{shares:,}` 股 | 均價: {avg_text}\n'
                f' └ 現價: {cur_text}\n'
                f' └ 市值: `{cur_total_coin:,}` 碎片 | '
                f'未實現損益: `{sign}{pl:,}` 碎片 (`{sign}{round(roi, 2)}%`)\n'
            )
        details = '\n'.join(lines)

    net_worth = wallet + bank_balance + total_stock_coin
    total_sign = '+' if total_pl_coin >= 0 else ''
    total_roi = (total_pl_coin / total_cost_coin) * 100 if total_cost_coin > 0 else 0.0

    final_title = title or f'🏦 {username} 的電子銀行 & 股市帳戶'
    color = discord.Color.dark_teal()

    # Page 1: 持有金錢 / 銀行 / 股票市值 / 淨資產
    p1 = discord.Embed(title=final_title, color=color)
    p1.add_field(name='💰 錢包', value=f'`{wallet:,}` 碎片', inline=True)
    p1.add_field(name='🏦 銀行存款', value=f'`{bank_balance:,}` 碎片', inline=True)
    p1.add_field(name='📊 股票市值', value=f'`{total_stock_coin:,}` 碎片', inline=True)
    p1.add_field(name='💎 淨資產 (Net Worth)',
                 value=f'`{net_worth:,}` 碎片', inline=False)

    pages: list[discord.Embed] = [p1]

    # Page 2: extras（只有 /帳戶總覽 會傳）
    if extra_fields:
        p2 = discord.Embed(title=final_title, color=color)
        for name, value, inline in extra_fields:
            p2.add_field(name=name, value=value, inline=inline)
        pages.append(p2)

    # Page 3: 總未實現損益 + 持股詳細
    p3 = discord.Embed(title=final_title, color=color)
    p3.add_field(name='🔥 總未實現損益',
                 value=f'`{total_sign}{total_pl_coin:,}` 碎片 '
                       f'(`{total_sign}{round(total_roi, 2)}%`)',
                 inline=False)
    p3.add_field(name='📜 持股詳細清單', value=details[:1024], inline=False)
    pages.append(p3)

    total = len(pages)
    for i, e in enumerate(pages, 1):
        page_label = f'頁碼 {i}/{total}'
        if e is p3:
            e.set_footer(text=f'{_DATA_SOURCE_FOOTER} ｜ {page_label}')
        else:
            e.set_footer(text=page_label)
    return pages


async def _refresh_main_menu(interaction: discord.Interaction,
                              *, back_to_shop=None,
                              refresh_target: discord.Interaction | None = None) -> None:
    """刷新銀行主面板。refresh_target 決定編輯哪個訊息：
      - 提供時（/shop 進來）：編輯 root 的 @original（E_main），且**只更新 embed
        不換 view** — modal 重建 view 後 Discord 端 component custom_id 跟 bot
        view store 偶爾不同步，user click 會 dispatch 不到 callback。保留原
        view instance 就避開這問題。lock 內檢查 is_user_in_bank 防搶。
      - None（standalone /電子銀行股市）：維持既有「重建 view」行為。
    """
    user = interaction.user
    pages = await build_account_pages(user.id, user.display_name)
    target = refresh_target if refresh_target is not None else interaction

    if refresh_target is not None:
        # 從 /shop 進來：只刷 embed 不重建 view，避免 PATCH view 後 button callback dispatch 失靈
        async with get_e_main_lock(user.id):
            if not is_user_in_bank(user.id):
                return  # 用戶已點返回，不要把 shop main 蓋回 bank view
            try:
                await target.edit_original_response(embed=pages[0])
            except Exception:
                pass
    else:
        # standalone /電子銀行股市：原本行為（重建 view 維持 close 按鈕等狀態）
        try:
            await target.edit_original_response(
                embed=pages[0],
                view=StockSystemView(pages, page_idx=0),
            )
        except Exception:
            pass


# ==========================================
# 槓桿（保證金）系統
# ==========================================
# 整體設計：
# - 資料：bank_stock.json 每位用戶 acc 下 'margin': {'positions': {ticker: {...}}}
# - 資金：開倉 / 補保證金 可選錢包 or 銀行；釋放 / 平倉 / 強制清算一律入錢包
# - 進場價以「原幣 raw + currency」儲存；保證金/借款以「碎片」儲存
#   → 計算當前 equity 時以當下 FX 把現價 raw 換成碎片，自然包含幣別損益
# - 強制清算：權益/初始保證金 ≤ 15% 時清空倉位（equity clamp 0）
# - SL/TP：以原幣 raw 比較當前報價，達標即觸發全平
# - background loop 每 2 分鐘掃一次，所有寫入走 _db_lock + save_json_async

# ── 資金來源扣款 / 釋放 ────────────────────────────────────────────
async def _deduct_funding(uid: str, amount: int, source: str,
                          db: dict[str, Any]) -> tuple[bool, str]:
    """從 source ('wallet' / 'bank') 扣 amount 碎片。需呼叫方持 _db_lock。

    扣款邏輯放在 _db_lock 包好的呼叫端裡執行；
    wallet 透過 _wallet.apply_delta（內部會自己 save），
    bank 直接改 db['<uid>']['bank']['balance']（呼叫方負責 save_json_async）。
    """
    if source == 'wallet':
        bal = _wallet.get_balance(uid)
        if bal < amount:
            return (False, f'錢包餘額不足！需要 `{amount:,}`，剩 `{bal:,}` 碎片。')
        await _wallet.apply_delta(uid, -amount)
        return (True, '')
    if source == 'bank':
        acc = _get_user_account(int(uid), db)
        _apply_interest(acc['bank'])  # 先結算利息再判斷
        bank_bal = int(acc['bank']['balance'])
        if bank_bal < amount:
            return (False, f'銀行餘額不足！需要 `{amount:,}`，剩 `{bank_bal:,}` 碎片。')
        acc['bank']['balance'] = bank_bal - amount
        return (True, '')
    return (False, '未知資金來源。')


def _source_label(src: str) -> str:
    return '錢包 💰' if src == 'wallet' else '銀行 🏦'


# ── 進場 Modal（選完幣別資金來源後填細節） ─────────────────────────
class MarginOpenFinalModal(Modal):
    def __init__(self, *, ticker: str, currency: str, price_raw: float,
                 pos_type: str, source: str,
                 back_to_shop, refresh_target) -> None:
        kind = '做多' if pos_type == 'long' else '做空'
        super().__init__(title=f'{kind} {ticker}（{_source_label(source)}）')
        self.ticker, self.currency = ticker, currency
        self.price_raw, self.pos_type = price_raw, pos_type
        self.source = source
        self.back_to_shop = back_to_shop
        self.refresh_target = refresh_target

        self.shares = TextInput(label='股數', required=True, max_length=10)
        self.leverage = TextInput(
            label=f'槓桿倍數（{_MARGIN_LEVERAGE_MIN}~{_MARGIN_LEVERAGE_MAX}）',
            required=True, max_length=2,
        )
        self.tp = TextInput(
            label='止盈價（選填，留空不設）',
            placeholder='達此價自動全平', required=False, max_length=15,
        )
        self.sl = TextInput(
            label='止損價（選填，留空不設）',
            placeholder='達此價自動全平', required=False, max_length=15,
        )
        for it in (self.shares, self.leverage, self.tp, self.sl):
            self.add_item(it)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            qty = int(self.shares.value)
            lev = int(self.leverage.value)
            if qty <= 0 or lev < _MARGIN_LEVERAGE_MIN or lev > _MARGIN_LEVERAGE_MAX:
                raise ValueError
            tp_val = float(self.tp.value.strip()) if self.tp.value.strip() else None
            sl_val = float(self.sl.value.strip()) if self.sl.value.strip() else None
            if tp_val is not None and tp_val <= 0: raise ValueError
            if sl_val is not None and sl_val <= 0: raise ValueError
        except Exception:
            await interaction.followup.send('❌ 數值錯誤（股數正整、槓桿 2~10、價格正數）', ephemeral=True)
            return

        # SL/TP 方向驗證：long 要 tp>entry, sl<entry；short 反過來
        if self.pos_type == 'long':
            if tp_val is not None and tp_val <= self.price_raw:
                await interaction.followup.send('❌ 做多的止盈價需 > 進場價。', ephemeral=True); return
            if sl_val is not None and sl_val >= self.price_raw:
                await interaction.followup.send('❌ 做多的止損價需 < 進場價。', ephemeral=True); return
        else:
            if tp_val is not None and tp_val >= self.price_raw:
                await interaction.followup.send('❌ 做空的止盈價需 < 進場價。', ephemeral=True); return
            if sl_val is not None and sl_val <= self.price_raw:
                await interaction.followup.send('❌ 做空的止損價需 > 進場價。', ephemeral=True); return

        uid = str(interaction.user.id)
        # 重抓最新報價，避免使用者填表期間價格跳動
        latest = await _fetch_quote(self.ticker)
        if latest is None:
            await interaction.followup.send('❌ 無法取得最新報價，請稍後再試。', ephemeral=True)
            return
        latest_raw, latest_currency = latest

        position_value = await _to_coin(latest_raw * qty, latest_currency)
        if position_value is None or position_value <= 0:
            await interaction.followup.send('❌ 暫時無法取得匯率或金額過小，請稍後再試。', ephemeral=True)
            return

        margin_required = position_value // lev
        borrowed = position_value - margin_required
        if margin_required <= 0:
            await interaction.followup.send('❌ 所需保證金不足 1 碎片，請增加股數。', ephemeral=True)
            return

        async with _db_lock:
            db = _load_db()
            acc = _get_user_account(interaction.user.id, db)
            positions = acc['margin']['positions']

            existing = positions.get(self.ticker)
            if existing and existing.get('type', 'long') != self.pos_type:
                await interaction.followup.send(
                    '❌ 同一標的不能同時做多與做空，請先平掉舊倉。',
                    ephemeral=True,
                )
                return

            ok, msg = await _deduct_funding(uid, margin_required, self.source, db)
            if not ok:
                await interaction.followup.send(f'❌ {msg}', ephemeral=True)
                return

            if existing:
                old_qty = int(existing['shares'])
                old_entry = float(existing['entry_price'])
                new_qty = old_qty + qty
                # 以「原幣加權」更新進場價，幣別保留舊倉幣別（同 ticker 通常一致）
                new_entry = round(
                    (old_entry * old_qty + latest_raw * qty) / new_qty, 4,
                )
                existing['shares'] = new_qty
                existing['entry_price'] = new_entry
                existing['margin_locked'] = int(existing.get('margin_locked', 0)) + margin_required
                existing['borrowed_funds'] = int(existing.get('borrowed_funds', 0)) + borrowed
                # 加倉後槓桿用整體值反推（顯示用，計算只看 margin_locked / borrowed_funds）
                total_value = existing['margin_locked'] + existing['borrowed_funds']
                existing['leverage'] = round(total_value / max(existing['margin_locked'], 1), 1)
                if tp_val is not None: existing['tp'] = tp_val
                if sl_val is not None: existing['sl'] = sl_val
            else:
                positions[self.ticker] = {
                    'type':           self.pos_type,
                    'leverage':       float(lev),
                    'shares':         qty,
                    'entry_price':    latest_raw,
                    'currency':       latest_currency,
                    'margin_locked':  margin_required,
                    'borrowed_funds': borrowed,
                    'tp':             tp_val,
                    'sl':             sl_val,
                    'opened_at':      datetime.now(_TZ).isoformat(),
                }
            await save_json_async(_DB_FILE, db)

        kind = '做多 📈' if self.pos_type == 'long' else '做空 📉'
        await _refresh_margin_panel(
            interaction, back_to_shop=self.back_to_shop,
            refresh_target=self.refresh_target,
        )
        await interaction.followup.send(
            f'✅ **{kind} {self.ticker}** 進場成功（{_source_label(self.source)}）\n'
            f'股數：`{qty:,}` 股 ｜ 槓桿：`{lev}x`\n'
            f'進場價：{_format_raw_price(latest_raw, latest_currency)} ({latest_currency})\n'
            f'保證金：`{margin_required:,}` 碎片 ｜ 借入：`{borrowed:,}` 碎片',
            ephemeral=True,
        )


# ── 進場確認 + 選資金來源 View ──────────────────────────────────
class MarginOpenSourceView(View):
    def __init__(self, *, ticker: str, currency: str, price_raw: float,
                 pos_type: str, back_to_shop, refresh_target) -> None:
        super().__init__(timeout=300)
        self.ticker, self.currency = ticker, currency
        self.price_raw, self.pos_type = price_raw, pos_type
        self.back_to_shop = back_to_shop
        self.refresh_target = refresh_target

        def _make_source_btn(src: str, label: str, style):
            b = Button(label=label, style=style, row=0)
            async def _cb(inter: discord.Interaction) -> None:
                await inter.response.send_modal(MarginOpenFinalModal(
                    ticker=ticker, currency=currency, price_raw=price_raw,
                    pos_type=pos_type, source=src,
                    back_to_shop=back_to_shop, refresh_target=refresh_target,
                ))
            b.callback = _cb
            return b

        self.add_item(_make_source_btn('wallet', '從錢包扣 💰', discord.ButtonStyle.success))
        self.add_item(_make_source_btn('bank',   '從銀行扣 🏦', discord.ButtonStyle.primary))

        cancel = Button(label='取消 ✖', style=discord.ButtonStyle.secondary, row=1)
        async def _cancel(inter: discord.Interaction) -> None:
            embed = await build_margin_embed(inter.user.id, inter.user.display_name)
            await inter.response.edit_message(embed=embed, view=MarginSystemView(
                back_to_shop=self.back_to_shop, refresh_target=self.refresh_target,
            ))
        cancel.callback = _cancel
        self.add_item(cancel)


# ── 詢問 ticker Modal（做多/做空入口） ──────────────────────────
class MarginTickerModal(Modal):
    def __init__(self, *, pos_type: str, back_to_shop, refresh_target) -> None:
        kind = '做多' if pos_type == 'long' else '做空'
        super().__init__(title=f'槓桿 {kind} - 查詢標的')
        self.pos_type = pos_type
        self.back_to_shop = back_to_shop
        self.refresh_target = refresh_target
        self.stock_code = TextInput(
            label='股票代碼',
            placeholder='例如 2330.TW 或 AAPL',
            required=True, max_length=15,
        )
        self.add_item(self.stock_code)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        ticker = self.stock_code.value.upper().strip()
        quote = await _fetch_quote(ticker)
        if quote is None:
            await interaction.followup.send(
                f'❌ 找不到 `{ticker}` 或無法取得報價。', ephemeral=True,
            )
            return
        price_raw, currency = quote

        wallet = _wallet.get_balance(str(interaction.user.id))
        async with _db_lock:
            db = _load_db()
            acc = _get_user_account(interaction.user.id, db)
            bank = int(acc['bank'].get('balance', 0))

        max_value_wallet = await _to_coin(price_raw, currency)
        unit_coin = max_value_wallet or 0
        # 10x 最大可買股數（單股保證金 = 單股價值 / 10）
        max_shares_10x = 0
        if unit_coin > 0:
            max_shares_10x = ((wallet + bank) * _MARGIN_LEVERAGE_MAX) // unit_coin

        kind = '做多 📈' if self.pos_type == 'long' else '做空 📉'
        color = discord.Color.green() if self.pos_type == 'long' else discord.Color.red()
        embed = discord.Embed(
            title=f'⚡ 槓桿 {kind}｜{ticker}',
            description='請確認資訊後選擇資金來源。',
            color=color,
        )
        embed.add_field(name='即時報價',
                        value=f'`{_format_raw_price(price_raw, currency)}` ({currency})',
                        inline=True)
        embed.add_field(name='單股約值',
                        value=f'`{unit_coin:,}` 碎片', inline=True)
        embed.add_field(name='錢包餘額', value=f'`{wallet:,}` 碎片', inline=True)
        embed.add_field(name='銀行餘額', value=f'`{bank:,}` 碎片', inline=True)
        embed.add_field(
            name=f'{_MARGIN_LEVERAGE_MAX}x 最大可開股數（錢包+銀行）',
            value=f'`{max_shares_10x:,}` 股', inline=False,
        )
        embed.set_footer(text='⚠ 槓桿交易高風險：權益跌至初始保證金 15% 將強制清算')
        await interaction.edit_original_response(
            embed=embed,
            view=MarginOpenSourceView(
                ticker=ticker, currency=currency, price_raw=price_raw,
                pos_type=self.pos_type,
                back_to_shop=self.back_to_shop,
                refresh_target=self.refresh_target,
            ),
        )


# ── 部分平倉 Modal ───────────────────────────────────────────
class MarginPartialCloseModal(Modal):
    def __init__(self, *, ticker: str, max_shares: int,
                 back_to_shop, refresh_target) -> None:
        super().__init__(title=f'部分平倉 {ticker}')
        self.ticker, self.max_shares = ticker, max_shares
        self.back_to_shop = back_to_shop
        self.refresh_target = refresh_target
        self.shares = TextInput(
            label=f'平倉股數（最多 {max_shares:,}）',
            required=True, max_length=10,
        )
        self.add_item(self.shares)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            close_qty = int(self.shares.value)
            if close_qty <= 0 or close_qty > self.max_shares:
                raise ValueError
        except Exception:
            await interaction.followup.send('❌ 股數無效。', ephemeral=True)
            return

        uid = str(interaction.user.id)
        async with _db_lock:
            db = _load_db()
            acc = _get_user_account(interaction.user.id, db)
            pos = acc['margin']['positions'].get(self.ticker)
            if not pos:
                await interaction.followup.send('❌ 找不到該倉位。', ephemeral=True)
                return
            quote = await _fetch_quote(self.ticker)
            if quote is None:
                await interaction.followup.send('❌ 無法取得報價。', ephemeral=True)
                return
            current_raw, current_currency = quote
            total_equity = await _compute_position_equity(pos, current_raw, current_currency)
            if total_equity is None:
                await interaction.followup.send('❌ 匯率暫無，請稍後。', ephemeral=True)
                return

            total_qty = int(pos['shares'])
            ratio = close_qty / total_qty
            released_margin = int(round(int(pos['margin_locked']) * ratio))
            released_borrowed = int(round(int(pos['borrowed_funds']) * ratio))
            released_equity = int(round(total_equity * ratio))
            realized_pl = released_equity - released_margin

            await _wallet.apply_delta(uid, released_equity)
            if close_qty == total_qty:
                del acc['margin']['positions'][self.ticker]
            else:
                pos['shares'] = total_qty - close_qty
                pos['margin_locked'] = int(pos['margin_locked']) - released_margin
                pos['borrowed_funds'] = int(pos['borrowed_funds']) - released_borrowed
            await save_json_async(_DB_FILE, db)

        sign = '+' if realized_pl >= 0 else ''
        await _refresh_margin_panel(
            interaction, back_to_shop=self.back_to_shop,
            refresh_target=self.refresh_target,
        )
        await interaction.followup.send(
            f'✅ **部分平倉成功** {self.ticker} × {close_qty:,} 股\n'
            f'入帳：`{released_equity:,}` 碎片 ｜ 實現損益：`{sign}{realized_pl:,}` 碎片',
            ephemeral=True,
        )


# ── 平倉選擇 View ────────────────────────────────────────────
class MarginCloseOptionsView(View):
    def __init__(self, *, ticker: str, max_shares: int,
                 back_to_shop, refresh_target) -> None:
        super().__init__(timeout=300)
        self.ticker, self.max_shares = ticker, max_shares
        self.back_to_shop = back_to_shop
        self.refresh_target = refresh_target

        partial = Button(label='輸入股數平倉 ✏️', style=discord.ButtonStyle.primary, row=0)
        async def _partial(inter: discord.Interaction) -> None:
            await inter.response.send_modal(MarginPartialCloseModal(
                ticker=ticker, max_shares=max_shares,
                back_to_shop=back_to_shop, refresh_target=refresh_target,
            ))
        partial.callback = _partial
        self.add_item(partial)

        full = Button(label='一鍵全平 🚪', style=discord.ButtonStyle.danger, row=0)
        async def _full(inter: discord.Interaction) -> None:
            await inter.response.defer()
            uid = str(inter.user.id)
            async with _db_lock:
                db = _load_db()
                acc = _get_user_account(inter.user.id, db)
                pos = acc['margin']['positions'].get(self.ticker)
                if not pos:
                    await inter.followup.send('❌ 倉位已不存在。', ephemeral=True)
                    return
                quote = await _fetch_quote(self.ticker)
                if quote is None:
                    await inter.followup.send('❌ 無法取得報價。', ephemeral=True)
                    return
                current_raw, current_currency = quote
                equity = await _compute_position_equity(pos, current_raw, current_currency)
                if equity is None:
                    await inter.followup.send('❌ 匯率暫無。', ephemeral=True)
                    return
                margin_locked = int(pos['margin_locked'])
                profit = equity - margin_locked
                await _wallet.apply_delta(uid, equity)
                del acc['margin']['positions'][self.ticker]
                await save_json_async(_DB_FILE, db)
            sign = '+' if profit >= 0 else ''
            await _refresh_margin_panel(
                inter, back_to_shop=self.back_to_shop,
                refresh_target=self.refresh_target,
            )
            await inter.followup.send(
                f'✅ **全平 {self.ticker}** 入帳 `{equity:,}` 碎片，實現損益 `{sign}{profit:,}` 碎片',
                ephemeral=True,
            )
        full.callback = _full
        self.add_item(full)

        back = Button(label='⬅️ 返回', style=discord.ButtonStyle.secondary, row=1)
        async def _back(inter: discord.Interaction) -> None:
            embed = await build_margin_embed(inter.user.id, inter.user.display_name)
            await inter.response.edit_message(embed=embed, view=MarginSystemView(
                back_to_shop=self.back_to_shop, refresh_target=self.refresh_target,
            ))
        back.callback = _back
        self.add_item(back)


class _MarginCloseSelect(discord.ui.Select):
    def __init__(self, positions: dict[str, Any],
                 *, back_to_shop, refresh_target) -> None:
        opts = []
        for ticker, pos in list(positions.items())[:25]:
            t_label = '多' if pos.get('type', 'long') == 'long' else '空'
            opts.append(discord.SelectOption(
                label=ticker,
                description=f'{t_label} ｜ 股數 {int(pos["shares"]):,}',
                value=ticker,
            ))
        super().__init__(placeholder='選擇要平倉的倉位…', min_values=1, max_values=1, options=opts)
        self.back_to_shop = back_to_shop
        self.refresh_target = refresh_target

    async def callback(self, interaction: discord.Interaction) -> None:
        ticker = self.values[0]
        async with _db_lock:
            db = _load_db()
            acc = _get_user_account(interaction.user.id, db)
            pos = acc['margin']['positions'].get(ticker)
            if not pos:
                await interaction.response.send_message('❌ 倉位已被清掉。', ephemeral=True)
                return
            max_shares = int(pos['shares'])
        embed = discord.Embed(
            title=f'平倉設定 ｜ {ticker}',
            description='可選擇全部平倉或自訂股數部分平倉。\n收益會直接入錢包。',
            color=discord.Color.orange(),
        )
        await interaction.response.edit_message(embed=embed, view=MarginCloseOptionsView(
            ticker=ticker, max_shares=max_shares,
            back_to_shop=self.back_to_shop, refresh_target=self.refresh_target,
        ))


class MarginCloseSelectView(View):
    def __init__(self, positions: dict[str, Any],
                 *, back_to_shop, refresh_target) -> None:
        super().__init__(timeout=300)
        self.back_to_shop = back_to_shop
        self.refresh_target = refresh_target
        self.add_item(_MarginCloseSelect(
            positions, back_to_shop=back_to_shop, refresh_target=refresh_target,
        ))
        back = Button(label='⬅️ 返回', style=discord.ButtonStyle.secondary, row=1)
        async def _back(inter: discord.Interaction) -> None:
            embed = await build_margin_embed(inter.user.id, inter.user.display_name)
            await inter.response.edit_message(embed=embed, view=MarginSystemView(
                back_to_shop=self.back_to_shop, refresh_target=self.refresh_target,
            ))
        back.callback = _back
        self.add_item(back)


# ── 補保證金 Modal ──────────────────────────────────────────
class MarginAddFinalModal(Modal):
    def __init__(self, *, ticker: str, source: str, pos_type: str,
                 borrowed_cap: int,
                 back_to_shop, refresh_target) -> None:
        super().__init__(title=f'補保證金 {ticker}（{_source_label(source)}）')
        self.ticker = ticker
        self.source = source
        self.pos_type = pos_type
        self.borrowed_cap = borrowed_cap
        self.back_to_shop = back_to_shop
        self.refresh_target = refresh_target

        hint = f'最多 {borrowed_cap:,}' if pos_type == 'long' else '無上限'
        self.amount = TextInput(
            label=f'補入碎片數（{hint}）',
            required=True, max_length=15,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            amt = int(self.amount.value)
            if amt <= 0: raise ValueError
        except Exception:
            await interaction.followup.send('❌ 金額無效。', ephemeral=True)
            return

        uid = str(interaction.user.id)
        async with _db_lock:
            db = _load_db()
            acc = _get_user_account(interaction.user.id, db)
            pos = acc['margin']['positions'].get(self.ticker)
            if not pos:
                await interaction.followup.send('❌ 倉位已不存在。', ephemeral=True)
                return
            if self.pos_type == 'long' and amt > int(pos['borrowed_funds']):
                await interaction.followup.send(
                    f'❌ 補入過多，做多倉借款剩 `{int(pos["borrowed_funds"]):,}`。',
                    ephemeral=True,
                )
                return
            ok, msg = await _deduct_funding(uid, amt, self.source, db)
            if not ok:
                await interaction.followup.send(f'❌ {msg}', ephemeral=True)
                return
            pos['margin_locked'] = int(pos['margin_locked']) + amt
            if self.pos_type == 'long':
                pos['borrowed_funds'] = int(pos['borrowed_funds']) - amt
            # 重新計算槓桿（顯示用）
            total_value = int(pos['margin_locked']) + int(pos['borrowed_funds'])
            pos['leverage'] = round(total_value / max(int(pos['margin_locked']), 1), 1)
            await save_json_async(_DB_FILE, db)

        await _refresh_margin_panel(
            interaction, back_to_shop=self.back_to_shop,
            refresh_target=self.refresh_target,
        )
        await interaction.followup.send(
            f'✅ 已補保證金 `{amt:,}` 碎片（{_source_label(self.source)}）到 {self.ticker}',
            ephemeral=True,
        )


class _MarginAddSelect(discord.ui.Select):
    def __init__(self, positions: dict[str, Any],
                 *, back_to_shop, refresh_target) -> None:
        opts = []
        for ticker, pos in list(positions.items())[:25]:
            t_label = '多' if pos.get('type', 'long') == 'long' else '空'
            opts.append(discord.SelectOption(
                label=ticker,
                description=f'{t_label} ｜ 已鎖保證金 {int(pos["margin_locked"]):,}',
                value=ticker,
            ))
        super().__init__(placeholder='選擇要補保證金的倉位…', min_values=1, max_values=1, options=opts)
        self.back_to_shop = back_to_shop
        self.refresh_target = refresh_target

    async def callback(self, interaction: discord.Interaction) -> None:
        ticker = self.values[0]
        async with _db_lock:
            db = _load_db()
            acc = _get_user_account(interaction.user.id, db)
            pos = acc['margin']['positions'].get(ticker)
            if not pos:
                await interaction.response.send_message('❌ 倉位已被清掉。', ephemeral=True)
                return
            pos_type = pos.get('type', 'long')
            borrowed_cap = int(pos.get('borrowed_funds', 0))

        embed = discord.Embed(
            title=f'補保證金 ｜ {ticker}',
            description='選擇資金來源。做多倉補入會等額沖銷借款；做空倉純加保證金。',
            color=discord.Color.blue(),
        )

        view = View(timeout=300)
        def _make(src: str, label: str, style):
            b = Button(label=label, style=style, row=0)
            async def _cb(inter: discord.Interaction) -> None:
                await inter.response.send_modal(MarginAddFinalModal(
                    ticker=ticker, source=src, pos_type=pos_type,
                    borrowed_cap=borrowed_cap,
                    back_to_shop=self.back_to_shop,
                    refresh_target=self.refresh_target,
                ))
            b.callback = _cb
            return b
        view.add_item(_make('wallet', '從錢包 💰', discord.ButtonStyle.success))
        view.add_item(_make('bank',   '從銀行 🏦', discord.ButtonStyle.primary))

        back = Button(label='⬅️ 返回', style=discord.ButtonStyle.secondary, row=1)
        bts, rt = self.back_to_shop, self.refresh_target
        async def _back(inter: discord.Interaction) -> None:
            embed2 = await build_margin_embed(inter.user.id, inter.user.display_name)
            await inter.response.edit_message(embed=embed2, view=MarginSystemView(
                back_to_shop=bts, refresh_target=rt,
            ))
        back.callback = _back
        view.add_item(back)

        await interaction.response.edit_message(embed=embed, view=view)


class MarginAddSelectView(View):
    def __init__(self, positions: dict[str, Any],
                 *, back_to_shop, refresh_target) -> None:
        super().__init__(timeout=300)
        self.back_to_shop = back_to_shop
        self.refresh_target = refresh_target
        self.add_item(_MarginAddSelect(
            positions, back_to_shop=back_to_shop, refresh_target=refresh_target,
        ))
        back = Button(label='⬅️ 返回', style=discord.ButtonStyle.secondary, row=1)
        async def _back(inter: discord.Interaction) -> None:
            embed = await build_margin_embed(inter.user.id, inter.user.display_name)
            await inter.response.edit_message(embed=embed, view=MarginSystemView(
                back_to_shop=self.back_to_shop, refresh_target=self.refresh_target,
            ))
        back.callback = _back
        self.add_item(back)


# ── 主面板 embed + view ─────────────────────────────────────
async def build_margin_embed(user_id: int, username: str) -> discord.Embed:
    """組槓桿子面板 embed：總保證金、總未實現損益、各倉位列表。"""
    async with _db_lock:
        db = _load_db()
        acc = _get_user_account(user_id, db)
    positions: dict[str, Any] = acc['margin']['positions']

    embed = discord.Embed(
        title=f'⚡ {username} 的槓桿持倉',
        color=discord.Color.gold(),
    )

    if not positions:
        embed.description = '*目前沒有任何槓桿倉位。*\n按下方「做多 📈 / 做空 📉」開倉。'
        embed.set_footer(
            text=f'槓桿範圍 {_MARGIN_LEVERAGE_MIN}~{_MARGIN_LEVERAGE_MAX}x ｜'
                 f'維持率 ≤ {int(_MARGIN_LIQUIDATION_RATIO*100)}% 自動清算',
        )
        return embed

    tickers = list(positions.keys())
    quotes = await asyncio.gather(
        *(_fetch_quote(t) for t in tickers),
        return_exceptions=True,
    )

    total_margin = 0
    total_pl = 0
    lines: list[str] = []
    for ticker, q in zip(tickers, quotes):
        pos = positions[ticker]
        currency = pos.get('currency') or _guess_currency(ticker)
        if isinstance(q, BaseException) or q is None:
            cur_raw, cur_currency = float(pos['entry_price']), currency
        else:
            cur_raw, cur_currency = q
        equity = await _compute_position_equity(pos, cur_raw, cur_currency)
        margin_locked = int(pos.get('margin_locked', 0))
        if equity is None:
            lines.append(f'**🔹 `{ticker}`** ({currency}) — 匯率暫無，無法計價')
            continue
        pl = equity - margin_locked
        ratio = (equity / margin_locked * 100) if margin_locked > 0 else 0.0
        total_margin += margin_locked
        total_pl += pl
        sign = '+' if pl >= 0 else ''
        risk_emoji = '🟢' if ratio > 50 else ('🟡' if ratio > 20 else '🔴')
        pos_type = pos.get('type', 'long')
        kind = '多' if pos_type == 'long' else '空'
        tp_s = f' ｜ TP {_format_raw_price(float(pos["tp"]), currency)}' if pos.get('tp') else ''
        sl_s = f' ｜ SL {_format_raw_price(float(pos["sl"]), currency)}' if pos.get('sl') else ''
        lines.append(
            f'**{risk_emoji} `{ticker}`** ({kind} {pos.get("leverage", "?")}x ｜ {currency})\n'
            f' └ 進場：`{_format_raw_price(float(pos["entry_price"]), currency)}` '
            f'｜ 現價：`{_format_raw_price(cur_raw, cur_currency)}`{tp_s}{sl_s}\n'
            f' └ 股數：`{int(pos["shares"]):,}` ｜ 保證金：`{margin_locked:,}` 碎片\n'
            f' └ 未實現：`{sign}{pl:,}` 碎片 ｜ 維持率：`{round(ratio,2)}%`\n'
        )

    t_sign = '+' if total_pl >= 0 else ''
    embed.add_field(name='💰 總保證金', value=f'`{total_margin:,}` 碎片', inline=True)
    embed.add_field(name='🔥 總未實現損益',
                    value=f'`{t_sign}{total_pl:,}` 碎片', inline=True)
    embed.add_field(name='📜 倉位列表', value='\n'.join(lines)[:1024] or '—', inline=False)
    embed.set_footer(
        text=f'維持率 ≤ {int(_MARGIN_LIQUIDATION_RATIO*100)}% 自動清算 ｜ 行情快取 60 秒',
    )
    return embed


class MarginSystemView(View):
    """槓桿子面板。從 StockSystemView 的「槓桿交易」按鈕進來；
    返回按鈕會切回 StockSystemView（保留 back_to_shop / refresh_target context）。"""

    def __init__(self, *, back_to_shop=None, refresh_target=None) -> None:
        super().__init__(timeout=300)
        self.back_to_shop = back_to_shop
        self.refresh_target = refresh_target
        self._build()

    def _build(self) -> None:
        bts, rt = self.back_to_shop, self.refresh_target

        long_btn = Button(label='做多 📈', style=discord.ButtonStyle.success, row=0)
        long_btn.callback = lambda i: i.response.send_modal(MarginTickerModal(
            pos_type='long', back_to_shop=bts, refresh_target=rt,
        ))
        self.add_item(long_btn)

        short_btn = Button(label='做空 📉', style=discord.ButtonStyle.danger, row=0)
        short_btn.callback = lambda i: i.response.send_modal(MarginTickerModal(
            pos_type='short', back_to_shop=bts, refresh_target=rt,
        ))
        self.add_item(short_btn)

        close_btn = Button(label='平倉 📊', style=discord.ButtonStyle.primary, row=0)
        async def _open_close(inter: discord.Interaction) -> None:
            async with _db_lock:
                db = _load_db()
                acc = _get_user_account(inter.user.id, db)
                positions = dict(acc['margin']['positions'])
            if not positions:
                await inter.response.send_message('❌ 沒有可平倉的倉位。', ephemeral=True)
                return
            await inter.response.edit_message(view=MarginCloseSelectView(
                positions, back_to_shop=bts, refresh_target=rt,
            ))
        close_btn.callback = _open_close
        self.add_item(close_btn)

        add_btn = Button(label='補保證金 🛡', style=discord.ButtonStyle.secondary, row=1)
        async def _open_add(inter: discord.Interaction) -> None:
            async with _db_lock:
                db = _load_db()
                acc = _get_user_account(inter.user.id, db)
                positions = dict(acc['margin']['positions'])
            if not positions:
                await inter.response.send_message('❌ 沒有倉位可補保證金。', ephemeral=True)
                return
            await inter.response.edit_message(view=MarginAddSelectView(
                positions, back_to_shop=bts, refresh_target=rt,
            ))
        add_btn.callback = _open_add
        self.add_item(add_btn)

        refresh = Button(label='刷新 🔄', style=discord.ButtonStyle.secondary, row=1)
        async def _redraw(inter: discord.Interaction) -> None:
            embed = await build_margin_embed(inter.user.id, inter.user.display_name)
            await inter.response.edit_message(embed=embed, view=self)
        refresh.callback = _redraw
        self.add_item(refresh)

        back = Button(label='⬅️ 返回銀行', style=discord.ButtonStyle.secondary, row=2)
        async def _back(inter: discord.Interaction) -> None:
            pages = await build_account_pages(inter.user.id, inter.user.display_name)
            view = StockSystemView(
                pages, page_idx=0,
                back_to_shop=bts, refresh_target=rt,
            )
            await inter.response.edit_message(embed=pages[0], view=view)
        back.callback = _back
        self.add_item(back)


async def _refresh_margin_panel(interaction: discord.Interaction, *,
                                 back_to_shop, refresh_target) -> None:
    """Modal submit 完後刷 margin 面板。interaction.message 是 panel 本身。"""
    try:
        embed = await build_margin_embed(interaction.user.id, interaction.user.display_name)
        view = MarginSystemView(back_to_shop=back_to_shop, refresh_target=refresh_target)
        if interaction.message is not None:
            await interaction.message.edit(embed=embed, view=view)
    except Exception as e:
        print(f'[MARGIN] refresh panel failed: {e}')


# ==========================================
# 背景任務：SL/TP + 強制清算
# ==========================================
async def _margin_monitor_once() -> None:
    """掃所有用戶的槓桿倉位，觸發 SL/TP 或強制清算，所有變更走 _db_lock。"""
    async with _db_lock:
        db = _load_db()
        # 先收齊所有 ticker，一次抓報價
        tickers: set[str] = set()
        for uid, acc in db.items():
            margin = acc.get('margin') if isinstance(acc, dict) else None
            if not margin: continue
            for t in (margin.get('positions') or {}).keys():
                tickers.add(t)

    if not tickers:
        return

    tickers_list = list(tickers)
    quotes = await asyncio.gather(
        *(_fetch_quote(t) for t in tickers_list),
        return_exceptions=True,
    )
    quote_map: dict[str, tuple[float, str]] = {}
    for t, q in zip(tickers_list, quotes):
        if not isinstance(q, BaseException) and q is not None:
            quote_map[t] = q

    async with _db_lock:
        db = _load_db()
        changed = False
        for uid, acc in list(db.items()):
            if not isinstance(acc, dict):
                continue
            margin = acc.get('margin') or {}
            positions = margin.get('positions') or {}
            for ticker, pos in list(positions.items()):
                quote = quote_map.get(ticker)
                if quote is None:
                    continue
                current_raw, current_currency = quote
                margin_locked = int(pos.get('margin_locked', 0))
                equity = await _compute_position_equity(pos, current_raw, current_currency)
                if equity is None:
                    continue

                trigger: str | None = _check_sl_tp_trigger(pos, current_raw)
                # 強制清算：權益 ≤ 初始保證金 × 15%
                if margin_locked > 0 and (equity / margin_locked) <= _MARGIN_LIQUIDATION_RATIO:
                    trigger = trigger or 'liquidate'
                    equity = 0  # 強制清算保證金歸零

                if trigger:
                    await _wallet.apply_delta(uid, equity)
                    del positions[ticker]
                    changed = True
                    print(f'[MARGIN] uid={uid} ticker={ticker} trigger={trigger} '
                          f'equity_returned={equity}')
        if changed:
            await save_json_async(_DB_FILE, db)


async def _margin_monitor_loop() -> None:
    while True:
        try:
            await _margin_monitor_once()
        except Exception as e:
            print(f'[MARGIN] monitor loop error: {e}')
        await asyncio.sleep(_MARGIN_MONITOR_INTERVAL_SEC)


def start_liquidation_task() -> asyncio.Task:
    return asyncio.create_task(_margin_monitor_loop())


# ==========================================
# 指令註冊
# ==========================================
def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name='電子銀行股市',
                  description='開啟電子銀行 + 股市虛擬交易主面板')
    async def slash_bank_stock(interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        pages = await build_account_pages(
            interaction.user.id, interaction.user.display_name,
        )
        await interaction.followup.send(
            embed=pages[0], view=StockSystemView(pages, page_idx=0),
        )
