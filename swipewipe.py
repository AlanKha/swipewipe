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
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

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
# Served as the untouched original for the big card view, so right-click > Save/Copy
# image gives you the real file. (HEIC etc. must be converted for the browser.)
RAW_OK = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

try:
    from PIL import Image, ImageOps
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

NOWINDOW = 0x08000000 if IS_WIN else 0


# ---- progress (shared between machines via iCloud Drive) ---------------------
# Keys are "<filename without (1)>|<size in bytes>#<copy number>", which is the same
# for a photo on Windows and Mac. Keys starting with "@" are bookkeeping (months
# marked done).
#
# iCloud Drive can't cope with a file being rewritten on every swipe: it splits it
# into conflict copies ("progress 2.json", ...) and can leave the main file empty.
# So the real copy lives on this computer (LOCAL_FILE), and a background thread
# copies it to iCloud Drive at most every few seconds. Every entry carries a
# timestamp, and merging keeps the newest decision per photo, so the PC and the Mac
# (and any conflict copies iCloud made) combine without losing anything. A file that
# can't be read is skipped, never treated as empty.
LOCAL_FILE = os.path.join(CACHE_DIR, "progress-local.json")
SYNC_EVERY = 3      # seconds between writes to iCloud Drive while there are changes
PULL_EVERY = 30     # seconds between checks for changes from the other computer


class Progress:
    def __init__(self, local, shared):
        self.local, self.shared = local, shared
        self.lock = threading.Lock()
        self.items = {}  # key -> {"d": decision or None, "t": unix time}
        self.dirty = False
        data = self._load(local)
        if data is not None:
            self.items = data
        else:
            self._migrate_legacy()
            self._save_local()
            self.dirty = True
        self.pull()
        threading.Thread(target=self._loop, daemon=True).start()

    # -- file helpers
    @staticmethod
    def _load(path, legacy_ok=False):
        """Entries from a v2 file (or a legacy flat file if legacy_ok). None if unreadable."""
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        if isinstance(data, dict) and data.get("v") == 2 and isinstance(data.get("items"), dict):
            return {k: e for k, e in data["items"].items() if isinstance(e, dict) and "t" in e}
        if legacy_ok and isinstance(data, dict):
            t = os.path.getmtime(path)
            return {k: {"d": v, "t": t} for k, v in data.items() if isinstance(v, str)}
        return None

    def _shared_files(self):
        folder, base = os.path.split(self.shared)
        stem, ext = os.path.splitext(base)
        try:
            names = os.listdir(folder)
        except OSError:
            return []
        pat = re.compile(re.escape(stem) + r"( \d+)?" + re.escape(ext) + "$")
        return sorted((os.path.join(folder, n) for n in names if pat.match(n)), key=os.path.getmtime)

    def _migrate_legacy(self):
        """First run of this version: take the newest non-empty old-style progress file.
        (Older conflict copies are earlier snapshots of the same progress.)"""
        best = None
        for path in self._shared_files():
            data = self._load(path, legacy_ok=True)
            if data:
                best = data
        if best:
            self.items = best
            print(f"Recovered {len(best)} decisions from earlier progress files.")

    def _merge(self, other):
        changed = False
        for k, e in other.items():
            cur = self.items.get(k)
            if cur is None or e.get("t", 0) > cur.get("t", 0):
                self.items[k] = {"d": e.get("d"), "t": e.get("t", 0)}
                changed = True
        return changed

    def _dump(self):
        return json.dumps({"v": 2, "items": self.items}, separators=(",", ":"))

    def _save_local(self):
        tmp = self.local + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(self._dump())
        os.replace(tmp, self.local)  # local disk: atomic replace is fine here

    # -- sync with iCloud Drive
    def pull(self):
        """Merge in anything newer from iCloud Drive (incl. conflict copies)."""
        with self.lock:
            changed = False
            for path in self._shared_files():
                data = self._load(path)
                if data:
                    changed |= self._merge(data)
            if changed:
                self._save_local()
                self.dirty = True

    def push(self):
        with self.lock:
            if not self.dirty:
                return
            payload = self._dump()
            self.dirty = False
        try:
            # write in place (no rename) - renames are what confuse iCloud Drive
            with open(self.shared, "w", encoding="utf-8") as f:
                f.write(payload)
        except OSError as e:
            print("Couldn't save progress to iCloud Drive (will retry):", e)
            with self.lock:
                self.dirty = True

    def _loop(self):
        last_pull = time.time()
        while True:
            time.sleep(SYNC_EVERY)
            try:
                if time.time() - last_pull > PULL_EVERY:
                    self.pull()
                    last_pull = time.time()
                self.push()
            except Exception as e:
                print("progress sync:", e)

    # -- used by the server
    def view(self):
        with self.lock:
            return {k: e["d"] for k, e in self.items.items() if e.get("d")}

    def get(self, key):
        with self.lock:
            e = self.items.get(key)
            return e and e.get("d")

    def update(self, changes):
        """changes: {key: decision or None (=remove)}"""
        now = time.time()
        with self.lock:
            for k, v in changes.items():
                self.items[k] = {"d": v, "t": now}
            self._save_local()
            self.dirty = True


