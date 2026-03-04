"""Movie Downloader — FastAPI backend."""
import asyncio
import json
import re
import shutil
import time
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT                = Path(__file__).parent.parent
FRONTEND            = ROOT / "frontend"
CONFIG_PATH         = ROOT / "config.json"
PENDING_PATH        = ROOT / "pending.json"
RATINGS_CACHE_PATH  = ROOT / "ratings_cache.json"

OMDB_BASE = "https://www.omdbapi.com"
RT_SEARCH = "https://www.rottentomatoes.com/api/private/v2.0/movies"

DEFAULT_CONFIG = {
    "utorrent":      {"url": "http://127.0.0.1:8080", "username": "admin", "password": ""},
    "opensubtitles": {"api_key": "", "username": "", "password": ""},
    "movies_folder": "",
    "auto_copy":     True,
    "omdb_api_key":  "",
}

app = FastAPI()
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")


@app.on_event("startup")
async def on_startup():
    asyncio.create_task(_bg_prefetch_ratings())


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return DEFAULT_CONFIG.copy()

def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")

def load_pending() -> dict:
    if PENDING_PATH.exists():
        return json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    return {}

def save_pending(p: dict):
    PENDING_PATH.write_text(json.dumps(p, indent=2), encoding="utf-8")

def fmt_size(size_bytes: int) -> str:
    if not size_bytes:
        return "?"
    gb = size_bytes / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.1f} GB"
    return f"{size_bytes / (1024 ** 2):.0f} MB"

def parse_quality(name: str) -> str:
    n = name.upper()
    if any(x in n for x in ("2160P", "4K UHD", "4K", "UHD")):
        return "4K"
    if any(x in n for x in ("1080P", "1080I")):
        return "1080p"
    if any(x in n for x in ("720P", "720I")):
        return "720p"
    if "480P" in n:
        return "480p"
    return "SD"

def parse_source(name: str) -> str:
    n = name.upper()
    if any(x in n for x in ("BLURAY", "BLU-RAY", "BDRIP", "BDREMUX")):
        return "BluRay"
    if any(x in n for x in ("WEB-DL", "WEBDL")):
        return "WEB-DL"
    if "WEBRIP" in n:
        return "WEBRip"
    if "HDTV" in n:
        return "HDTV"
    if any(x in n for x in ("DVDRIP", "DVDR")):
        return "DVDRip"
    if any(x in n for x in ("HDCAM", "HDCAM")):
        return "CAM"
    return ""

def clean_title(name: str) -> str:
    """Extract human-readable title from torrent name."""
    # Replace dots/underscores with spaces
    s = re.sub(r"[._]", " ", name)
    # Cut off at year or quality keyword
    m = re.search(
        r"\b(19\d{2}|20\d{2}|1080[pi]|720[pi]|2160p|4K|UHD|BluRay|WEB[-. ]?DL|WEBRip|HDTV|DVDRip|HEVC|x264|x265|H\.?264|H\.?265|AAC|DTS|REMUX)\b",
        s, re.IGNORECASE,
    )
    title = s[: m.start()].strip() if m else s.strip()
    return title if title else name

TRACKERS = (
    "&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337"
    "&tr=udp%3A%2F%2Ftracker.openbittorrent.com%3A6969"
    "&tr=udp%3A%2F%2Fopen.stealth.si%3A80"
)

def make_magnet(info_hash: str, name: str) -> str:
    return f"magnet:?xt=urn:btih:{info_hash}&dn={quote(name)}{TRACKERS}"


# ── Serve frontend ────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return FileResponse(str(FRONTEND / "index.html"))


# ── Search ────────────────────────────────────────────────────────────────────

@app.get("/api/search")
async def search(q: str):
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.get(
                "https://apibay.org/q.php",
                params={"q": q, "cat": 200},
                headers={"User-Agent": "Mozilla/5.0"},
            )
            r.raise_for_status()
            raw = r.json()
        except httpx.TimeoutException:
            raise HTTPException(504, "TPB API timed out")
        except Exception as e:
            raise HTTPException(502, f"TPB API error: {e}")

    results = []
    for t in raw:
        if t.get("id") == "0":
            continue
        size = int(t.get("size", 0))
        results.append({
            "id":        t["id"],
            "name":      t["name"],
            "title":     clean_title(t["name"]),
            "seeders":   int(t.get("seeders", 0)),
            "leechers":  int(t.get("leechers", 0)),
            "size":      fmt_size(size),
            "size_bytes": size,
            "quality":   parse_quality(t["name"]),
            "source":    parse_source(t["name"]),
            "magnet":    make_magnet(t["info_hash"], t["name"]),
            "info_hash": t["info_hash"].upper(),
            "imdb":      t.get("imdb", ""),
            "uploader":  t.get("username", ""),
        })

    results.sort(key=lambda x: x["seeders"], reverse=True)
    return {"results": results, "source": "piratebay"}


