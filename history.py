"""
本地聊天歷史持久化模組。
每次 Bot 啟動時從 data/chat_history.json 載入，
每次回覆後儲存至相同檔案。
"""
import os
import json
from config import DATA_DIR, HISTORY_FILE, HISTORY_MAX_TURNS
from summary import save_summary


def _ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def load_history() -> dict:
    """
    從本地檔案載入對話歷史。
    回傳 dict：{ channel_id (int) -> session_dict }

    session_dict 結構：
        chat_obj          : None  (尚未初始化的 Gemini ChatSession)
        model             : None
        raw_history       : list  (從檔案讀取的歷史紀錄)
        current_web_context: str | None
    """
    _ensure_data_dir()

    if not os.path.exists(HISTORY_FILE):
        print("ℹ️ 無歷史檔，建立空檔")
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump({}, f)
        return {}

    try:
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)

        sessions: dict = {}
        for cid_str, sess_data in data.items():
            # Discord 頻道 ID 為純數字；LINE 使用 'line_...' 字串 key
            try:
                cid = int(cid_str)
            except ValueError:
                cid = cid_str
            sessions[cid] = {
                'chat_obj': None,
                'model': None,
                'raw_history': sess_data.get('raw_history', []),
                'current_web_context': sess_data.get('current_web_context'),
            }

        print(f"✅ 歷史已載: {HISTORY_FILE} ({len(sessions)} 個頻道)")
        return sessions

    except json.JSONDecodeError as e:
        print(f"❌ 歷史檔格式錯誤，重置: {e}")
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump({}, f)
        return {}
    except Exception as e:
        print(f"❌ 載入歷史失敗: {e}")
        return {}


def save_history(chat_sessions: dict) -> None:
    """
    將對話歷史儲存至本地檔案。
    優先從 chat_obj.history 取得最新內容，否則使用 raw_history。
    """
    _ensure_data_dir()

    try:
        data: dict = {}
        for cid, sess in chat_sessions.items():
            chat_obj = sess.get('chat_obj')
            if chat_obj is not None and hasattr(chat_obj, 'get_history'):
                # 新 SDK 使用 get_history() 方法（無 .history 屬性）
                # image/blob parts 的 p.text 為 None，以 "[附件]" 替代
                hist = [
                    {"role": m.role, "parts": [{"text": p.text if p.text else "[附件]"} for p in m.parts]}
                    for m in chat_obj.get_history()
                ]
            else:
                hist = sess.get('raw_history', [])

            # 超出上限時原位刪除最舊的紀錄
            if len(hist) > HISTORY_MAX_TURNS:
                del hist[:-HISTORY_MAX_TURNS]

            data[str(cid)] = {
                'raw_history': hist,
                'current_web_context': sess.get('current_web_context'),
            }

        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        # 同步更新各頻道的可讀 TXT 摘要（LINE string key 直接傳入）
        for cid_str, sess_data in data.items():
            try:
                cid_key = int(cid_str)
            except ValueError:
                cid_key = cid_str
            save_summary(cid_key, sess_data.get('raw_history', []))

        print(f"✅ 歷史已存: {HISTORY_FILE}")

    except Exception as e:
        print(f"❌ 存檔失敗: {e}")
