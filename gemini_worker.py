"""
Gemini API 工作器模組（使用新版 google-genai SDK）。
負責：Client 初始化、API Key 輪替、請求佇列限速處理。
"""
import asyncio
import re
import traceback
from typing import Any

import requests
from google import genai
from google.genai import types

from config import (
    GEMINI_API_KEYS,
    GEMINI_MODEL_NAME,
    PERSONALITY,
    API_DELAY,
    HISTORY_MAX_TURNS,
    LM_STUDIO_BASE_URL,
    LM_STUDIO_MODEL,
    LM_STUDIO_API_KEY,
    LM_STUDIO_MAX_CONTEXT_CHARS,
)
from history import save_history_async
from utils.text_processing import postprocess_response

# --- API Key 輪替 ---
_key_index: int = 0
_client: genai.Client | None = None
_lmstudio_model_cache: str | None = None

_LMSTUDIO_NOTHINK_DIRECTIVE = (
    "【輸出規則】直接輸出最終回應，僅使用繁體中文。"
    "禁止輸出任何思考過程、推理步驟、草稿或自我分析；"
    "禁止使用 <think>/<thinking>/<reasoning> 標籤或 [THINKING] 方括號；"
    "禁止出現 Reaction、Confirmation、Action/Plea、Draft、Refining、"
    "Persona、Context、Constraints、Final Polish 等英文分析標頭。"
    "只回覆人格該說的話本身。\n\n"
)


def _create_client() -> genai.Client | None:
    if not GEMINI_API_KEYS:
        return None
    key = GEMINI_API_KEYS[_key_index % len(GEMINI_API_KEYS)]
    return genai.Client(api_key=key)


def rotate_api_key() -> None:
    """切換至下一組 API Key 並重建 Client。"""
    global _key_index, _client
    if not GEMINI_API_KEYS:
        return
    _key_index += 1
    _client = _create_client()
    print(f"[ROTATE] API Key index={_key_index % len(GEMINI_API_KEYS)}")


# 初始化 Client
_client = _create_client()

# --- 各人格的對話設定 ---
# Lite 模型不支援 google_search grounding tool
_supports_search = 'lite' not in GEMINI_MODEL_NAME.lower()
_tools = [types.Tool(google_search=types.GoogleSearch())] if _supports_search else []

_SAFETY_OFF = [
    types.SafetySetting(category=c, threshold=types.HarmBlockThreshold.OFF)
    for c in (
        types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        types.HarmCategory.HARM_CATEGORY_CIVIC_INTEGRITY,
    )
]

