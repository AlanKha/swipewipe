"""SwipeWipe - swipe through your iCloud Photos, keep or bin. Windows + macOS.

Run:  python swipewipe.py   (opens http://127.0.0.1:8765 in your browser)

Windows: reads the iCloud for Windows photos folder. Confirmed deletions go to
         the Recycle Bin, and iCloud syncs them to your other devices.
macOS:   reads your Photos library with osxphotos. Confirmed deletions are put
         in a "SwipeWipe - To Delete" album in Photos, ready to delete there.

Nothing is deleted while you swipe; only when you confirm in Review bin.
Progress is saved to iCloud Drive (SwipeWipe/progress.json) when available,
so it is shared between your PC and Mac.
"""
import hashlib
import io
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
PORT = int(os.environ.get("SWIPEWIPE_PORT", "8765"))
HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")

if IS_WIN:
    CACHE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", HERE), "SwipeWipe", "cache")
    ICLOUD_DRIVE = os.path.join(HOME, "iCloudDrive")
else:
    CACHE_DIR = os.path.join(HOME, "Library", "Caches", "SwipeWipe") if IS_MAC else os.path.join(HERE, ".cache")
    ICLOUD_DRIVE = os.path.join(HOME, "Library", "Mobile Documents", "com~apple~CloudDocs")
os.makedirs(CACHE_DIR, exist_ok=True)

if os.environ.get("SWIPEWIPE_STATE"):
    STATE_FILE = os.environ["SWIPEWIPE_STATE"]
elif os.path.isdir(ICLOUD_DRIVE):
    STATE_FILE = os.path.join(ICLOUD_DRIVE, "SwipeWipe", "progress.json")
else:
    STATE_FILE = os.path.join(HERE, "progress.json")
os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".webm"}
RAW_OK = {".png", ".gif", ".webp"}  # served as-is for the big card view

try:
    from PIL import Image, ImageOps
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

NOWINDOW = 0x08000000 if IS_WIN else 0


# ---- progress (shared between machines via iCloud Drive) ---------------------
# Keys are "<filename without (1)>|<size in bytes>#<copy number>", which is the same
# for a photo on Windows and Mac. Keys starting with "@" are bookkeeping (months
# marked done). Every write re-reads the file first, so two machines merge.
state_lock = threading.Lock()


def read_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def update_state(changes):
    """changes: {key: value or None (=remove)}. Merges with what's on disk."""
    with state_lock:
        st = read_state()
        for k, v in changes.items():
            if v is None:
                st.pop(k, None)
            else:
                st[k] = v
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, STATE_FILE)
        return st


COPY_RE = re.compile(r"\((\d+)\)(?=\.[^.]+$)")


def photo_key(name, size, copy_no):
    return f"{COPY_RE.sub('', name).lower()}|{size}#{copy_no}"


def finish_items(items):
    """Add month, duplicate group and sync key; sort newest first."""
    groups, seen = {}, {}
    for i in items:
        taken = i.pop("taken")
        i["dated"] = bool(taken)
        i["mtime"] = taken or i["mtime"]
        i["month"] = datetime.fromtimestamp(i["mtime"]).strftime("%Y-%m") if taken else "undated"
        base = COPY_RE.sub("", i["name"]).lower()
        groups.setdefault((i["size"], taken) if taken else (i["size"], base), []).append(i)
        m = COPY_RE.search(i["name"])
        i["_copy"] = int(m.group(1)) if m else 0
    for i in sorted(items, key=lambda x: (x["_copy"], x["id"])):
        k = photo_key(i["name"], i["size"], i["_copy"])
        while k in seen:  # same name+size twice (Mac duplicates) -> next copy number
            i["_copy"] += 1
            k = photo_key(i["name"], i["size"], i["_copy"])
        seen[k] = True
        i["key"] = k
        del i["_copy"]
    gid = 0
    for members in groups.values():
        if len(members) > 1:
            gid += 1
            for m in members:
                m["dup"] = gid
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


