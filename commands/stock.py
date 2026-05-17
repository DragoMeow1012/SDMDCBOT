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


async def _get_usd_twd_rate() -> float | None:
    """1 USD = ? TWD，10 分鐘快取一次。"""
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
        await _refresh_main_menu(interaction)


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
        await _refresh_main_menu(interaction)


# ==========================================
# Modal: 存款 / 提款
# ==========================================
class DepositModal(Modal, title='銀行存款'):
    amount: TextInput = TextInput(
        label='存入碎片數量', placeholder='請輸入正整數',
        required=True, max_length=12,
    )

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
        await _refresh_main_menu(interaction)


class WithdrawModal(Modal, title='銀行提款'):
    amount: TextInput = TextInput(
        label='提取碎片數量', placeholder='請輸入正整數',
        required=True, max_length=12,
    )

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
        await _refresh_main_menu(interaction)


# ==========================================
# View: 主面板按鈕列
# ==========================================
class StockSystemView(View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label='買入股票 📈', style=discord.ButtonStyle.success,
                       custom_id='btn_buy_stock', row=0)
    async def buy_button(self, interaction: discord.Interaction, button: Button) -> None:
        await interaction.response.send_modal(BuyStockModal())

    @discord.ui.button(label='賣出股票 📉', style=discord.ButtonStyle.danger,
                       custom_id='btn_sell_stock', row=0)
    async def sell_button(self, interaction: discord.Interaction, button: Button) -> None:
        await interaction.response.send_modal(SellStockModal())

    @discord.ui.button(label='存款 🏦', style=discord.ButtonStyle.success,
                       custom_id='btn_deposit', row=1)
    async def deposit_button(self, interaction: discord.Interaction, button: Button) -> None:
        await interaction.response.send_modal(DepositModal())

    @discord.ui.button(label='提款 💵', style=discord.ButtonStyle.danger,
                       custom_id='btn_withdraw', row=1)
    async def withdraw_button(self, interaction: discord.Interaction, button: Button) -> None:
        await interaction.response.send_modal(WithdrawModal())

    @discord.ui.button(label='刷新資產 🔄', style=discord.ButtonStyle.primary,
                       custom_id='btn_refresh_stock', row=2)
    async def refresh_button(self, interaction: discord.Interaction, button: Button) -> None:
        await interaction.response.defer()
        await _refresh_main_menu(interaction)


# ==========================================
# 渲染：資產 + 銀行 Embed
# ==========================================
async def _build_account_embed(user_id: int, username: str) -> discord.Embed:
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

    embed = discord.Embed(
        title=f'🏦 {username} 的電子銀行 & 股市帳戶',
        color=discord.Color.dark_teal(),
    )

    total_stock_coin = 0
    total_pl_coin = 0
    total_cost_coin = 0

    if not portfolio:
        details = '*目前無任何持股紀錄，趕快點擊下方按鈕開始交易吧！*'
    else:
        lines: list[str] = []
        for ticker, data in portfolio.items():
            shares = int(data['shares'])
            avg_raw = float(data['average_price'])
            currency = data.get('currency') or _guess_currency(ticker)

            quote = await _fetch_quote(ticker)
            cur_raw, cur_currency = (avg_raw, currency) if quote is None else quote

            cur_total_coin = await _to_coin(cur_raw * shares, cur_currency)
            cost_total_coin = await _to_coin(avg_raw * shares, currency)
            if cur_total_coin is None or cost_total_coin is None:
                # 匯率拿不到時，跳過此檔避免顯示錯誤數字
                lines.append(
                    f'**🔹 `{ticker}`** ({currency}) 持股 `{shares:,}` — 匯率暫無，無法計價'
                )
                continue

            pl = cur_total_coin - cost_total_coin
            roi = (pl / cost_total_coin) * 100 if cost_total_coin > 0 else 0.0

            total_stock_coin += cur_total_coin
            total_pl_coin += pl
            total_cost_coin += cost_total_coin

            unit_avg_coin = await _to_coin(avg_raw, currency)
            unit_cur_coin = await _to_coin(cur_raw, cur_currency)
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

    embed.add_field(name='💰 錢包', value=f'`{wallet:,}` 碎片', inline=True)
    embed.add_field(name='🏦 銀行存款', value=f'`{bank_balance:,}` 碎片', inline=True)
    embed.add_field(name='📊 股票市值', value=f'`{total_stock_coin:,}` 碎片', inline=True)
    embed.add_field(name='💎 淨資產 (Net Worth)',
                    value=f'`{net_worth:,}` 碎片', inline=False)
    embed.add_field(name='🔥 總未實現損益',
                    value=f'`{total_sign}{total_pl_coin:,}` 碎片 '
                          f'(`{total_sign}{round(total_roi, 2)}%`)',
                    inline=False)
    embed.add_field(name='📜 持股詳細清單', value=details[:1024], inline=False)

    embed.set_footer(
        text='資料來源：Yahoo Finance（台股報價約延遲 15-20 分鐘）｜'
             '匯率：1 NT$ = 1 咕嚕喵碎片'
    )
    return embed


async def _refresh_main_menu(interaction: discord.Interaction) -> None:
    user = interaction.user
    embed = await _build_account_embed(user.id, user.display_name)
    try:
        await interaction.edit_original_response(embed=embed, view=StockSystemView())
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
        embed = await _build_account_embed(
            interaction.user.id, interaction.user.display_name,
        )
        await interaction.followup.send(embed=embed, view=StockSystemView())
