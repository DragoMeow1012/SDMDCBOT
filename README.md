# 小龍喵 Discord Bot

基於 Gemini AI 的 Discord / LINE 聊天機器人，支援雙人格、本地 / 雲端 AI 雙 provider、漫畫翻譯、網頁抓取、圖片反搜、對話記憶跨重啟保留，以及大規模 Pixiv 圖片爬蟲。

---

## 功能

- **AI 對話**：Gemini 2.5 Flash 線上 + LM Studio 本地雙 provider，`/ai模型` 隨時切換；@提及即可對話
- **雙人格模式**：主人（龍龍喵）與一般訪客使用不同人格設定（[config.py](config.py) `PERSONALITY`）
- **對話記憶**：歷史儲存於 `data/chat_history.json`，重啟自動載入
- **對話摘要**：自動序列化為 TXT（`data/summaries/`），供模型跨 session 參考
- **網頁抓取**：訊息附上 URL 自動抓取並摘要或回答問題
- **圖片反搜**：`/以圖搜圖` 或附圖關鍵字觸發反向圖片搜尋（SauceNAO + soutubot）
- **漫畫翻譯**：`/translate-img` — 圖片或整本壓縮檔（zip/cbz/rar/cbr），背後接自家 fork 的 [manga-image-translator](../manga-image-translator) HTTP server
- **名言佳句**：右鍵訊息生成 1920×1080 精美引言圖（Pillow，左頭像漸層 + 右文字）
- **LINE 整合**：LINE Bot Webhook，LINE 聊天也能與 Gemini 對話（`@小龍喵` 觸發）
- **API Key 輪替**：支援最多 7 組 Gemini API Key（`GEMINI_API_KEY` 至 `GEMINI_API_KEY6`），429 / 5xx 自動切換
- **Pixiv 爬蟲**：全站 tag／ranking／作者擴散爬取，pHash + FAISS 二值索引去重，支援指定作者優先佇列

---

## 專案結構

