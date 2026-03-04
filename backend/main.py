"""Movie Downloader — FastAPI backend."""
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

ROOT         = Path(__file__).parent.parent
FRONTEND     = ROOT / "frontend"
CONFIG_PATH  = ROOT / "config.json"
PENDING_PATH = ROOT / "pending.json"

DEFAULT_CONFIG = {
    "utorrent":      {"url": "http://127.0.0.1:8080", "username": "admin", "password": ""},
    "opensubtitles": {"api_key": "", "username": "", "password": ""},
    "movies_folder": "",
    "auto_copy":     True,
}

app = FastAPI()
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")


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

async def _ut_token(cfg: dict) -> tuple[str, str, tuple | None]:
    ut   = cfg["utorrent"]
    base = ut["url"]
    auth = (ut["username"], ut["password"]) if ut.get("password") else None
    try:
        async with httpx.AsyncClient(auth=auth, timeout=5) as client:
            r = await client.get(f"{base}/gui/token.html")
            m = re.search(r"<div[^>]+id=['\"]token['\"][^>]*>([^<]+)", r.text)
            if not m:
                raise HTTPException(500, "Could not read uTorrent token")
            return base, m.group(1).strip(), auth
    except httpx.ConnectError:
        raise HTTPException(503, "Cannot connect to uTorrent — make sure it's running with WebUI enabled (Options → Preferences → Web UI)")

async def ut_api(params: dict) -> dict:
    cfg = load_config()
    base, token, auth = await _ut_token(cfg)
    async with httpx.AsyncClient(auth=auth, timeout=10) as client:
        r = await client.get(f"{base}/gui/", params={"token": token, **params})
        r.raise_for_status()
        return r.json()


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
        done = bool(status & 32)

        if h in pending and done and load_config().get("auto_copy"):
            _try_auto_copy(h, name)

        torrents.append({
            "hash":       h,
            "name":       name,
            "size":       fmt_size(int(size)) if size else "?",
            "progress":   round(progress / 10, 1),
            "done":       done,
            "is_tracked": h in pending,
            "copied":     pending.get(h, {}).get("copied", False),
        })

    torrents.sort(key=lambda x: (not x["is_tracked"], not x["done"]))
    return {"torrents": torrents}


def _try_auto_copy(info_hash: str, name: str):
    """Attempt auto-copy synchronously (best-effort)."""
    cfg = load_config()
    movies_folder = Path(cfg.get("movies_folder", ""))
    if not movies_folder or not movies_folder.exists():
        return
    pending = load_pending()
    if pending.get(info_hash, {}).get("copied"):
        return
    # We'll trigger via /api/copy endpoint from the UI instead
    # (uTorrent getprops is async; mark for copy on next poll)
    pending[info_hash]["needs_copy"] = True
    save_pending(pending)


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

    VIDEO_EXT = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv", ".flv"}
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

def os_headers(cfg: dict) -> dict:
    h = {
        "Api-Key":      cfg.get("opensubtitles", {}).get("api_key", ""),
        "User-Agent":   "MovieDL v1.0",
        "Content-Type": "application/json",
    }
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
            headers=os_headers(cfg),
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

    params: dict = {"languages": "he"}
    if imdb_id:
        numeric = re.sub(r"\D", "", imdb_id)
        if numeric:
            params["imdb_id"] = numeric
    if "imdb_id" not in params:
        params["query"] = q

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{OS_BASE}/subtitles", params=params, headers=os_headers(cfg))

    if r.status_code != 200:
        raise HTTPException(r.status_code, f"OpenSubtitles: {r.text}")

    subs = []
    for sub in r.json().get("data", []):
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
            headers=os_headers(cfg),
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
