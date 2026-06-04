import datetime
import json
import os
import random
import re
import sqlite3
import sys
import time
import logging
import urllib.request
import urllib.parse
import urllib.error
from typing import Optional

logger = logging.getLogger("workshop_core")

BROWSE_URL = "https://steamcommunity.com/workshop/browse/"
ITEM_URL = "https://steamcommunity.com/sharedfiles/filedetails/"
STEAM_API_URL = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"
_HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__))
CACHE_DB = os.path.join(_HERE, "workshop_cache.db")
IGNORED_DB = os.path.join(_HERE, "workshop_ignored.db")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _tag_combo_key(tags: Optional[list]) -> str:
    return ",".join(sorted(tags)) if tags else ""


def _init_cache(app_id: Optional[int] = None):
    db_path = os.path.join(_HERE, f"workshop_cache_{app_id}.db") if app_id is not None else CACHE_DB
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cache (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS item_details (
            item_id INTEGER PRIMARY KEY,
            name TEXT,
            thumbnail TEXT,
            size TEXT,
            posted TEXT,
            updated TEXT,
            tags TEXT,
            fetched REAL
        )
    """)
    # Check if known_valid exists and migrate if needed
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='known_valid'")
    exists = cur.fetchone()
    if exists:
        cur = conn.execute("PRAGMA table_info(known_valid)")
        cols = {row[1] for row in cur.fetchall()}
        if "tag_combo" not in cols:
            conn.execute("DROP TABLE known_valid")
            conn.execute("""
                CREATE TABLE known_valid (
                    app_id INTEGER,
                    tag_combo TEXT,
                    item_id INTEGER,
                    PRIMARY KEY (app_id, tag_combo, item_id)
                )
            """)
    else:
        conn.execute("""
            CREATE TABLE known_valid (
                app_id INTEGER,
                tag_combo TEXT,
                item_id INTEGER,
                PRIMARY KEY (app_id, tag_combo, item_id)
            )
        """)
    try:
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_known_valid_app
            ON known_valid (app_id, tag_combo)
        """)
    except sqlite3.OperationalError:
        pass
    conn.commit()
    return conn


def _cache_get(conn, key: str) -> Optional[str]:
    row = conn.execute(
        "SELECT value FROM cache WHERE key = ?", (key,)
    ).fetchone()
    return row[0] if row else None


def _cache_set(conn, key: str, value: str):
    conn.execute(
        "REPLACE INTO cache (key, value, updated) VALUES (?, ?, ?)",
        (key, value, time.time()),
    )
    conn.commit()


def _fetch(url: str, max_retries: int = 5, status_callback: Optional[callable] = None) -> str:
    for attempt in range(max_retries):
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries - 1:
                wait = 5 * (attempt + 1)
                msg = f"Rate limited (429) — retrying in {wait}s (attempt {attempt + 2}/{max_retries})"
                logger.warning("429 Too Many Requests — retrying in %ss (attempt %s/%s)", wait, attempt + 1, max_retries)
                if status_callback:
                    status_callback(msg)
                time.sleep(wait)
                continue
            if e.code == 429:
                msg = "Fetching API page - Waiting 24 hours due to API key limit [86400]"
                logger.error(msg)
                if status_callback:
                    status_callback(msg)
            raise


def validate_item_id(item_id: int, app_id: int) -> bool:
    url = f"{ITEM_URL}?id={item_id}"
    logger.debug("Validating item %s...", item_id)
    try:
        html = _fetch(url)
    except Exception as e:
        logger.warning("HTTP error validating %s: %s", item_id, e)
        return False
    if "Steam Community :: Error" in html or "<h2>Error</h2>" in html:
        logger.debug("Item %s not found (error page)", item_id)
        return False
    m = re.search(r'<meta\s+property="og:title"\s+content="([^"]*)"', html)
    if m:
        og_title = m.group(1)
        if "Steam Workshop::" in og_title or "Steam Community :: Error" not in og_title:
            logger.debug("Item %s is valid: %s", item_id, og_title)
            return True
    logger.debug("Item %s could not be validated via og:title", item_id)
    return False