```
DCbot_1.0/
├── main.py                    # 入口：Discord 事件、session 管理、URL/附件處理
├── state.py                   # 共享可變狀態（chat_sessions / nicknames / knowledge_entries）
├── config.py                  # 環境變數、PERSONALITY 人格設定、常數
├── gemini_worker.py           # Gemini API Worker、模型初始化、Key 輪替
├── ai_session.py              # AI 供應商抽象層（Gemini / LM Studio）
├── history.py                 # 本地聊天歷史讀寫；atomic write + save_history_async
├── summary.py                 # 對話摘要序列化（data/summaries/）
├── web.py                     # 非同步網頁抓取（aiohttp + BeautifulSoup）
├── line_bot.py                # LINE Bot Webhook 伺服器
├── reverse_search.py          # 圖片反向搜尋（SauceNAO + soutubot）
├── quote_image.py             # 名言佳句圖片生成（Pillow，1920×1080）
├── graph_render.py            # 本群關係圖網絡渲染（matplotlib + networkx）
├── manga_translate.py         # 漫畫翻譯 HTTP client（接 manga-image-translator server）
├── manga_translator_server.py # manga-image-translator server 子進程管理
├── logger.py                  # 統一 logging 設定
├── pixiv_crawler/             # Pixiv 全站非同步爬蟲套件（asyncio + aiohttp + AppPixivAPI）
├── pixiv_feature.py           # pHash 特徵提取 + FAISS 二值索引管理
├── pixiv_database.py          # Pixiv SQLite 資料庫操作（pixiv.db）
├── pixiv_config.py            # Pixiv 模組設定（路徑、tag 列表、爬取參數）
├── pixiv_status_app.py        # Streamlit 爬取狀態監控頁面（port 8766）
├── utils/                     # 共用工具
│   ├── json_store.py          # load_json / save_json（原子寫入） / save_json_async
│   ├── discord_helpers.py     # Discord 成員查詢、權限判斷
│   ├── ai_helpers.py          # AI 回呼、錯誤訊息格式化
│   └── text_processing.py     # 預編譯正則、文字清理
├── commands/                  # 斜線指令套件（多數已合併為單指令 + 選項參數）
│   ├── __init__.py            # setup_all(tree) 統一注冊
│   ├── admin.py               # /清除記憶
│   ├── ai.py                  # /ai模型 — 切換 Gemini 雲端 / LM Studio 本地
│   ├── translate.py           # /translate-img — 翻譯圖片或壓縮檔（zip/cbz/rar/cbr）
│   ├── image_search.py        # /以圖搜圖
│   ├── nhentai.py             # /random-nhentai — NSFW 頻道專用
│   ├── daily_mom.py           # /抽今日媽媽
│   ├── pixiv.py               # /pixiv 選項=[爬蟲|狀態|停止]
│   ├── relationship.py        # /relationship 選項=[認養寵物|認主人|放生寵物|本群關係圖|
│   │                          #   認媽媽|拋棄兒子|和今日媽媽斷絕關係|電子皮鞭|解除調教|炮決蘿莉控]
│   ├── tool.py                # /tool 選項=[電子口球|口球輪盤|電子氣泡紙|電子木魚|
│   │                          #   賽博體重計|擲硬幣|擲硬幣幹話版|roll|丟骰子|分隊伍|賽博釣群友]
│   ├── rank.py                # /rank 選項=[功德|炮決|調教|清除]
│   └── quote.py               # 右鍵選單：「名言佳句」、「Make it Quote」
├── data/                      # 全部 runtime 狀態檔（自動生成；JSON 採 atomic write）
│   ├── chat_history.json      # 各頻道對話歷史
│   ├── summaries/             # 各頻道對話摘要 TXT（給跨 session 注入）
│   ├── merit.json             # 電子木魚功德
│   ├── artillery_records.json # 炮決次數
│   ├── whip_records.json      # 調教（電子皮鞭）次數
│   ├── whip_relations.json    # 調教關係（trainer → trainee）
│   ├── relationships.json     # 主寵關係
│   ├── wife_records.json      # 今日媽媽（跨日自動清除）
│   ├── picture/
│   │   └── artillerylolicon.jpg
│   └── logs/
│       ├── bot_YYYY-MM-DD.log         # bot 主 log（依日期切檔）
│       └── manga_translator_server.log  # manga-image-translator 子進程 log（>50MB 滾 .old）
├── pixivdata/                 # Pixiv 爬蟲資料根目錄（自動生成）
│   ├── images/                # 下載的圖片（按 illust_id 子目錄）
│   ├── data/
│   │   ├── pixiv.db           # SQLite 作品資料庫
│   │   ├── feature.index      # FAISS 二值索引（pHash）
│   │   ├── feature.index.ids.npy          # 索引 ID 映射（encoded illust_id + page）
│   │   ├── tag_crawl_progress.json        # tag 爬取斷點記錄
│   │   ├── user_id_scan_cursor.json       # 作者 ID 掃描游標
│   │   └── status.json                    # 爬取狀態（供 Streamlit 讀取）
│   ├── logs/
│   │   ├── spider.log         # 爬蟲主 log
│   │   └── pixiv_query.log    # 指令操作 log
│   └── pagedata/
│       ├── page_log.jsonl     # 每輪相變診斷日誌
│       └── timeout_log.jsonl  # 超時事件日誌
├── .env                       # 金鑰設定（不提交 git）
├── .env.example               # 金鑰範本
├── requirements.txt           # Python 依賴套件
├── Dockerfile
└── docker-compose.yml
```

---

## 安裝與設定

### 1. 安裝 Python 3.12+

```bash
winget install Python.Python.3.12
```

### 2. 安裝依賴套件

```bash
pip install -r requirements.txt
playwright install chromium
```

### 3. 設定金鑰

```bash
copy .env.example .env
```

編輯 `.env`：

