"""
/賭博：賭博小遊戲分派指令。

選遊戲 → 派遣到各遊戲檔案的 start_setup()：
  - 老虎機 (slot)
  - 骰子 (dice, Sic Bo)
  - 輪盤 (roulette, 歐式)
  - 21 點 (blackjack)
  - 爆點 (crash) 🚀
  - 踩地雷 (minesweeper) 💣

下注金額不再寫在 slash 參數，而是在各遊戲的「設定畫面」用按鈕設定。
共同錢包：data/morning_records.json `users[uid].balance` (與 /早安小龍喵 共用)。
"""
from __future__ import annotations

import discord
from discord import app_commands

from commands import blackjack, crash, dice, minesweeper, pvp, roulette, slot


_GAMES = [
    app_commands.Choice(name='老虎機 3×3',        value='slot'),
    app_commands.Choice(name='骰子 (Sic Bo)',     value='dice'),
    app_commands.Choice(name='輪盤 (歐式)',       value='roulette'),
    app_commands.Choice(name='21 點 (Blackjack)', value='blackjack'),
    app_commands.Choice(name='爆點 (Crash) 🚀',   value='crash'),
    app_commands.Choice(name='踩地雷 💣',          value='mines'),
]

def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(
        name='賭博',
        description='賭博小遊戲（單人選遊戲，或填「對戰」對他人發起 PVP）',
    )
    @app_commands.describe(
        遊戲選擇='要玩哪個單人賭博遊戲（PVP 時不用填）',
        對戰='發起 PVP 對戰的對象（金額在設定畫面選）',
    )
    @app_commands.choices(遊戲選擇=_GAMES)
    async def slash_gamble(
        interaction: discord.Interaction,
        遊戲選擇: app_commands.Choice[str] | None = None,
        對戰: discord.Member | None = None,
    ):
        # PVP 分支
        if 對戰 is not None:
            if 遊戲選擇 is not None:
                await interaction.response.send_message(
                    'PVP 與「遊戲選擇」不能同時填', ephemeral=True,
                )
                return
            await pvp.start_setup(interaction, 對戰)
            return

        # 單人分支
        if 遊戲選擇 is None:
            await interaction.response.send_message(
                '請選一個遊戲，或填「對戰」對他人發起 PVP', ephemeral=True,
            )
            return

        v = 遊戲選擇.value
        if v == 'slot':
            await slot.start_setup(interaction)
        elif v == 'dice':
            await dice.start_setup(interaction)
        elif v == 'roulette':
            await roulette.start_setup(interaction)
        elif v == 'blackjack':
            await blackjack.start_setup(interaction)
        elif v == 'crash':
            await crash.start_setup(interaction)
        elif v == 'mines':
            await minesweeper.start_setup(interaction)
        else:
            await interaction.response.send_message('未知的遊戲', ephemeral=True)
