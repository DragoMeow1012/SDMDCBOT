"""
/tool：所有小型互動工具單一指令。

用法：/tool 選項:<功能> [秒數] [對象] [尺寸] [長] [寬] [隊伍數量]

選項：
    電子口球（秒數+對象）/ 口球輪盤
    電子氣泡紙（尺寸/長/寬）/ 電子木魚
    賽博體重計 / 擲硬幣 / 擲硬幣幹話版
    roll / 丟骰子 / 分隊伍（隊伍數量）/ 賽博釣群友
"""
from __future__ import annotations

import asyncio
import datetime
import os
import random
from typing import Literal, Optional

import discord
from discord import app_commands

from config import MASTER_ID
from utils.discord_helpers import owner_only_button_check
from utils.json_store import load_json, save_json
from commands.relationship import is_trainer_of


_MERIT_FILE = os.path.join('data', 'merit.json')
_GAG_MAX_SECONDS = 2419200  # Discord timeout 上限 28 天

_CHINESE_NUMS = ['一','二','三','四','五','六','七','八','九','十',
                 '十一','十二','十三','十四','十五','十六','十七','十八','十九','二十']


def _team_name(n: int) -> str:
    if 1 <= n <= len(_CHINESE_NUMS):
        return f'第{_CHINESE_NUMS[n-1]}隊'
    return f'第{n}隊'


async def _send_error(interaction: discord.Interaction, msg: str) -> None:
    embed = discord.Embed(description=msg, color=discord.Color.red())
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ─── 電子口球 ────────────────────────────────────────────────────
async def _apply_gag(target: discord.Member, seconds: int) -> str | None:
    try:
        await target.timeout(datetime.timedelta(seconds=seconds), reason='電子口球')
        return None
    except discord.Forbidden:
        return 'Bot 缺少「管理成員」權限'
    except discord.HTTPException as e:
        return f'套用口球失敗：{e}'


