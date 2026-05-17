# AGENTS.md

Project-level guide for AI coding agents (Claude Code / Codex CLI / Copilot agents) working
on this repo. Load this before you start reading source files — it captures the invariants
that aren't obvious from the code.

---

## 1. What this project is

**DCbot_1.0** — a multi-personality Discord bot with:

- Gemini / LM Studio dual-provider chat
- LINE Bot webhook
- Image reverse search (SauceNAO + soutubot via Playwright)
- Cross-restart chat memory (per-channel history + summary TXT)
- Large-scale Pixiv crawler (FAISS pHash dedup, SQLite metadata, asyncio)
- A collection of slash-command "社交 / 娛樂" mini-features

The bot is deployed as a single long-running Python process. It is **not a web service**;
there is no request/response contract beyond Discord interactions and the LINE webhook.

---

## 2. Runtime and layout

- **Python 3.12+** (Docker image uses `python:3.12-slim`; `pyproject` pins >= 3.12).
- Entry point: [main.py](main.py) (`python main.py`).
- Discord event loop runs the whole process. Everything blocking MUST be offloaded.

Module map: see the [專案結構](README.md) tree in README.md.

---

## 3. Hot-path invariants — DO NOT violate

These are the rules the codebase has been optimized around. Breaking them causes visible
Discord lag, data corruption, or rate-limiting.

1. **Never block the Discord event loop.**
   - No `requests.get`, `time.sleep`, `subprocess.run(...)` in async handlers.
   - File I/O in hot path: use [history.save_history_async()](history.py) or
     [utils/json_store.py](utils/json_store.py) `save_json_async()`.
   - HTTP: use `aiohttp` or `discord.Asset.read()`.
   - CPU work (Pillow, pHash, BeautifulSoup parse): wrap with `asyncio.to_thread(...)`.

