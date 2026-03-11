"""
Discord Bot 主程式入口。
負責：Discord 事件處理、對話 session 管理、URL 偵測與分派。

啟動方式：
    PYTHONIOENCODING=utf-8 python -u main.py
"""
import os
import sys
import re
import asyncio
import datetime
import discord
from discord import app_commands

# 修正 Windows cp932 編碼問題：使用 line_buffering=True 避免破壞 -u 無緩衝模式
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', line_buffering=True)

from config import DISCORD_TOKEN, MASTER_ID
from history import load_history, save_history
from web import fetch_url
from gemini_worker import create_chat, msg_queue, gemini_worker
from nicknames import (
    load_nicknames, save_nicknames,
    build_all_nicknames_summary,
)
from knowledge import (
    load_knowledge, add_entry, remove_entry, build_knowledge_context,
)
from summary import load_summary

# --- Discord Client ---
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# chat_sessions 結構：
#   { channel_id (int) -> {
#       'chat_obj': Chat | None,
#       'personality': str | None,
#       'raw_history': list,
#       'current_web_context': str | None,
#   }}
chat_sessions: dict = {}
nicknames: dict[str, str] = {}        # { user_id_str -> nickname }
knowledge_entries: list[dict] = []    # 知識庫條目
_worker_started: bool = False


# ---------------------------------------------------------------------------
# 全域斜線指令
# ---------------------------------------------------------------------------
@tree.command(name="nick", description="設定你的暱稱")
@app_commands.describe(暱稱="你想要設定的暱稱")
async def slash_nick(interaction: discord.Interaction, 暱稱: str):
    nicknames[str(interaction.user.id)] = 暱稱
    save_nicknames(nicknames)
    await interaction.response.send_message(f'好的，我會記住你叫「{暱稱}」！', ephemeral=True)


# ---------------------------------------------------------------------------
# /kb 指令群組（新增 / 載入知識庫）
# ---------------------------------------------------------------------------
kb_group = app_commands.Group(name="kb", description="知識庫管理")
tree.add_command(kb_group)


@kb_group.command(name="add", description="新增內容到知識庫（文字或上傳檔案）")
@app_commands.describe(文字="要儲存的文字內容", 檔案="要儲存的文字檔案（.txt/.md 等）")
async def slash_kb_add(interaction: discord.Interaction,
                       文字: str = None,
                       檔案: discord.Attachment = None):
    global knowledge_entries
    parts = []
    if 文字:
        parts.append(文字.strip())
    if 檔案:
        try:
            raw = await 檔案.read()
            parts.append(f'[{檔案.filename}]\n{raw.decode("utf-8", errors="replace")[:5000]}')
        except Exception as e:
            await interaction.response.send_message(f'讀取檔案失敗: {e}', ephemeral=True)
            return
    if not parts:
        await interaction.response.send_message('請提供文字內容或上傳檔案喵！', ephemeral=True)
        return
    entry = add_entry(knowledge_entries, '\n'.join(parts), interaction.user.id)
    await interaction.response.send_message(f'已儲存至知識庫 `#{entry["id"]}`！', ephemeral=True)