# ---- Windows: iCloud for Windows folder ---------------------------------------
class WindowsLibrary:
    platform = "windows"

    def __init__(self):
        self.dir = os.environ.get("SWIPEWIPE_DIR", os.path.join(HOME, "Pictures", "iCloud Photos", "Photos"))
        if not os.path.isdir(self.dir):
            sys.exit(f"Photo folder not found: {self.dir}\nSet SWIPEWIPE_DIR to your iCloud Photos folder.")
        self.dates_file = os.path.join(CACHE_DIR, "dates.json")
        try:
            with open(self.dates_file, encoding="utf-8") as f:
                self.dates = json.load(f)
        except (OSError, ValueError):
            self.dates = {}

    def describe(self):
        return self.dir

    def fill_dates(self, names):
        """Real 'Date taken' via the Windows shell (works on cloud-only files, no download)."""
        missing = [n for n in names if n not in self.dates]
        if not missing:
            return
        lst = os.path.join(CACHE_DIR, "names.txt")
        with open(lst, "w", encoding="utf-8") as f:
            f.write("\n".join(missing))
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 os.path.join(HERE, "dates.ps1"), "-Dir", self.dir, "-List", lst],
                capture_output=True, timeout=600, creationflags=NOWINDOW,
            ).stdout.decode("utf-8", "replace")
        except Exception as e:
            print("Date lookup failed, falling back to file dates:", e)
            return
        for line in out.splitlines():
            name, _, ts = line.strip("﻿").partition("\t")
            if ts.strip().isdigit():
                self.dates[name] = int(ts)
        for n in missing:
            self.dates.setdefault(n, None)
        with open(self.dates_file, "w", encoding="utf-8") as f:
            json.dump(self.dates, f)

    def list(self):
        items = []
        with os.scandir(self.dir) as it:
            for e in it:
                if not e.is_file():
                    continue
                ext = os.path.splitext(e.name)[1].lower()
                kind = "image" if ext in IMAGE_EXT else "video" if ext in VIDEO_EXT else None
                if kind:
                    st = e.stat()
                    items.append({"id": e.name, "name": e.name, "kind": kind, "ext": ext,
                                  "size": st.st_size, "mtime": st.st_mtime})
        self.fill_dates([i["name"] for i in items])
        for i in items:
            i["taken"] = self.dates.get(i["name"])
        return finish_items(items)

    def path(self, pid):
        if not pid or "/" in pid or "\\" in pid or pid in (".", ".."):
            return None
        p = os.path.join(self.dir, pid)
        return p if os.path.isfile(p) else None

    def heic_to_jpeg(self, src, dst, max_px):
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             os.path.join(HERE, "heic2jpg.ps1"), "-In", src, "-Out", dst, "-Max", str(max_px)],
            capture_output=True, timeout=120, creationflags=NOWINDOW,
        )
        if r.returncode != 0:
            raise RuntimeError("HEIC convert failed: " + r.stderr.decode("utf-8", "replace")[-300:])

    def remove(self, ids):
        """Send to the Recycle Bin. Returns ids that are gone."""
        import ctypes
        import ctypes.wintypes as wt

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", wt.HWND), ("wFunc", wt.UINT), ("pFrom", wt.LPCWSTR), ("pTo", wt.LPCWSTR),
                ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wt.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wt.LPCWSTR),
            ]
        done = []
        for pid in ids:
            p = self.path(pid)
            if not p:
                continue
            op = SHFILEOPSTRUCTW()
            op.wFunc = 3  # FO_DELETE
            op.pFrom = p + "\0\0"
            op.fFlags = 0x40 | 0x10 | 0x4 | 0x400  # ALLOWUNDO | NOCONFIRMATION | SILENT | NOERRORUI
            rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
            if rc == 0 and not op.fAnyOperationsAborted and not os.path.exists(p):
                done.append(pid)
        return done


# ---- macOS: Photos library via osxphotos --------------------------------------
ALBUM = "SwipeWipe - To Delete"


class MacLibrary:
    platform = "mac"

    def __init__(self):
        try:
            import osxphotos
        except ImportError:
            sys.exit("osxphotos is needed on macOS:  python3 -m pip install osxphotos")
        print("Reading your Photos library (first run can take a minute)...")
        try:
            self.db = osxphotos.PhotosDB()
        except Exception as e:
            sys.exit(f"Couldn't open the Photos library: {e}\n"
                     "Give your terminal Full Disk Access: System Settings > Privacy & Security > Full Disk Access.")
        self.paths = {}

    def describe(self):
        return self.db.library_path

    def list(self):
        items, self.paths = [], {}
        for p in self.db.photos():
            if p.intrash:
                continue
            name = p.original_filename or p.filename
            ext = os.path.splitext(name)[1].lower()
            # Prefer the edited version, then the original, then Photos' own preview
            # (with "Optimize Mac Storage" the original may only be in iCloud).
            src = p.path_edited or p.path
            if not src:
                ders = [d for d in (p.path_derivatives or []) if os.path.exists(d)]
                src = ders[0] if ders else None
            self.paths[p.uuid] = src
            try:
                taken = p.date.timestamp() if p.date else None
            except Exception:
                taken = None
            items.append({
                "id": p.uuid, "name": name, "kind": "video" if p.ismovie else "image", "ext": ext,
                "size": p.original_filesize or 0, "mtime": taken or 0, "taken": taken,
                "local": bool(src),
            })
        return finish_items(items)

    def path(self, pid):
        p = self.paths.get(pid)
        return p if p and os.path.isfile(p) else None

    def heic_to_jpeg(self, src, dst, max_px):
        r = subprocess.run(["sips", "-s", "format", "jpeg", "-Z", str(max_px), src, "--out", dst],
                           capture_output=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError("sips failed: " + r.stderr.decode("utf-8", "replace")[-300:])

    def remove(self, ids):
        """Photos can't be deleted by script, so add them to an album to delete in Photos."""
        ids = [i for i in ids if re.fullmatch(r"[0-9A-Fa-f-]+", i)]
        if not ids:
            return []
        id_list = ", ".join(f'"{i}/L0/001"' for i in ids)
        script = f'''
        tell application "Photos"
            if not (exists album "{ALBUM}") then make new album named "{ALBUM}"
            set theItems to {{}}
            repeat with i in {{{id_list}}}
                set end of theItems to media item id (contents of i)
            end repeat
            add theItems to album "{ALBUM}"
        end tell'''
        r = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=300)
        if r.returncode != 0:
            print("Photos album add failed:", r.stderr.decode("utf-8", "replace"))
            return []
        return ids


