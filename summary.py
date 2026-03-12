"""
對話摘要模組：將聊天歷史序列化為可讀 TXT，供模型跨 session 讀取。

TXT 路徑：data/summaries/{channel_id}.txt
每次儲存只保留最近 MAX_LINES 條對話（user + model 各算一條）。
"""
import os
import time
from config import DATA_DIR

SUMMARIES_DIR = os.path.join(DATA_DIR, "summaries")
MAX_LINES = 1000  # 保留最近幾條訊息（user + model 各一條 = 一輪算兩條）

_ROLE_LABEL = {'user': '[User]', 'model': '[Bot]'}


def _ensure_dir() -> None:
    os.makedirs(SUMMARIES_DIR, exist_ok=True)


def _hist_to_lines(hist: list[dict]) -> list[str]:
    """
    將 raw_history 轉換為可讀文字行。
    每條訊息取第一個 text part 的前 300 字。
    """
    lines = []
    for msg in hist:
        role = msg.get('role', 'user')
        label = _ROLE_LABEL.get(role, f'[{role}]')
        parts = msg.get('parts', [])
        text = next(
            (p.get('text', '') for p in parts if p.get('text')),
            '[附件]'
        )
        # 截斷過長的訊息
        text = text[:300].replace('\n', ' ')
        lines.append(f'{label} {text}')
    return lines


def save_summary(channel_id: int | str, hist: list[dict]) -> None:
    """
    將 hist（raw_history 格式）序列化為 TXT 並儲存。
    只保留最後 MAX_LINES 條。
    """
    _ensure_dir()
    lines = _hist_to_lines(hist)
    # 只保留最後 MAX_LINES 條
    lines = lines[-MAX_LINES:]

    path = os.path.join(SUMMARIES_DIR, f"{channel_id}.txt")
    header = (
        f"=== 頻道 {channel_id} 對話記錄 ===\n"
        f"最後更新：{time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    )
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(header)
            f.write('\n'.join(lines))
        print(f"[SUMMARY] 已儲存 ch={channel_id} ({len(lines)} 條)")
    except Exception as e:
        print(f"[SUMMARY] 儲存失敗 ch={channel_id}: {e}")


def load_summary(channel_id: int | str) -> str | None:
    """
    讀取頻道對話摘要 TXT，回傳字串；檔案不存在則回傳 None。
    """
    _ensure_dir()
    path = os.path.join(SUMMARIES_DIR, f"{channel_id}.txt")
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
        return content if content else None
    except Exception as e:
        print(f"[SUMMARY] 讀取失敗 ch={channel_id}: {e}")
        return None
