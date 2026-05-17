"""
對話摘要模組：將聊天歷史序列化為可讀 TXT，供模型跨 session 讀取。

TXT 路徑：data/summaries/{channel_id}.txt
每次儲存只保留最近 MAX_LINES 條對話（user + model 各算一條）。
"""
import os
import tempfile
import time
from config import DATA_DIR

SUMMARIES_DIR = os.path.join(DATA_DIR, "summaries")
MAX_LINES = 50    # user + model 各一條 = 一輪算兩條
MAX_CHARS = 1000


_ROLE_LABEL = {'user': '[User]', 'model': '[Bot]'}


def _ensure_dir() -> None:
    os.makedirs(SUMMARIES_DIR, exist_ok=True)


def _hist_to_lines(hist: list[dict]) -> list[str]:
    """每條訊息取第一個 text part 的前 300 字，無文字 part 則標記 [附件]。"""
    lines = []
    for msg in hist:
        role = msg.get('role', 'user')
        label = _ROLE_LABEL.get(role, f'[{role}]')
        parts = msg.get('parts', [])
        text = next(
            (p.get('text', '') for p in parts if p.get('text')),
            '[附件]'
        )
        text = text[:300].replace('\n', ' ')
        lines.append(f'{label} {text}')
    return lines


def _atomic_write_text(path: str, content: str) -> None:
    """tmp + os.replace：避免中斷時留下半寫入的壞檔。"""
    dir_path = os.path.dirname(path) or '.'
    fd, tmp_path = tempfile.mkstemp(
        prefix='.' + os.path.basename(path) + '.', suffix='.tmp', dir=dir_path,
    )
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def save_summary(channel_id: int | str, hist: list[dict]) -> None:
    """
    將 hist 序列化為 TXT 原子寫入。只保留最後 MAX_LINES 條且總字數 ≤ MAX_CHARS。
    """
    _ensure_dir()
    lines = _hist_to_lines(hist)[-MAX_LINES:]

    # 從尾端累計長度，反向找到可保留的起點，避免 O(n²) 的 pop(0) 迴圈
    if lines:
        total = 0
        start = len(lines)
        for i in range(len(lines) - 1, -1, -1):
            total += len(lines[i])
            if total > MAX_CHARS:
                start = i + 1
                break
            start = i
        lines = lines[start:]

    path = os.path.join(SUMMARIES_DIR, f"{channel_id}.txt")
    content = (
        f"=== 頻道 {channel_id} 對話記錄 ===\n"
        f"最後更新：{time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        + '\n'.join(lines)
    )
    try:
        _atomic_write_text(path, content)
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
