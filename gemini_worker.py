"""
Gemini API 工作器模組。
負責：模型初始化、API Key 輪替、請求佇列限速處理。
"""
import asyncio
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted

from config import GEMINI_API_KEYS, GEMINI_MODEL_NAME, PERSONALITY, API_DELAY
from history import save_history

# --- API Key 輪替 ---
_key_index: int = 0


def _configure_api() -> None:
    """依照目前 index 設定 Gemini API Key。"""
    key = GEMINI_API_KEYS[_key_index % len(GEMINI_API_KEYS)]
    genai.configure(api_key=key)


def rotate_api_key() -> None:
    """切換至下一組 API Key 並重新設定。"""
    global _key_index
    _key_index += 1
    _configure_api()
    print(f"🔄 已切換 Gemini API Key (index={_key_index % len(GEMINI_API_KEYS)})")


# 初始化 API
_configure_api()

# --- Gemini 模型實例 ---
models: dict = {
    'general': genai.GenerativeModel(
        GEMINI_MODEL_NAME,
        system_instruction=PERSONALITY['general']
    ),
    'master': genai.GenerativeModel(
        GEMINI_MODEL_NAME,
        system_instruction=PERSONALITY['master']
    ),
}

# --- 請求佇列 ---
msg_queue: asyncio.Queue = asyncio.Queue()
_last_api_time: float = 0.0


async def gemini_worker(chat_sessions: dict) -> None:
    """
    持續從 msg_queue 取出請求並呼叫 Gemini API。
    - 實施 API_DELAY 秒的最短間隔限速
    - 支援 ResourceExhausted / rate limit / timeout 錯誤處理
    - 確保 task_done() 在所有路徑皆被呼叫
    """
    global _last_api_time

    while True:
        req = await msg_queue.get()
        cid: int = req['channel_id']
        prompt: str = req['prompt_text']
        msg = req['message_object']

        try:
            sess = chat_sessions.get(cid)
            if not sess or sess.get('chat_obj') is None:
                print(f"⚠️ ch={cid} 無對話物件，略過此請求")
                continue

            chat = sess['chat_obj']

            # 限速：距上次呼叫需間隔 API_DELAY 秒
            loop = asyncio.get_running_loop()
            elapsed = loop.time() - _last_api_time
            if elapsed < API_DELAY:
                await asyncio.sleep(API_DELAY - elapsed)

            async with msg.channel.typing():
                try:
                    resp = await asyncio.to_thread(chat.send_message, prompt)
                    _last_api_time = asyncio.get_running_loop().time()
                    text: str = resp.text

                    # 分段發送超過 2000 字的回應
                    if len(text) > 2000:
                        await msg.reply("我的回應太長了，我會分段傳送：")
                        for i in range(0, len(text), 1990):
                            await msg.channel.send(text[i:i + 1990])
                    else:
                        await msg.reply(text)

                    # 回覆後同步儲存歷史至本地
                    save_history(chat_sessions)

                except ResourceExhausted as e:
                    print(f"⚠️ ResourceExhausted ch={cid}: {e}")
                    await msg.reply("你們傳送的太快了喵! 稍等一分鐘後再試一次！哼喵!")

                except Exception as e:
                    err = str(e).lower()
                    if any(kw in err for kw in ["quota", "rate limit", "429", "toomanyrequests"]):
                        print(f"⚠️ 限速觸發 ch={cid}: {e}")
                        rotate_api_key()
                        await msg.reply("你們傳送的太快了喵! 等我換一下API再試一次！嗚喵!")
                    elif "timeout" in err:
                        print(f"⏱️ API逾時 ch={cid}: {e}")
                        await msg.reply("喵嗚...Gemini API 回應時間太長了，請稍後再試試看喔！")
                    else:
                        print(f"❌ 未知錯誤 {type(e).__name__}: {e}")
                        await msg.reply("抱歉，我在處理您的請求時遇到了未知的錯誤喵。")

        finally:
            # 確保無論成功或失敗，task_done() 必被呼叫
            msg_queue.task_done()
