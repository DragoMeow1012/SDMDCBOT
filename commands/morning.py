"""
/早安小龍喵：每日打卡（以台灣時間 00:00 為刷新點）。

  - 依時段切換標題：早安 / 午安 / 暮色（0~3 點禁止打卡）
  - 全域 + 本群排名
  - 累計 / 連續打卡天數
  - 咕嚕喵碎片 500~5000 隨機獎勵（每日全域只發一次）
  - Gemini 隨機生成幸運物（失敗自動 fallback）
  - 財運 / 桃花運 / 事業運 0~150 + 綜合運勢 + 星評

重複打卡規則：
  - 同一伺服器第 2 次以上 → 顯示提醒 + 今日成績單
  - 跨到其他伺服器：第 1 次不提醒、僅顯示今日成績單；第 2 次以上才提醒
  - 不論在哪個群再打，獎勵都不會重發

/查看帳戶餘額：顯示咕嚕喵碎片與打卡狀態。

資料檔 data/morning_records.json：
{
  "day_key": "YYYY-MM-DD",                     # 當前邏輯日（台灣 00:00 為界）
  "global_order": ["uid", ...],                # 今日全域首打順序
  "guilds_order": {"gid": ["uid", ...]},       # 今日各群首打順序
  "guild_signin_count": {"gid": {"uid": n}},   # 今日同伺服器打卡次數
  "user_today": {                              # 凍結的今日成績單（重複打卡時直接顯示）
    "uid": {"signin_time": "...", "global_rank": int,
            "lucky_item": str, "coin": int,
            "money_luck": int, "love_luck": int, "work_luck": int,
            "fortune_name": str, "stars": int,
            "total_days": int, "streak": int,
            "title": str, "greeting": str, "color": int,
            "guild_id": str,                     # 補簽重繪 embed 用
            "prev_last_day": str | None,         # 補簽判定：=2 天前才允許
            "prev_streak":   int}                # 補簽復原 streak 的基準
  },
  "users": {
    "uid": {"balance": int, "total_days": int,
            "streak": int, "last_day": "YYYY-MM-DD"}
  }
}
"""
from __future__ import annotations

import asyncio
import os
import random
from datetime import date, datetime, timedelta, timezone
from typing import Any

import discord
from discord import app_commands

from utils.json_store import load_json, save_json_async


_FILE = os.path.join('data', 'morning_records.json')
_TZ   = timezone(timedelta(hours=8))  # 台灣時間

_MAKEUP_COST = 500  # 補簽費用（咕嚕喵碎片）

