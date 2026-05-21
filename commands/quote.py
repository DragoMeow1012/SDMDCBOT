"""
名言佳句指令：右鍵選單「名言佳句」/ 「Make it Quote」
"""
import asyncio
import io
import re
import discord
from discord import app_commands


def _resolve_mentions(text: str, mentions: list) -> str:
    """將訊息中的 <@ID> 或 <@!ID> 替換為 @伺服器顯示名稱。"""
    lookup = {str(u.id): (u.display_name if isinstance(u, discord.Member) else u.name)
              for u in mentions}

    def _replace(m):
        uid = m.group(1)
        return f'@{lookup[uid]}' if uid in lookup else f'@{uid}'

    return re.sub(r'<@!?(\d+)>', _replace, text)


class QuoteToggleView(discord.ui.View):
    def __init__(self, avatar_url: str, quote: str, author_name: str, author_id: int, grayscale: bool = True):
        super().__init__(timeout=86400)
        self.avatar_url  = avatar_url
        self.quote       = quote
        self.author_name = author_name
        self.author_id   = author_id
        self.grayscale   = grayscale
        self._update_label()

    def _update_label(self):
        self.toggle_btn.label = '切換彩色 🎨' if self.grayscale else '切換黑白 ⬛'

    @discord.ui.button(label='切換彩色 🎨', style=discord.ButtonStyle.secondary)
    async def toggle_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        from quote_image import make_quote_image
        self.grayscale = not self.grayscale
        self._update_label()
        await interaction.response.defer()
        img_bytes = await asyncio.get_running_loop().run_in_executor(
            None, lambda: make_quote_image(
                self.avatar_url, self.quote, self.author_name, self.author_id,
                grayscale=self.grayscale))
        await interaction.edit_original_response(
            attachments=[discord.File(io.BytesIO(img_bytes), filename='quote.png')],
            view=self)


async def _make_quote(interaction: discord.Interaction, message: discord.Message) -> None:
    from quote_image import make_quote_image

    raw = message.content.strip()
    if not raw:
        await interaction.response.send_message('這則訊息沒有文字內容喵！', ephemeral=True)
        return

    await interaction.response.defer()

    text       = _resolve_mentions(raw, message.mentions)
    target     = message.author
    avatar_url = target.display_avatar.replace(size=4096).url
    nick       = target.display_name if isinstance(target, discord.Member) else target.name

    img_bytes = await asyncio.get_running_loop().run_in_executor(
        None, lambda: make_quote_image(avatar_url, text, nick, target.id))

    view = QuoteToggleView(avatar_url, text, nick, target.id, grayscale=True)
    await interaction.followup.send(
        file=discord.File(io.BytesIO(img_bytes), filename='quote.png'),
        view=view)


def setup(tree: app_commands.CommandTree) -> None:

    @tree.context_menu(name="名言佳句")
    async def ctx_quote(interaction: discord.Interaction, message: discord.Message):
        await _make_quote(interaction, message)

    @tree.context_menu(name="Make it Quote")
    async def ctx_quote_en(interaction: discord.Interaction, message: discord.Message):
        await _make_quote(interaction, message)
