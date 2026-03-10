"""
Discord Bot 主程式入口。
負責：Discord 事件處理、對話 session 管理、URL 偵測與分派。

啟動方式：
    python main.py
"""
import re
import asyncio
import discord

from config import DISCORD_TOKEN, MASTER_ID
from history import load_history, save_history
from web import fetch_url
from gemini_worker import models, msg_queue, gemini_worker

# --- Discord Client ---
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# chat_sessions 結構：
#   { channel_id (int) -> {
#       'chat_obj': ChatSession | None,
#       'model': GenerativeModel | None,
#       'raw_history': list,
#       'current_web_context': str | None,
#   }}
chat_sessions: dict = {}
_worker_started: bool = False


def _init_session(cid: int, model, sess: dict | None) -> None:
    """初始化或切換頻道的對話 session。"""
    raw_history = sess.get('raw_history', []) if sess else []
    web_context = sess.get('current_web_context') if sess else None

    chat_sessions[cid] = {
        'chat_obj': model.start_chat(history=raw_history),
        'model': model,
        'raw_history': raw_history,
        'current_web_context': web_context,
    }


@client.event
async def on_ready() -> None:
    global _worker_started

    print(f'✅ 已登入: {client.user}')

    # 載入本地歷史記憶
    loaded = load_history()
    chat_sessions.update(loaded)

    # 防止重連時重複建立 worker
    if not _worker_started:
        _worker_started = True
        asyncio.create_task(gemini_worker(chat_sessions))

    print(f"✅ Bot 就緒！已載入 {len(chat_sessions)} 個頻道的歷史記憶。")


@client.event
async def on_message(msg: discord.Message) -> None:
    # 忽略 Bot 自身訊息或未被提及的訊息
    if msg.author == client.user or not client.user.mentioned_in(msg):
        return

    cid: int = msg.channel.id
    is_master: bool = (msg.author.id == MASTER_ID)
    model = models['master'] if is_master else models['general']

    # 若頻道尚未初始化，或使用者身分導致模型需要切換，則重建 session
    sess = chat_sessions.get(cid)
    if not sess or sess.get('model') != model:
        print(f"🔄 ch={cid} 初始化{'主人' if is_master else '一般'}模型")
        _init_session(cid, model, sess)

    # 移除 @提及 後取得純文字 prompt
    prompt: str = msg.content.replace(f'<@{client.user.id}>', '').strip()
    if not prompt:
        await msg.reply("主...主人...請問...需...需要什麼協助嗎？喵嗚...")
        return

    print(f"📨 ch={cid} {'[主人]' if is_master else '[訪客]'}: {prompt[:80]}")

    # --- URL 偵測 ---
    if url_match := re.search(r'https?://\S+', prompt):
        url: str = url_match.group(0)
        query: str = prompt.replace(url, '').strip()

        await msg.channel.send("喵嗚~ 偵測到網址，正在抓取內容中...")
        print(f"🌐 抓取 URL: {url}")

        content = await fetch_url(url)
        print(f"📄 抓取結果前 200 字: {content[:200]}")

        if content.startswith("錯誤:") or not content:
            await msg.reply(f"喵嗚... 抓取網頁失敗: {content}")
            # 抓取失敗：仍以原始 prompt 繼續
        else:
            chat_sessions[cid]['current_web_context'] = content
            await msg.reply("喵嗚！已成功抓取網頁內容囉！")
            prompt = (
                f"請簡潔摘要以下網頁內容：\n```\n{content}\n```\n原始網址：{url}"
                if not query else
                f"以下是從網址 `{url}` 抓取到的內容：\n```\n{content}\n```\n請根據這些內容，回答我的問題：{query}"
            )

    # --- 使用已儲存的網頁上下文 ---
    elif web_ctx := chat_sessions[cid].get('current_web_context'):
        prompt = f"根據我之前讀取的內容：\n```\n{web_ctx}\n```\n請問：{prompt}"
        print(f"📚 使用已存網頁上下文 (前 200 字): {web_ctx[:200]}")

    # 將請求放入佇列
    await msg_queue.put({
        'channel_id': cid,
        'prompt_text': prompt,
        'message_object': msg,
    })


if __name__ == '__main__':
    try:
        print("正在連線至 Discord...")
        client.run(DISCORD_TOKEN)
    except discord.errors.LoginFailure:
        print("❌ Discord Bot Token 無效，請檢查 .env 檔案。")
    except KeyboardInterrupt:
        print("🛑 手動中斷，Bot 關閉。")
    except Exception as e:
        print(f"❌ 啟動 Bot 時發生錯誤: {e}")