# (起時, 迄時(含), 標題, 固定招呼語, 隨機第二句清單, embed 顏色)
_PERIODS: list[tuple[int, int, str, str, list[str], discord.Color]] = [
    (4, 11,
     '早安打卡！美好的一天開始囉 ⸜(* ॑꒳ ॑* )⸝',
     '',
     [
         '新的一天也要閃閃發光呀 ✨',
         '用微笑開啟美好的一天 ☀️(๑´ㅂ`๑)',
         '伸個懶腰！今天也要加油鴨 ٩(ˊᗜˋ*)و',
         '把昨天的煩惱都格式化吧 (•̀ᴗ•́)و ̑̑',
         '帶著滿滿的正能量，出發！(๑•̀ㅂ•́)و✧',
         '呼吸新鮮空氣，今天也是幸運爆棚的一天 🍀',
         '鬧鐘響起，又是充滿希望的開始 ٩(ˊᗜˋ*)و',
         '迎接晨光！今天也要元氣滿滿喔 ( `∇´ )',
         '把生活調成自己喜歡的頻道 🎵',
         '美好的一天，從一杯香濃的咖啡開始 ☕️',
         '滿血復活！新的一天請多多指教囉 ⸜(* ॑꒳ ॑* )⸝',
         '給自己一個大大的微笑，早安！✨',
     ],
     discord.Color.gold()),
    (12, 17,
     '(๑´ㅂ`๑) 醒來變午安！雖然錯過了早餐！但是靈魂充飽電感覺超級爽啦～',
     '',
     [
         '我的時差跟世界好像不太一樣 (º﹃º)',
         '醒來直接吃午餐，省了一餐賺到了',
         '完美的睡眠，就是對週末最大的尊重✨',
         '雖然錯過了早晨，但午后的陽光剛剛好 🌤️',
         '靈魂休眠結束！午安，世界 🔋(•̀ᴗ•́)و ̑̑',
         '睡到自然醒的快樂，誰懂❤️',
         '早起的人有鳥吃，那晚起的人吃午餐 🐦',
         '成功躲過早上的所有麻煩事 ✌️(✨∀✨)',
         '午安！我的床今天抓著我不放，真的不怪我 🛌',
         '太陽都曬到屁股了，那我翻個身繼續……啊不行該醒了 🍑',
         '錯過早晨不可惜，因為精彩的午后才剛開始 🥤',
         '醒來就看到大太陽，心情直接好起來 🌞',
         '下午茶時間到！這才是我的開機時間 🍰',
     ],
     discord.Color.orange()),
    (18, 23,
     '暮色降臨！夜貓子開機了~睡飽飽就是最棒的超能力！雖然跳過了白天，但夜晚的世界依然很精彩✨',
     '',
     [
         '太陽下山，才是我的高光時刻 🌙(✧﹃ ✧)',
         '越夜越美麗！夜貓子聯誼會現在開始 ✨',
         '白天在睡覺，晚上在拯救世界 (🛠️•͈ᴗ•͈)',
         '睡飽後的夜晚，連空氣都是自由的味道 🍃',
         '完美的避開了白天的喧囂🎉',
         "晚安是別人的，我的精彩才剛要開始 (◦'⌣'◦)",
         '白天都在充電，現在是放電時間 ⚡',
         '夜幕低垂，點燃我的夜貓魂 🔥',
         '錯過了白天的太陽，但我擁有整片星空 ⭐',
         '睡飽飽的超能力，讓我在晚上閃閃發光 🌟',
         '屬於夜貓子的派對，現在才要嗨起來 🎵',
         '夜晚的寧靜，配上睡飽的靈魂🍷',
         '抱歉了白天，我真的是屬於夜晚的生物 🦇',
         '黑夜給了我黑色的眼睛，我用它來熬夜 (👁️ᴗ👁️)',
         '越晚精神越好，這到底是什麼神祕體質 🌀',
         '夜晚的世界，連風都特別溫柔 ✨',
         '滿血復活的夜貓，準備出來覓食囉 🍕',
         '跳過白天的忙碌，直接享受夜晚的精彩 ✌️',
     ],
     discord.Color.purple()),
]

# 由壞到好的運勢階梯：吉運每階佔 2 格、凶運每階佔 1 格（總計 19 格）。
# 用 (財運+桃花運+事業運)/450 對應到 ratio，再依比例落入某一階。
_FORTUNE_TIERS: list[tuple[str, int]] = [
    ('大凶', 1), ('末凶', 1), ('半凶', 1), ('小凶', 1), ('凶', 1),
    ('末小吉', 2), ('末吉', 2), ('半吉', 2), ('吉', 2),
    ('小吉', 2), ('中吉', 2), ('大吉', 2),
]
_FORTUNE_TOTAL: int = sum(w for _, w in _FORTUNE_TIERS)

_FALLBACK_SOUP = [
    '當你不再害怕跌倒，整個世界都會為你讓出一條路。',
    '別讓昨天的雨，淋濕了今天的太陽。',
    '勇敢不是不害怕，而是即使害怕也願意往前走一步。',
    '生活不是等待風暴過去，而是學會在雨中跳舞。',
    '不必活成誰的樣子，做自己就是最美的風景。',
    '走得慢沒關係，只要方向是對的，遲早會抵達。',
    '今天的努力，是明天的禮物。',
    '把每一個平凡的日子，都過成自己喜歡的樣子。',
    '心若向陽，無懼悲傷；步若堅定，何懼遠方。',
    '黑夜再長，總會迎來黎明；困難再大，總會被你跨過。',
    '善待自己，是一切美好的起點。',
    '世界很大，請保持柔軟，繼續發光。',
    '微笑是最便宜的禮物，卻能改變整個世界。',
    '不被理解時，記得：你不需要被所有人懂，只需要被自己愛。',
    '每一次跌倒，都是為了讓你站得更穩。',
    '把焦慮留給昨天，把希望留給今天。',
]

