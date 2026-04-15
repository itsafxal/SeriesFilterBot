# CyberBot — Telegram Series Delivery Bot

A fast, production-grade Telegram bot that indexes TV series files from channels and delivers them to users via a smart search → season → language → quality flow. Built with Pyrogram, MongoDB (dual-DB), and SQLite.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [File Structure](#file-structure)
- [How It Works](#how-it-works)
- [Bug Fixes & Known Issues](#bug-fixes--known-issues)
- [Configuration](#configuration)
- [Admin Commands](#admin-commands)
- [Admin Settings Panel](#admin-settings-panel)
- [FSub (Force Subscribe)](#fsub-force-subscribe)
- [Indexing](#indexing)
- [Database Design](#database-design)
- [Delivery Flow](#delivery-flow)
- [Poster & Metadata](#poster--metadata)
- [Stickers](#stickers)
- [Auto-Delete](#auto-delete)
- [Performance Notes](#performance-notes)

---

## Architecture Overview

```
User types series name
        │
        ▼
   filters.py  ──► search_series() ──► SQLite FTS
        │
        ├─ Exact match found ──► Season selection buttons
        │
        └─ No match ──► suggest_series_with_year() ──► fuzzy + TMDB
                              │
                              ▼
                     "Did you mean?" buttons
                              │
                              ▼
                     Season → Language → Quality
                              │
                              ▼
                    encode_payload() → t.me/bot?start=send_TOKEN
                              │
                              ▼
                     /start with payload ──► FSub check
                              │
                              ▼
                     deliver_files_direct() → media group send
```

---

## File Structure

```
CyberTG/
├── filters.py        — Search, season/language/quality selection callbacks
├── files.py          — File delivery engine (session management, media groups)
├── poster.py         — TMDB + OMDB metadata & poster fetching
├── command.py        — Admin commands (/start, /stats, /broadcast, etc.)
├── menus.py          — Admin settings UI (inline menus for all config)
├── index.py          — Bulk channel indexer (bot-token mode)
└── fsub.py           — Force-subscribe gate with dual-slot support

DB/
├── database.py       — Async wrappers over SQLite + dual MongoDB
└── sqlite_db.py      — SQLite schema, queries, migration, LRU caches

utils/
├── cyber_index.py    — File parser: filename → (series, season, ep, quality, lang, codec)
├── cyber_series.py   — Smart series suggestion engine (SQL → fuzzy → TMDB enrichment)
├── cyber_filter.py   — Short-token payload encode/decode with MongoDB persistence
├── cyber_poster.py   — SQLite poster cache (thread-local WAL connection)
├── cyber_sticker.py  — Neon season sticker generator (Pillow, multi-style)
└── cyber_delete.py   — APScheduler-based auto-delete for messages
```

---

## How It Works

### Search Flow

1. User sends any text message in private chat or group.
2. `search_handler` in `filters.py` queries SQLite via `search_series()`.
3. If an exact or prefix match is found, the season selection UI is shown immediately.
4. If no match, `suggest_series_with_year()` is called — this does SQL → rapidfuzz → TMDB/OMDB enrichment and returns titles with release years for the "Did you mean?" screen.
5. Suggestion buttons embed only the **clean series name** (no year suffix) in `callback_data` so subsequent DB queries always find the correct row.

### Delivery Flow

1. User taps a quality button → gets a `t.me/bot?start=send_TOKEN` link.
2. Token is persisted to MongoDB **before** the URL is shown (no race condition).
3. User taps the link → `/start send_TOKEN` → FSub gate checked.
4. If FSub passes → `deliver_files_direct()` fetches all matching files from SQLite and sends them in media groups of up to 10.
5. Optional auto-delete timer applied to all sent files.

---

## Bug Fixes & Known Issues

### Bug 1 — Season Button Broken for "Dark" and Similar Series (FIXED)

**Symptom:** Tapping a season button after selecting a series from the "Did you mean?" suggestion screen does nothing — no languages appear.

**Root Cause:**

`suggest_series_with_year()` returns `(title, year)` tuples. `format_suggestion_label()` produces display strings like `"Dark (2017)"`. In the old code, the **full display string including the year** was placed into the `sug|...` callback_data:

```python
# OLD (broken)
buttons.append([InlineKeyboardButton(
    f"🎬  {s}",           # s = "Dark (2017)"
    callback_data=f"sug|{s}"   # ← year baked into payload!
)])
```

The `sug` handler then passed `"dark (2017)"` directly into `build_season_pages()`, which embedded it into `gss|...|dark (2017)|1`. When the season button was tapped, `get_unique_languages_by_season("dark (2017)", 1)` was called — but SQLite only knows `"Dark"` (no year), so the query returned empty and the button appeared broken.

**Fix Applied (in `filters.py`):**

A `_strip_year()` helper strips `" (YYYY)"` suffixes and lowercases the result. It is applied at every entry point:

- `search_handler` — strips before embedding series into button data
- Suggestion button builder — stores `clean_title` (no year) in `callback_data`, keeps year only in the display label
- `sug`, `gss`, `gls`, `spg`, `bk_season`, `bk_lang` handlers — all call `_strip_year()` defensively on incoming callback data

This means the year is shown to the user for identification but never reaches the database query layer.

```python
_YEAR_SUFFIX_RE = re.compile(r"\s*\(\d{4}\)\s*$")

def _strip_year(series: str) -> str:
    return _YEAR_SUFFIX_RE.sub("", series.lower()).strip()
```

**Series affected:** Any series whose name alone is ambiguous and triggers TMDB enrichment — "Dark", "Lost", "Severance", "Beef", "Them", "Us", "You", "Hit", etc.

---

### Other Bugs Previously Fixed (reference)

**`cyber_filter.py` — Token race condition:** `encode_payload()` was sync and used `create_task()` for MongoDB persist, meaning the token might not exist when the user tapped the link. Fixed: `encode_payload()` is now `async` and `await`s the persist before returning.

**`database.py` — Unbounded LRU cache:** `_async_cache` was a plain dict that grew forever. Fixed with `OrderedDict` capped at 4096 entries.

**`cyber_series.py` — Unbounded API cache:** `_API_CACHE` was a plain dict. Fixed with `OrderedDict` capped at 2048 entries with TTL eviction.

**`fsub.py` — Delivery lock memory leak:** `_DELIVERY_LOCK` was a plain `set()`. If the callback after delivery failed, the lock was never released and the user was permanently blocked from receiving files. Fixed with a `{uid: timestamp}` dict and 5-minute TTL eviction.

**`sqlite_db.py` — Missing codec on re-index:** `INSERT OR IGNORE` silently dropped re-indexed files even when the new file had codec info the original didn't. Fixed with `INSERT ... ON CONFLICT DO UPDATE` that updates codec/file_id without clobbering existing valid data.

**`command.py` — `/stats` showed 0 for all DB sizes:** `get_db_size_info()` returns keys `"config"`, `"db1"`, `"db2"`, `"sqlite"` but the old code read `"mongo_config_mb"` etc. Fixed to use the correct keys.

**`cyber_index.py` — Language-agnostic duplicate detection:** The dedup check only matched on `(name, season, ep, quality, language)`. If TMDB was slow and returned `"Unknown"` for a file previously stored as `"Hindi"`, a second row was inserted. Fixed with a two-level check: exact match + language-agnostic match.

---

## Configuration

Key settings live in `config.py`. Runtime settings are stored in MongoDB under the `SeriesBotConfig.bot_state` collection and managed via the admin panel.

| Setting | Key | Default | Description |
|---|---|---|---|
| TMDB API Key | `TMDB_API_KEY` | — | For poster/metadata lookup |
| OMDB API Key | `OMDB_API_KEY` | — | Fallback metadata |
| MongoDB URIs | `MONGO_URI_CONFIG`, `MONGO_URI_DB1`, `MONGO_URI_DB2` | — | Three separate MongoDB connections |
| Filter enabled | `FILTER_ENABLED` | `True` | Master on/off for search |
| Poster enabled | `POSTER_ENABLED` | `True` | Show poster images in results |
| Poster landscape | `POSTER_LANDSCAPE` | `False` | Wide vs portrait poster |
| Auto-delete (search) | `AUTO_DELETE_DELAY` | `0` | Seconds before result messages delete |
| Auto-delete (files) | `FILE_AUTODEL_DELAY` | `0` | Seconds before sent files delete |
| Caption language | `CAPTION_LANG` | `False` | Use caption text for language detection |
| Maintenance mode | `MAINTENANCE_MODE` | `False` | Block non-admin users |

---

## Admin Commands

All commands are restricted to user IDs listed in `ADMIN_USERS`.

| Command | Description |
|---|---|
| `/start` | Welcome message; with `?start=send_TOKEN` triggers file delivery |
| `/adminsetting` | Open the admin settings panel |
| `/stats` | Detailed bot statistics (users, files, series, DB sizes) |
| `/total` | Quick stats summary |
| `/list` | Paginated series list with export option |
| `/broadcast` | Broadcast a replied-to message to all users |
| `/gbroadcast` | Broadcast to all connected groups |
| `/deleteall` | Delete all indexed files (with confirmation prompt) |
| `/deletefile <cid> <mid>` | Delete one file by channel ID and message ID |
| `/logs` | Send current log file |
| `/maintenance on\|off` | Toggle maintenance mode |
| `/indexlink <url> [url]` | Start bulk index from a channel link or range |
| `/setskip <msg_id>` | Skip messages below this ID during next index |
| `/getskip` | Show current skip ID |
| `/missing` | Generate a full per-series missing-episode report (TMDB-compared) |

---

## Admin Settings Panel

Access via `/adminsetting` → `Global Settings`. All settings persist to MongoDB.

- **Index Channels** — Add or remove channels the bot auto-indexes on new uploads
- **File Caption** — Set a custom caption template for delivered files (supports `{series}`, `{season}`, `{episode}`, `{language}`, `{quality}`, `{year}`, `{genre}`, `{rating}`, `{file_name}`)
- **Auto Delete** — Set delay (seconds) for search result message deletion
- **File Auto Del** — Set delay (seconds) for delivered file deletion
- **Caption Clean** — Keywords to strip from file captions before sending
- **FSub Settings** — Configure force-subscribe channels (see below)
- **Caption Lang** — Toggle whether message captions are used for language detection during indexing
- **Filter** — Master toggle for the search feature
- **Poster** — Enable/disable poster images, toggle portrait vs landscape orientation
- **File Bot** — Configure a secondary bot for file delivery (dual-bot mode)

---

## FSub (Force Subscribe)

Two independent slots, each supporting a primary and secondary channel with rotation.

- **Mode: Request** — User must submit a join request; tracked in MongoDB
- **Mode: Invite** — User must actually join (tracked via `chat_member_updated` event)
- **Threshold** — After N join requests on the primary, delivery automatically rotates to the secondary channel
- Auto-deliver triggers on join request receipt or member join event — no user action needed after joining

### FSub flow

```
User taps start link
      │
      ▼
check FSub slots (get_slot_statuses)
      │
      ├─ All passed ──► deliver files immediately
      │
      └─ Not passed ──► Show join buttons, register pending
                              │
                         User joins channel
                              │
                         _try_auto_deliver()
                              │
                         Re-check slots ──► deliver
```

---

## Indexing

### Real-Time Index (RTI)

When a new file is posted to a configured index channel, `rti` handler in `index.py` fires automatically and saves the file via `save_series_file_for_bulk_index()`.

### Bulk Index

Forward any message from a channel or use `/indexlink` to trigger bulk indexing. The bot reads message IDs in batches of 50, respects FloodWait, uses an adaptive cooldown, and saves progress checkpoints so indexing can resume after interruption.

Target can be `DB1`, `DB2`, or `Both`.

### Filename Parsing

`cyber_index.py` extracts the following from each filename:

- **Series name** — Text before the first season/episode marker, cleaned of junk tokens
- **Season & Episode** — Supports `S01E01`, `S01EP01`, `Season 1 Episode 1`, `1x01`
- **Quality** — `4K`, `1080P`, `720P`, `480P`, `360P` (or inferred from video resolution)
- **Codec** — `H.265`, `H.264`, `AV1`, `VP9`, `HDR`
- **Language** — Detected from filename, then caption, then TMDB/OMDB API

---

## Database Design

### SQLite (`series_index.db`) — Primary query layer

```sql
CREATE TABLE files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id      INTEGER NOT NULL,
    message_id      INTEGER NOT NULL,
    file_name       TEXT    NOT NULL,
    normalized_name TEXT    NOT NULL COLLATE NOCASE,
    season          INTEGER NOT NULL,
    episode         INTEGER NOT NULL,
    quality         TEXT    NOT NULL,
    language        TEXT    NOT NULL,
    file_id         TEXT,
    media_type      TEXT    DEFAULT 'document',
    caption         TEXT    DEFAULT '',
    codec           TEXT    DEFAULT '',
    UNIQUE(channel_id, message_id),
    UNIQUE(normalized_name, season, episode, quality, language)
)
```

Key indexes: `(normalized_name, language, season)`, `(normalized_name, season, quality, language)`, `(channel_id, message_id)`.

Uses WAL mode, 32MB page cache, and a persistent thread-local connection pool for maximum concurrent read throughput.

### MongoDB — Persistence & config layer

Three MongoDB connections are used:

- **SeriesBotConfig** — Bot state, global settings, FSub config, user list, pending deliveries, sticker cache, groups
- **SeriesBotFiles1** — File documents (MongoDB mirror of SQLite, DB1)
- **SeriesBotFiles2** — File documents (MongoDB mirror of SQLite, DB2)

New files are alternated between DB1 and DB2 on each save for load distribution. On bot start, MongoDB is migrated to SQLite if the content fingerprint has changed.

### Poster cache (`series_index.db` → `poster_cache` table)

Caches TMDB/OMDB results keyed by series name (and `name:wide` for landscape mode). Thread-local WAL connection, same db file.

---

## Delivery Flow

`deliver_files_direct()` in `files.py`:

1. Reads session data (series, season, language, quality) set during payload decode
2. Queries `get_all_files_by_season_and_quality()` from SQLite
3. Sorts by episode number
4. Sends in batches of up to 10 using `send_media_group()` (or `send_document()` for single files)
5. Falls back to individual sends if the media group fails
6. Applies auto-delete timer to each sent message if configured

Sessions are stored both in process memory and MongoDB, with a 10-minute TTL. This means sessions survive bot restarts within the TTL window.

---

## Poster & Metadata

`poster.py` fetches from TMDB first, falls back to OMDB. Both portrait and landscape orientations are cached separately (key suffix `:wide`).

`get_series_info()` returns `(poster_url, year, genre, rating)` and is used by all filter callbacks to display a rich info card alongside the season/language/quality selection UI.

---

## Stickers

`cyber_sticker.py` generates WebP stickers using Pillow with neon glow effects. Two sticker types:

- **Start sticker** — Shown before the first episode of a season is sent (2 random styles)
- **End sticker** — Shown after the last episode of a season is sent (3 random styles)

Style indices are saved per-season to MongoDB so the same style is used if the same season is delivered multiple times to the same user.

---

## Auto-Delete

Two independent auto-delete delays:

- **`AUTO_DELETE_DELAY`** — Applied to search result messages (poster cards, suggestion lists, "no results" messages). Set via admin panel → Auto Delete.
- **`FILE_AUTODEL_DELAY`** — Applied to sent files and stickers. Set via admin panel → File Auto Del.

Both use APScheduler (`AsyncIOScheduler`) with `DateTrigger` for exact-time scheduling. The scheduler is started once after `await cyber.start()` in `main.py`.

---

## Performance Notes

- **LRU caches** on all frequently-read SQLite queries (`lru_cache(maxsize=1024)`) — cleared on any write
- **Async cache** in `database.py` with 4096-entry LRU — avoids redundant `asyncio.to_thread()` round-trips for repeated queries
- **Concurrent DB fetches** — `asyncio.gather()` used in `gss`, `gls`, `bk_season`, `bk_lang` to run poster fetch and DB query in parallel
- **Payload encoding** — All quality payloads for a series are encoded concurrently via `asyncio.gather()` in `gls`, avoiding sequential MongoDB writes that would blow Telegram's 10s callback timeout
- **Bot identity cache** — `get_me()` is called at most once per process lifetime; result cached in module globals
- **Settings TTL cache** — Global settings fetched at most once per 30 seconds regardless of traffic
- **Thread-local SQLite connections** — No per-query connection open/close; connections reused across calls in the same thread, WAL mode enables concurrent reads