# ── uTorrent ──────────────────────────────────────────────────────────────────

async def ut_api(params: dict) -> dict:
    """Call uTorrent WebUI API. Uses a single client so the GUID session
    cookie from token.html is automatically included in the API request."""
    cfg  = load_config()
    ut   = cfg["utorrent"]
    base = ut["url"]
    auth = (ut["username"], ut["password"]) if ut.get("password") else None
    headers = {"Referer": f"{base}/gui/"}

    try:
        async with httpx.AsyncClient(auth=auth, timeout=10, headers=headers) as client:
            # Step 1: fetch token (sets GUID cookie in client's jar)
            r = await client.get(f"{base}/gui/token.html")
            m = re.search(r"<div[^>]+id=['\"]token['\"][^>]*>([^<]+)", r.text)
            if not m:
                raise HTTPException(500, "Could not read uTorrent token")
            token = m.group(1).strip()

            # Step 2: API call with same client (GUID cookie preserved)
            r = await client.get(f"{base}/gui/", params={"token": token, **params})
            r.raise_for_status()
            return r.json()
    except httpx.ConnectError:
        raise HTTPException(503, "Cannot connect to uTorrent — make sure it's running with WebUI enabled (Options → Preferences → Web UI)")


@app.post("/api/download")
async def download(body: dict):
    magnet = body.get("magnet")
    if not magnet:
        raise HTTPException(400, "No magnet link provided")

    await ut_api({"action": "add-url", "s": magnet})

    info_hash = body.get("info_hash", "").upper()
    if info_hash:
        pending = load_pending()
        pending[info_hash] = {
            "name":     body.get("name", ""),
            "title":    body.get("title", ""),
            "imdb":     body.get("imdb", ""),
            "added_at": time.time(),
            "copied":   False,
        }
        save_pending(pending)

    return {"ok": True}


@app.get("/api/torrents")
async def list_torrents():
    try:
        data = await ut_api({"list": "1"})
    except Exception as e:
        return {"torrents": [], "error": str(e)}

    pending = load_pending()
    torrents = []

    for t in data.get("torrents", []):
        if len(t) < 5:
            continue
        hash_, status, name, size, progress = t[0], t[1], t[2], t[3], t[4]
        h = hash_.upper()
        done = (progress >= 1000)   # 1000 = 100% in uTorrent's tenths-of-percent format

        p = pending.get(h, {})
        # Trigger auto-copy+subtitle once when download completes
        if h in pending and done and not p.get("auto_started") and load_config().get("auto_copy"):
            pending[h]["auto_started"] = True
            save_pending(pending)
            asyncio.create_task(auto_copy_and_subtitle(h))

        torrents.append({
            "hash":       h,
            "name":       name,
            "size":       fmt_size(int(size)) if size else "?",
            "progress":   round(progress / 10, 1),
            "done":       done,
            "is_tracked": h in pending,
            "copied":     p.get("copied", False),
            "auto_status": p.get("auto_status", ""),   # copying|subtitle|done|error
            "auto_note":   p.get("auto_note", ""),
        })

    torrents.sort(key=lambda x: (not x["is_tracked"], not x["done"]))
    return {"torrents": torrents}


def _set_auto_status(info_hash: str, status: str, note: str = ""):
    pending = load_pending()
    if info_hash in pending:
        pending[info_hash]["auto_status"] = status
        if note:
            pending[info_hash]["auto_note"] = note
        save_pending(pending)


