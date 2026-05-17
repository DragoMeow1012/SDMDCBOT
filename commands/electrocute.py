"""
/電擊蘿莉控：每次隨機抽一位本群成員 + 0~5 編號，搭配對應圖檔回覆。

紀錄寫到 data/artillery_records.json（沿用原資料檔；/排行 炮決 仍可讀取）。
"""
from __future__ import annotations

import asyncio
import os
import random

import discord
from discord import app_commands

from utils.json_store import load_json, save_json


_PIC_DIR      = os.path.join('data', 'picture')
_RECORDS_FILE = os.path.join('data', 'artillery_records.json')

# (圖檔, 後半段台詞)
_VARIANTS: list[tuple[str, str]] = [
    ('0.jpg', '...這個沒救了直接炮決'),
    ('1.png', '低容谷地電'),
    ('2.png', '中容谷地電'),
    ('3.png', '高容谷地電'),
    ('4.png', '低容武陵電'),
    ('5.png', '中容武陵電'),
]


def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name='電擊蘿莉控', description='隨機抽一位成員進行電擊（每次效果隨機）')
    async def slash_electrocute(interaction: discord.Interaction):
        guild   = interaction.guild
        channel = interaction.channel
        if guild is None or channel is None:
            await interaction.response.send_message(
                embed=discord.Embed(description='此指令只能在伺服器中使用',
                                    color=discord.Color.red()),
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        if not guild.chunked:
            try:
                await asyncio.wait_for(guild.chunk(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

        if isinstance(channel, discord.TextChannel):
            members = [m for m in guild.members
                       if not m.bot and channel.permissions_for(m).view_channel]
        else:
            members = [m for m in guild.members if not m.bot]
        if not members:
            await interaction.followup.send(
                embed=discord.Embed(description='找不到可電擊的對象',
                                    color=discord.Color.red()),
                ephemeral=True,
            )
            return

        victim = random.choice(members)
        fresh  = guild.get_member(victim.id)
        if fresh is None:
            try:
                fresh = await guild.fetch_member(victim.id)
            except discord.HTTPException:
                fresh = None
        if fresh is not None:
            victim = fresh

        filename, label = random.choice(_VARIANTS)

        gid = str(guild.id)
        uid = str(victim.id)
        records = load_json(_RECORDS_FILE)
        records.setdefault(gid, {})[uid] = records.get(gid, {}).get(uid, 0) + 1
        save_json(_RECORDS_FILE, records)

        embed = discord.Embed(
            title='電擊蘿莉控',
            description=(
                f'今天要電擊的蘿莉控是 **{victim.display_name}**\n'
                f'({victim.mention})\n'
                f'今天用{label}'
            ),
            color=discord.Color.dark_red(),
        )
        path = os.path.join(_PIC_DIR, filename)
        if os.path.exists(path):
            embed.set_image(url=f'attachment://{filename}')
            await interaction.followup.send(
                embed=embed,
                file=discord.File(path, filename=filename),
            )
        else:
            await interaction.followup.send(embed=embed)