class _GagConfirmView(discord.ui.View):
    def __init__(self, target: discord.Member, seconds: int):
        super().__init__(timeout=30)
        self.target  = target
        self.seconds = seconds

    @discord.ui.button(label='同意', style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await owner_only_button_check(interaction, self.target.id):
            return
        err = await _apply_gag(self.target, self.seconds)
        if err:
            embed = discord.Embed(description=err, color=discord.Color.red())
        else:
            embed = discord.Embed(
                title='電子口球',
                description=f'**{self.target.display_name}** 已戴上電子口球 {self.seconds} 秒',
                color=discord.Color.dark_orange(),
            )
        await interaction.response.edit_message(embed=embed, content=None, view=None)
        self.stop()

    @discord.ui.button(label='拒絕', style=discord.ButtonStyle.secondary)
    async def deny(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await owner_only_button_check(interaction, self.target.id):
            return
        embed = discord.Embed(
            description=f'**{self.target.display_name}** 拒絕了電子口球',
            color=discord.Color.dark_grey(),
        )
        await interaction.response.edit_message(embed=embed, content=None, view=None)
        self.stop()


class _RouletteView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=86400)
        self.participants: list[discord.Member] = []
        self.closed = False

    @discord.ui.button(label='參加輪盤', style=discord.ButtonStyle.danger)
    async def join(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if self.closed:
            await interaction.response.send_message('報名已結束', ephemeral=True)
            return
        if any(m.id == interaction.user.id for m in self.participants):
            await interaction.response.send_message('你已經報名了', ephemeral=True)
            return
        if interaction.user.bot:
            await interaction.response.send_message('Bot 不能參加輪盤', ephemeral=True)
            return
        self.participants.append(interaction.user)
        embed = discord.Embed(
            title='口球輪盤',
            description=(
                '60 秒內點下方按鈕報名，時間到將從參加者中隨機抽出一人戴上電子口球 30 秒\n\n'
                f'目前報名：{len(self.participants)} 人'
            ),
            color=discord.Color.dark_red(),
        )
        await interaction.response.edit_message(embed=embed, content=None)
        await interaction.followup.send('已報名', ephemeral=True)


# ─── 電子木魚 ────────────────────────────────────────────────────
class _MeritView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.session_count = 0
        self.guild_id = guild_id

    @discord.ui.button(label='功德+1', style=discord.ButtonStyle.success, custom_id='merit_btn')
    async def merit_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        gid = str(self.guild_id)
        uid = str(interaction.user.id)
        data = load_json(_MERIT_FILE)
        guild_data = data.setdefault(gid, {})
        guild_data[uid] = guild_data.get(uid, 0) + 1
        save_json(_MERIT_FILE, data)
        self.session_count += 1
        embed = discord.Embed(
            title='電子木魚',
            description=f'本次功德：**{self.session_count}** 下',
            color=discord.Color.dark_gold(),
        )
        embed.set_footer(
            text=f'{interaction.user.display_name} 累計功德：{guild_data[uid]} 下'
        )
        await interaction.response.edit_message(embed=embed, content=None, view=self)


# ─── 分隊伍 ──────────────────────────────────────────────────────
class _TeamSignupView(discord.ui.View):
    def __init__(self, num_teams: int):
        super().__init__(timeout=None)
        self.num_teams = num_teams
        self.participants: list[discord.Member] = []
        self.message: discord.Message | None = None

    def _signup_embed(self) -> discord.Embed:
        return discord.Embed(
            title='分隊伍報名中',
            description=(
                f'分為 **{self.num_teams}** 隊，30 秒內按下方按鈕報名\n\n'
                f'目前 **{len(self.participants)}** 人'
            ),
            color=discord.Color.blurple(),
        )

    @discord.ui.button(label='參與', style=discord.ButtonStyle.primary)
    async def join(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        user = interaction.user
        if any(p.id == user.id for p in self.participants):
            await interaction.response.send_message('你已經報名了', ephemeral=True)
            return
        self.participants.append(user)
        await interaction.response.defer()
        if self.message:
            await self.message.edit(embed=self._signup_embed())


# ─── 賽博釣群友 ──────────────────────────────────────────────────
_FISHING_PHRASES = [
    '其實...我平時在家都偷偷穿女裝的，有人想看嗎？',
    '汪汪！我是主人的小修勾！誰要來領養我？',
    '各位哥哥好，我是新來的笨蛋小男娘...',
    '有沒有人可以教教我怎麼化妝呀？我買了裙子不敢穿。',
    '嗚嗚嗚，今天也被自己可愛到了，想被摸摸頭。',
    '其實我的聲音可以夾得比女生還細喔，要聽聽看嗎？',
    '大家都叫我哥，其實我內心是個小公主啦。',
    '喵喵喵～今天也是求抱抱的一天！',
    '我已經準備好白絲了，有沒有哥哥來誇誇我？',
    '討厭啦，不要一直盯著人家看，人家會害羞。',
    '其實人家私底下超愛撒嬌的，只是平時裝得很man。',
    '今晚有人要陪小男娘打遊戲嗎？我很菜但很會叫喔。',
    '偷偷說...我的衣櫃裡其實都是 JK 制服。',
    '哥哥們，我這樣可愛嗎？',
    '每天出門前都要花一小時練習夾娃娃音，好累喔。',
    '誰能拒絕一個會嚶嚶嚶的男孩子呢？',
    '我不管，我就是全群最嬌弱的寶寶，快寵我！',
    '其實我的夢想是被當成小寵物養起來。',
    '偷偷告訴你們，我連內褲都是粉紅色的。',
    '唉，身為一個猛男，卻有顆想當魔法少女的心，好煩惱。',
    '兄弟們不瞞你說，我一天不鹿管就渾身難受。',
    '我承認了，我是個無可救藥的腿控加變態。',
    '為什麼我都交不到女朋友？難道是因為我硬碟裡有2T的片？',
    '我單身 20 年了，現在看電線桿都覺得眉清目秀。',
    '完了，我又進入發情期了，誰來救救我。',
    '我對二次元老婆的愛已經超越了碳基生物的極限！',
    '兄弟們，我又色色了，對不起這個社會。',
    '我每天都在幻想自己是個後宮番男主角。',
    '我宣布，從今天起我就是群裡第一大變態，誰贊成誰反對？',
    '好想被包養喔，阿姨我不想努力了。',
    '吾乃漆黑烈焰使，凡人們，還不快跪下顫抖！',
    '我的右手又在隱隱作痛了，封印快解除了嗎？',
    '其實我是外星人派來地球的臥底，今天準備收網了。',
    '大家都以為我是普通人，其實我昨晚才拯救了世界。',
    '不要靠近我，我體內的洪荒之力會傷到你的。',
    '錯的不是我，是這個世界！',
    '我已經看透了紅塵，準備明天就剃度出家。',
    '其實我有特異功能，我可以用意念控制自己睡覺。',
    '我每天都在和鏡子裡的自己猜拳，而且經常輸。',
    '我覺得我是天選之子，只是還沒覺醒而已。',
    '其實我一直在暗戀群裡的某個人，但我不敢說是誰。',
    '我是一個沒有感情的殺手，但今晚我想吃麥當勞。',
    '媽媽說我是世界上最聰明的寶寶，對吧？',
]


class _FishingView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=86400)

    @discord.ui.button(label='咬鉤', style=discord.ButtonStyle.danger)
    async def bite(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        user = interaction.user
        try:
            hooks = await channel.webhooks()
            wh = next((h for h in hooks if h.name == '賽博釣魚'), None)
            if wh is None:
                wh = await channel.create_webhook(name='賽博釣魚')
        except discord.Forbidden:
            await interaction.followup.send('Bot 缺少管理 Webhook 的權限', ephemeral=True)
            return
        avatar_url = user.display_avatar.replace(size=256).url
        phrase = random.choice(_FISHING_PHRASES)
        await wh.send(phrase, username=user.display_name, avatar_url=avatar_url)
        await interaction.followup.send('上鉤了', ephemeral=True)


# ─── 擲硬幣幹話 ──────────────────────────────────────────────────
_COIN_DRAMA = [
    '硬幣拋向了空中...',
    '一陣風吹過，硬幣飛得更高了...',
    '硬幣突破了對流層...',
    '硬幣衝出了大氣層...',
    '硬幣撞到了馬斯克的衛星，彈了回來...',
    '硬幣路過月球，嚇到了一隻兔子...',
    '硬幣被外星人短暫研究後歸還...',
    '硬幣開始自轉，產生了引力場...',
    '硬幣被小龍喵一把抓住，然後又吐了出來...',
    '硬幣懸浮在空中，陷入了哲學思考...',
    '薛丁格的貓路過，硬幣暫時同時是正面和反面...',
    '硬幣被一隻鴿子叼走，又被另一隻鴿子搶走...',
    '硬幣飛過了某個平行宇宙，裡面的你沒有丟硬幣...',
    '硬幣不小心進入了量子疊加態，工程師正在除錯...',
    '硬幣路過 7-11，買了一瓶茶飲料...',
    '硬幣被誤認為是隕石，NASA 發了一篇論文...',
    '硬幣決定先去旅遊，訂了張機票...',
    '硬幣在空中停了一下，拍了張自拍...',
    '硬幣終於開始下落了...（好像）',
    '一隻手從天而降，接住了硬幣，然後鬆開了...',
    '硬幣掉進了 Python 的 while True: 無窮迴圈，永遠無法落地。',
    '硬幣連續掉落太快，觸發了 HTTP 429 Too Many Requests，被強制暫停在半空中。',
    '硬幣掉進了 Linux 終端機，直到有人輸入 sudo reboot 才會重新出現。',
    '硬幣被當作訓練資料送進了神經網路，現在它只是一堆權重參數。',
    '硬幣忘了更新 SSL 憑證，被地心引力拒絕連線。',
    '硬幣遇到了 404 Not Found 錯誤，掉進了異次元空間。',
    '硬幣被當成比特幣挖走，瞬間數位化消失。',
    '硬幣在掉落過程中被垃圾回收機制 (Garbage Collection) 清除了。',
    '防毒軟體將硬幣的高速自轉判定為惡意行為，已將其隔離。',
    '硬幣被打包進 Docker 容器，結果找不到對應的 Port 可以掉出來。',
    'API 達到呼叫上限 (Rate Limit)，硬幣只能明天早上八點再繼續掉落。',
    '硬幣被升級成量子位元，現在處於疊加態，你一觀測它就壞掉了。',
    '硬幣不小心採用了覆晶封裝 (Flip Chip) 技術，正反面焊死在空氣分子上。',
    '硬幣經歷了高溫迴焊 (Reflow) 製程，落地時已經融化成一灘金屬。',
    '硬幣的純度達到了 99.9999999%，被外星人當作高科技材料劫走。',
    '硬幣被吸進了真空腔體，再也沒有掉下來的重力。',
    '時間暫停器被誤觸，硬幣成為了世界上唯一還在動的物體。',
    '硬幣的速度超越了光速，回到了你拋出它的前一天。',
    '質量濃縮產生了微型黑洞，把周圍的空氣和你一起吸了進去。',
    '硬幣引發了蝴蝶效應，導致地球另一端發生了八級大地震，而它自己穩穩落地。',
    '硬幣經歷了量子穿隧效應，直接穿透了地板，掉到了樓下鄰居的湯裡。',
    '熵值瞬間逆轉，硬幣重新飛回了你的口袋裡。',
    '蟲洞在硬幣下方開啟，它掉到了仙女座星系。',
    '硬幣經歷了洛倫茲收縮，變成了一條二維的線段。',
    '宇宙膨脹速度大於硬幣掉落速度，地板距離硬幣越來越遠。',
    '宙斯看中了這枚硬幣，把它變成了天上的一顆星座。',
    '索爾的雷神之鎚飛過，強大的磁力把硬幣直接吸走了。',
    '奇異博士開啟了傳送門，硬幣掉進了多重宇宙的瘋狂之中。',
    '土地公覺得你太窮，把硬幣變成了金條，砸碎了你的腳趾。',
    '孫悟空拔了一根毫毛，把硬幣變成了漫山遍野的猴子。',
    '財神爺覺得面額太小，一腳把它踢飛到太平洋。',
    '一隻鴿子飛過，精準地把大便拉在硬幣上，增加的重量讓它提前落地。',
    '一台疾駛而過的砂石車輾過了硬幣，把它壓成了一個平底鍋。',
    '硬幣突然開口說話：「主人...請、請不要隨便把我丟出去...我會怕...」',
    '突然一陣狂風吹來，硬幣精準地飛進了五十公尺外販賣機的投幣口。',
    '硬幣覺得每天被拋來拋去太累了，決定在空中躺平，直接罷工。',
    '硬幣終於落地了，但它站得筆直，周圍的人開始對它膜拜，建立了一個新的宗教。',
]


# ─── 各功能處理函式 ─────────────────────────────────────────────
async def _handle_gag(interaction: discord.Interaction, seconds: int | None,
                      target: discord.Member | None):
    if seconds is None or not (1 <= seconds <= _GAG_MAX_SECONDS):
        await _send_error(interaction, '電子口球需要填入 1~2419200 之間的「秒數」')
        return
    target = target or interaction.user
    is_master = (interaction.user.id == MASTER_ID)
    is_self   = (target.id == interaction.user.id)

    if target.bot:
        await _send_error(interaction, '不能對 Bot 套口球')
        return

    is_trainer = (interaction.guild is not None and
                  is_trainer_of(interaction.guild.id, interaction.user.id, target.id))
    if is_master or is_self or is_trainer:
        err = await _apply_gag(target, seconds)
        if err:
            await _send_error(interaction, err)
            return
        embed = discord.Embed(
            title='電子口球',
            description=f'**{target.display_name}** 已戴上電子口球 {seconds} 秒',
            color=discord.Color.dark_orange(),
        )
        await interaction.response.send_message(
            embed=embed,
            ephemeral=is_self and not is_master and not is_trainer,
        )
        return

    view = _GagConfirmView(target, seconds)
    embed = discord.Embed(
        title='電子口球邀請',
        description=(
            f'**{target.display_name}**\n({target.mention})\n'
            f'**{interaction.user.display_name}** '
            f'想幫你戴上電子口球 {seconds} 秒，你同意嗎？'
        ),
        color=discord.Color.dark_orange(),
    )
    await interaction.response.send_message(embed=embed, view=view)


async def _handle_roulette(interaction: discord.Interaction):
    view = _RouletteView()
    embed = discord.Embed(
        title='口球輪盤',
        description='60 秒內點下方按鈕報名，時間到將從參加者中隨機抽出一人戴上電子口球 30 秒',
        color=discord.Color.dark_red(),
    )
    await interaction.response.send_message(embed=embed, view=view)
    await asyncio.sleep(60)
    view.closed = True

    if not view.participants:
        done = discord.Embed(
            title='口球輪盤結束',
            description='沒有人報名，輪盤空轉了',
            color=discord.Color.dark_grey(),
        )
        await interaction.edit_original_response(embed=done, view=None)
        return

    victim = random.choice(view.participants)
    names  = '、'.join(m.display_name for m in view.participants)
    err    = await _apply_gag(victim, 30)
    result = (f'恭喜 **{victim.display_name}** 獲得電子口球 30 秒'
              if not err else
              f'抽中了 **{victim.display_name}**，但是… {err}')
    done = discord.Embed(
        title='口球輪盤結束',
        description=f'參加者：{names}\n\n{result}',
        color=discord.Color.dark_red(),
    )
    await interaction.edit_original_response(embed=done, view=None)


async def _handle_bubblewrap(interaction: discord.Interaction,
                             size: str, length: int | None, width: int | None):
    if size == 'custom':
        if length is None or width is None:
            await _send_error(interaction, '自訂尺寸需同時填入「長」與「寬」（各 1~50）')
            return
        rows, cols = length, width
    elif size == '5x2':
        rows, cols = 2, 5
    else:
        rows, cols = 5, 10
    grid = '\n'.join(' '.join('||啵||' for _ in range(cols)) for _ in range(rows))
    embed = discord.Embed(
        title=f'電子氣泡紙 {cols}×{rows}',
        description=grid,
        color=discord.Color.teal(),
    )
    await interaction.response.send_message(embed=embed)


async def _handle_merit(interaction: discord.Interaction):
    if interaction.guild is None:
        await _send_error(interaction, '此指令只能在伺服器中使用')
        return
    view = _MeritView(interaction.guild.id)
    embed = discord.Embed(
        title='電子木魚',
        description='本次功德：**0** 下',
        color=discord.Color.dark_gold(),
    )
    await interaction.response.send_message(embed=embed, view=view)


async def _handle_weight(interaction: discord.Interaction):
    weight_kg = random.randint(10, 150)
    desc = f'賽博體重計顯示：**{weight_kg} kg**'
    if weight_kg > 100 and random.random() < 0.05:
        desc += '\n天啊你是柚子廚'
    embed = discord.Embed(title='賽博體重計', description=desc, color=discord.Color.teal())
    await interaction.response.send_message(embed=embed)


async def _handle_coin(interaction: discord.Interaction):
    result = random.choice(['🌕 正面', '🌑 反面'])
    embed = discord.Embed(
        title='擲硬幣',
        description=f'擲出結果：**{result}**',
        color=discord.Color.gold(),
    )
    await interaction.response.send_message(embed=embed)


async def _handle_coin_drama(interaction: discord.Interaction):
    result = random.choice(['🌕 **正面**', '🌑 **反面**'])
    lines  = random.sample(_COIN_DRAMA, random.randint(1, 10))

    embed = discord.Embed(
        title='擲硬幣幹話版',
        description=lines[0],
        color=discord.Color.gold(),
    )
    await interaction.response.send_message(embed=embed)

    accumulated = lines[0]
    for line in lines[1:]:
        await asyncio.sleep(random.uniform(1.2, 2.2))
        accumulated += f'\n{line}'
        embed.description = accumulated
        await interaction.edit_original_response(embed=embed)

    await asyncio.sleep(random.uniform(1.2, 2.0))
    accumulated += f'\n硬幣落地！結果是… {result}'
    embed.description = accumulated
    await interaction.edit_original_response(embed=embed)


async def _handle_roll(interaction: discord.Interaction):
    n = random.randint(1, 100)
    embed = discord.Embed(
        title='Roll',
        description=f'**{interaction.user.display_name}** 抽到了 **{n}** 點',
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed)


async def _handle_dice(interaction: discord.Interaction):
    n = random.randint(1, 6)
    embed = discord.Embed(
        title='丟骰子',
        description=f'**{interaction.user.display_name}** 投到了 **{n}** 點',
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed)


async def _handle_split_teams(interaction: discord.Interaction, num_teams: int | None):
    if num_teams is None or not (2 <= num_teams <= 20):
        await _send_error(interaction, '分隊伍需要填入 2~20 之間的「隊伍數量」')
        return
    view = _TeamSignupView(num_teams)
    await interaction.response.send_message(embed=view._signup_embed(), view=view)
    msg = await interaction.original_response()
    view.message = msg

    await asyncio.sleep(30)
    view.stop()
    for item in view.children:
        item.disabled = True

    if not view.participants:
        embed = discord.Embed(
            title='分隊結果',
            description='沒有人報名',
            color=discord.Color.dark_grey(),
        )
        await msg.edit(embed=embed, view=view)
        return

    members = view.participants[:]
    random.shuffle(members)
    teams: list[list[discord.Member]] = [[] for _ in range(num_teams)]
    for i, member in enumerate(members):
        teams[i % num_teams].append(member)

    lines = []
    for i, team in enumerate(teams):
        names = ' '.join(m.display_name for m in team) if team else '（無人）'
        lines.append(f'**{_team_name(i + 1)}**：{names}')

    embed = discord.Embed(
        title='分隊結果',
        description='\n'.join(lines),
        color=discord.Color.green(),
    )
    await msg.edit(embed=embed, view=view)


async def _handle_fishing(interaction: discord.Interaction):
    view = _FishingView()
    embed = discord.Embed(
        title='賽博釣魚中',
        description='點下方「咬鉤」按鈕，會以你的暱稱發出一句奇妙的話',
        color=discord.Color.dark_teal(),
    )
    await interaction.response.send_message(embed=embed, view=view)


# ─── 指令註冊 ───────────────────────────────────────────────────
_ToolOption = Literal[
    '電子口球', '口球輪盤',
    '電子氣泡紙', '電子木魚',
    '賽博體重計', '擲硬幣', '擲硬幣幹話版',
    'roll', '丟骰子',
    '分隊伍', '賽博釣群友',
]


def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name='tool', description='抽籤 / 趣味 / 互動工具')
    @app_commands.describe(
        選項='要執行的功能',
        秒數='電子口球用：禁言持續秒數（1~2419200）',
        對象='電子口球用：目標成員（不填則對自己）',
        尺寸='電子氣泡紙用：尺寸',
        長='電子氣泡紙自訂用：列數（1~50）',
        寬='電子氣泡紙自訂用：欄數（1~50）',
        隊伍數量='分隊伍用：要分成幾隊（2~20）',
    )
    @app_commands.choices(尺寸=[
        app_commands.Choice(name='5×2（10顆）',  value='5x2'),
        app_commands.Choice(name='10×5（50顆）', value='10x5'),
        app_commands.Choice(name='自訂',          value='custom'),
    ])
    async def slash_tool(
        interaction: discord.Interaction,
        選項: _ToolOption,
        秒數: Optional[app_commands.Range[int, 1, _GAG_MAX_SECONDS]] = None,
        對象: discord.Member = None,
        尺寸: app_commands.Choice[str] | None = None,
        長: Optional[app_commands.Range[int, 1, 50]] = None,
        寬: Optional[app_commands.Range[int, 1, 50]] = None,
        隊伍數量: Optional[app_commands.Range[int, 2, 20]] = None,
    ):
        if 選項 == '電子口球':
            await _handle_gag(interaction, 秒數, 對象)
        elif 選項 == '口球輪盤':
            await _handle_roulette(interaction)
        elif 選項 == '電子氣泡紙':
            size = (尺寸.value if 尺寸 else '5x2')
            await _handle_bubblewrap(interaction, size, 長, 寬)
        elif 選項 == '電子木魚':
            await _handle_merit(interaction)
        elif 選項 == '賽博體重計':
            await _handle_weight(interaction)
        elif 選項 == '擲硬幣':
            await _handle_coin(interaction)
        elif 選項 == '擲硬幣幹話版':
            await _handle_coin_drama(interaction)
        elif 選項 == 'roll':
            await _handle_roll(interaction)
        elif 選項 == '丟骰子':
            await _handle_dice(interaction)
        elif 選項 == '分隊伍':
            await _handle_split_teams(interaction, 隊伍數量)
        elif 選項 == '賽博釣群友':
            await _handle_fishing(interaction)