_FALLBACK_LUCKY = [
    '溫熱奶茶', '糖葫蘆', '熱呼呼包子', '小熊軟糖',
    '巧克力布丁', '草莓棒棒糖', '檸檬塔', '抹茶蛋糕',
    '銅鑼燒', '馬卡龍', '蘋果汁', '一顆水蜜桃',
    '櫻花髮夾', '貓爪襪', '可愛口罩', '潤唇膏',
    '護手霜', '保溫杯', '療癒手帳本', '楓葉書籤',
    '幸運星貼紙', '彩虹原子筆', '蘋果造型橡皮擦', '兔子玩偶',
]

# (類別, 範例) — 每次呼叫 Gemini 前隨機抽一類，避免結果群聚到少數熱門品項。
_LUCKY_CATEGORIES: list[tuple[str, str]] = [
    ('飲品',     '熱拿鐵、珍珠奶茶、椰子水、蜂蜜檸檬'),
    ('甜點',     '布丁、銅鑼燒、蛋塔、提拉米蘇'),
    ('鹹食',     '飯糰、可頌、咖哩飯、玉子燒'),
    ('水果',     '蘋果、葡萄、芒果、奇異果'),
    ('零食',     '洋芋片、巧克力、牛軋糖、魷魚絲'),
    ('文具',     '原子筆、便利貼、橡皮擦、書籤'),
    ('書籍刊物', '小說、漫畫、散文集、雜誌'),
    ('飾品',     '手鍊、髮圈、耳環、項鍊'),
    ('個人保養', '潤唇膏、護手霜、面膜、髮蠟'),
    ('日用品',   '保溫杯、馬克杯、口罩、雨傘'),
    ('衣物配件', '帽子、襪子、圍巾、手帕'),
    ('3C 小物', '無線耳機、滑鼠墊、隨身碟、行動電源'),
    ('家居小物', '抱枕、毛毯、香氛蠟燭、月曆'),
    ('運動用品', '瑜伽墊、運動毛巾、跳繩、水壺'),
    ('植物',     '多肉植物、鬱金香、向日葵、薄荷盆栽'),
    ('紀念小物', '明信片、貼紙、鑰匙圈、徽章'),
    ('玩具',     '魔術方塊、絨毛娃娃、撲克牌、桌遊'),
]


def _now_tw() -> datetime:
    return datetime.now(_TZ)


def _signin_day_key(now: datetime | None = None) -> str:
    """台灣 00:00 為界，回傳當日 key (YYYY-MM-DD)。"""
    return (now or _now_tw()).date().isoformat()


def _yesterday_key(day_key: str) -> str:
    return (date.fromisoformat(day_key) - timedelta(days=1)).isoformat()


def _period_info(now: datetime) -> tuple[str, str, list[str], discord.Color]:
    h = now.hour
    for start, end, title, greeting, extras, color in _PERIODS:
        if start <= h <= end:
            return title, greeting, extras, color
    return _PERIODS[0][2], _PERIODS[0][3], _PERIODS[0][4], _PERIODS[0][5]


def _ensure_today(data: dict[str, Any], day_key: str) -> None:
    """日期換了就清掉舊的 daily 統計（保留 users）。"""
    if data.get('day_key') != day_key:
        data['day_key']            = day_key
        data['global_order']       = []
        data['guilds_order']       = {}
        data['guild_signin_count'] = {}
        data['user_today']         = {}
    data.setdefault('global_order', [])
    data.setdefault('guilds_order', {})
    data.setdefault('guild_signin_count', {})
    data.setdefault('user_today', {})


def _format_signin_time(now: datetime) -> str:
    """西元年/月/日 H:MM，月日時不補零、分補零。"""
    return f'{now.year}/{now.month}/{now.day} {now.hour}:{now.minute:02d}'


def _fortune_from_ratio(ratio: float) -> str:
    """ratio ∈ [0, 1] → 對應 19 格運勢階梯，吉運每階寬 2 格、凶運每階寬 1 格。"""
    ratio = max(0.0, min(1.0, ratio))
    pos = ratio * _FORTUNE_TOTAL
    cum = 0
    for name, weight in _FORTUNE_TIERS:
        cum += weight
        if pos < cum:
            return name
    return _FORTUNE_TIERS[-1][0]