@kb_group.command(name="load", description="將知識庫內容注入此頻道對話供模型參考")
async def slash_kb_load(interaction: discord.Interaction):
    cid = interaction.channel_id
    sess = chat_sessions.get(cid)
    if not sess or not sess.get('chat_obj'):
        await interaction.response.send_message('此頻道尚未開始對話喵！請先 @我 說話。', ephemeral=True)
        return
    kb_ctx = build_knowledge_context(knowledge_entries)
    if not kb_ctx.strip():
        await interaction.response.send_message('知識庫目前是空的喵！', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    chat = sess['chat_obj']
    await asyncio.to_thread(chat.send_message, kb_ctx)
    save_history(chat_sessions)
    await interaction.followup.send('✅ 知識庫已注入此頻道對話！', ephemeral=True)


async def _apply_gag(target: discord.Member, duration: int) -> str | None:
    """套用全伺服器禁言。回傳錯誤訊息或 None（成功）。"""
    try:
        await target.timeout(datetime.timedelta(seconds=duration), reason='電子口球')
        return None
    except discord.Forbidden:
        return '喵嗚... Bot 缺少「管理成員」權限，請在伺服器設定中授予 Bot 此權限！'


class GagConfirmView(discord.ui.View):
    def __init__(self, target: discord.Member, duration: int):
        super().__init__(timeout=30)
        self.target = target
        self.duration = duration

    @discord.ui.button(label='同意 🔇', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target.id:
            await interaction.response.send_message('這不是你的確認按鈕喵！', ephemeral=True)
            return
        err = await _apply_gag(self.target, self.duration)
        if err:
            await interaction.response.edit_message(content=err, view=None)
        else:
            await interaction.response.edit_message(
                content=f'🔇 {self.target.mention} 已戴上電子口球 {self.duration} 秒！', view=None)
        self.stop()

    @discord.ui.button(label='拒絕 ❌', style=discord.ButtonStyle.secondary)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target.id:
            await interaction.response.send_message('這不是你的確認按鈕喵！', ephemeral=True)
            return
        await interaction.response.edit_message(
            content=f'❌ {self.target.mention} 拒絕了電子口球！', view=None)
        self.stop()


@tree.command(name="電子口球", description="對成員套用全伺服器禁言（Timeout）")
@app_commands.describe(time="持續秒數", who="目標（預設為自己）")
async def slash_gag(interaction: discord.Interaction,
                    time: int,
                    who: discord.Member = None):
    target = who or interaction.user
    is_master = (interaction.user.id == MASTER_ID)
    is_self = (target.id == interaction.user.id)

    if time <= 0:
        await interaction.response.send_message('秒數必須大於 0 喵！', ephemeral=True)
        return

    if is_master or is_self:
        err = await _apply_gag(target, time)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
        else:
            await interaction.response.send_message(
                f'🔇 {target.mention} 已戴上電子口球 {time} 秒！', ephemeral=is_self)
        return

    view = GagConfirmView(target, time)
    await interaction.response.send_message(
        f'{target.mention}，{interaction.user.mention} 想幫你戴上電子口球 {time} 秒，你同意嗎？',
        view=view)


def _init_session(cid: int, personality: str, sess: dict | None) -> None:
    """初始化或切換頻道的對話 session。"""
    raw_history = sess.get('raw_history', []) if sess else []
    web_context = sess.get('current_web_context') if sess else None
    summary = load_summary(cid)  # 若有摘要 TXT，在 history 為空時注入

    chat_sessions[cid] = {
        'chat_obj': create_chat(personality, raw_history, summary),
        'personality': personality,
        'raw_history': raw_history,
        'current_web_context': web_context,
    }


# ---------------------------------------------------------------------------
# !nick 文字指令（主人管理用）
#   !nick 列表                          → 列出所有暱稱
#   !nick 設定 <@user|user_id> <暱稱>   → 設定暱稱（主人：任意；訪客：僅限自己）
#   !nick 刪除 <@user|user_id>          → 刪除暱稱（主人限定）
# ---------------------------------------------------------------------------
async def handle_nick_command(msg: discord.Message, args: str) -> None:
    global nicknames

    args = args.strip()
    is_master = (msg.author.id == MASTER_ID)

    if args in ('列表', 'list') and is_master:
        if not nicknames:
            await msg.reply('目前沒有任何已登記的暱稱。')
        else:
            lines = '\n'.join(f'`{uid}` → {nick}' for uid, nick in nicknames.items())
            await msg.reply(f'**已登記暱稱清單：**\n{lines}')
        return

    if args.startswith('設定 ') or args.startswith('設定　'):
        parts = args[3:].strip().split(None, 1)
        if len(parts) < 2:
            await msg.reply('語法：`!nick 設定 <@user 或 user_id> <暱稱>`')
            return
        target_raw, new_nick = parts[0], parts[1].strip()
        target_id = _parse_user_id(target_raw, msg)
        if target_id is None:
            await msg.reply('無法辨識目標用戶，請使用 @提及 或直接輸入 user_id。')
            return
        if not is_master and target_id != msg.author.id:
            await msg.reply('你只能設定自己的暱稱喔。')
            return
        nicknames[str(target_id)] = new_nick
        save_nicknames(nicknames)
        await msg.reply(f'已將 `{target_id}` 的暱稱設為「{new_nick}」。')
        return

    if (args.startswith('刪除 ') or args.startswith('刪除　')) and is_master:
        target_id = _parse_user_id(args[3:].strip(), msg)
        if target_id is None:
            await msg.reply('無法辨識目標用戶。')
            return
        uid_str = str(target_id)
        if uid_str in nicknames:
            removed = nicknames.pop(uid_str)
            save_nicknames(nicknames)
            await msg.reply(f'已刪除 `{target_id}` 的暱稱（原為「{removed}」）。')
        else:
            await msg.reply(f'`{target_id}` 沒有登記暱稱。')
        return

    await msg.reply('語法：`!nick 列表` / `!nick 設定 <user> <暱稱>` / `!nick 刪除 <user>`')


# ---------------------------------------------------------------------------
# !kb 文字指令（主人管理用；新增/載入請用 /kb add 和 /kb load）
#   !kb 列表         → 列出所有條目（主人限定）
#   !kb 刪除 <id>    → 刪除條目（主人限定）
# ---------------------------------------------------------------------------
async def handle_kb_command(msg: discord.Message, args: str) -> None:
    global knowledge_entries

    args = args.strip()
    is_master = (msg.author.id == MASTER_ID)

    if not is_master:
        await msg.reply('知識庫管理指令限主人使用，新增請用 `/kb add` 喵！')
        return

    if args in ('列表', 'list'):
        if not knowledge_entries:
            await msg.reply('知識庫目前是空的。')
        else:
            lines = '\n'.join(
                f'`#{e["id"]}` [{e["timestamp"]}] {e["content"][:80]}'
                for e in knowledge_entries
            )
            await msg.reply(f'**知識庫條目（共 {len(knowledge_entries)} 筆）：**\n{lines}')
        return

    if args.startswith('刪除 ') or args.startswith('刪除　'):
        id_str = args.split(None, 1)[1].strip()
        if not id_str.isdigit():
            await msg.reply('請提供有效的條目 ID（數字）。')
            return
        if remove_entry(knowledge_entries, int(id_str)):
            await msg.reply(f'已刪除知識庫條目 `#{id_str}`。')
        else:
            await msg.reply(f'找不到條目 `#{id_str}`。')
        return

    await msg.reply('語法：`!kb 列表` / `!kb 刪除 <id>`')


# ---------------------------------------------------------------------------
# 附件處理輔助
# ---------------------------------------------------------------------------
_INLINE_MIME_TYPES: frozenset[str] = frozenset({
    'image/jpeg', 'image/png', 'image/gif', 'image/webp', 'application/pdf',
})
_TEXT_EXTENSIONS: frozenset[str] = frozenset({
    '.txt', '.py', '.js', '.ts', '.json', '.md', '.csv', '.html', '.htm',
    '.css', '.xml', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.log',
    '.sh', '.bat', '.c', '.cpp', '.h', '.java', '.go', '.rs', '.rb',
})


def _guess_mime(filename: str) -> str:
    """從副檔名推測 MIME type。"""
    ext = os.path.splitext(filename)[1].lower()
    return {
        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
        '.png': 'image/png', '.gif': 'image/gif', '.webp': 'image/webp',
        '.pdf': 'application/pdf',
    }.get(ext, 'application/octet-stream')


def _is_text_file(filename: str) -> bool:
    return os.path.splitext(filename)[1].lower() in _TEXT_EXTENSIONS


def _parse_user_id(raw: str, msg: discord.Message) -> int | None:
    """從 <@id>、<@!id> 或純數字字串解析 user_id。"""
    # @提及格式
    m = re.match(r'<@!?(\d+)>', raw)
    if m:
        return int(m.group(1))
    # 純數字
    if raw.isdigit():
        return int(raw)
    return None


# ---------------------------------------------------------------------------
# Discord 事件
# ---------------------------------------------------------------------------

@client.event
async def on_ready() -> None:
    global _worker_started, nicknames, knowledge_entries

    print(f'[OK] Logged in as: {client.user}')

    loaded = load_history()
    chat_sessions.update(loaded)

    nicknames = load_nicknames()
    knowledge_entries = load_knowledge()

    if not _worker_started:
        _worker_started = True
        asyncio.create_task(gemini_worker(chat_sessions))

    await tree.sync()
    print(f'[OK] Bot ready! {len(chat_sessions)} channels, {len(nicknames)} nicknames, {len(knowledge_entries)} KB entries.')


@client.event
async def on_message(msg: discord.Message) -> None:
    if msg.author == client.user or not client.user.mentioned_in(msg):
        return

    cid: int = msg.channel.id
    is_master: bool = (msg.author.id == MASTER_ID)
    personality: str = 'master' if is_master else 'general'

    sess = chat_sessions.get(cid)
    if not sess or sess.get('personality') != personality:
        print(f'[INIT] ch={cid} personality={personality}')
        _init_session(cid, personality, sess)

    # 移除 @提及，取得純文字
    raw_text: str = msg.content.replace(f'<@{client.user.id}>', '').strip()

    # 空白且無附件才回「需要什麼協助」
    if not raw_text and not msg.attachments:
        await msg.reply('主...主人...請問...需...需要什麼協助嗎？喵嗚...')
        return

    # --- !nick 指令攔截（不送 Gemini）---
    if raw_text.startswith('!nick ') or raw_text.startswith('!nick　'):
        await handle_nick_command(msg, raw_text[6:])
        return

    # --- !kb 指令攔截（不送 Gemini）---
    if raw_text.startswith('!kb ') or raw_text.startswith('!kb　'):
        await handle_kb_command(msg, raw_text[4:])
        return

    print(f'[MSG] ch={cid} [{personality}]: {raw_text[:80]}')

    # --- 建立用戶身分前綴注入給模型 ---
    uid_str = str(msg.author.id)
    nick = nicknames.get(uid_str)
    display_name = msg.author.display_name

    if nick:
        user_ctx = f'[User ID: {msg.author.id}, 暱稱: {nick}]'
    else:
        user_ctx = f'[User ID: {msg.author.id}, 伺服器名稱: {display_name}（未設定自訂暱稱，請以此名稱稱呼對方）]'

    if is_master:
        nick_summary = build_all_nicknames_summary(nicknames)
        identity_prefix = f'{nick_summary}\n{user_ctx}\n'
    else:
        identity_prefix = f'{user_ctx}\n'

    # 純圖片/附件無文字時使用預設提示
    prompt: str = raw_text if raw_text else '請描述這個附件的內容。'

    # --- 附件處理（圖片/PDF → file_parts；文字檔 → 附加到 prompt）---
    file_parts: list[dict] = []
    if msg.attachments:
        await msg.channel.send('喵嗚~ 偵測到附件，讀取中...')
        for attachment in msg.attachments:
            mime = attachment.content_type or _guess_mime(attachment.filename)
            # 去掉 MIME 參數部分（如 "image/png; charset=utf-8"）
            mime = mime.split(';')[0].strip()

            if mime in _INLINE_MIME_TYPES:
                try:
                    data = await attachment.read()
                    file_parts.append({'data': data, 'mime_type': mime})
                    print(f'[FILE] {attachment.filename} ({mime}, {len(data)} bytes)')
                except Exception as e:
                    await msg.reply(f'讀取 `{attachment.filename}` 失敗: {e}')

            elif _is_text_file(attachment.filename):
                try:
                    data = await attachment.read()
                    text_content = data.decode('utf-8', errors='replace')
                    prompt += f'\n\n[附件 {attachment.filename}]:\n```\n{text_content[:3000]}\n```'
                    print(f'[FILE] 文字附件 {attachment.filename} ({len(text_content)} chars)')
                except Exception as e:
                    await msg.reply(f'讀取 `{attachment.filename}` 失敗: {e}')

            else:
                await msg.reply(
                    f'喵嗚... 不支援 `{attachment.filename}` 的格式（{mime}），'
                    '目前支援：圖片（jpg/png/gif/webp）、PDF、文字檔。'
                )

    # --- URL 偵測 ---
    if url_match := re.search(r'https?://\S+', prompt):
        url: str = url_match.group(0)
        query: str = prompt.replace(url, '').strip()

        await msg.channel.send('喵嗚~ 偵測到網址，正在抓取內容中...')
        print(f'[WEB] Fetching: {url}')

        content = await fetch_url(url)

        if content.startswith('錯誤:') or not content:
            await msg.reply(f'喵嗚... 抓取網頁失敗: {content}')
        else:
            chat_sessions[cid]['current_web_context'] = content
            await msg.reply('喵嗚！已成功抓取網頁內容囉！')
            prompt = (
                f'請簡潔摘要以下網頁內容：\n```\n{content}\n```\n原始網址：{url}'
                if not query else
                f'以下是從網址 `{url}` 抓取到的內容：\n```\n{content}\n```\n請根據這些內容，回答我的問題：{query}'
            )

    # --- 使用已儲存的網頁上下文 ---
    elif web_ctx := chat_sessions[cid].get('current_web_context'):
        prompt = f'根據我之前讀取的內容：\n```\n{web_ctx}\n```\n請問：{prompt}'

    final_prompt = identity_prefix + prompt

    await msg_queue.put({
        'channel_id': cid,
        'prompt_text': final_prompt,
        'file_parts': file_parts,
        'message_object': msg,
    })


if __name__ == '__main__':
    try:
        print('Connecting to Discord...')
        client.run(DISCORD_TOKEN)
    except discord.errors.LoginFailure:
        print('[ERROR] Invalid Discord Bot Token. Check your .env file.')
    except KeyboardInterrupt:
        print('[STOP] Bot stopped by user.')
    except Exception as e:
        print(f'[ERROR] Failed to start bot: {e}')
    finally:
        if chat_sessions:
            print('[SAVE] 關閉前儲存聊天歷史...')
            save_history(chat_sessions)
