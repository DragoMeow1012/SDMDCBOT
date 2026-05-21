"""
共用 Discord 輔助函式。
提取指令模組中重複的按鈕權限檢查、排行榜分頁、成員查找等邏輯。

排行榜採「PIL 卡片圖 + embed」雙層：
  Discord embed 限制每張只能有一個 thumbnail / author icon，沒辦法逐行帶頭像。
  所以一頁 5 列的排行榜直接畫成一張 PNG 卡片（圓形頭像在每行最前面），
  用 embed.set_image 塞進 embed 當主圖。embed 本身保留標題 / footer / 顏色。
"""
from __future__ import annotations

import asyncio
import io
import os

import aiohttp
import discord
from PIL import Image, ImageDraw, ImageFont


async def owner_only_button_check(interaction: discord.Interaction, owner_id: int) -> bool:
    """
    檢查按下按鈕的使用者是否為指定的 owner。
    若不是，自動回覆拒絕訊息並回傳 False。
    """
    if interaction.user.id == owner_id:
        return True
    await interaction.response.send_message('這不是你的確認按鈕喵！', ephemeral=True)
    return False


async def get_member_safe(guild: discord.Guild, uid: int) -> discord.Member | None:
    """先從快取取成員，失敗再用 API fetch，都找不到回傳 None。"""
    member = guild.get_member(uid)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(uid)
    except discord.NotFound:
        return None


async def format_leaderboard(
    records: dict[str, int],
    guild: discord.Guild,
    title: str,
    limit: int = 10,
) -> str:
    """將 {uid_str: count} 格式化為排行榜文字（保留向下相容）。"""
    top = sorted(records.items(), key=lambda x: x[1], reverse=True)[:limit]
    lines = [title]
    for rank, (uid, cnt) in enumerate(top, 1):
        member = await get_member_safe(guild, int(uid))
        name = member.display_name if member else f'（已離開：{uid}）'
        lines.append(f'`{rank}.` {name} — **{cnt}** 次')
    return '\n'.join(lines)


# ─── 排行榜分頁元件 ──────────────────────────────────────────────
_RANK_PER_PAGE = 5

# 排名牌底色：金/銀/銅，4-5 用中性灰
_RANK_BADGE_COLORS = {
    1: (255, 196, 0),
    2: (192, 192, 192),
    3: (205, 127, 50),
}
_RANK_BADGE_DEFAULT = (96, 102, 112)

# CJK TTF 字型路徑：Windows 常駐 → macOS → Linux noto；找不到就退回 PIL default
# （default 不支援中文，極端 fallback 才會走到）
_FONT_CANDIDATES = [
    r'C:\Windows\Fonts\msjh.ttc',     # Microsoft JhengHei（繁中）
    r'C:\Windows\Fonts\msjhbd.ttc',
    r'C:\Windows\Fonts\msyh.ttc',     # Microsoft YaHei（簡中）
    r'C:\Windows\Fonts\msyhbd.ttc',
    '/System/Library/Fonts/PingFang.ttc',
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
]


def _find_font_path() -> str | None:
    for p in _FONT_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