def _star_bar(score: float, total: int = 10) -> str:
    """整數星格 (四捨五入) + 「(n.n/10)」分數標示，n 保留 1 位小數。"""
    score = max(0.0, min(float(total), float(score)))
    filled = int(round(score))
    bar    = '★' * filled + '☆' * (total - filled)
    return f'{bar} ({score:.1f}/{total})'


async def _generate_soup() -> str:
    """讓 Gemini 生成一句心靈雞湯（正能量短語），失敗 fallback 內建清單。"""
    try:
        from gemini_worker import _client  # type: ignore[attr-defined]
        from config import GEMINI_MODEL_NAME
        if _client is None:
            raise RuntimeError('gemini client not initialised')

        def _call() -> str:
            resp = _client.models.generate_content(
                model=GEMINI_MODEL_NAME,
                contents=(
                    '請隨機生成一句「心靈雞湯」，提供溫暖正能量的短語或語錄。'
                    '20~50 字、使用繁體中文。'
                    '只回覆語錄本身，不加引號、標題、署名、表情符號。'
                ),
            )
            return (resp.text or '').strip()

        text = await asyncio.wait_for(asyncio.to_thread(_call), timeout=8.0)
        text = text.splitlines()[0].strip(' 「」"\'：:　')
        return text[:120] if text else random.choice(_FALLBACK_SOUP)
    except Exception:
        return random.choice(_FALLBACK_SOUP)


async def _generate_lucky_item() -> str:
    """讓 Gemini 抽一項幸運物，逾時或失敗時 fallback 到內建清單。

    每次呼叫隨機抽一個類別塞進 prompt，避免 Gemini 收斂到少數熱門答案；
    並調高 temperature 讓同類別內也有變化。
    """
    category, examples = random.choice(_LUCKY_CATEGORIES)
    try:
        from google.genai import types

        from gemini_worker import _client  # type: ignore[attr-defined]
        from config import GEMINI_MODEL_NAME
        if _client is None:
            raise RuntimeError('gemini client not initialised')

        def _call() -> str:
            resp = _client.models.generate_content(
                model=GEMINI_MODEL_NAME,
                contents=(
                    f'從「{category}」這個類別，挑一項今天的「幸運物」。\n'
                    f'參考方向：{examples}（請再想一個別的，不要直接重複範例）。\n'
                    '必須是日常真實存在、隨手可得的具體物品；'
                    '禁止奇幻、魔法、會發光、會說話、虛構角色周邊、神秘扭蛋等不真實的東西。\n'
                    '只回覆物品名稱本身，繁體中文 4~12 字，'
                    '不加任何說明、引號、標點、表情符號。'
                ),
                config=types.GenerateContentConfig(temperature=1.2),
            )
            return (resp.text or '').strip()

        text = await asyncio.wait_for(asyncio.to_thread(_call), timeout=8.0)
        text = text.splitlines()[0].strip(' 「」"\'：:。.！!?？　')
        return text[:24] if text else random.choice(_FALLBACK_LUCKY)
    except Exception:
        return random.choice(_FALLBACK_LUCKY)


def _build_embed(today: dict[str, Any], guild_rank: int,
                 *, display_name: str, balance: int) -> discord.Embed:
    desc_lines: list[str] = []
    if today.get('greeting'):
        desc_lines.append(today['greeting'])
    if today.get('extra_line'):
        desc_lines.append(today['extra_line'])
    desc_lines += [
        '',
        f'打卡時間：**{today["signin_time"]}**',
        f'您今天是第 **{today["global_rank"]}** 位，'
        f'也是本群第 **{guild_rank}** 位起床的~',
        f'🎉 累計早安 **{today["total_days"]}** 天，'
        f'連續早安 **{today["streak"]}** 天啦~',
        f'今天你的幸運物是：**{today["lucky_item"]}**',
        f'【咕嚕喵碎片 +{today["coin"]}】',
        '--------------------------------------',
        '今日雞湯：',
        today.get('soup', ''),
        '--------------------------------------',
        '少女祈禱中(~▽~")...您今天的運勢為...',
        f'財運：**{today["money_luck"]}**',
        f'桃花運：**{today["love_luck"]}**',
        f'事業運：**{today["work_luck"]}**',
        f'綜合運勢：**{today["fortune_name"]}**',
        _star_bar(today['stars']),
    ]

    embed = discord.Embed(
        title=today['title'],
        description='\n'.join(desc_lines),
        color=discord.Color(today['color']),
    )
    embed.set_footer(text=f'{display_name}　帳戶餘額：{balance} 咕嚕喵碎片')
    return embed