def _extract_item_ids(html: str) -> list:
    ids = set()
    for m in re.finditer(r'/sharedfiles/filedetails/\?id=(\d+)', html):
        ids.add(int(m.group(1)))
    for m in re.finditer(r'data-publishedfileid="(\d+)"', html):
        ids.add(int(m.group(1)))
    return sorted(ids)


def _extract_item_ids_from_json(html: str) -> list:
    ids = set()
    for m in re.finditer(r'"publishedfileid"\s*:\s*"(\d+)"', html):
        ids.add(int(m.group(1)))
    return sorted(ids)


def _clean_html(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    return (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&#x2F;", "/").replace("&#x27;", "'")
            .replace("\\/", "/").replace("\\n", "\n").strip())


def _ts_str(val) -> Optional[str]:
    if val is None:
        return None
    try:
        t = int(val)
        return datetime.datetime.utcfromtimestamp(t).strftime("%d %b, %Y @ %I:%M%p").lstrip("0")
    except (ValueError, OSError, TypeError):
        return None


def _size_str(val) -> Optional[str]:
    if val is None:
        return None
    try:
        s = int(val)
        if s < 1024:
            return f"{s} B"
        elif s < 1024 * 1024:
            return f"{s / 1024:.1f} KB"
        else:
            return f"{s / (1024 * 1024):.2f} MB"
    except (ValueError, TypeError):
        return str(val) if val else None


def _parse_item_from_json(item_data: dict) -> dict:
    raw_id = item_data.get("publishedfileid", "")

    # Tags come as [{tag: "Name", display_name: "Name"}, ...] in API responses
    tags: list[str] = []
    for t in (item_data.get("tags") or []):
        tag_name = (t.get("tag") or t.get("display_name") or "") if isinstance(t, dict) else str(t)
        if tag_name:
            tags.append(tag_name)

    item = {
        "id": int(raw_id) if str(raw_id).isdigit() else 0,
        "name": _clean_html(item_data.get("title")),
        "thumbnail": _clean_html(item_data.get("preview_url")),
        "size": _size_str(item_data.get("file_size")),
        "posted": _ts_str(item_data.get("time_created")),
        "updated": _ts_str(item_data.get("time_updated")),
        "tags": tags,
    }
    return item


def _extract_json_items_from_scripts(html: str) -> list[dict]:
    """Try to extract complete item data from JSON embedded in script tags."""
    items = []

    # Method A: JSON in <script type="application/json" ...>
    for script in re.finditer(
        r'<script[^>]+type\s*=\s*["\']application/json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        content = script.group(1).strip()
        if not content:
            continue
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            continue
        found = _recurse_find_items(data)
        if found:
            items.extend(found)

    if items:
        return items

    # Method B: JavaScript var g_rgAssets = {...}; in <script> tags
    for script in re.finditer(r'<script[^>]*>(.*?)</script>', html, re.DOTALL):
        content = script.group(1)
        for m in re.finditer(r'(?:var\s+)?g_rgAssets\s*=\s*', content):
            start_idx = m.end()
            if start_idx >= len(content) or content[start_idx] != '{':
                continue
            depth = 0
            i = start_idx
            in_str = False
            esc = False
            while i < len(content):
                ch = content[i]
                if esc:
                    esc = False
                elif ch == '\\':
                    esc = True
                elif ch == '"':
                    in_str = not in_str
                elif not in_str:
                    if ch == '{':
                        depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            raw = content[start_idx:i + 1]
                            try:
                                data = json.loads(raw)
                                if isinstance(data, dict):
                                    for val in data.values():
                                        if isinstance(val, dict) and "publishedfileid" in val:
                                            items.append(_parse_item_from_json(val))
                                    if items:
                                        return items
                            except (json.JSONDecodeError, ValueError):
                                pass
                            break
                i += 1

    # Method C: un-escaped JSON with publishedfileid keys
    for script in re.finditer(r'<script[^>]*>(.*?)</script>', html, re.DOTALL):
        content = script.group(1)
        for m in re.finditer(r'"publishedfileid"\s*:\s*"(\d+)"', content):
            iid = m.group(1)
            start = max(0, m.start() - 200)
            snippet = content[start:m.end() + 300]
            try:
                brace_start = snippet.index("{")
                # Find matching closing brace
                depth = 0
                for j, ch in enumerate(snippet[brace_start:]):
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            obj = json.loads(snippet[brace_start:brace_start + j + 1])
                            items.append(_parse_item_from_json(obj))
                            break
            except (ValueError, json.JSONDecodeError):
                continue

    return items


def _recurse_find_items(data) -> list[dict]:
    """Recursively search for publishedfileid items in nested JSON data."""
    items = []
    if isinstance(data, dict):
        if "publishedfileid" in data:
            items.append(_parse_item_from_json(data))
            return items
        for val in data.values():
            items.extend(_recurse_find_items(val))
    elif isinstance(data, list):
        for val in data:
            items.extend(_recurse_find_items(val))
    return items


def _extract_browse_items(html: str) -> list[dict]:
    """Extract items from Steam workshop browse page HTML.

    Only extracts items that are inside workshopItem divs to avoid
    picking up sidebar/recommendation/footer items.
    """
    items = []

    # Method 1: Try complete JSON extraction from script tags
    items = _extract_json_items_from_scripts(html)
    if items:
        return items

    # Method 2: Extract from workshopBrowseItems container only
    container_m = re.search(
        r'<div[^>]*\b(?:id|class)\s*=\s*["\'][^"\']*\bworkshopBrowseItems\b[^"\']*["\'][^>]*>',
        html, re.IGNORECASE,
    )
    if container_m:
        start = container_m.end()
        # Find container boundary: scan for closing </div> by counting nesting
        depth = 1
        i = start
        while i < len(html) and depth > 0:
            open_tag = html.find('<div', i, i + 5000)
            close_tag = html.find('</div>', i, i + 5000)
            if close_tag < 0:
                break
            if open_tag >= 0 and open_tag < close_tag:
                depth += 1
                i = open_tag + 4
            else:
                depth -= 1
                i = close_tag + 6
        container_end = i - 6 if depth == 0 else start + 5000
        listing_html = html[start:container_end]

        seen = set()
        for m in re.finditer(r'data-publishedfileid="(\d+)"', listing_html):
            iid = int(m.group(1))
            if iid in seen:
                continue
            seen.add(iid)
            block_start = listing_html.rfind('<div', 0, m.start())
            if block_start < 0:
                block_start = 0
            block = listing_html[block_start:m.start() + 600]
            title_m = re.search(r'class="workshopItemTitle[^"]*"[^>]*>([^<]+)<', block)
            title = _clean_html(title_m.group(1)) if title_m else None
            thumb_m = re.search(r'<img[^>]+src="([^"]+)"[^>]*>', block)
            thumbnail = thumb_m.group(1).replace("&amp;", "&") if thumb_m else None
            items.append({
                "id": iid, "name": title, "thumbnail": thumbnail,
                "size": None, "posted": None, "updated": None,
            })

        if items:
            return items

    # Method 3: Fallback — extract from entire page using data-publishedfileid
    seen = set()
    for iid in _extract_item_ids(html):
        if iid in seen:
            continue
        seen.add(iid)
        items.append({
            "id": iid,
            "name": None,
            "thumbnail": None,
            "size": None,
            "posted": None,
            "updated": None,
        })

    return items


def _store_items(conn, app_id: int, combo: str, items: list[dict]) -> list[int]:
    """Store items in known_valid and item_details tables."""
    ids = []
    for item in items:
        iid = item.get("id", 0)
        if not iid:
            continue
        ids.append(iid)
        conn.execute(
            "INSERT OR IGNORE INTO known_valid (app_id, tag_combo, item_id) VALUES (?, ?, ?)",
            (app_id, combo, iid),
        )
        if item.get("name"):
            conn.execute(
                "INSERT OR REPLACE INTO item_details (item_id, name, thumbnail, size, posted, updated, tags, fetched) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (iid, item["name"], item.get("thumbnail"), item.get("size"),
                 item.get("posted"), item.get("updated"),
                 json.dumps(item.get("tags") or []), time.time()),
            )
    return ids


_PAGE_SIZE = 30


def _fetch_with_callback(url: str, status_callback: Optional[callable] = None) -> str:
    """Wrapper around _fetch that also passes status_callback for 429 reporting."""
    return _fetch(url, status_callback=status_callback)


# ---------------------------------------------------------------------------
# Steam Web API — IPublishedFileService/QueryFiles  (cursor-based pagination)
# ---------------------------------------------------------------------------

def _query_workshop_api(
    app_id: int,
    cursor: str = "*",
    tags: Optional[list] = None,
    api_key: str = "",
    num_per_page: int = 100,
    status_callback: Optional[callable] = None,
) -> Optional[dict]:
    """Call Steam's IPublishedFileService/QueryFiles endpoint.

    Returns the ``response`` dict on success, or None on error.
    Requires a free Steam Web API key (steamcommunity.com/dev/apikey).
    """
    params: dict = {
        "key": api_key,
        "query_type": 1,           # 1 = ranked by publication date (newest first)
        "cursor": cursor,
        "numperpage": min(max(1, num_per_page), 100),
        "appid": app_id,
        "return_tags": 1,
        "return_metadata": 1,
        "return_previews": 1,
        "return_children": 0,
        "return_short_description": 0,
        "return_vote_data": 0,
    }
    if tags:
        for i, tag in enumerate(tags):
            params[f"requiredtags[{i}]"] = tag

    url = f"{STEAM_API_URL}?{urllib.parse.urlencode(params)}"
    safe_url = re.sub(r"key=[^&]+", "key=***", url)
    logger.debug("Steam API → %s", safe_url[:180])
    try:
        raw = _fetch(url, status_callback=status_callback)
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Steam API: JSON parse error — %s", exc)
        return None
    except Exception as exc:
        logger.error("Steam API: request failed — %s", exc)
        return None

    response = data.get("response")
    if response is None:
        # Steam returns {"error": "..."} at top level for bad keys etc.
        err = data.get("error") or data.get("message") or str(data)[:200]
        logger.error("Steam API: unexpected response — %s", err)
        return None
    return response


def harvest_ids_api(
    app_id: int,
    api_key: str,
    tags: Optional[list] = None,
    max_pages: Optional[int] = None,
    status_callback: Optional[callable] = None,
) -> list:
    """Harvest workshop IDs via the Steam Web API using cursor-based pagination.

    Each page fetches up to 100 items.  Pass ``max_pages=None`` to harvest
    everything Steam will return.
    """
    conn = _init_cache(app_id)
    combo = _tag_combo_key(tags)
    all_ids: list[int] = []
    cursor = "*"
    page = 0
    page_limit = max_pages if max_pages is not None else 999_999
    tag_label = ", ".join(tags) if tags else "(all)"

    while page < page_limit:
        page += 1
        msg = (
            f"API page {page}/{page_limit} · tags: {tag_label}"
            if max_pages else
            f"API page {page} · tags: {tag_label}"
        )
        logger.info("Fetching %s …", msg)
        if status_callback:
            status_callback(f"Fetching {msg}…")

        response = _query_workshop_api(app_id, cursor, tags, api_key, status_callback=status_callback)
        if response is None:
            logger.warning("Aborting harvest: API returned None on page %s", page)
            break

        total = response.get("total", 0)
        files: list[dict] = response.get("publishedfiledetails") or []

        if not files:
            logger.info("  No files in response — end of results (total reported: %s)", total)
            break

        parsed = [_parse_item_from_json(f) for f in files]
        valid  = [it for it in parsed if it.get("id")]
        ids    = _store_items(conn, app_id, combo, valid)
        conn.commit()
        all_ids.extend(ids)

        logger.info("  Page %s: %s items — %s cumulative / %s total reported",
                    page, len(ids), len(all_ids), total)
        if status_callback:
            status_callback(
                f"Page {page}: {len(ids)} items — {len(all_ids)} total harvested"
            )

        next_cursor = (response.get("next_cursor") or "").strip()
        if not next_cursor or next_cursor == cursor:
            logger.info("  No further cursor — harvest complete")
            break
        cursor = next_cursor

        time.sleep(0.25)   # polite pause between API calls

    # Mark "All" cache as preserved so specific tags can filter from it
    if not tags:
        _cache_set(conn, f"preserved_{app_id}", "1")

    db_count = conn.execute(
        "SELECT COUNT(*) FROM known_valid WHERE app_id = ? AND tag_combo = ?",
        (app_id, combo),
    ).fetchone()[0]
    conn.close()
    logger.info("API harvest done: %s fetched this run, %s total in DB",
                len(all_ids), db_count)
    return all_ids


def harvest_ids(
    app_id: int,
    tags: Optional[list] = None,
    max_pages: Optional[int] = 50,
    status_callback: Optional[callable] = None,
    api_key: Optional[str] = None,
) -> list:
    # Prefer the official API when a key is available — it gives proper
    # cursor-based pagination with up to 100 items per request.
    if api_key:
        return harvest_ids_api(
            app_id, api_key,
            tags=tags, max_pages=max_pages,
            status_callback=status_callback,
        )

    # ------------------------------------------------------------------ #
    # Legacy HTML scraper — only returns whatever Steam SSR-renders        #
    # (typically the first ~30 items).  Kept as a no-key fallback.         #
    # ------------------------------------------------------------------ #
    conn = _init_cache(app_id)
    harvested = []
    combo = _tag_combo_key(tags)

    page_limit = max_pages if max_pages is not None else 999999

    if tags and len(tags) > 1:
        tag_queries = [[t] for t in tags]
    else:
        tag_queries = [tags]

    total_tag_combos = len(tag_queries)
    combo_idx = 0
    global_unique_ids = set()

    for query_tags in tag_queries:
        combo_idx += 1
        tag_label = _clean_html(query_tags[0]) if query_tags else "all"

        params = {"appid": app_id, "sort": "mostrecent"}
        if query_tags:
            for t in query_tags:
                params.setdefault("requiredtags[]", []).append(t)

        for page in range(1, page_limit + 1):
            page_params = {**params, "paged": page}
            url = f"{BROWSE_URL}?{urllib.parse.urlencode(page_params, doseq=True)}"
            label = f"tag '{tag_label}', page {page}/{page_limit if max_pages else 'MAX'} ({combo_idx}/{total_tag_combos})"
            logger.info("Harvesting %s...", label)
            if status_callback:
                status_callback(f"Harvesting {label}...")

            try:
                html = _fetch_with_callback(url, status_callback)
            except urllib.error.HTTPError as e:
                logger.warning("HTTP %s on %s — stopping harvest", e.code, label)
                if status_callback:
                    status_callback(f"HTTP {e.code} — stopping harvest")
                break
            except Exception as e:
                logger.warning("Failed to fetch %s: %s", label, e)
                break

            items = _extract_browse_items(html)
            if not items:
                logger.info("  No items on page %s — reached end of listings", page)
                break

            ids = _store_items(conn, app_id, combo, items)
            conn.commit()

            # Track unique IDs across all pages
            new_unique = sum(1 for iid in ids if iid not in global_unique_ids)
            global_unique_ids.update(ids)
            harvested.extend(ids)

            logger.info("  Page %s: %s items (%s new, %s cumulative)",
                        page, len(ids), new_unique, len(global_unique_ids))

            # Warn if all items on this page were already seen (pagination broken)
            if new_unique == 0 and page > 1:
                logger.warning("  Page %s has zero new items — pagination may not be working", page)
                if status_callback:
                    status_callback("Pagination issue: same items on every page")
                break

            if len(ids) < _PAGE_SIZE:
                logger.info("  Partial page — no further pages")
                break

            time.sleep(1.5)

    # Mark "All" cache as preserved so specific tags can filter from it
    if not tags:
        _cache_set(conn, f"preserved_{app_id}", "1")

    # Verify actual DB count
    db_count = conn.execute(
        "SELECT COUNT(*) FROM known_valid WHERE app_id = ? AND tag_combo = ?",
        (app_id, combo),
    ).fetchone()[0]

    conn.close()
    logger.info("Harvest complete: %s from pages, %s unique in DB",
                len(harvested), db_count)
    return harvested


def _extract_json_from_script(html: str, key: str) -> Optional[dict]:
    for script in re.finditer(r'<script[^>]*>(.*?)</script>', html, re.DOTALL):
        content = script.group(1)
        if f'"{key}"' not in content and f'\\"{key}\\"' not in content:
            continue
        pos = content.find(f'"{key}"')
        if pos < 0:
            pos = content.find(f'\\"{key}\\"')
        if pos < 0:
            continue
        start_key = content.find(':', pos)
        if start_key < 0:
            continue
        val_start = start_key + 1
        if content[val_start] != '{':
            continue
        depth = 0
        i = val_start
        in_string = False
        escape = False
        while i < len(content):
            ch = content[i]
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_string = not in_string
            elif not in_string:
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        raw = content[val_start:i + 1]
                        raw_clean = raw.replace('\\"', '"').replace('\\n', '\n').replace('\\/', '/')
                        try:
                            return json.loads(raw_clean)
                        except json.JSONDecodeError:
                            return None
            i += 1
    return None


def get_available_tags(app_id: int) -> dict:
    logger.info("Fetching tags for app %s...", app_id)
    url = f"{BROWSE_URL}?appid={app_id}"
    try:
        html = _fetch(url)
    except Exception as e:
        logger.error("Failed to fetch tag page: %s", e)
        return {}

    declared = _extract_json_from_script(html, "declaredTags")
    if not declared:
        logger.info("No tag data found in page")
        return {}

    flat_tags: dict[str, str] = {}

    section_map = {
        "guide_tags": "Guides",
        "video_tags": "Videos",
        "screenshot_tags": "Screenshots",
        "image_tags": "Images",
        "merch_tags": "Merch",
        "mtx_tags": "MTX",
        "readytouse_tags": "Ready to Use",
        "collection_tags": "Collections",
    }

    for json_key, section_label in section_map.items():
        entries = declared.get(json_key, [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            category_name = entry.get("name", "")
            tag_list = entry.get("tags", [])
            for t in tag_list:
                tag_name = t.get("name", "")
                display_name = t.get("display_name", "") or tag_name
                if tag_name:
                    key = tag_name.lower().replace(" ", "")
                    if key not in flat_tags:
                        flat_tags[key] = {
                            "tag": tag_name,
                            "display": display_name,
                            "category": category_name,
                            "section": section_label,
                        }

    result = {}
    for info in flat_tags.values():
        label = info["display"]
        if info["category"]:
            label = f"{info['display']} ({info['category']})"
        result[info["tag"]] = label

    logger.info("Found %s tags", len(result))
    return result


def get_item_name(item_id: int) -> Optional[str]:
    url = f"{ITEM_URL}?id={item_id}"
    try:
        html = _fetch(url)
    except Exception:
        return None
    m = re.search(r'<meta\s+property="og:title"\s+content="([^"]*)"', html)
    if m:
        og = m.group(1)
        return og.replace("Steam Workshop::", "").strip()
    m = re.search(r'<title>(.*?)</title>', html)
    if m:
        title = m.group(1)
        return title.replace("Steam Community :: ", "").strip()
    return None


def get_item_details(item_id: int, app_id: Optional[int] = None) -> Optional[dict]:
    conn = _init_cache(app_id)
    row = conn.execute(
        "SELECT name, thumbnail, size, posted, updated, tags FROM item_details WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    if row:
        conn.close()
        return {
            "id": item_id,
            "name": row[0],
            "url": f"{ITEM_URL}?id={item_id}",
            "thumbnail": row[1],
            "size": row[2],
            "posted": row[3],
            "updated": row[4],
            "tags": json.loads(row[5]) if row[5] else [],
        }

    try:
        html = _fetch(f"{ITEM_URL}?id={item_id}")
    except Exception:
        conn.close()
        return None

    og_title = re.search(r'<meta\s+property="og:title"\s+content="([^"]*)"', html)
    name = og_title.group(1).replace("Steam Workshop::", "").strip() if og_title else None

    thumbnail = None
    og_img = re.search(r'<meta\s+property="og:image"\s+content="([^"]*)"', html)
    if og_img:
        thumbnail = og_img.group(1).replace("&amp;", "&")

    stats = re.findall(r'class="detailsStatRight[^"]*"[^>]*>([^<]+)</div>', html)
    size_str = stats[0].strip() if len(stats) > 0 else None
    posted = stats[1].strip() if len(stats) > 1 else None
    updated = stats[2].strip() if len(stats) > 2 else None

    tags = []
    for block in re.finditer(
        r'<div[^>]*class="[^"]*workshopTags[^"]*"[^>]*>'
        r'<span[^>]*class="workshopTagsTitle"[^>]*>[^<]*</span>\s*'
        r'(.*?)\s*</div>',
        html,
        re.IGNORECASE | re.DOTALL,
    ):
        for a in re.finditer(r'<a[^>]*>([^<]+)</a>', block.group(1)):
            t = a.group(1).strip()
            if t and t not in tags:
                tags.append(t)

    details = {
        "id": item_id,
        "name": name or "Unknown",
        "url": f"{ITEM_URL}?id={item_id}",
        "thumbnail": thumbnail,
        "size": size_str,
        "posted": posted,
        "updated": updated,
        "tags": tags,
    }

    if name:
        conn.execute(
            "INSERT OR REPLACE INTO item_details (item_id, name, thumbnail, size, posted, updated, tags, fetched) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, name, thumbnail, size_str, posted, updated, json.dumps(tags), time.time()),
        )
        conn.commit()

    conn.close()
    return details


def roll_random_item(
    app_id: int,
    tags: Optional[list] = None,
    max_pages: Optional[int] = 50,
    exclude_ids: Optional[set] = None,
    status_callback: Optional[callable] = None,
    api_key: Optional[str] = None,
) -> Optional[dict]:
    conn = _init_cache(app_id)
    combo = _tag_combo_key(tags)

    count_row = conn.execute(
        "SELECT COUNT(*) FROM known_valid WHERE app_id = ? AND tag_combo = ?",
        (app_id, combo),
    ).fetchone()
    cached_count = count_row[0] if count_row else 0

    if cached_count == 0:
        # Check if there's a preserved "All" cache to filter from
        preserved = _cache_get(conn, f"preserved_{app_id}")
        all_row = conn.execute(
            "SELECT COUNT(*) FROM known_valid WHERE app_id = ? AND tag_combo = ?",
            (app_id, ""),
        ).fetchone()
        all_count = all_row[0] if all_row else 0

        if tags is not None and (preserved or all_count > 0):
            logger.info("Using preserved 'All' cache to filter for tags: %s", tags)
            if status_callback:
                status_callback(f"Filtering from All cache for tags: {', '.join(tags)}")
            rows = conn.execute(
                "SELECT k.item_id, d.tags FROM known_valid k "
                "JOIN item_details d ON k.item_id = d.item_id "
                "WHERE k.app_id = ? AND k.tag_combo = ? AND d.tags IS NOT NULL",
                (app_id, ""),
            ).fetchall()
            tag_filter = [t.lower().replace(" ", "") for t in tags]
            matching_ids = []
            for item_id, tags_json in rows:
                item_tags = json.loads(tags_json) if tags_json else []
                item_tag_keys = [t.lower().replace(" ", "") for t in item_tags]
                if any(tf in item_tag_keys for tf in tag_filter):
                    matching_ids.append(item_id)
            for mid in matching_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO known_valid (app_id, tag_combo, item_id) VALUES (?, ?, ?)",
                    (app_id, combo, mid),
                )
            conn.commit()
            cached_count = len(matching_ids)
            logger.info("Filtered %s items from All cache for tags: %s", cached_count, tags)
        else:
            logger.info("No cached items for tag combo '%s' — harvesting...", combo or "(all)")
            harvest_ids(app_id, tags=tags, max_pages=max_pages,
                        status_callback=status_callback, api_key=api_key)

    count_row = conn.execute(
        "SELECT COUNT(*) FROM known_valid WHERE app_id = ? AND tag_combo = ?",
        (app_id, combo),
    ).fetchone()
    total_known = count_row[0] if count_row else 0

    if total_known == 0:
        logger.error("No items found for app %s with tags %s", app_id, tags)
        conn.close()
        return None

    if exclude_ids:
        exclude_placeholders = ",".join("?" for _ in exclude_ids)
        row = conn.execute(
            f"SELECT item_id FROM known_valid WHERE app_id = ? AND tag_combo = ? AND item_id NOT IN ({exclude_placeholders}) ORDER BY RANDOM() LIMIT 1",
            (app_id, combo) + tuple(exclude_ids),
        ).fetchone()
    else:
        pick = random.randint(0, total_known - 1)
        row = conn.execute(
            "SELECT item_id FROM known_valid WHERE app_id = ? AND tag_combo = ? LIMIT 1 OFFSET ?",
            (app_id, combo, pick),
        ).fetchone()

    if row is None:
        logger.error("No more unique items available for app %s with tags %s", app_id, tags)
        conn.close()
        return None

    cid = row[0]
    logger.info("Picked item: %s", cid)
    conn.close()
    return {"id": cid, "url": f"{ITEM_URL}?id={cid}"}


def clear_cache(app_id: Optional[int] = None):
    conn = _init_cache(app_id)
    if app_id is not None:
        conn.execute("DELETE FROM known_valid")
        conn.execute("DELETE FROM cache")
        logger.info("Cleared cache for app %s", app_id)
    else:
        conn.execute("DELETE FROM known_valid")
        conn.execute("DELETE FROM cache WHERE key LIKE 'last_tags_%'")
        logger.info("Cleared all workshop caches")
    conn.commit()
    conn.close()


def get_cached_count(app_id: int) -> dict:
    conn = _init_cache(app_id)
    rows = conn.execute(
        "SELECT tag_combo, COUNT(*) FROM known_valid WHERE app_id = ? GROUP BY tag_combo",
        (app_id,),
    ).fetchall()
    conn.close()
    result = {}
    for combo, count in rows:
        label = combo if combo else "(all)"
        result[label] = count
    return result


def _init_ignored_cache():
    conn = sqlite3.connect(IGNORED_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ignored_ids (
            app_id INTEGER,
            item_id INTEGER,
            PRIMARY KEY (app_id, item_id)
        )
    """)
    conn.commit()
    return conn


def load_ignored_ids(app_id: Optional[int] = None) -> set[int]:
    conn = _init_ignored_cache()
    if app_id is not None:
        rows = conn.execute(
            "SELECT item_id FROM ignored_ids WHERE app_id = ?", (app_id,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT item_id FROM ignored_ids").fetchall()
    conn.close()
    return {row[0] for row in rows}


def save_ignored_ids(app_id: int, ids: list[int]):
    conn = _init_ignored_cache()
    for iid in ids:
        conn.execute(
            "INSERT OR IGNORE INTO ignored_ids (app_id, item_id) VALUES (?, ?)",
            (app_id, iid),
        )
    conn.commit()
    conn.close()
