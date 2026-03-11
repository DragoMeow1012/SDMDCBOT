"""
知識庫模組：管理跨頻道永久儲存的知識條目。
儲存於 data/knowledge.json。

指令（在 main.py 中處理）：
  !kb 儲存 <內容>         - 儲存一條知識（任何人）
  !kb 列表               - 列出全部條目（主人限定）
  !kb 刪除 <id>          - 刪除指定條目（主人限定）
  !kb 查詢 <關鍵字>       - 搜尋相關條目（任何人）
"""
import json
import time
import os

from config import DATA_DIR

KNOWLEDGE_FILE = os.path.join(DATA_DIR, "knowledge.json")


def _ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def load_knowledge() -> list[dict]:
    """從檔案載入知識庫，回傳條目列表。"""
    _ensure_data_dir()
    if not os.path.exists(KNOWLEDGE_FILE):
        return []
    try:
        with open(KNOWLEDGE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        print(f"[KB] 已載入 {len(data)} 條知識條目")
        return data
    except Exception as e:
        print(f"[KB] 載入失敗: {e}")
        return []


def save_knowledge(entries: list[dict]) -> None:
    """將知識庫寫回檔案。"""
    _ensure_data_dir()
    try:
        with open(KNOWLEDGE_FILE, 'w', encoding='utf-8') as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[KB] 存檔失敗: {e}")


def add_entry(entries: list[dict], content: str, saved_by: int) -> dict:
    """新增一條知識條目，回傳新條目。"""
    next_id = max((e["id"] for e in entries), default=0) + 1
    entry = {
        "id": next_id,
        "content": content,
        "saved_by": str(saved_by),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    entries.append(entry)
    save_knowledge(entries)
    return entry


def remove_entry(entries: list[dict], entry_id: int) -> bool:
    """刪除指定 ID 條目，成功回傳 True。"""
    before = len(entries)
    entries[:] = [e for e in entries if e["id"] != entry_id]
    if len(entries) < before:
        save_knowledge(entries)
        return True
    return False


def search_entries(entries: list[dict], keyword: str) -> list[dict]:
    """搜尋內容包含關鍵字的條目（不區分大小寫）。"""
    kw = keyword.lower()
    return [e for e in entries if kw in e["content"].lower()]


def consolidate_knowledge(entries: list[dict]) -> dict | None:
    """
    將所有條目合併為單一統整條目，in-place 更新 entries 並存檔。
    - 0 筆：回傳 None
    - 1 筆：不動，回傳該條目
    - 2+ 筆：合併所有 content，清空後插入新統整條目
    """
    if len(entries) <= 1:
        return entries[0] if entries else None
    original_count = len(entries)
    combined = "\n---\n".join(
        f"[{e['timestamp']}] {e['content']}" for e in entries
    )
    entries.clear()
    entry = {
        "id": 1,
        "content": f"[統整記憶·{time.strftime('%Y-%m-%d %H:%M:%S')}]\n{combined}",
        "saved_by": "system",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    entries.append(entry)
    save_knowledge(entries)
    print(f"[KB] 已統整為 1 筆（原 {original_count} 筆）")
    return entry


def list_sections(entries: list[dict]) -> list[str]:
    """
    將統整條目解析為各節清單（依 --- 分割）。
    非統整格式或一般條目則整筆視為第 1 節。
    """
    if not entries:
        return []
    content = entries[0]['content']
    if content.startswith('[統整記憶·'):
        _, _, body = content.partition('\n')
        sections = [s.strip() for s in body.split('---') if s.strip()]
    else:
        sections = [content.strip()]
    return sections


def remove_section(entries: list[dict], section_idx: int) -> bool:
    """
    刪除統整條目中的第 section_idx 節（1-based）。
    成功回傳 True；索引超出範圍回傳 False。
    刪到最後一節時清空整個 entries。
    """
    sections = list_sections(entries)
    if not sections or section_idx < 1 or section_idx > len(sections):
        return False
    sections.pop(section_idx - 1)
    if not sections:
        entries.clear()
    else:
        combined = '\n---\n'.join(sections)
        entries[0]['content'] = (
            f"[統整記憶·{time.strftime('%Y-%m-%d %H:%M:%S')}]\n{combined}"
        )
        entries[0]['timestamp'] = time.strftime('%Y-%m-%d %H:%M:%S')
    save_knowledge(entries)
    return True


def build_knowledge_context(entries: list[dict]) -> str:
    """格式化知識庫為模型注入字串，空庫時回傳空字串。"""
    if not entries:
        return ""
    lines = ["【知識庫·重要記憶】以下為人工儲存的重要資訊，對話時需參考："]
    for e in entries:
        lines.append(f"  #{e['id']} [{e['timestamp']}]: {e['content']}")
    return "\n".join(lines) + "\n"
