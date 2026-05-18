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
    """抓即時收盤價 + 幣別。yfinance 內 fast_info 拿不到 currency 時用字尾猜。"""
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
    return await asyncio.to_thread(_fetch)


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


def _get_user_account(user_id: int, db: dict[str, Any]) -> dict[str, Any]:
    uid = str(user_id)
    acc = db.get(uid)
    if not acc:
        acc = {'portfolio': {}, 'bank': {'balance': 0, 'last_interest': None}}
        db[uid] = acc
        return acc
    acc.setdefault('portfolio', {})
    acc.setdefault('bank', {'balance': 0, 'last_interest': None})
    return acc


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
        # row 0: 交易
        buy = Button(label='買入股票 📈', style=discord.ButtonStyle.success, row=0)
        buy.callback = lambda i: i.response.send_modal(
            BuyStockModal(back_to_shop=back, refresh_target=rt))
        self.add_item(buy)

        sell = Button(label='賣出股票 📉', style=discord.ButtonStyle.danger, row=0)
        sell.callback = lambda i: i.response.send_modal(
            SellStockModal(back_to_shop=back, refresh_target=rt))
        self.add_item(sell)

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
        async def _refresh(inter: discord.Interaction) -> None:
            await inter.response.defer()
            await _refresh_main_menu(inter, back_to_shop=back, refresh_target=rt)
        refresh.callback = _refresh
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
    """/帳戶總覽 用的唯讀分頁 view。只有 prev / next / close。"""

    def __init__(self, pages: list[discord.Embed],
                 page_idx: int = 0) -> None:
        super().__init__(timeout=300)
        self.pages    = pages
        self.page_idx = page_idx
        self._build(pages, page_idx)

    def _build(self, pages: list[discord.Embed], page_idx: int) -> None:
        if len(pages) > 1:
            _add_pager_buttons(self, pages, page_idx, row=0)
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