```env
# === Discord（必填）===
DISCORD_BOT_TOKEN=你的_Discord_Bot_Token

# === Gemini API Keys（最多 7 組輪替；雲端 AI 必填至少 1 組）===
GEMINI_API_KEY=你的_主要_Gemini_API_Key
GEMINI_API_KEY1=備用_Key_1
GEMINI_API_KEY2=備用_Key_2
# ... 最多到 GEMINI_API_KEY6

# === AI provider（可選；預設 gemini）===
AI_PROVIDER_DEFAULT=gemini             # gemini 或 lmstudio
LM_STUDIO_BASE_URL=http://127.0.0.1:1234
LM_STUDIO_MODEL=                       # 留空 = 自動抓 /v1/models 第一個
LM_STUDIO_API_KEY=                     # LM Studio 預設不檢查
LM_STUDIO_MAX_CONTEXT_CHARS=12000

# === 漫畫翻譯（manga-image-translator 後端，可選）===
MANGA_TRANSLATOR_AUTOSTART=1           # 1=bot 啟動時 spawn server 子進程
MANGA_TRANSLATOR_DIR=d:\VScode\manga-image-translator
MANGA_TRANSLATOR_PYTHON=d:\VScode\manga-image-translator\.venv\Scripts\python.exe
MANGA_TRANSLATOR_URL=http://127.0.0.1:8001
MANGA_TRANSLATOR_USE_GPU=1
MANGA_TRANSLATOR_NUM_WORKERS=2         # 每個 worker 各佔一份 VRAM
MANGA_TRANSLATOR_CONCURRENCY=10        # bot 端 in-flight 上限 = N×K
MANGA_TRANSLATOR_USE_LOCAL=0           # 0=Gemini 雲端、1=本地 LM Studio
MANGA_TRANSLATOR_GEMINI_MODEL=gemini-3-flash-preview
MANGA_TRANSLATOR_OPENAI_MODEL=qwen2.5-vl-7b-instruct
MANGA_TRANSLATOR_FONT=Arial-Unicode-Regular.ttf

# === Pixiv 爬蟲（可選；不填則 /pixiv 指令會回未設定錯誤）===
PIXIV_REFRESH_TOKEN=你的_主要_token
PIXIV_REFRESH_TOKEN1=備用_token_1      # 多組會分配給 main / scan / diffusion workers
PIXIV_WEB_COOKIE=你的_Pixiv_Web_Cookie  # 可選，提升搜尋配額
NGROK_AUTH_TOKEN=                      # 可選，公開狀態頁
NGROK_DOMAIN=                          # 可選，固定 ngrok 子網域

# === SauceNAO（可選；提升 200 次/天配額）===
SAUCENAO_API_KEY=

# === imsearch 本機搜圖伺服器（可選）===
IMSEARCH_URL=http://127.0.0.1:8000

# === LINE Bot（可選）===
LINE_CHANNEL_ACCESS_TOKEN=你的_LINE_Channel_Access_Token
LINE_CHANNEL_SECRET=你的_LINE_Channel_Secret
LINE_WEBHOOK_PORT=8080
```

