"""
/抽今日媽媽：依使用者要求保留為獨立指令（不併入 /relationship）。

當日抽過則沿用同一位媽媽；跨日後紀錄自動清除（由 commands.relationship 共用）。
"""
from __future__ import annotations

import asyncio
import io
import os
import random

import discord
from discord import app_commands

from utils.json_store import load_json, save_json
from utils.discord_helpers import get_member_safe
from commands.relationship import _purge_expired, _record_day_key, _today_key


_WIFE_FILE  = os.path.join('data', 'wife_records.json')
_LOVE_EMOJI = '<:klllove:1486300373068152832>'


def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name='抽今日媽媽', description='從本群隨機抽一位成員作為你的媽媽（當日只會抽一次）')
    async def slash_draw_wife(interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                embed=discord.Embed(description='此指令只能在伺服器中使用', color=discord.Color.red()),
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        if not guild.chunked:
            try:
                await asyncio.wait_for(guild.chunk(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

        candidates = [m for m in guild.members
                      if not m.bot and m.id != interaction.user.id]
        if not candidates:
            await interaction.followup.send(
                embed=discord.Embed(description='找不到可以抽的對象', color=discord.Color.red()),
                ephemeral=True,
            )
            return

        gid  = str(guild.id)
        uid  = str(interaction.user.id)
        data = _purge_expired(load_json(_WIFE_FILE))
        rec  = data.get(gid, {}).get(uid)

        wife: discord.Member | None = None
        wife_id: str | None = None
        if rec is not None and _record_day_key(rec) == _today_key():
            wife_id = rec.get('wife_id')
            if wife_id:
                wife = await get_member_safe(guild, int(wife_id))
        else:
            wife    = random.choice(candidates)
            wife_id = str(wife.id)
            data.setdefault(gid, {})[uid] = {
                'date':    _today_key(),
                'wife_id': wife_id,
            }
            save_json(_WIFE_FILE, data)

        if wife is not None:
            # 顯示為「顯示名 (@username)」，避免 embed 內單獨 mention 渲染異常
            display = f'{wife.display_name}\n({wife.mention})'
            asset = wife.display_avatar.replace(size=512)
            try:
                avatar_bytes = await asset.read()
            except Exception:
                avatar_bytes = None
        else:
            display = '**對方**'
            avatar_bytes = None

        embed = discord.Embed(
            title='抽今日媽媽',
            description=f'你今天的媽媽是：{display}\n要好好對待她哦 {_LOVE_EMOJI}',
            color=discord.Color.pink(),
        )
        if avatar_bytes:
            embed.set_image(url='attachment://wife.png')
            await interaction.followup.send(
                embed=embed,
                file=discord.File(io.BytesIO(avatar_bytes), filename='wife.png'),
            )
        else:
            await interaction.followup.send(embed=embed)