async def auto_copy_and_subtitle(info_hash: str):
    """Background task: copy completed torrent → download best Hebrew subtitle."""

    _set_auto_status(info_hash, "copying")

    # ── Step 1: Copy ──────────────────────────────────────────────────────
    cfg           = load_config()
    movies_folder = Path(cfg.get("movies_folder", ""))
    if not movies_folder or not movies_folder.exists():
        _set_auto_status(info_hash, "error", "Movies folder not set or unreachable")
        return

    try:
        data      = await ut_api({"action": "getprops", "hash": info_hash})
        props     = data.get("props", [])
        save_path = Path(props[0].get("path", "")) if props else Path()
    except Exception as e:
        _set_auto_status(info_hash, "error", f"uTorrent: {e}")
        return

    copied = []
    try:
        if save_path.is_file() and save_path.suffix.lower() in VIDEO_EXT:
            dest = movies_folder / save_path.name
            if not dest.exists():
                shutil.copy2(save_path, dest)
            copied.append(dest)
        elif save_path.is_dir():
            for f in save_path.rglob("*"):
                if f.is_file() and f.suffix.lower() in VIDEO_EXT:
                    dest = movies_folder / f.relative_to(save_path.parent)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if not dest.exists():
                        shutil.copy2(f, dest)
                    copied.append(dest)
    except Exception as e:
        _set_auto_status(info_hash, "error", f"Copy failed: {e}")
        return

    if not copied:
        _set_auto_status(info_hash, "error", "No video files found to copy")
        return

    pending = load_pending()
    if info_hash in pending:
        pending[info_hash]["copied"] = True
        save_pending(pending)

    # Determine dest folder (for subtitle save)
    dest_folder = copied[0].parent

    # ── Step 2: Subtitle ──────────────────────────────────────────────────
    api_key = cfg.get("opensubtitles", {}).get("api_key", "")
    if not api_key:
        _set_auto_status(info_hash, "done", "✓ Copied (no subtitle API key configured)")
        return

    _set_auto_status(info_hash, "subtitle")

    pending   = load_pending()
    raw_name  = pending.get(info_hash, {}).get("name", "") or save_path.name
    title     = clean_title(raw_name)
    headers   = os_headers(cfg)

    # Search (try IMDB id first if available, then title)
    subs = []
    try:
        imdb_id = pending.get(info_hash, {}).get("imdb", "")
        if imdb_id:
            numeric = re.sub(r"\D", "", imdb_id)
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(f"{OS_BASE}/subtitles",
                                params={"languages": "he", "imdb_id": numeric},
                                headers=headers)
            if r.status_code == 200:
                subs = r.json().get("data", [])
        if not subs:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(f"{OS_BASE}/subtitles",
                                params={"languages": "he", "query": title},
                                headers=headers)
            if r.status_code == 200:
                subs = r.json().get("data", [])
    except Exception as e:
        _set_auto_status(info_hash, "done", f"✓ Copied — subtitle search failed: {e}")
        return

    if not subs:
        _set_auto_status(info_hash, "done", "✓ Copied — no Hebrew subtitles found")
        return

    best    = max(subs, key=lambda x: x.get("attributes", {}).get("download_count", 0))
    attrs   = best.get("attributes", {})
    files   = attrs.get("files", [{}])
    file_id = files[0].get("file_id") if files else None

    if not file_id:
        _set_auto_status(info_hash, "done", "✓ Copied — could not get subtitle file ID")
        return

    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{OS_BASE}/download",
                             json={"file_id": file_id},
                             headers=os_headers(cfg, json_body=True))
            if r.status_code != 200:
                raise Exception(r.text)
            link    = r.json()["link"]
            content = (await c.get(link)).content

        sub_path = dest_folder / (title + ".he.srt")
        sub_path.write_bytes(content)
        _set_auto_status(info_hash, "done", f"✓ Copied + subtitles saved")
    except Exception as e:
        _set_auto_status(info_hash, "done", f"✓ Copied — subtitle download failed: {e}")


@app.post("/api/copy/{info_hash}")
async def copy_torrent(info_hash: str):
    cfg           = load_config()
    movies_folder = Path(cfg.get("movies_folder", ""))
    if not movies_folder or not movies_folder.exists():
        raise HTTPException(400, "Movies folder not configured or not found. Set it in Settings.")

    try:
        data  = await ut_api({"action": "getprops", "hash": info_hash})
        props = data.get("props", [])
        if not props:
            raise HTTPException(404, "Torrent not found in uTorrent")
        save_path = Path(props[0].get("path", ""))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"uTorrent error: {e}")

    copied = []

    if save_path.is_file():
        if save_path.suffix.lower() in VIDEO_EXT:
            dest = movies_folder / save_path.name
            if not dest.exists():
                shutil.copy2(save_path, dest)
            copied.append(str(dest))
    elif save_path.is_dir():
        for f in save_path.rglob("*"):
            if f.is_file() and f.suffix.lower() in VIDEO_EXT:
                dest = movies_folder / f.relative_to(save_path.parent)
                dest.parent.mkdir(parents=True, exist_ok=True)
                if not dest.exists():
                    shutil.copy2(f, dest)
                copied.append(str(dest))

    if not copied:
        raise HTTPException(404, "No video files found to copy (supported: mkv, mp4, avi, mov)")

    pending = load_pending()
    if info_hash in pending:
        pending[info_hash]["copied"] = True
        save_pending(pending)

    return {"ok": True, "copied": copied}