_CHAT_CONFIGS: dict[str, types.GenerateContentConfig] = {
    'general': types.GenerateContentConfig(
        system_instruction=PERSONALITY['general'],
        tools=_tools,
        safety_settings=_SAFETY_OFF,
    ),
    'master': types.GenerateContentConfig(
        system_instruction=PERSONALITY['master'],
        tools=_tools,
        safety_settings=_SAFETY_OFF,
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

    if _client is None:
        raise RuntimeError("Gemini 尚未設定 API Key（GEMINI_API_KEY）。")

    converted = _to_contents(history) if history else []
    return _client.chats.create(
        model=GEMINI_MODEL_NAME,
        config=_CHAT_CONFIGS[personality],
        history=converted,
    )


def _compact_history(chat: genai.chats.Chat) -> list[dict]:
    """
    取出 chat.get_history() 並轉成可序列化、只含文字的格式。
    對於圖片/Blob parts（p.text 為 None）以 "[附件]" 取代，避免把二進位留在記憶體中。
    """
    hist = [
        {"role": m.role, "parts": [{"text": p.text if p.text else "[附件]"} for p in m.parts]}
        for m in chat.get_history()
    ]
    if len(hist) > HISTORY_MAX_TURNS:
        del hist[:-HISTORY_MAX_TURNS]
    return hist


def _should_rebuild_chat(hist: list[dict]) -> bool:
    """
    是否需要把 chat session 整個重建一次。
    只在歷史長度達到上限時重建，避免「曾經上傳過附件」就逐則訊息都重建。
    （附件 blob 已於 _compact_history 被替換成 [附件] 文字，SDK 端亦不再持有。）
    """
    return len(hist) >= HISTORY_MAX_TURNS


from utils.ai_helpers import normalize_provider as _normalize_provider


def _trim_messages_for_lmstudio(messages: list[dict[str, str]],
                                 budget: int) -> list[dict[str, str]]:
    """
    LM Studio 本地模型 context 有限，HISTORY_MAX_TURNS=150 容易爆。
    保留 system（若存在，固定第 0 個）+ 最後一則 user prompt，從最舊歷史對開始刪到 budget 內。
    """
    if not messages:
        return messages
    has_system = messages[0].get("role") == "system"
    sys_msg = messages[:1] if has_system else []
    last_user = messages[-1:] if messages[-1].get("role") == "user" else []
    middle = messages[len(sys_msg):len(messages) - len(last_user)]

    def total_chars(msgs: list[dict[str, str]]) -> int:
        return sum(len(m.get("content", "")) for m in msgs)

    while middle and total_chars(sys_msg + middle + last_user) > budget:
        del middle[0]
    trimmed = sys_msg + middle + last_user
    if len(trimmed) < len(messages):
        print(f"[LMSTUDIO] 歷史過長，裁切 {len(messages) - len(trimmed)} 則 "
              f"(剩 {total_chars(trimmed)} chars / budget {budget})")
    return trimmed


def _raw_history_to_text_messages(raw_history: list[dict]) -> list[dict[str, str]]:
    """
    將 {role, parts:[{text}]} 轉成 OpenAI chat messages 需要的 {role, content}。
    """
    messages: list[dict[str, str]] = []
    for item in raw_history or []:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        parts = item.get("parts") or []
        texts: list[str] = []
        for part in parts:
            if isinstance(part, dict) and part.get("text"):
                texts.append(str(part["text"]))
        if not texts:
            continue
        content = "\n".join(texts)
        if role == "model":
            messages.append({"role": "assistant", "content": content})
        elif role == "user":
            messages.append({"role": "user", "content": content})
    return messages


def _lmstudio_get_model_id() -> str:
    """
    LM_STUDIO_MODEL 有值就直接用；否則嘗試從 /v1/models 取第一個 model id。
    """
    global _lmstudio_model_cache

    if LM_STUDIO_MODEL:
        return LM_STUDIO_MODEL
    if _lmstudio_model_cache:
        return _lmstudio_model_cache

    try:
        resp = requests.get(f"{LM_STUDIO_BASE_URL}/v1/models", timeout=6)
        resp.raise_for_status()
        data: Any = resp.json()
        model_id = None
        if isinstance(data, dict):
            items = data.get("data")
            if isinstance(items, list) and items:
                first = items[0]
                if isinstance(first, dict):
                    model_id = first.get("id")
        if model_id:
            _lmstudio_model_cache = str(model_id)
            return _lmstudio_model_cache
    except Exception as e:
        print(f"[LMSTUDIO] fetch /v1/models failed: {e}")

    _lmstudio_model_cache = "local-model"
    return _lmstudio_model_cache

def _lmstudio_chat_completion(messages: list[dict[str, str]]) -> str:
    url = f"{LM_STUDIO_BASE_URL}/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    if LM_STUDIO_API_KEY:
        headers["Authorization"] = f"Bearer {LM_STUDIO_API_KEY}"

    payload: dict[str, Any] = {
        "model": _lmstudio_get_model_id(),
        "messages": messages,
        "temperature": 0.7,
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=120)
    if resp.status_code >= 400:
        raise RuntimeError(f"LM Studio HTTP {resp.status_code}: {resp.text[:500]}")
    data: Any = resp.json()
    try:
        return str(data["choices"][0]["message"]["content"])
    except Exception as e:
        raise RuntimeError(f"LM Studio 回傳格式異常: {data!r}") from e


# --- 共用回應輔助 ---

async def _deliver_text(text: str, reply_fn, send_fn) -> None:
    """發送回應文字，超過 2000 字自動分段。"""
    if len(text) > 2000:
        await send_fn("我的回應太長了，我會分段傳送：")
        for i in range(0, len(text), 1990):
            await send_fn(text[i:i + 1990])
    else:
        await reply_fn(text)


# --- 請求佇列 ---
msg_queue: asyncio.Queue = asyncio.Queue()
_last_api_time: float = 0.0


async def gemini_worker(chat_sessions: dict) -> None:
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

        try:
            sess = chat_sessions.get(cid)
            if not sess:
                print(f"[WARN] ch={cid} 無 session，略過此請求")
                continue

            provider = _normalize_provider(sess.get("ai_provider"))
            personality = sess.get("personality", "general")

            # 限速：距上次呼叫需間隔 API_DELAY 秒
            loop = asyncio.get_running_loop()
            elapsed = loop.time() - _last_api_time
            if elapsed < API_DELAY:
                await asyncio.sleep(API_DELAY - elapsed)

            async with typing_ctx:
                if provider == "lmstudio":
                    if file_parts:
                        await reply_fn("本地 LM Studio 暫不支援圖片/PDF 附件，請用 `/ai模型 model:線上` 切換到線上模型喵。")
                        continue

                    system = PERSONALITY.get(personality, "")
                    if system:
                        system = _LMSTUDIO_NOTHINK_DIRECTIVE + system
                    messages: list[dict[str, str]] = ([{"role": "system", "content": system}] if system else [])
                    messages += _raw_history_to_text_messages(sess.get("raw_history", []))
                    messages.append({"role": "user", "content": prompt})
                    messages = _trim_messages_for_lmstudio(messages, LM_STUDIO_MAX_CONTEXT_CHARS)

                    try:
                        text = await asyncio.to_thread(_lmstudio_chat_completion, messages)
                        _last_api_time = asyncio.get_running_loop().time()
                    except Exception as e:
                        print(f"[LMSTUDIO] ch={cid} error: {type(e).__name__}: {e}")
                        traceback.print_exc()
                        try:
                            await reply_fn(f"LM Studio 呼叫失敗：{e}")
                        except Exception:
                            pass
                        continue

                    text = postprocess_response(text or "", is_lmstudio=True)
                    if not text:
                        await reply_fn("本地模型沒有回傳內容喵。")
                        continue

                    await _deliver_text(text, reply_fn, send_fn)

                    hist = list(sess.get("raw_history", []) or [])
                    hist.append({"role": "user", "parts": [{"text": prompt}]})
                    hist.append({"role": "model", "parts": [{"text": text}]})
                    if len(hist) > HISTORY_MAX_TURNS:
                        del hist[:-HISTORY_MAX_TURNS]
                    sess["raw_history"] = hist

                    await save_history_async(chat_sessions)
                    continue

                # --- Gemini ---
                if _client is None or not GEMINI_API_KEYS:
                    await reply_fn("Gemini 尚未設定 API Key（GEMINI_API_KEY），無法使用線上模型喵。")
                    continue

                chat = sess.get("chat_obj")
                if chat is None:
                    try:
                        chat = create_chat(personality, sess.get("raw_history", []))
                        sess["chat_obj"] = chat
                    except Exception as e:
                        await reply_fn(f"Gemini 初始化失敗：{e}")
                        continue

                max_attempts = len(GEMINI_API_KEYS)
                for attempt in range(max_attempts):
                    try:
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
                        text: str = postprocess_response(resp.text or '')

                        if not text:
                            await reply_fn('喵嗚... 這個問題我沒辦法回答')
                            break

                        await _deliver_text(text, reply_fn, send_fn)

                        try:
                            compact = _compact_history(chat)
                            sess['raw_history'] = compact
                            if file_parts or _should_rebuild_chat(compact):
                                chat = create_chat(personality, compact)
                                sess['chat_obj'] = chat
                        except Exception as e:
                            print(f"[WARN] compact/rebuild chat 失敗 ch={cid}: {e}")

                        await save_history_async(chat_sessions)
                        break

                    except Exception as e:
                        err = f"{type(e).__name__}: {e}".lower()
                        is_quota = any(kw in err for kw in
                                       ["quota", "rate limit", "429", "resource_exhausted", "toomanyrequests"])
                        is_5xx = any(kw in err for kw in
                                     ["500", "502", "503", "504", "internal error",
                                      "unavailable", "servererror", "service_unavailable",
                                      "deadline_exceeded", "internalservererror"])
                        if is_quota or is_5xx:
                            if attempt < max_attempts - 1:
                                tag = "quota" if is_quota else "5xx"
                                print(f"[WARN] {tag} 觸發 ch={cid} attempt={attempt + 1}/{max_attempts}，輪替 Key...")
                                rotate_api_key()
                                hist = _compact_history(chat)
                                chat = create_chat(personality, hist)
                                sess['chat_obj'] = chat
                                sess['raw_history'] = hist
                                continue
                            else:
                                if is_quota:
                                    print(f"[ERROR] 所有 {max_attempts} 組 Key 均已耗盡 ch={cid}")
                                    await reply_fn("所有 API Key 都達到用量限制了喵...請稍後再試！")
                                else:
                                    print(f"[ERROR] Gemini 5xx 重試 {max_attempts} 次仍失敗 ch={cid}: {e}")
                                    await reply_fn("Gemini 伺服器目前不穩定（5xx），稍後再試喵...")
                        elif "timeout" in err:
                            print(f"[WARN] API逾時 ch={cid}: {e}")
                            await reply_fn("喵嗚...Gemini API 回應時間太長了，請稍後再試試看喔！")
                        else:
                            print(f"[ERROR] {type(e).__name__}: {e}")
                            await reply_fn("抱歉，我在處理您的請求時遇到了未知的錯誤喵。")
                        break

        except Exception as e:
            print(f"[ERROR] gemini_worker 未預期錯誤 ch={cid}: {type(e).__name__}: {e}")
            try:
                await reply_fn("抱歉，小龍喵處理訊息時發生意外錯誤，請稍後再試喵...")
            except Exception:
                pass
        finally:
            msg_queue.task_done()