PROGRESS = None


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

    # Thumbnails straight from the Windows shell: iCloud provides these for cloud-only
    # photos *without* downloading the original, so they're instant placeholders.
    THUMB_HELPERS = 2

    def shell_thumb(self, src, max_px):
        """Cached JPEG bytes of the shell thumbnail, or None if there isn't one."""
        st = os.stat(src)
        key = hashlib.sha1(f"shell|{src}|{st.st_mtime}|{st.st_size}|{max_px}".encode()).hexdigest()
        cp = os.path.join(CACHE_DIR, key + ".jpg")
        if not os.path.exists(cp):
            if not hasattr(self, "_helpers"):
                import queue
                self._helpers = queue.Queue()
                for _ in range(self.THUMB_HELPERS):
                    self._helpers.put(None)  # started lazily
            proc = self._helpers.get()
            try:
                if proc is None or proc.poll() is not None:
                    proc = subprocess.Popen(
                        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", os.path.join(HERE, "shellthumb.ps1")],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        creationflags=NOWINDOW, text=True, encoding="utf-8", bufsize=1)
                tmp = f"{cp}.{threading.get_ident()}.tmp"
                proc.stdin.write(f"{src}\t{tmp}\t{max_px}\n")
                proc.stdin.flush()
                reply = proc.stdout.readline().strip()
                if reply != "ok" or not os.path.exists(tmp):
                    return None
                os.replace(tmp, cp)
            except OSError:
                proc = None  # helper died; a new one starts next time
                return None
            finally:
                self._helpers.put(proc)
        with open(cp, "rb") as f:
            return f.read()

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
        self.paths, self.names = {}, {}

    def describe(self):
        return self.db.library_path

    def list(self):
        items, self.paths, self.names, self.thumbs = [], {}, {}, {}
        for p in self.db.photos():
            # Shared Album items aren't part of your library: Photos won't let us add
            # them to an album, so they could never be binned.
            if p.intrash or p.shared:
                continue
            name = p.original_filename or p.filename
            ext = os.path.splitext(name)[1].lower()
            # Photos' own JPEG previews (largest first). With "Optimize Mac Storage" the
            # original may only be in iCloud, and these are all that's on disk.
            ders = sorted((d for d in (p.path_derivatives or []) if os.path.exists(d)),
                          key=os.path.getsize, reverse=True)
            self.thumbs[p.uuid] = ders[0] if ders else None
            # Prefer the edited version, then the original, then (photos only) the preview.
            # A video's preview is a still JPEG, so it can't stand in for the video itself.
            src = p.path_edited or p.path
            if not src and not p.ismovie:
                src = self.thumbs[p.uuid]
            self.paths[p.uuid] = src
            self.names[p.uuid] = name
            try:
                taken = p.date.timestamp() if p.date else None
            except Exception:
                taken = None
            items.append({
                "id": p.uuid, "name": name, "kind": "video" if p.ismovie else "image", "ext": ext,
                "size": p.original_filesize or 0, "mtime": taken or 0, "taken": taken,
                "local": bool(src), "favorite": bool(p.favorite),
            })
        return finish_items(items)

    def path(self, pid):
        p = self.paths.get(pid)
        return p if p and os.path.isfile(p) else None

    def thumb_path(self, pid):
        p = getattr(self, "thumbs", {}).get(pid)
        return p if p and os.path.isfile(p) else None

    def heic_to_jpeg(self, src, dst, max_px):
        r = subprocess.run(["sips", "-s", "format", "jpeg", "-Z", str(max_px), src, "--out", dst],
                           capture_output=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError("sips failed: " + r.stderr.decode("utf-8", "replace")[-300:])

    def set_favorite(self, pid, on):
        """Mark a photo as a Favorite (the heart) in Photos."""
        if not re.fullmatch(r"[0-9A-Fa-f-]+", pid):
            return False
        script = f'tell application "Photos" to set favorite of media item id "{pid}/L0/001" to {"true" if on else "false"}'
        r = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=60)
        if r.returncode != 0:
            print("Photos favorite failed:", r.stderr.decode("utf-8", "replace"))
        return r.returncode == 0

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


# ---- background prefetch -----------------------------------------------------
# Cloud-only photos take ~1s to download from iCloud (HEIC ~4s with conversion).
# The page tells us which photos come next; we download/convert them in the
# background so they're on disk by the time you get there.
class Prefetcher:
    WORKERS = 4
    MAX_VIDEO = 200 * 1024 * 1024  # don't pull huge videos ahead of time

    def __init__(self):
        self.lock = threading.Lock()
        self.queue = []          # ids in the order they'll be shown
        self.done, self.busy = set(), set()
        self.wake = threading.Condition(self.lock)
        for _ in range(self.WORKERS):
            threading.Thread(target=self.worker, daemon=True).start()

    def want(self, ids):
        """Replace the wish list (photos no longer upcoming are dropped)."""
        with self.lock:
            self.queue = [i for i in ids if i not in self.done and i not in self.busy]
            self.wake.notify_all()

    def worker(self):
        while True:
            with self.lock:
                while not self.queue:
                    self.wake.wait()
                pid = self.queue.pop(0)
                self.busy.add(pid)
            ok = False
            try:
                ok = self.fetch(pid)
            except Exception as e:
                print(f"prefetch {pid}: {e}")
            with self.lock:
                self.busy.discard(pid)
                if ok:
                    self.done.add(pid)

    def fetch(self, pid):
        path = LIB.path(pid)
        if not path:
            return False
        ext = os.path.splitext(path)[1].lower()
        if ext in VIDEO_EXT and os.path.getsize(path) > self.MAX_VIDEO:
            return False
        with open(path, "rb") as f:  # reading the file makes iCloud download it
            while f.read(1 << 20):
                pass
        if ext in IMAGE_EXT and ext not in RAW_OK and (HAVE_PIL or IS_MAC or ext in (".heic", ".heif")):
            rendered(path, 1800)  # same conversion the card view uses, now cached
        return True


PREFETCH = None


# ---- image rendering ---------------------------------------------------------
def rendered(path, max_px):
    """Cached JPEG bytes of the image, upright and downscaled."""
    st = os.stat(path)
    key = hashlib.sha1(f"{path}|{st.st_mtime}|{st.st_size}|{max_px}".encode()).hexdigest()
    cp = os.path.join(CACHE_DIR, key + ".jpg")
    if not os.path.exists(cp):
        ext = os.path.splitext(path)[1].lower()
        if ext in (".heic", ".heif") or not HAVE_PIL:
            # write to a temp name first: the prefetcher and a request may convert at the same time
            tmp = f"{cp}.{threading.get_ident()}.jpg"
            LIB.heic_to_jpeg(path, tmp, max_px)  # Windows WIC / macOS sips
            os.replace(tmp, cp)
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
            tmp = f"{cp}.{threading.get_ident()}.tmp"
            with open(tmp, "wb") as f:
                f.write(buf.getvalue())
            os.replace(tmp, cp)
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
        if u.path == "/api/ping":
            return self.send_json({"ok": True})
        if u.path == "/api/photos":
            photos = LIB.list()
            PROGRESS.pull()
            return self.send_json({"photos": photos, "state": PROGRESS.view(), "platform": LIB.platform,
                                   "album": ALBUM, "progressFile": STATE_FILE})
        if u.path.startswith("/media/"):
            return self.serve_media(unquote(u.path[len("/media/"):]), parse_qs(u.query))
        self.send_bytes(b"not found", "text/plain", 404)

    def serve_media(self, pid, q):
        path = LIB.path(pid)
        size = q.get("s", ["full"])[0]
        # Mac: Photos keeps a JPEG preview even for videos and cloud-only items
        if size in ("thumb", "ph") and hasattr(LIB, "thumb_path"):
            t = LIB.thumb_path(pid)
            if t and (not path or os.path.splitext(path)[1].lower() in VIDEO_EXT):
                return self.serve_file(t, os.path.splitext(t)[1].lower())
        if not path:
            return self.send_bytes(b"not available on this computer", "text/plain", 404)
        ext = os.path.splitext(path)[1].lower()
        # s=ph: card placeholder - only if the shell already has a thumbnail; never download for it
        # s=thumb: grid tile - fall back to building one from the photo if needed
        if size == "ph" and not hasattr(LIB, "shell_thumb"):
            size = "thumb"  # Mac: thumbnails come from local previews anyway
        if size in ("thumb", "ph") and hasattr(LIB, "shell_thumb"):
            try:
                data = LIB.shell_thumb(path, 768)
            except Exception as e:
                print(f"shell thumbnail {pid}: {e}")
                data = None
            if data:
                return self.send_bytes(data, "image/jpeg", extra={"Cache-Control": "max-age=86400"})
            if ext in VIDEO_EXT or size == "ph":  # no shell thumbnail: don't download just for a preview
                return self.send_bytes(b"no thumbnail", "text/plain", 404)
        convert = ext in IMAGE_EXT and (size == "thumb" or ext not in RAW_OK) and (HAVE_PIL or IS_MAC or ext in (".heic", ".heif"))
        # Suggested filename for right-click > Save image (Mac ids are UUIDs, converted files are JPEGs)
        name = getattr(LIB, "names", {}).get(pid, pid)
        if convert:
            name = os.path.splitext(name)[0] + ".jpg"
        disp = {"Content-Disposition": "inline; filename*=UTF-8''" + quote(name)}
        last = None
        # Cloud-only files download on first read, and that download sometimes fails when
        # iCloud is busy. Retry a few times before giving up.
        for attempt in range(4):
            try:
                if convert:
                    data = rendered(path, 420 if size == "thumb" else 1800)
                    return self.send_bytes(data, "image/jpeg", extra={"Cache-Control": "max-age=86400", **disp})
                return self.serve_file(path, ext, disp)
            except (ConnectionError, BrokenPipeError):
                return
            except Exception as e:
                last = e
                time.sleep(1.5 * (attempt + 1))
        print(f"Couldn't load {pid}: {last}")
        try:
            self.send_bytes(f"{type(last).__name__}: {last}".encode(), "text/plain", 500)
        except Exception:
            pass

    def serve_file(self, path, ext, extra=None):
        ctype = {".mov": "video/mp4"}.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
        total = os.path.getsize(path)
        with open(path, "rb") as f:  # make iCloud fetch it now, while we can still retry
            f.read(1)
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
        if ext in IMAGE_EXT:  # lets the browser reuse images it preloaded
            self.send_header("Cache-Control", "max-age=86400")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
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
            # {"key", "decision"} or a batch: {"changes": [{"key", "decision"}, ...]} (applied in order)
            changes = {}
            for c in body.get("changes") or [body]:
                key, d = c.get("key"), c.get("decision")
                if not isinstance(key, str):
                    return self.send_json({"ok": False}, 400)
                if key.startswith("@"):
                    changes[key] = "1" if d == "1" else None
                else:
                    changes[key] = d if d in ("keep", "delete") else None
            PROGRESS.update(changes)
            return self.send_json({"ok": True})
        if u.path == "/api/favorite":
            pid = body.get("id")
            ok = isinstance(pid, str) and hasattr(LIB, "set_favorite") and LIB.set_favorite(pid, bool(body.get("on")))
            return self.send_json({"ok": bool(ok)}, 200 if ok else 500)
        if u.path == "/api/prefetch":
            ids = [i for i in body.get("ids", []) if isinstance(i, str)][:50]
            PREFETCH.want(ids)
            return self.send_json({"ok": True})
        if u.path == "/api/commit":
            # body.items: [{id, key}] - only items still marked "delete" are touched
            wanted = [it for it in body.get("items", []) if PROGRESS.get(it.get("key")) == "delete"]
            done_ids = set(LIB.remove([it["id"] for it in wanted]))
            done = [it for it in wanted if it["id"] in done_ids]
            # Windows: file is gone -> forget it. Mac: in the album -> remember as "sent".
            PROGRESS.update({it["key"]: (None if IS_WIN else "sent") for it in done})
            PROGRESS.push()
            return self.send_json({"done": [it["id"] for it in done],
                                   "failed": [it["id"] for it in wanted if it["id"] not in done_ids],
                                   "mode": "recycle" if IS_WIN else "album"})
        self.send_bytes(b"not found", "text/plain", 404)


def main():
    global LIB, PREFETCH, PROGRESS
    PREFETCH = Prefetcher()
    PROGRESS = Progress(LOCAL_FILE, STATE_FILE)
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