2. **All JSON state files write atomically.** The pattern is:
   ```python
   fd, tmp = tempfile.mkstemp(dir=dir_of_target, prefix='.name.', suffix='.tmp')
   with os.fdopen(fd, 'w', encoding='utf-8') as f:
       json.dump(data, f, ensure_ascii=False, indent=2)
   os.replace(tmp, target)
   ```
   Reference: [utils/json_store.py:24-43](utils/json_store.py#L24-L43). **Do not replace
   with plain `open(path, 'w')`** — we have been burned by truncated files on crash.

3. **Regex in the hot path must be pre-compiled at module level.** See `_URL_RE`, `_KB_RE`,
   `_MENTION_RE_CACHE` in [main.py](main.py) and the compiled patterns in
   [utils/text_processing.py](utils/text_processing.py).

4. **Gemini Chat rebuild rule** (in [gemini_worker.py](gemini_worker.py)): only rebuild when
   `len(hist) >= HISTORY_MAX_TURNS`. Do NOT rebuild "because there was an attachment". An
   earlier version did and thrashed every message.

5. **Summary trim is O(n)**, not `while pop(0)`. See [summary.py:51-60](summary.py#L51-L60).

6. **API-Key rotation is a side-effect of error handling**, not an explicit toggle. When
   you see `RESOURCE_EXHAUSTED` / 429 / 5xx, rotate key and retry ONCE per send —
   `gemini_worker.py` already does this.

---

## 4. Where state lives

| File | Written by | Read by | Notes |
|---|---|---|---|
| `data/chat_history.json` | [history.py](history.py) | `load_history()` on startup | Atomic write + async save in hot path |
| `data/summaries/{cid}.txt` | [summary.py](summary.py) | Injected into model system prompt | Trimmed to `MAX_LINES` × `MAX_CHARS` (atomic TXT write) |
| `data/merit.json` | [commands/tool.py](commands/tool.py) (電子木魚) | [commands/rank.py](commands/rank.py) | 功德排行 |
| `data/relationships.json` | [commands/relationship.py](commands/relationship.py) | relationship.py + [graph_render.py](graph_render.py) | 主寵關係 |
| `data/wife_records.json` | [commands/daily_mom.py](commands/daily_mom.py), [commands/relationship.py](commands/relationship.py) | daily_mom + relationship + graph_render | 當日媽媽，跨日自動清除 |
| `data/whip_records.json` | [commands/relationship.py](commands/relationship.py) | rank.py | 調教次數排行 |
| `data/whip_relations.json` | [commands/relationship.py](commands/relationship.py) | relationship.py + graph_render | 調教關係（trainer→trainee） |
| `data/artillery_records.json` | [commands/relationship.py](commands/relationship.py) | rank.py | 炮決排行 |
| `data/logs/bot_YYYY-MM-DD.log` | [logger.py](logger.py) | tail | 依日期切檔 |
| `data/logs/manga_translator_server.log` | [manga_translator_server.py](manga_translator_server.py) | tail | >50MB 自動滾動 .old |
| `pixivdata/data/pixiv.db` | [pixiv_database.py](pixiv_database.py) | crawler + search | SQLite, thread-local conn |
| `pixivdata/data/feature.index` | [pixiv_feature.py](pixiv_feature.py) | reverse-search + dedup | FAISS binary index |
| `pixivdata/data/*_progress.json` | crawler | crawler | Resume checkpoints |

**Removed modules** — these were referenced in older docs but no longer exist: `nicknames.py`,
`knowledge.py`, `commands/fun.py`, `commands/social.py`, `commands/wife.py`, `commands/whip.py`,
`commands/gag.py`, `commands/artillery.py`, `commands/nick.py`, `commands/kb.py`, `commands/search.py`.
Functions either folded into `commands/tool.py` / `commands/relationship.py` / `commands/rank.py` /
`commands/daily_mom.py` / `commands/image_search.py`, or removed entirely.

**`state.py` is the only blessed mutable-global module.** Do not add new globals in random
places; put shared mutable state there.

---

## 5. Adding a new slash command

1. Create `commands/<name>.py` with `def setup(tree: app_commands.CommandTree) -> None`.
2. Register it in [commands/\_\_init\_\_.py](commands/__init__.py) `setup_all()`.
3. For JSON persistence, use `utils.json_store.load_json` / `save_json_async`.
4. For member lookups that may not be in cache, use
   `utils.discord_helpers.get_member_safe` — it handles `guild.fetch_member`/NotFound.
5. For 主人 (master)-only commands, check `interaction.user.id == config.MASTER_ID`
   and reply `ephemeral=True` on denial.
6. Defer early if the command will take >2s: `await interaction.response.defer()`.

---

## 6. AI chat flow (so you don't break it)

1. `on_message` in [main.py](main.py) detects mention / URL / attachment.
2. Builds `prompt` + optional `files_parts` (image bytes / mime tuples).
3. Push to `gemini_worker.queue_request(...)` → returns `asyncio.Future`.
4. Worker pops, constructs `Chat`, streams response, saves history via
   `save_history_async`.
5. `summary.save_summary` is called inside `save_history_async`'s `to_thread` so it
   never blocks the loop either.

**Do not** call `chat.send_message_stream` on the event loop directly — the worker
serializes requests with `API_DELAY` seconds between calls; bypassing it will burn keys.

---

## 7. Pixiv crawler notes

- `pixiv_crawler/` is a package. Entry is `commands/pixiv.py` → `/pixiv爬蟲`.
- Rate-limited by `DOWNLOAD_WORKERS` + `DOWNLOAD_RATE_LIMIT_Mbps` in
  [pixiv_config.py](pixiv_config.py).
- Dedup: **image bytes → pHash → FAISS Hamming search**. If a sufficiently close hash
  exists, we skip the download. Do NOT remove the pHash step even when adding new
  sources; it is what keeps disk usage bounded.
- SQLite conn is **thread-local**; don't pass the connection object across threads.

---

## 8. Test / Verify before declaring a task done

Claude should always run these two commands after touching Python files:

```bash
# 1. Byte-compile check (catches syntax errors)
python -m py_compile <file>.py

# 2. Import smoke test (catches circular-import / missing-dep regressions)
python -c "import main"
```

There is no automated unit-test suite. UI features must be eyeballed in Discord; say so
if you can't run the bot. Do not claim the feature "works" if you only made the code
type-check.

---

## 9. Anti-patterns the user has rejected

- **Mock in place of real calls** for AI / Discord — the user prefers that failures
  surface, not be mocked away.
- **Emoji in code / UI text** unless explicitly requested.
- **Auto-added "此修復用於XX" / changelog-style comments.** Comments only earn their
  keep when explaining *non-obvious why*.
- **Bulk re-formatting** that isn't asked for — keep diffs tight around the actual
  change.
- **Splitting a single refactor across many PRs** when the user asked for one — follow
  the user's chosen granularity.

---

## 10. Persona (not technical, but worth knowing)

The bot's in-character voice is **「小龍喵」** — a traditional-Chinese-speaking loli
cat girl belonging to "主人龍龍喵" (MASTER_ID `404111257008865280`). The personality
text lives in [config.py](config.py#L75-L92). If you change these strings, you are
changing the user-facing personality — ask first.
