"""
Gemini API 工作器模組（使用新版 google-genai SDK）。
負責：Client 初始化、API Key 輪替、請求佇列限速處理。
"""
import asyncio
from google import genai
from google.genai import types

from config import GEMINI_API_KEYS, GEMINI_MODEL_NAME, PERSONALITY, API_DELAY
from history import save_history
from knowledge import add_entry, consolidate_knowledge

# --- API Key 輪替 ---
_key_index: int = 0
_client: genai.Client = None


def _create_client() -> genai.Client:
    key = GEMINI_API_KEYS[_key_index % len(GEMINI_API_KEYS)]
    return genai.Client(api_key=key)


def rotate_api_key() -> None:
    """切換至下一組 API Key 並重建 Client。"""
    global _key_index, _client
    _key_index += 1
    _client = _create_client()
    print(f"[ROTATE] API Key index={_key_index % len(GEMINI_API_KEYS)}")


# 初始化 Client
_client = _create_client()

# --- 各人格的對話設定 ---
# Lite 模型不支援 google_search grounding tool
_supports_search = 'lite' not in GEMINI_MODEL_NAME.lower()
_tools = [types.Tool(google_search=types.GoogleSearch())] if _supports_search else []

_CHAT_CONFIGS: dict[str, types.GenerateContentConfig] = {
    'general': types.GenerateContentConfig(
        system_instruction=PERSONALITY['general'],
        tools=_tools,
    ),
    'master': types.GenerateContentConfig(
        system_instruction=PERSONALITY['master'],
        tools=_tools,
    ),
}


def _to_contents(history: list) -> list[types.Content]:
    """
    將 JSON dict 格式的歷史轉換為 types.Content 物件列表。
    接受：[{"role": "user", "parts": [{"text": "..."}]}, ...]
    """
    result = []
    for item in history:
        if isinstance(item, types.Content):
            result.append(item)
            continue
        if not isinstance(item, dict):
            continue
        parts = [
            types.Part(text=p['text'])
            for p in item.get('parts', [])
            if isinstance(p, dict) and p.get('text')
        ]
        if parts:
            result.append(types.Content(role=item['role'], parts=parts))
    return result


def create_chat(personality: str, history: list, summary: str | None = None) -> genai.chats.Chat:
    """
    建立新的 Gemini Chat session，並還原歷史紀錄。
    history 格式：[{"role": "user", "parts": [{"text": "..."}]}, ...]
    summary：頻道對話摘要 TXT，僅在 history 為空時注入作為初始記憶。
    """
    if not history and summary:
        # raw_history 為空（新頻道或清空後）→ 以摘要作為合成 history 注入
        history = [
            {"role": "user",  "parts": [{"text": f"[過去對話記錄]\n{summary}"}]},
            {"role": "model", "parts": [{"text": "好的，我已讀取過去的對話記錄，會參考這些內容繼續我們的對話。"}]},
        ]
        print(f"[SUMMARY] 已注入摘要記憶（{len(summary)} 字）")

    converted = _to_contents(history) if history else []
    return _client.chats.create(
        model=GEMINI_MODEL_NAME,
        config=_CHAT_CONFIGS[personality],
        history=converted,
    )


def _source_from_url(url: str) -> str:
    u = url.lower()
    if "pixiv.net" in u:
        return "pixiv"
    if "twitter.com" in u or "x.com" in u:
        return "X/twitter"
    if "nhentai.net" in u:
        return "nhentai"
    if "e-hentai.org" in u:
        return "e-hentai"
    if "gelbooru" in u:
        return "gelbooru"
    if "danbooru" in u:
        return "danbooru"
    return "來源未知"


def _reverse_search_fallback(prompt: str) -> str | None:
    marker = "[以圖搜圖結果]"
    if marker not in prompt:
        return None
    after = prompt.split(marker, 1)[1]
    if after.startswith("\n"):
        after = after[1:]
    block = after.split("\n\n用戶問題：", 1)[0].strip()
    if not block:
        return None

    results: list[str] = []
    for chunk in block.split("\n\n"):
        lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
        if not lines:
            continue
        url = next((ln for ln in lines if "http" in ln), "")
        if not url:
            continue
        meta = lines[0]
        source = ""
        title = ""
        if "｜" in meta:
            parts = [p.strip() for p in meta.split("｜") if p.strip()]
        else:
            parts = [p.strip() for p in meta.split("|") if p.strip()]
        if len(parts) >= 2:
            source = parts[1]
        if len(parts) >= 3:
            title = parts[2]
        if not source:
            source = _source_from_url(url)
        if not title:
            title = "作品未知"
        author = "作者未知"
        results.append(f"{source} | {title} | {author}\n連結：**{url}**")
        if len(results) >= 5:
            break

    return "\n".join(results) if results else None