class MorningView(discord.ui.View):
    """打卡卡片底下的補簽按鈕。每張卡只配給原打卡者使用，24h 後自動失效。"""

    def __init__(self, uid: str):
        super().__init__(timeout=86400)
        self.uid = uid

    @discord.ui.button(
        label='昨天忘記簽到?花500咕嚕喵碎片補簽!',
        style=discord.ButtonStyle.primary,
    )
    async def makeup(self, interaction: discord.Interaction,
                     button: discord.ui.Button) -> None:
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message(
                '這不是你的打卡卡片喔', ephemeral=True,
            )
            return

        data        = load_json(_FILE)
        users       = data.setdefault('users', {})
        snap        = data.get('user_today', {}).get(self.uid)
        rec         = users.get(self.uid)
        day_key     = data.get('day_key', '')

        if not snap or not rec or not day_key:
            await interaction.response.send_message(
                '找不到今天的打卡紀錄，請先 /早安小龍喵', ephemeral=True,
            )
            return

        prev_last_day = snap.get('prev_last_day')
        prev_streak   = int(snap.get('prev_streak', 0))
        yesterday     = _yesterday_key(day_key)
        two_days_ago  = _yesterday_key(yesterday)

        if prev_last_day != two_days_ago:
            if prev_last_day == yesterday:
                msg = '你昨天本來就有打卡，不用補喔'
            elif prev_last_day is None:
                msg = '你之前沒打卡紀錄，沒得補'
            else:
                msg = '你已經斷超過一天了，補單一天救不回 streak ㄏㄏ'
            await interaction.response.send_message(msg, ephemeral=True)
            return

        balance = int(rec.get('balance', 0))
        if balance < _MAKEUP_COST:
            await interaction.response.send_message(
                f'餘額不足，補簽需要 {_MAKEUP_COST} 碎片，你只有 {balance}',
                ephemeral=True,
            )
            return

        # 套用補簽：扣費、累計+1、streak 復原為 prev_streak + 2
        # (prev_streak 是 2 天前的 streak；補上昨天 +1，再算今天 +1)
        rec['balance']    = balance - _MAKEUP_COST
        rec['total_days'] = int(rec.get('total_days', 0)) + 1
        rec['streak']     = prev_streak + 2

        snap['total_days']    = rec['total_days']
        snap['streak']        = rec['streak']
        snap['prev_last_day'] = yesterday    # 再按一次會被擋掉
        snap['prev_streak']   = prev_streak + 1

        await save_json_async(_FILE, data)

        # 重繪 embed：guild_rank 由 snap.guild_id 反查
        gid         = snap.get('guild_id', '')
        guild_order = data.get('guilds_order', {}).get(gid, [])
        guild_rank  = guild_order.index(self.uid) + 1 if self.uid in guild_order else 1
        new_embed   = _build_embed(
            snap, guild_rank,
            display_name=interaction.user.display_name,
            balance=rec['balance'],
        )

        button.disabled = True
        button.label    = '已補簽 ✓'
        await interaction.response.edit_message(embed=new_embed, view=self)
        self.stop()