- Discord Token：[Discord Developer Portal](https://discord.com/developers/applications)
- Gemini API Key：[Google AI Studio](https://aistudio.google.com/apikey)
- Pixiv Refresh Token：使用 [pixivpy3](https://github.com/upbit/pixivpy) 的 `refresh_token` 取得方式取得
- LINE Token：[LINE Developers Console](https://developers.line.biz/)
- SauceNAO Key：[SauceNAO 註冊頁](https://saucenao.com/user.php)

---

## 啟動

### 前景執行（開發用）

```bash
python main.py
```

### 背景執行（持續運行）

```bash
python main.py > data/bot.log 2>&1 &
```

啟動後會顯示 PID，記下備用：

```
[1] 1234
```

查看 log：

```bash
tail -20 data/bot.log
```

查詢 Bot PID：

```bash
ps aux | grep "main.py" | grep -v grep
```

關閉 Bot：

```bash
kill <PID>
# 或全部關閉
pkill -f "main.py"
```

重啟 Bot：

```bash
kill <PID>; sleep 1 && python main.py > data/bot.log 2>&1 &
```

### Docker

```bash
docker compose up -d
```

---

## 使用方式

在 Discord 頻道中 **@小龍喵** 即可開始對話。

| 操作 | 說明 |
|------|------|
| `@小龍喵 你好` | 一般對話 |
| `@小龍喵 https://example.com` | 抓取網頁並摘要 |
| `@小龍喵 https://example.com 這篇說什麼？` | 抓取後依問題回答 |
| 附圖 + `@小龍喵` | 圖片分析（多模態） |
| 附圖 + `@小龍喵 來源？` | 自動觸發反向圖片搜尋 |

---

## 斜線指令

### AI 與記憶

| 指令 | 說明 | 權限 |
|------|------|------|
| `/ai模型 model:[線上\|本地]` | 切換本頻道的 AI provider；本地走 LM Studio、線上走 Gemini | 所有人 |
| `/清除記憶` | 清除本頻道聊天記憶（被安全過濾卡住時用） | 主人限定 |

### 翻譯與圖像

| 指令 | 說明 | 權限 |
|------|------|------|
| `/translate-img 圖片1..10` | 翻譯圖片附件（最多 10 張），支援多語言 | 所有人 |
| `/translate-img 壓縮檔` | 翻譯整本漫畫 zip / cbz / rar / cbr，回傳譯後 zip；超過 Discord 上限自動上傳 litterbox（72h 暫存） | 所有人 |
| `/以圖搜圖 圖片` | 用截圖找來源（pixiv / twitter / x / nh） | 所有人 |
| 右鍵 → `名言佳句` / `Make it Quote` | 將訊息製成 1920×1080 名言圖 | 所有人 |
| `/random-nhentai [tag] [限定中文]` | 從 nhentai 隨機抽一本，可選 tag、可只抽中文版 | NSFW 頻道 |

### Pixiv 爬蟲（`/pixiv 選項`）

| 選項 | 說明 | 權限 |
|------|------|------|
| `爬蟲` | 開始背景爬取（不填 author_id = 全站；填 = 該作者優先） | 主人限定 |
| `狀態` | 查看作品統計、本輪進度，附 Streamlit 即時狀態頁 URL | 所有人 |
| `停止` | 優雅停止當前批次 | 主人限定 |

### 工具與娛樂（`/tool 選項`）

| 選項 | 說明 | 權限 |
|------|------|------|
| `電子口球` | 對成員套用 Timeout 禁言（秒數 1~2419200） | 主人直接執行；對他人需確認 |
| `口球輪盤` | 1 分鐘報名，隨機抽一人禁言 30 秒 | 所有人 |
| `電子氣泡紙` | 5×2 / 10×5 / 自訂（最大 50×50）可點擊氣泡紙 | 所有人 |
| `電子木魚` | 敲木魚積功德按鈕 | 所有人 |
| `賽博體重計` | 量測賽博體重 | 所有人 |
| `擲硬幣` / `擲硬幣幹話版` | 擲一枚硬幣（幹話版有奇妙旅程 1~10 句） | 所有人 |
| `roll` | 抽籤；本頻道在線成員隨機一個 | 所有人 |
| `丟骰子` | 1~6 隨機 | 所有人 |
| `分隊伍 隊伍數量` | 把語音頻道內的成員隨機分隊（2~20 隊） | 所有人 |
| `賽博釣群友` | 放出釣魚按鈕，咬鉤者會被偽裝發言（webhook） | 所有人 |

### 關係互動（`/relationship 選項`）

| 選項 | 說明 | 權限 |
|------|------|------|
| `認養寵物` 用戶 | 邀請對方成為你的寵物 | 所有人 |
| `認主人` 用戶 | 邀請對方成為你的主人 | 所有人 |
| `放生寵物` 用戶 | 解除主寵關係 | 所有人 |
| `本群關係圖` | 視覺化本伺服器主寵 + 母子 + 調教關係網（matplotlib + networkx） | 所有人 |
| `認媽媽` 用戶 | 強制指定一位成員作為你的媽媽 | 所有人 |
| `拋棄兒子` 用戶 | 解除指定用戶認你為媽媽的關係 | 所有人 |
| `和今日媽媽斷絕關係` | 與今日抽到的媽媽解除關係 | 所有人 |
| `電子皮鞭` 用戶 [用戶b] | 鞭打對方；填 用戶b 則 用戶=調教者、用戶b=被調教者 | 所有人 |
| `解除調教` 用戶 | 解除調教關係 | 所有人 |
| `炮決蘿莉控` [用戶] | 隨機或指定炮決，記錄次數 | 所有人 |

`/抽今日媽媽` 為獨立指令：每人每日隨機抽一位成員作為今日媽媽。

### 排行榜（`/rank 選項`）

| 選項 | 說明 | 權限 |
|------|------|------|
| `功德` | 電子木魚功德 TOP 10 | 所有人 |
| `炮決` | 被炮決次數 TOP 10 | 所有人 |
| `調教` | 被調教次數 TOP 10 | 所有人 |
| `清除` 清除類型=[功德\|炮決\|調教] | 清除本伺服器指定排行榜 | 主人限定 |

---

## 設定說明

### Discord Bot（[config.py](config.py)）

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `MASTER_ID` | `404111257008865280` | 主人的 Discord 用戶 ID |
| `GEMINI_MODEL_NAME` | `models/gemma-4-31b-it` | 使用的 Gemini 模型（hard-coded） |
| `API_DELAY` | `5.0` 秒 | 每次 API 請求最短間隔 |
| `HISTORY_MAX_TURNS` | `150` | 每頻道保留的最大歷史訊息筆數 |
| `LM_STUDIO_MAX_CONTEXT_CHARS` | `12000` | LM Studio chat messages 字元上限（system + history 合計），超過從最舊歷史開始裁 |

### Pixiv 爬蟲（pixiv_config.py）

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `DOWNLOAD_WORKERS` | `6` | 並行下載 worker 數 |
| `DOWNLOAD_RATE_LIMIT_Mbps` | `50` | 下載頻寬上限（Mbps） |
| `TAG_PAGES_PER_VISIT` | `200` | 每個 tag/sort 每輪最多抓的頁數 |
| `USER_SCAN_BATCH_SIZE` | `100` | 每次 user_scan 掃描的有效用戶數 |
| `MIN_BOOKMARKS` | `0` | 最低收藏數過濾（0 = 不過濾） |
| `MAX_GALLERY_PAGES` | `100` | 每件漫畫作品最多索引的頁數 |
| `STATUS_WEB_PORT` | `8766` | Streamlit 狀態頁面 port |
| `ALL_TAGS` | （見 pixiv_config.py） | 爬取的 tag 列表 |

---

## Pixiv 爬蟲說明

### 爬取流程

1. **Tag 爬取**：依序爬取 `ALL_TAGS` 中每個 tag 的 `date_desc` / `date_asc` 兩個排序方向，每個 tag 最多 `TAG_PAGES_PER_VISIT` 頁
2. **Ranking 爬取**：每日執行一次，爬取 `day / week / month / day_male / day_female / week_original / week_rookie` 七種排行榜
3. **作者擴散**：新下載作品自動將其作者推入擴散佇列，爬取該作者全部作品
4. **相關作品擴散**：新作品的相關作品也會被採樣爬取，進一步擴展覆蓋率
5. **User ID 掃描**：從 user_id=1 起順序掃描，發現新作者自動爬取

### 去重機制

- **FAISS pHash 索引**：每張圖片計算 64-bit pHash，存入 FAISS 二值索引，Hamming 距離去重
- **SQLite 資料庫**：記錄每件作品的下載狀態與索引狀態，批次查詢已索引作品避免重複下載
- **斷點續爬**：tag 爬取進度存於 `tag_crawl_progress.json`，重啟後從斷點繼續

### 取得 Pixiv Refresh Token

```bash
pip install pixivpy3
python -c "
from pixivpy3 import AppPixivAPI
api = AppPixivAPI()
# 使用瀏覽器登入 Pixiv，從開發者工具取得 code
# 詳見 https://github.com/upbit/pixivpy/issues/158
"
```

---

## 效能與可靠性設計

- **Atomic write**：所有 JSON / TXT 狀態檔（`chat_history.json`、`summaries/*.txt`、`merit.json`、`relationships.json`、`wife_records.json`、`whip_records.json`、`whip_relations.json`、`artillery_records.json`、`tag_crawl_progress.json` …）都採用「tmp 檔 + `os.replace`」寫入，確保程式中斷或斷電時不會留下半寫入的壞檔。
- **非同步存檔**：熱路徑（AI 回覆、訊息送出、爬蟲入庫）使用 `save_json_async` / `save_history_async`，把寫檔丟給 thread pool，不會阻塞 Discord `event loop`。
- **原生 aiohttp 抓取**：`web.py`、`graph_render.py` 的頭像、`commands/wife.py` 的頭像皆改用 `aiohttp` / `discord.Asset.read()`，取代會 block event loop 的 `requests.get`。
- **預編譯正則**：`main.py` 的 URL/指令/提及偵測、`utils/text_processing.py` 等熱路徑全部採用 module-level `re.compile`。
- **Gemini Chat 會話重建**：僅在歷史達到 `HISTORY_MAX_TURNS` 時重建，避免「曾經有附件就每一輪重建」的 N² 成本。
- **O(n) 摘要裁剪**：`summary.py` 以反向累計長度找起點，不用 `pop(0)` 的 O(n²) 迴圈。
- **多 Key 輪替**：`gemini_worker.py` 在配額或 5xx 錯誤時自動切換下一組 Gemini API Key，單一 Key 被封鎖仍可持續服務。

---

## 注意事項

- `.env` 包含敏感金鑰，請勿提交至 git（已加入 `.gitignore`）
- `data/chat_history.json` 儲存對話記錄，請定期備份
- Bot 需在 Discord Developer Portal 開啟 **Message Content Intent**
- LINE Bot 需設定 Webhook URL 為 `https://<your-domain>:8080/webhook`
- 圖片字型依賴 Windows 字型（`NotoSansTC-VF.ttf` / `msjhbd.ttc`），Linux 需另行安裝
- Pixiv 爬蟲需設定 `PIXIV_REFRESH_TOKEN`，否則 `/pixiv爬蟲` 會回傳未設定錯誤
- `pixivdata/` 目錄體積會隨爬取量持續增長（數萬張圖片可達數十 GB），請確認磁碟空間充足
- Streamlit 狀態頁面在 `/pixiv狀態` 指令時自動啟動（port 8766），也可手動執行 `streamlit run pixiv_status_app.py`
- 設定 `NGROK_AUTH_TOKEN` 後，狀態頁面可透過 ngrok 公開存取