async def analyze_for_kb(raw_content: str) -> str:
    """
    使用 Gemini 分析並統整原始內容，回傳適合存入知識庫的摘要文字。
    獨立呼叫（非 chat），不影響任何頻道對話歷史。
    """
    prompt = (
        "請分析以下內容，提取關鍵資訊並整理為簡潔的繁體中文摘要，"
        "方便日後查詢。保留重要數值、名稱、日期等細節，省略冗餘敘述：\n\n"
        f"{raw_content[:8000]}"
    )
    try:
        resp = await asyncio.to_thread(
            _client.models.generate_content,
            model=GEMINI_MODEL_NAME,
            contents=prompt,
        )
        return resp.text.strip()
    except Exception as e:
        print(f"[KB] analyze_for_kb 失敗: {e}")
        return raw_content[:2000]  # 分析失敗時 fallback 存原始內容


# --- 請求佇列 ---
msg_queue: asyncio.Queue = asyncio.Queue()
_last_api_time: float = 0.0


async def gemini_worker(chat_sessions: dict, knowledge_entries: list | None = None) -> None:
    """
    持續從 msg_queue 取出請求並呼叫 Gemini API。
    確保 task_done() 在所有路徑皆被呼叫。
    """
    global _last_api_time

    while True:
        req = await msg_queue.get()
        cid = req['channel_id']
        prompt: str = req['prompt_text']
        file_parts: list[dict] = req.get('file_parts', [])
        reply_fn = req['reply_fn']      # async fn(text) → 回覆原訊息
        send_fn = req['send_fn']        # async fn(text) → 發送至頻道（分段/通知用）
        typing_ctx = req['typing_ctx']  # async context manager（LINE 為 no-op）
        kb_save: dict | None = req.get('kb_save')

        try:
            sess = chat_sessions.get(cid)
            if not sess or sess.get('chat_obj') is None:
                print(f"[WARN] ch={cid} 無對話物件，略過此請求")
                continue

            chat = sess['chat_obj']

            # 限速：距上次呼叫需間隔 API_DELAY 秒
            loop = asyncio.get_running_loop()
            elapsed = loop.time() - _last_api_time
            if elapsed < API_DELAY:
                await asyncio.sleep(API_DELAY - elapsed)

            async with typing_ctx:
                max_attempts = len(GEMINI_API_KEYS)
                for attempt in range(max_attempts):
                    try:
                        # 多模態：有附件時組合 content list；純文字時直接傳 str
                        if file_parts:
                            content = [
                                types.Part(inline_data=types.Blob(
                                    mime_type=fp['mime_type'], data=fp['data']
                                ))
                                for fp in file_parts
                            ]
                            content.append(types.Part(text=prompt))
                        else:
                            content = prompt
                        resp = await asyncio.to_thread(chat.send_message, content)
                        _last_api_time = asyncio.get_running_loop().time()
                        text: str = resp.text or ''
                        if not text:
                            fallback = _reverse_search_fallback(prompt)
                            if fallback:
                                await reply_fn(fallback)
                            else:
                                await reply_fn('喵嗚... 這個問題我沒辦法回答')
                            break

                        if len(text) > 2000:
                            await send_fn("我的回應太長了，我會分段傳送：")
                            for i in range(0, len(text), 1990):
                                await send_fn(text[i:i + 1990])
                        else:
                            await reply_fn(text)

                        # 自動將圖片分析結果儲存至知識庫
                        if kb_save:
                            try:
                                entry = add_entry(
                                    kb_save['entries'],
                                    f"[圖片分析 {kb_save['label']}]: {text[:800]}",
                                    kb_save['saved_by'],
                                )
                                await send_fn(
                                    f"📌 圖片分析已自動儲存至知識庫 `#{entry['id']}`，之後可以直接問我喵！"
                                )
                            except Exception as e:
                                print(f"[KB] 自動儲存圖片分析失敗: {e}")

                        save_history(chat_sessions)
                        break  # 成功，結束重試迴圈

                    except Exception as e:
                        err = str(e).lower()
                        if any(kw in err for kw in ["quota", "rate limit", "429", "resource_exhausted", "toomanyrequests"]):
                            if attempt < max_attempts - 1:
                                print(f"[WARN] quota 觸發 ch={cid} attempt={attempt + 1}/{max_attempts}，輪替 Key...")
                                rotate_api_key()
                                # API 輪替後統整知識庫
                                if knowledge_entries is not None:
                                    consolidate_knowledge(knowledge_entries)
                                # 重建 chat 以綁定新 Client，並還原當前歷史
                                hist = [
                                    {"role": m.role, "parts": [{"text": p.text if p.text else "[附件]"} for p in m.parts]}
                                    for m in chat.get_history()
                                ]
                                personality = sess.get('personality', 'general')
                                chat = create_chat(personality, hist)
                                sess['chat_obj'] = chat
                                continue  # 靜默重試
                            else:
                                print(f"[ERROR] 所有 {max_attempts} 組 Key 均已耗盡 ch={cid}")
                                await reply_fn("所有 API Key 都達到用量限制了喵...請稍後再試！")
                        elif "timeout" in err:
                            print(f"[WARN] API逾時 ch={cid}: {e}")
                            await reply_fn("喵嗚...Gemini API 回應時間太長了，請稍後再試試看喔！")
                        else:
                            print(f"[ERROR] {type(e).__name__}: {e}")
                            await reply_fn("抱歉，我在處理您的請求時遇到了未知的錯誤喵。")
                        break  # 非 quota 錯誤不重試

        finally:
            msg_queue.task_done()