class TransferView(discord.ui.View):
    """/轉帳 邀請訊息底下的同意 / 拒絕按鈕。"""

    def __init__(self, sender_id: str, recipient_id: str,
                 amount: int, sender_name: str):
        super().__init__(timeout=86400)
        self.sender_id    = sender_id
        self.recipient_id = recipient_id
        self.amount       = amount
        self.sender_name  = sender_name

    async def _only_recipient(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.recipient_id:
            await interaction.response.send_message(
                '這不是給你的轉帳邀請', ephemeral=True,
            )
            return False
        return True

    def _disable_all(self) -> None:
        for c in self.children:
            if isinstance(c, discord.ui.Button):
                c.disabled = True

    @discord.ui.button(label='同意接收', style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction,
                     button: discord.ui.Button) -> None:
        if not await self._only_recipient(interaction):
            return

        data   = load_json(_FILE)
        users  = data.setdefault('users', {})
        sender = users.get(self.sender_id)
        if not sender or int(sender.get('balance', 0)) < self.amount:
            self._disable_all()
            await interaction.response.edit_message(
                content=None,
                embed=discord.Embed(
                    title='❌ 轉帳失敗',
                    description='發送方餘額不足',
                    color=discord.Color.red(),
                ),
                view=self,
            )
            self.stop()
            return

        recipient = users.setdefault(self.recipient_id, {
            'balance':    0,
            'total_days': 0,
            'streak':     0,
            'last_day':   None,
        })
        sender['balance']    = int(sender.get('balance', 0)) - self.amount
        recipient['balance'] = int(recipient.get('balance', 0)) + self.amount

        await save_json_async(_FILE, data)

        self._disable_all()
        await interaction.response.edit_message(
            content=None,
            embed=discord.Embed(
                title='💸 轉帳完成',
                description=(
                    f'**{self.sender_name}** → {interaction.user.mention}\n'
                    f'金額：**{self.amount}** 咕嚕喵碎片 ✓'
                ),
                color=discord.Color.green(),
            ),
            view=self,
        )
        self.stop()

    @discord.ui.button(label='拒絕接收', style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction,
                     button: discord.ui.Button) -> None:
        if not await self._only_recipient(interaction):
            return
        self._disable_all()
        await interaction.response.edit_message(
            content=None,
            embed=discord.Embed(
                title='💸 轉帳已拒絕',
                description=(
                    f'{interaction.user.mention} 拒絕了來自 '
                    f'**{self.sender_name}** 的 **{self.amount}** 咕嚕喵碎片轉帳'
                ),
                color=discord.Color.red(),
            ),
            view=self,
        )
        self.stop()


def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name='早安小龍喵', description='每日打卡')
    async def slash_morning(interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                embed=discord.Embed(description='此指令只能在伺服器中使用',
                                    color=discord.Color.red()),
                ephemeral=True,
            )
            return

        now = _now_tw()
        if now.hour < 4:
            await interaction.response.send_message(
                '還沒4點，早安個鬼<(￣^￣)>。 趕緊給我去睡覺',
            )
            return

        await interaction.response.defer()

        day_key = _signin_day_key(now)

        data = load_json(_FILE)
        _ensure_today(data, day_key)

        users        = data.setdefault('users', {})
        user_today   = data['user_today']
        global_order = data['global_order']
        guilds_order = data['guilds_order']

        uid = str(interaction.user.id)
        gid = str(guild.id)

        guild_order   = guilds_order.setdefault(gid, [])
        gid_count_map = data['guild_signin_count'].setdefault(gid, {})
        prev_count    = int(gid_count_map.get(uid, 0))
        gid_count_map[uid] = prev_count + 1

        is_same_guild_repeat = prev_count >= 1
        is_new_today         = uid not in user_today

        if is_new_today:
            title, greeting, extras, color = _period_info(now)
            extra_line = random.choice(extras) if extras else ''

            global_order.append(uid)
            if uid not in guild_order:
                guild_order.append(uid)

            user_rec = users.setdefault(uid, {
                'balance':    0,
                'total_days': 0,
                'streak':     0,
                'last_day':   None,
            })
            # 保留打卡前的 last_day / streak，給補簽按鈕判斷與還原 streak 用
            prev_last_day = user_rec.get('last_day')
            prev_streak   = int(user_rec.get('streak', 0))
            if prev_last_day == _yesterday_key(day_key):
                user_rec['streak'] = prev_streak + 1
            else:
                user_rec['streak'] = 1
            user_rec['total_days'] = int(user_rec.get('total_days', 0)) + 1
            user_rec['last_day']   = day_key

            coin = random.randint(500, 5000)
            user_rec['balance'] = int(user_rec.get('balance', 0)) + coin

            lucky, soup = await asyncio.gather(
                _generate_lucky_item(), _generate_soup(),
            )

            money_luck = random.randint(0, 150)
            love_luck  = random.randint(0, 150)
            work_luck  = random.randint(0, 150)
            ratio      = (money_luck + love_luck + work_luck) / 450.0
            fortune_name = _fortune_from_ratio(ratio)
            stars        = round(ratio * 10, 1)

            user_today[uid] = {
                'signin_time':   _format_signin_time(now),
                'global_rank':   len(global_order),
                'lucky_item':    lucky,
                'soup':          soup,
                'coin':          coin,
                'money_luck':    money_luck,
                'love_luck':     love_luck,
                'work_luck':     work_luck,
                'fortune_name':  fortune_name,
                'stars':         stars,
                'total_days':    user_rec['total_days'],
                'streak':        user_rec['streak'],
                'title':         title,
                'greeting':      greeting,
                'extra_line':    extra_line,
                'color':         color.value,
                'guild_id':      gid,
                'prev_last_day': prev_last_day,
                'prev_streak':   prev_streak,
            }
        elif not is_same_guild_repeat:
            # 別群第一次：加入該群順序，不領獎
            if uid not in guild_order:
                guild_order.append(uid)

        today_snapshot = user_today[uid]
        guild_rank     = guild_order.index(uid) + 1 if uid in guild_order else len(guild_order)
        balance        = int(users.get(uid, {}).get('balance', 0))

        embed = _build_embed(
            today_snapshot,
            guild_rank,
            display_name=interaction.user.display_name,
            balance=balance,
        )
        content = '_今天已經打卡過了，這是你今天的紀錄～_' if is_same_guild_repeat else None

        await save_json_async(_FILE, data)
        await interaction.followup.send(
            content=content, embed=embed, view=MorningView(uid),
        )


    @tree.command(name='查看帳戶餘額',
                  description='查看你的咕嚕喵碎片總數與打卡狀態')
    async def slash_balance(interaction: discord.Interaction):
        uid  = str(interaction.user.id)
        data = load_json(_FILE)
        rec  = data.get('users', {}).get(uid)

        if not rec:
            embed = discord.Embed(
                title='帳戶餘額',
                description='你還沒有任何記錄，先用 `/早安小龍喵` 打卡吧！',
                color=discord.Color.light_grey(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        balance    = int(rec.get('balance', 0))
        total_days = int(rec.get('total_days', 0))
        streak     = int(rec.get('streak', 0))

        embed = discord.Embed(
            title='帳戶餘額',
            description=(
                f'**{interaction.user.display_name}** 的小金庫\n'
                f'咕嚕喵碎片：**{balance}**\n'
                f'累計打卡：**{total_days}** 天\n'
                f'連續打卡：**{streak}** 天'
            ),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


    @tree.command(name='轉帳', description='轉帳咕嚕喵碎片給其他用戶')
    @app_commands.describe(
        用戶='要轉給的用戶',
        金額='要轉的咕嚕喵碎片數量（必須大於 0）',
    )
    async def slash_transfer(
        interaction: discord.Interaction,
        用戶: discord.Member,
        金額: app_commands.Range[int, 1, None],
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                '此指令只能在伺服器中使用', ephemeral=True,
            )
            return
        if 用戶.bot or 用戶.id == interaction.user.id:
            await interaction.response.send_message(
                '不能轉給自己或機器人', ephemeral=True,
            )
            return

        sender_id  = str(interaction.user.id)
        data       = load_json(_FILE)
        sender_rec = data.get('users', {}).get(sender_id, {})
        balance    = int(sender_rec.get('balance', 0))
        if balance < 金額:
            await interaction.response.send_message(
                f'你的餘額不足（目前 {balance} 咕嚕喵碎片）',
                ephemeral=True,
            )
            return

        view  = TransferView(
            sender_id, str(用戶.id), int(金額),
            interaction.user.display_name,
        )
        embed = discord.Embed(
            title='💸 轉帳邀請',
            description=(
                f'**{interaction.user.display_name}** 想轉 '
                f'**{金額}** 咕嚕喵碎片給 {用戶.mention}\n\n'
                '按「同意接收」收下，或「拒絕接收」退回。'
            ),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(
            content=用戶.mention, embed=embed, view=view,
        )
