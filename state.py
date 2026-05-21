"""
共享可變狀態模組。
各 commands 子模組與事件處理器皆從此處 import 全域狀態。
"""

# Discord chat session 字典
# { channel_id (int) -> {
#     'chat_obj': Chat | None,
#     'personality': str | None,
#     'raw_history': list,
#     'current_web_context': str | None,
# }}
chat_sessions: dict = {}

# Worker 啟動旗標（防止重連時重複建立）
_worker_started: bool = False

# 礦工每整點派發 task 啟動旗標（防止重連時重複建立）
_miner_task_started: bool = False

# Persistent View（檢舉 / 管理員處置）註冊旗標
_persistent_views_registered: bool = False

# /fishing 背景任務（贈禮過期）啟動旗標 + active_session 重啟清理旗標
_fishing_task_started: bool = False

# 槓桿（保證金）SL/TP + 強制清算背景任務啟動旗標
_margin_task_started: bool = False
