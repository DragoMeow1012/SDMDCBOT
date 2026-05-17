"""
管理員指令：/清除記憶
"""
import discord
from discord import app_commands

from config import HISTORY_FILE
from utils.json_store import load_json, save_json_async
import state


def setup(tree: app_commands.CommandTree) -> None:

    @tree.command(name='清除記憶', description='當小龍喵對話被安全過濾卡住時使用，清除本頻道聊天記憶')
    async def slash_clear_memory(interaction: discord.Interaction):
        cid = interaction.channel_id
        state.chat_sessions.pop(cid, None)

        try:
            data = load_json(HISTORY_FILE)
            if data.pop(str(cid), None) is not None:
                await save_json_async(HISTORY_FILE, data)
        except Exception as e:
            print(f'[RESET] 寫回 {HISTORY_FILE} 失敗 ch={cid}: {type(e).__name__}: {e}')

        embed = discord.Embed(
            title='清除記憶',
            description='本頻道的聊天記憶已清除，下次對話將重新開始',
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        print(f'[RESET] {interaction.user} 清除了頻道 {cid} 的聊天記憶。')