LIB = None


# ---- image rendering ---------------------------------------------------------
def rendered(path, max_px):
    """Cached JPEG bytes of the image, upright and downscaled."""
    st = os.stat(path)
    key = hashlib.sha1(f"{path}|{st.st_mtime}|{st.st_size}|{max_px}".encode()).hexdigest()
    cp = os.path.join(CACHE_DIR, key + ".jpg")
    if not os.path.exists(cp):
        ext = os.path.splitext(path)[1].lower()
        if ext in (".heic", ".heif") or not HAVE_PIL:
            LIB.heic_to_jpeg(path, cp, max_px)  # Windows WIC / macOS sips
        else:
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im)
                if im.mode in ("RGBA", "LA", "P"):
                    im = im.convert("RGBA")
                    bg = Image.new("RGB", im.size, (24, 24, 27))
                    bg.paste(im, mask=im.split()[-1])
                    im = bg
                else:
                    im = im.convert("RGB")
                im.thumbnail((max_px, max_px))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=85)
            with open(cp, "wb") as f:
                f.write(buf.getvalue())
    with open(cp, "rb") as f:
        return f.read()


# ---- HTTP ------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_bytes(self, data, ctype, status=200, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj, status=200):
        self.send_bytes(json.dumps(obj).encode(), "application/json", status)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self.send_bytes(f.read(), "text/html; charset=utf-8")
        if u.path == "/api/photos":
            photos = LIB.list()
            with state_lock:
                st = read_state()
            return self.send_json({"photos": photos, "state": st, "platform": LIB.platform,
                                   "album": ALBUM, "progressFile": STATE_FILE})
        if u.path.startswith("/media/"):
            return self.serve_media(unquote(u.path[len("/media/"):]), parse_qs(u.query))
        self.send_bytes(b"not found", "text/plain", 404)

    def serve_media(self, pid, q):
        path = LIB.path(pid)
        if not path:
            return self.send_bytes(b"not available on this computer", "text/plain", 404)
        ext = os.path.splitext(path)[1].lower()
        size = q.get("s", ["full"])[0]
        try:
            if ext in IMAGE_EXT and (size == "thumb" or ext not in RAW_OK) and (HAVE_PIL or IS_MAC or ext in (".heic", ".heif")):
                data = rendered(path, 420 if size == "thumb" else 1800)
                return self.send_bytes(data, "image/jpeg", extra={"Cache-Control": "max-age=86400"})
            return self.serve_file(path, ext)
        except (ConnectionError, BrokenPipeError):
            pass
        except Exception as e:  # unreadable/corrupt file
            try:
                self.send_bytes(str(e).encode(), "text/plain", 500)
            except Exception:
                pass

    def serve_file(self, path, ext):
        ctype = {".mov": "video/mp4"}.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
        total = os.path.getsize(path)
        start, end, status = 0, total - 1, 200
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].split(",")[0].partition("-")
            if a:
                start = int(a)
                end = int(b) if b else total - 1
            else:
                start = max(0, total - int(b))
            end = min(end, total - 1)
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_POST(self):
        u = urlparse(self.path)
        body = self.read_json()
        if u.path == "/api/decide":
            key, d = body.get("key"), body.get("decision")
            if not isinstance(key, str):
                return self.send_json({"ok": False}, 400)
            if key.startswith("@"):
                val = "1" if d == "1" else None
            else:
                val = d if d in ("keep", "delete") else None
            update_state({key: val})
            return self.send_json({"ok": True})
        if u.path == "/api/commit":
            # body.items: [{id, key}] - only items still marked "delete" are touched
            st = read_state()
            wanted = [it for it in body.get("items", []) if st.get(it.get("key")) == "delete"]
            done_ids = set(LIB.remove([it["id"] for it in wanted]))
            done = [it for it in wanted if it["id"] in done_ids]
            # Windows: file is gone -> forget it. Mac: in the album -> remember as "sent".
            update_state({it["key"]: (None if IS_WIN else "sent") for it in done})
            return self.send_json({"done": [it["id"] for it in done],
                                   "failed": [it["id"] for it in wanted if it["id"] not in done_ids],
                                   "mode": "recycle" if IS_WIN else "album"})
        self.send_bytes(b"not found", "text/plain", 404)


def main():
    global LIB
    if IS_WIN:
        LIB = WindowsLibrary()
    elif IS_MAC:
        LIB = MacLibrary()
    else:
        sys.exit("SwipeWipe runs on Windows (iCloud for Windows) or macOS (Photos).")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"SwipeWipe running at {url}  (Ctrl+C to quit)\nPhotos:   {LIB.describe()}\nProgress: {STATE_FILE}")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