# ── OpenSubtitles ─────────────────────────────────────────────────────────────

OS_BASE = "https://api.opensubtitles.com/api/v1"

def os_headers(cfg: dict, json_body: bool = False) -> dict:
    h = {
        "Api-Key":    cfg.get("opensubtitles", {}).get("api_key", ""),
        "User-Agent": "MovieDL v1.0",
    }
    if json_body:
        h["Content-Type"] = "application/json"
    token = cfg.get("opensubtitles", {}).get("token", "")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


@app.post("/api/opensubtitles/login")
async def os_login():
    cfg  = load_config()
    cred = cfg.get("opensubtitles", {})
    if not cred.get("api_key"):
        raise HTTPException(400, "OpenSubtitles API key not configured")
    if not cred.get("username") or not cred.get("password"):
        raise HTTPException(400, "OpenSubtitles username/password not configured")

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"{OS_BASE}/login",
            json={"username": cred["username"], "password": cred["password"]},
            headers=os_headers(cfg, json_body=True),
        )
        if r.status_code != 200:
            raise HTTPException(r.status_code, f"Login failed: {r.text}")
        token = r.json().get("token", "")

    cfg["opensubtitles"]["token"] = token
    save_config(cfg)
    return {"ok": True}


@app.get("/api/subtitles")
async def search_subtitles(q: str, imdb_id: str = ""):
    cfg = load_config()
    if not cfg.get("opensubtitles", {}).get("api_key"):
        raise HTTPException(400, "OpenSubtitles API key not configured. Add it in Settings.")

    headers = os_headers(cfg)

    async def _search(params: dict):
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{OS_BASE}/subtitles", params=params, headers=headers)
        if r.status_code != 200:
            raise HTTPException(r.status_code, f"OpenSubtitles: {r.text}")
        return r.json().get("data", [])

    # Try by IMDB id first; fall back to title query if no results
    data = []
    numeric_imdb = re.sub(r"\D", "", imdb_id) if imdb_id else ""
    if numeric_imdb:
        data = await _search({"languages": "he", "imdb_id": numeric_imdb})
    if not data:
        data = await _search({"languages": "he", "query": q})

    subs = []
    for sub in data:
        attrs = sub.get("attributes", {})
        files = attrs.get("files", [{}])
        subs.append({
            "id":                sub["id"],
            "file_id":           files[0].get("file_id") if files else None,
            "filename":          files[0].get("file_name", "subtitle.srt") if files else "subtitle.srt",
            "release":           attrs.get("release", "?"),
            "downloads":         attrs.get("download_count", 0),
            "year":              attrs.get("feature_details", {}).get("year"),
            "uploader":          attrs.get("uploader", {}).get("name", "?"),
            "fps":               attrs.get("fps"),
            "hearing_impaired":  attrs.get("hearing_impaired", False),
        })

    subs.sort(key=lambda x: x["downloads"], reverse=True)
    return {"subtitles": subs}


@app.post("/api/subtitle/download")
async def download_subtitle(body: dict):
    cfg = load_config()
    if not cfg.get("opensubtitles", {}).get("api_key"):
        raise HTTPException(400, "OpenSubtitles API key not configured")

    file_id  = body.get("file_id")
    save_dir = body.get("save_dir") or cfg.get("movies_folder") or ""
    filename = (body.get("filename") or "subtitle.he.srt").strip()
    if not filename.lower().endswith(".srt"):
        filename += ".srt"

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{OS_BASE}/download",
            json={"file_id": file_id},
            headers=os_headers(cfg, json_body=True),
        )
        if r.status_code != 200:
            raise HTTPException(r.status_code, f"OpenSubtitles: {r.text}")

        resp_data  = r.json()
        link       = resp_data["link"]
        remaining  = resp_data.get("remaining", "?")
        content    = (await client.get(link)).content

    if save_dir:
        path = Path(save_dir) / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return {"ok": True, "path": str(path), "remaining_quota": remaining}
    else:
        return StreamingResponse(
            iter([content]),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )


# ── Ratings ───────────────────────────────────────────────────────────────────

def _load_ratings_cache() -> dict:
    if RATINGS_CACHE_PATH.exists():
        try:
            return json.loads(RATINGS_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def _save_ratings_cache(cache: dict):
    RATINGS_CACHE_PATH.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")

def _extract_year(name: str) -> str:
    m = re.search(r'\b(19\d{2}|20\d{2})\b', name)
    return m.group(1) if m else ""

@app.get("/api/movie-rating")
async def movie_rating(title: str):
    """Fetch Tomatometer (🍅) + Popcornmeter (🍿) for a movie title/folder name."""
    clean = clean_title(title)
    year  = _extract_year(title)
    key   = clean.lower()

    cache = _load_ratings_cache()
    if key in cache:
        return cache[key]

    result: dict = {"tomatometer": None, "imdb": None}
    cfg = load_config()

    # ── OMDB: Tomatometer (🍅 critics) + IMDB rating (⭐ audience) ──────────
    omdb_key = cfg.get("omdb_api_key", "")
    if not omdb_key:
        cache[key] = result
        _save_ratings_cache(cache)
        return result

    try:
        params: dict = {"apikey": omdb_key, "t": clean, "type": "movie"}
        if year:
            params["y"] = year
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(OMDB_BASE, params=params)
            data = r.json()
            if data.get("Response") == "True":
                for rating in data.get("Ratings", []):
                    src, val = rating.get("Source", ""), rating.get("Value", "")
                    if src == "Rotten Tomatoes":
                        result["tomatometer"] = val
                    elif src == "Internet Movie Database":
                        result["imdb"] = val
    except Exception:
        pass

    cache[key] = result
    _save_ratings_cache(cache)
    return result


async def _bg_prefetch_ratings():
    """On startup: fetch and cache ratings for all movies not yet in the cache."""
    await asyncio.sleep(3)  # let server fully start
    cfg = load_config()
    if not cfg.get("omdb_api_key"):
        return
    root = Path(cfg.get("movies_folder", ""))
    if not root.exists():
        return
    cache = _load_ratings_cache()
    for p in sorted(root.iterdir(), key=lambda x: x.name.lower()):
        if not p.is_dir():
            continue
        key = clean_title(p.name).lower()
        if key not in cache:
            try:
                await movie_rating(title=p.name)
                await asyncio.sleep(0.4)  # stay under OMDB rate limit
            except Exception:
                pass


@app.post("/api/refresh-ratings")
async def refresh_ratings():
    """Manually trigger a background re-fetch of all movie ratings."""
    asyncio.create_task(_bg_prefetch_ratings())
    return {"ok": True}


# ── Movies folders ────────────────────────────────────────────────────────────

VIDEO_EXT    = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv", ".flv"}
SUBTITLE_EXT = {".srt", ".sub", ".ass", ".ssa"}

@app.get("/api/movies-folders")
def list_movies_folders():
    cfg  = load_config()
    root = Path(cfg.get("movies_folder", ""))
    if not root or not root.exists():
        return {"folders": [], "root": str(root), "accessible": False}

    folders = []
    for p in sorted(root.iterdir(), key=lambda x: x.name.lower()):
        if not p.is_dir():
            continue
        try:
            files        = list(p.iterdir())
            has_video    = any(f.suffix.lower() in VIDEO_EXT for f in files)
            has_subtitle = any(f.suffix.lower() in SUBTITLE_EXT for f in files)
        except PermissionError:
            has_video = has_subtitle = False
        folders.append({
            "name":         p.name,
            "path":         str(p),
            "has_video":    has_video,
            "has_subtitle": has_subtitle,
        })

    return {"folders": folders, "root": str(root), "accessible": True}


# ── Config ────────────────────────────────────────────────────────────────────

@app.get("/api/config")
def get_cfg():
    cfg = load_config()
    if cfg["utorrent"].get("password"):
        cfg["utorrent"]["password"] = "••••••••"
    if cfg["opensubtitles"].get("password"):
        cfg["opensubtitles"]["password"] = "••••••••"
    return cfg

@app.post("/api/config")
def set_cfg(body: dict):
    cfg = load_config()
    MASK = "••••••••"
    for key, val in body.items():
        if isinstance(val, dict):
            cfg.setdefault(key, {})
            for k, v in val.items():
                if v != MASK:
                    cfg[key][k] = v
        else:
            cfg[key] = val
    save_config(cfg)
    return {"ok": True}
