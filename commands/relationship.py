"""
/relationship：所有關係互動單一指令（除 /抽今日媽媽、/電擊蘿莉控 外）。

用法：/relationship 選項:<功能> [用戶] [用戶b]

選項：
    認養寵物 / 認主人 / 放生寵物 / 本群關係圖
    認媽媽 / 拋棄兒子 / 和今日媽媽斷絕關係
    電子皮鞭 / 解除調教
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from typing import Literal

import discord
from discord import app_commands

from config import MASTER_ID
from utils.json_store import load_json, save_json
from utils.discord_helpers import owner_only_button_check, get_member_safe


# ─── 共用資料路徑 ────────────────────────────────────────────────
_REL_FILE       = os.path.join('data', 'relationships.json')
_WIFE_FILE      = os.path.join('data', 'wife_records.json')
_WHIP_FILE      = os.path.join('data', 'whip_records.json')
_WHIP_REL_FILE  = os.path.join('data', 'whip_relations.json')
_WHIP_IMG       = os.path.join('picture', 'whip.png')

_LOVE_EMOJI  = '<:klllove:1486300373068152832>'
_CRY_EMOJI   = '<:crycat:1486308949173997730>'
_DAY_KEY_FMT = '%Y-%m-%d'


# ─── 媽媽記錄輔助 ────────────────────────────────────────────────
def _today_key() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime(_DAY_KEY_FMT)


def _record_day_key(rec: dict) -> str | None:
    if 'date' in rec:
        return rec.get('date')
    ts = rec.get('timestamp')
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts).strftime(_DAY_KEY_FMT)
    except Exception:
        return None


def _purge_expired(data: dict) -> dict:
    today = _today_key()
    for gid in list(data):
        for uid in list(data[gid]):
            if _record_day_key(data[gid][uid]) != today:
                del data[gid][uid]
        if not data[gid]:
            del data[gid]
    return data


def get_active_wife_rels(guild_id: int) -> dict[str, str]:
    data   = _purge_expired(load_json(_WIFE_FILE))
    gid    = str(guild_id)
    result = {}
    for uid, rec in data.get(gid, {}).items():
        if _record_day_key(rec) == _today_key():
            result[uid] = rec['wife_id']
    return result


# ─── 調教關係查詢（/tool 電子口球 沿用） ─────────────────────────
def is_trainer_of(guild_id: int, trainer_id: int, trainee_id: int) -> bool:
    rels = load_json(_WHIP_REL_FILE)
    return rels.get(str(guild_id), {}).get(str(trainee_id)) == str(trainer_id)


# ─── 共用回覆 ────────────────────────────────────────────────────
async def _send_error(interaction: discord.Interaction, msg: str) -> None:
    embed = discord.Embed(description=msg, color=discord.Color.red())
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


def _need_target(用戶: discord.Member | None) -> str | None:
    if 用戶 is None:
        return '此選項需要填入「用戶」'
    return None


# ─── 認養 / 認主人 互動 ─────────────────────────────────────────
class _RelationView(discord.ui.View):
    def __init__(self, requester: discord.Member, target: discord.Member,
                 guild_id: int, mode: str):
        super().__init__(timeout=86400)
        self.requester = requester
        self.target    = target
        self.guild_id  = guild_id
        self.mode      = mode

    @discord.ui.button(label='接受', style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await owner_only_button_check(interaction, self.target.id):
            return
        data   = load_json(_REL_FILE)
        gid    = str(self.guild_id)
        req_id = str(self.requester.id)
        tgt_id = str(self.target.id)
        data.setdefault(gid, {})

        if self.mode == 'pet':
            data[gid][tgt_id] = req_id
            owner_name = self.requester.display_name
            pet_name   = self.target.display_name
        else:
            data[gid][req_id] = tgt_id
            owner_name = self.target.display_name
            pet_name   = self.requester.display_name

        save_json(_REL_FILE, data)
        embed = discord.Embed(
            title='主寵關係建立',
            description=f'{pet_name} 成為了 {owner_name} 的寵物',
            color=discord.Color.fuchsia(),
        )
        await interaction.response.edit_message(embed=embed, content=None, view=None)
        self.stop()

    @discord.ui.button(label='拒絕', style=discord.ButtonStyle.secondary)
    async def deny(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await owner_only_button_check(interaction, self.target.id):
            return
        embed = discord.Embed(description='對方拒絕了', color=discord.Color.dark_grey())
        await interaction.response.edit_message(embed=embed, content=None, view=None)
        self.stop()


# ─── 電子皮鞭 ───────────────────────────────────────────────────
async def _do_whip(send_fn, trainer: discord.Member, trainee: discord.Member,
                   guild_id: int) -> None:
    gid = str(guild_id)
    uid = str(trainee.id)

    records = load_json(_WHIP_FILE)
    records.setdefault(gid, {})[uid] = records.get(gid, {}).get(uid, 0) + 1
    count = records[gid][uid]
    save_json(_WHIP_FILE, records)

    rels = load_json(_WHIP_REL_FILE)
    rels.setdefault(gid, {})[uid] = str(trainer.id)
    save_json(_WHIP_REL_FILE, rels)

    embed = discord.Embed(
        title='電子皮鞭',
        description=(
            f'**{trainee.display_name}** 被 **{trainer.display_name}** '
            f'用皮鞭狠狠調教了，現在是隻乖狗狗了'
        ),
        color=discord.Color.red(),
    )
    embed.add_field(name='累計被調教', value=f'{count} 次')
    if os.path.exists(_WHIP_IMG):
        embed.set_image(url='attachment://whip.png')
        await send_fn(embed=embed, file=discord.File(_WHIP_IMG, filename='whip.png'))
    else:
        await send_fn(embed=embed)


class _WhipConfirmView(discord.ui.View):
    def __init__(self, trainer: discord.Member, trainee: discord.Member, guild_id: int):
        super().__init__(timeout=30)
        self.trainer  = trainer
        self.trainee  = trainee
        self.guild_id = guild_id

    @discord.ui.button(label='願意', style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await owner_only_button_check(interaction, self.trainee.id):
            return
        await interaction.response.edit_message(content='調教中...', view=None)
        await _do_whip(interaction.followup.send, self.trainer, self.trainee, self.guild_id)
        self.stop()

    @discord.ui.button(label='拒絕', style=discord.ButtonStyle.secondary)
    async def deny(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        if not await owner_only_button_check(interaction, self.trainee.id):
            return
        embed = discord.Embed(
            description=f'**{self.trainee.display_name}** 拒絕了調教',
            color=discord.Color.dark_grey(),
        )
        await interaction.response.edit_message(embed=embed, content=None, view=None)
        self.stop()


# ─── 各功能處理函式 ─────────────────────────────────────────────
async def _handle_adopt(interaction: discord.Interaction, target: discord.Member):
    if target.id == interaction.user.id:
        await _send_error(interaction, '不能認養自己')
        return
    if target.bot:
        await _send_error(interaction, '不能認養 Bot')
        return
    view = _RelationView(interaction.user, target, interaction.guild_id, mode='pet')
    embed = discord.Embed(
        title='認養邀請',
        description=(
            f'**{target.display_name}**\n({target.mention})\n'
            f'**{interaction.user.display_name}** '
            f'想認養你為寵物，你願意嗎？'
        ),
        color=discord.Color.fuchsia(),
    )
    await interaction.response.send_message(embed=embed, view=view)


async def _handle_find_master(interaction: discord.Interaction, target: discord.Member):
    if target.id == interaction.user.id:
        await _send_error(interaction, '不能認自己為主人')
        return
    if target.bot:
        await _send_error(interaction, '不能認 Bot 為主人')
        return
    view = _RelationView(interaction.user, target, interaction.guild_id, mode='master')
    embed = discord.Embed(
        title='主人邀請',
        description=(
            f'**{target.display_name}**\n({target.mention})\n'
            f'**{interaction.user.display_name}** '
            f'想認你為主人，你願意嗎？'
        ),
        color=discord.Color.fuchsia(),
    )
    await interaction.response.send_message(embed=embed, view=view)


async def _handle_release(interaction: discord.Interaction, target: discord.Member):
    data   = load_json(_REL_FILE)
    gid    = str(interaction.guild_id)
    pet_id = str(target.id)
    req_id = str(interaction.user.id)
    if data.get(gid, {}).get(pet_id) != req_id:
        await _send_error(interaction, f'**{target.display_name}** 不是你的寵物')
        return
    del data[gid][pet_id]
    save_json(_REL_FILE, data)
    embed = discord.Embed(
        title='放生寵物',
        description=(
            f'**{target.display_name}** 被 **{interaction.user.display_name}** '
            f'放歸大自然了'
        ),
        color=discord.Color.green(),
    )
    await interaction.response.send_message(embed=embed)


async def _handle_rel_map(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await _send_error(interaction, '此指令只能在伺服器中使用')
        return
    data = load_json(_REL_FILE)
    gid  = str(guild.id)
    rels = data.get(gid, {})
    wife_rels = get_active_wife_rels(guild.id)
    if not rels and not wife_rels:
        await _send_error(interaction, '本群還沒有任何關係紀錄')
        return

    await interaction.response.defer()
    try:
        from graph_render import render_relation_graph
        buf = await render_relation_graph(guild, rels, wife_rels)
        embed = discord.Embed(title='今日羈絆圖譜', color=discord.Color.blurple())
        embed.set_image(url='attachment://relations.png')
        await interaction.followup.send(
            embed=embed,
            file=discord.File(buf, filename='relations.png'),
        )
    except Exception as e:
        print(f'[GRAPH] 圖形渲染失敗: {e}')
        await interaction.followup.send(
            embed=discord.Embed(description=f'圖形渲染失敗：{e}', color=discord.Color.red()),
            ephemeral=True,
        )


async def _handle_force_wife(interaction: discord.Interaction, target: discord.Member):
    if target.bot or target.id == interaction.user.id:
        await _send_error(interaction, '不能認 Bot / 自己當媽媽')
        return
    gid  = str(interaction.guild_id)
    uid  = str(interaction.user.id)
    data = _purge_expired(load_json(_WIFE_FILE))
    data.setdefault(gid, {})[uid] = {
        'date':    _today_key(),
        'wife_id': str(target.id),
    }
    save_json(_WIFE_FILE, data)
    embed = discord.Embed(
        title='認媽媽',
        description=(
            f'**{interaction.user.display_name}** 認了 '
            f'**{target.display_name}** 作為媽媽 {_LOVE_EMOJI}'
        ),
        color=discord.Color.pink(),
    )
    await interaction.response.send_message(embed=embed)


async def _handle_abandon_child(interaction: discord.Interaction, target: discord.Member):
    gid    = str(interaction.guild_id)
    uid    = str(target.id)
    my_id  = str(interaction.user.id)
    data   = _purge_expired(load_json(_WIFE_FILE))
    rec = data.get(gid, {}).get(uid)
    if rec is None or rec.get('wife_id') != my_id:
        await _send_error(interaction, '對方沒有認你為媽媽')
        return
    data[gid].pop(uid)
    save_json(_WIFE_FILE, data)
    embed = discord.Embed(
        title='拋棄兒子',
        description=(
            f'**{interaction.user.display_name}** 與 '
            f'**{target.display_name}** 斷絕了母子關係 {_CRY_EMOJI}'
        ),
        color=discord.Color.red(),
    )
    await interaction.response.send_message(embed=embed)


async def _handle_divorce(interaction: discord.Interaction):
    gid  = str(interaction.guild_id)
    uid  = str(interaction.user.id)
    data = _purge_expired(load_json(_WIFE_FILE))
    if uid not in data.get(gid, {}):
        await _send_error(interaction, '你目前沒有媽媽')
        return
    wife_id = data[gid][uid].get('wife_id')
    data[gid].pop(uid)
    save_json(_WIFE_FILE, data)
    wife_name = '對方'
    if wife_id and interaction.guild:
        member = await get_member_safe(interaction.guild, int(wife_id))
        if member:
            wife_name = member.display_name
    embed = discord.Embed(
        title='斷絕母子關係',
        description=f'你已和 **{wife_name}** 斷絕母子關係了 {_CRY_EMOJI}',
        color=discord.Color.red(),
    )
    await interaction.response.send_message(embed=embed)


async def _handle_whip(interaction: discord.Interaction,
                       trainer: discord.Member, trainee: discord.Member):
    guild = interaction.guild
    if guild is None:
        await _send_error(interaction, '此指令只能在伺服器中使用')
        return
    if trainee.bot:
        await _send_error(interaction, '不能調教 Bot')
        return
    if trainee.id == interaction.user.id and trainer.id == interaction.user.id:
        await _send_error(interaction, '不能調教自己')
        return
    if is_trainer_of(guild.id, trainer.id, trainee.id):
        await interaction.response.defer()
        await _do_whip(interaction.followup.send, trainer, trainee, guild.id)
        return
    view = _WhipConfirmView(trainer, trainee, guild.id)
    embed = discord.Embed(
        title='電子皮鞭邀請',
        description=(
            f'**{trainee.display_name}**\n({trainee.mention})\n'
            f'你願意被 **{trainer.display_name}** 調教嗎？'
        ),
        color=discord.Color.dark_red(),
    )
    await interaction.response.send_message(embed=embed, view=view)


async def _handle_whip_clear(interaction: discord.Interaction, target: discord.Member):
    guild = interaction.guild
    if guild is None:
        await _send_error(interaction, '此指令只能在伺服器中使用')
        return
    is_master = (interaction.user.id == MASTER_ID)
    is_admin  = interaction.user.guild_permissions.manage_guild
    if not is_master and not is_admin:
        await _send_error(interaction, '此選項限管理員或主人使用')
        return
    rels = load_json(_WHIP_REL_FILE)
    gid  = str(guild.id)
    uid  = str(target.id)
    if rels.get(gid, {}).pop(uid, None) is None:
        await _send_error(interaction, f'**{target.display_name}** 目前沒有調教關係')
        return
    save_json(_WHIP_REL_FILE, rels)
    embed = discord.Embed(
        title='解除調教',
        description=f'已解除 **{target.display_name}** 的調教關係',
        color=discord.Color.green(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─── 指令註冊 ───────────────────────────────────────────────────
_RelOption = Literal[
    '認養寵物', '認主人', '放生寵物', '本群關係圖',
    '認媽媽', '拋棄兒子', '和今日媽媽斷絕關係',
    '電子皮鞭', '解除調教',
]


def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name='relationship', description='主寵 / 媽媽 / 調教 等關係互動')
    @app_commands.describe(
        選項='要執行的功能',
        用戶='指定對象（多數選項需要）',
        用戶b='電子皮鞭：填此項時用戶為調教者、用戶b為被調教者',
    )
    async def slash_rel(
        interaction: discord.Interaction,
        選項: _RelOption,
        用戶: discord.Member = None,
        用戶b: discord.Member = None,
    ):
        # ── 不需要 用戶 的選項 ─────────────────────────────────
        if 選項 == '本群關係圖':
            await _handle_rel_map(interaction)
            return
        if 選項 == '和今日媽媽斷絕關係':
            await _handle_divorce(interaction)
            return

        # ── 需要 用戶 的選項 ───────────────────────────────────
        if 選項 == '電子皮鞭':
            trainer = 用戶 if 用戶b else interaction.user
            trainee = 用戶b if 用戶b else 用戶
            if trainee is None:
                await _send_error(interaction, '電子皮鞭需要填入「用戶」')
                return
            await _handle_whip(interaction, trainer, trainee)
            return

        err = _need_target(用戶)
        if err:
            await _send_error(interaction, err)
            return

        if 選項 == '認養寵物':
            await _handle_adopt(interaction, 用戶)
        elif 選項 == '認主人':
            await _handle_find_master(interaction, 用戶)
        elif 選項 == '放生寵物':
            await _handle_release(interaction, 用戶)
        elif 選項 == '認媽媽':
            await _handle_force_wife(interaction, 用戶)
        elif 選項 == '拋棄兒子':
            await _handle_abandon_child(interaction, 用戶)
        elif 選項 == '解除調教':
            await _handle_whip_clear(interaction, 用戶)