_FONT_PATH = _find_font_path()


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if _FONT_PATH:
        try:
            return ImageFont.truetype(_FONT_PATH, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _circle_avatar(raw: bytes, size: int) -> Image.Image | None:
    """圖片 bytes → 圓形 RGBA PIL Image（已經 resize 到 size×size）。"""
    try:
        img = Image.open(io.BytesIO(raw)).convert('RGBA').resize(
            (size, size), Image.LANCZOS,
        )
        mask = Image.new('L', (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
        img.putalpha(mask)
        return img
    except Exception as e:
        print(f'[RANK] 頭像處理失敗: {e}')
        return None


def _placeholder_avatar(size: int) -> Image.Image:
    """成員已離開或頭像下載失敗時的灰色圓形佔位。"""
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((0, 0, size, size), fill=(80, 84, 92, 255))
    return img


async def _fetch_avatar_bytes(member: discord.Member) -> bytes | None:
    try:
        return await member.display_avatar.replace(size=128).read()
    except (discord.HTTPException, aiohttp.ClientError) as e:
        print(f'[RANK] 頭像下載失敗 uid={member.id}: {e}')
        return None


def _render_card_sync(
    rows: list[tuple[int, str, int, Image.Image]],
    unit: str,
) -> bytes:
    """
    把一頁 5 列排行榜畫成 PNG bytes。
    標題在 embed 上方顯示、頁碼用底下分頁按鈕表示，圖內只畫資料列。
    rows: [(rank, name, count, avatar_image), ...]
    """
    W = 760
    PAD_X = 28
    PAD_TOP = 16
    PAD_BOTTOM = 16
    ROW_H = 96
    AVATAR = 64
    BG = (36, 39, 46, 255)
    ROW_BG = (46, 50, 58, 255)
    TXT = (236, 238, 242, 255)

    H = PAD_TOP + ROW_H * len(rows) + PAD_BOTTOM
    img = Image.new('RGBA', (W, H), BG)
    draw = ImageDraw.Draw(img)

    badge_font = _load_font(22)
    name_font  = _load_font(24)
    count_font = _load_font(28)

    y = PAD_TOP
    for rank, name, count, avatar in rows:
        # 列底色
        draw.rounded_rectangle(
            (PAD_X - 8, y, W - PAD_X + 8, y + ROW_H - 8),
            radius=14, fill=ROW_BG,
        )

        # 頭像
        ax = PAD_X
        ay = y + (ROW_H - 8 - AVATAR) // 2
        img.paste(avatar, (ax, ay), avatar)

        # 排名圓徽
        badge_d = 38
        bx = ax + AVATAR + 16
        by = y + (ROW_H - 8 - badge_d) // 2
        badge_color = _RANK_BADGE_COLORS.get(rank, _RANK_BADGE_DEFAULT)
        draw.ellipse((bx, by, bx + badge_d, by + badge_d),
                     fill=(*badge_color, 255))
        rank_str = str(rank)
        bb = draw.textbbox((0, 0), rank_str, font=badge_font)
        rw, rh = bb[2] - bb[0], bb[3] - bb[1]
        draw.text(
            (bx + (badge_d - rw) // 2 - bb[0],
             by + (badge_d - rh) // 2 - bb[1]),
            rank_str, fill=(20, 22, 28, 255), font=badge_font,
        )

        # 名稱
        nx = bx + badge_d + 16
        # 太長就截斷
        max_name_w = W - PAD_X - nx - 180
        display_name = name
        while display_name and draw.textlength(display_name, font=name_font) > max_name_w:
            display_name = display_name[:-1]
        if display_name != name:
            display_name = display_name[:-1] + '…' if display_name else name[:1] + '…'
        ny = y + (ROW_H - 8) // 2 - 16
        draw.text((nx, ny), display_name, fill=TXT, font=name_font)

        # 次數（右對齊）
        count_str = f'{count} {unit}'
        cb = draw.textbbox((0, 0), count_str, font=count_font)
        cw = cb[2] - cb[0]
        cx = W - PAD_X - cw - 4
        cy = y + (ROW_H - 8) // 2 - (cb[3] - cb[1]) // 2 - cb[1]
        draw.text((cx, cy), count_str, fill=TXT, font=count_font)

        y += ROW_H

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


class LeaderboardView(discord.ui.View):
    """排行榜分頁器。一頁 5 列，整頁畫成一張 PNG 卡片（每行附頭像）。"""

    def __init__(self, guild: discord.Guild, records: dict[str, int],
                 title: str, unit: str = '次',
                 color: discord.Color | None = None):
        super().__init__(timeout=86400)
        self.guild   = guild
        self.entries = sorted(records.items(), key=lambda x: x[1], reverse=True)
        self.title   = title
        self.unit    = unit
        self.color   = color or discord.Color.gold()
        self.page    = 0
        self.total_pages = max(1, (len(self.entries) + _RANK_PER_PAGE - 1) // _RANK_PER_PAGE)
        # 頭像快取，跨頁切換不重抓（key=uid str → PIL Image 64x64 圓形）
        self._avatar_cache: dict[str, Image.Image] = {}
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.prev_btn.disabled = (self.page <= 0)
        self.next_btn.disabled = (self.page >= self.total_pages - 1)
        self.page_btn.label = f'{self.page + 1} / {self.total_pages}'

    async def _resolve_chunk(self) -> list[tuple[int, str, int, Image.Image]]:
        """把目前 page 的資料解析成 (rank, name, count, avatar) 並抓頭像。"""
        start = self.page * _RANK_PER_PAGE
        chunk = self.entries[start:start + _RANK_PER_PAGE]

        AVATAR = 64

        async def _one(offset: int, uid: str, cnt: int):
            rank = start + offset + 1
            cached = self._avatar_cache.get(uid)
            member = await get_member_safe(self.guild, int(uid))

            if member is None:
                name = '（已離開）'
                avatar = cached or _placeholder_avatar(AVATAR)
            else:
                name = member.display_name
                if cached is None:
                    raw = await _fetch_avatar_bytes(member)
                    if raw is not None:
                        circ = await asyncio.to_thread(_circle_avatar, raw, AVATAR)
                        cached = circ or _placeholder_avatar(AVATAR)
                    else:
                        cached = _placeholder_avatar(AVATAR)
                    self._avatar_cache[uid] = cached
                avatar = cached
            return rank, name, cnt, avatar

        return await asyncio.gather(
            *(_one(i, uid, cnt) for i, (uid, cnt) in enumerate(chunk))
        )

    async def render(self) -> tuple[discord.Embed, discord.File | None]:
        # 標題：xx排行（伺服器名稱）；不放 footer，避免上下都是伺服器名重複
        header = f'{self.title}（{self.guild.name}）'
        embed = discord.Embed(title=header, color=self.color)

        if not self.entries:
            embed.description = '尚無紀錄'
            return embed, None

        rows = await self._resolve_chunk()
        png_bytes = await asyncio.to_thread(
            _render_card_sync, rows, self.unit,
        )
        file = discord.File(io.BytesIO(png_bytes), filename='leaderboard.png')
        embed.set_image(url='attachment://leaderboard.png')
        return embed, file

    @discord.ui.button(label='上一頁', style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        self.page = max(0, self.page - 1)
        self._sync_buttons()
        embed, file = await self.render()
        attachments = [file] if file else []
        await interaction.response.edit_message(
            embed=embed, attachments=attachments, view=self,
        )

    @discord.ui.button(label='1 / 1', style=discord.ButtonStyle.primary, disabled=True)
    async def page_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        # 純顯示頁碼，不接受互動
        await interaction.response.defer()

    @discord.ui.button(label='下一頁', style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        self.page = min(self.total_pages - 1, self.page + 1)
        self._sync_buttons()
        embed, file = await self.render()
        attachments = [file] if file else []
        await interaction.response.edit_message(
            embed=embed, attachments=attachments, view=self,
        )


async def send_leaderboard(
    interaction: discord.Interaction,
    records: dict[str, int],
    title: str,
    unit: str = '次',
    color: discord.Color | None = None,
) -> None:
    """高階入口：在 interaction 上回傳分頁排行榜。已 defer 或未 defer 都可用。"""
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message('此指令只能在伺服器中使用', ephemeral=True)
        return
    if not records:
        empty = discord.Embed(title=title, description='尚無紀錄',
                              color=color or discord.Color.dark_grey())
        if interaction.response.is_done():
            await interaction.followup.send(embed=empty)
        else:
            await interaction.response.send_message(embed=empty)
        return

    # 渲染卡片可能要下載 5 張頭像，視情況可能 1-3 秒；確保 interaction 已 defer
    if not interaction.response.is_done():
        await interaction.response.defer()

    view = LeaderboardView(guild, records, title, unit=unit, color=color)
    embed, file = await view.render()
    if file:
        await interaction.followup.send(embed=embed, file=file, view=view)
    else:
        await interaction.followup.send(embed=embed, view=view)
