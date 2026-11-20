import os, re, time, glob, json, shutil, textwrap, itertools, subprocess, threading, collections, statistics
from datetime import datetime, timezone
import requests
import pytchat

# ---------------- CONFIG ----------------
QUALITY = "720p,best"    # for 1080p use "1080p60,1080p,best"
DEFAULT_MIN_MSGS = None  # None = auto (bot learns each stream). Or a number to force a floor
AUTO_FLOOR = 10          # auto mode: a spike must still reach at least this score
BASELINE_WINDOW = 600    # seconds of chat history used to learn what 'normal' is
STD_FACTOR = 3           # spike must also beat normal by this many 'wobbles'
MULTIPLIER = 2.5         # must be this many times busier than normal (higher = pickier)
WARMUP = 120             # seconds of learning before any clip
PRE_SECONDS = 20         # seconds of video before the reaction peak
POST_SECONDS = 6         # seconds of video after the reaction peak
PEAK_WAIT = 12           # max seconds to wait for the reaction to peak
COOLDOWN = 60
MAX_RUNTIME = 5.5 * 3600
STATUS_EVERY = 30
MAX_RETRIES = 5
HYPE_WORDS = ["lmao", "lmfao", "clip", "pog", "omg", "wtf", "bruh", "💀", "😂", "🤣"]
AUDIO_MAX_BOOST = 0.5    # loud audio can raise the score by up to 50%
CLIP_TTL = 3 * 3600      # how long clips stay available for the buttons
CLIPS_DIR = "clips"
FONT_PATH = ""           # leave empty to auto-find a font on the phone
OUT_W, OUT_H = 1080, 1920
PANE_H = OUT_H // 2      # each half of the cam split-screen
# -----------------------------------------

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
START = time.time()
API = f"https://api.telegram.org/bot{TOKEN}"
VID_RE = re.compile(r"(?:v=|youtu\.be/|/live/|/shorts/)([\w-]{11})")
STATE = {}
ACTIVE = {}   # name -> stop Event
VIDS = {}     # name -> video id
CLIPS = {}    # clip id -> info (raw clip path, chosen features, hook text, ...)
CLIP_IDS = itertools.count(1)
WAITING_HOOK = None   # clip id we are waiting for hook text for
CAM_PRESETS = ("br", "bl", "tr", "tl")
OPT_LABELS = {"v": "vertical", "c": "cam split", "h": "hook", "b": "chat bubble"}


# ---------------- Telegram helpers ----------------
def tg_api(method, data=None, timeout=30):
    try:
        r = requests.post(f"{API}/{method}", data=data, timeout=timeout)
        return r.json()
    except Exception as e:
        print(f"telegram {method} error:", e)
        return {}


def tg_text(msg):
    try:
        r = requests.post(f"{API}/sendMessage", data={"chat_id": CHAT_ID, "text": msg}, timeout=30)
        return r.json().get("result", {}).get("message_id")
    except Exception as e:
        print("telegram text error:", e)
        return None


def tg_edit(msg_id, msg):
    try:
        requests.post(f"{API}/editMessageText",
                      data={"chat_id": CHAT_ID, "message_id": msg_id, "text": msg}, timeout=30)
    except Exception as e:
        print("telegram edit error:", e)


def tg_video(path, caption, kb=None, as_file=False):
    """Send a video (or a file). Returns the Telegram message id, or None if it failed."""
    method, field = ("sendDocument", "document") if as_file else ("sendVideo", "video")
    data = {"chat_id": CHAT_ID, "caption": caption[:1000]}
    if not as_file:
        data["supports_streaming"] = "true"
    if kb:
        data["reply_markup"] = json.dumps(kb)
    try:
        with open(path, "rb") as f:
            r = requests.post(f"{API}/{method}", data=data, files={field: f}, timeout=300)
        print("telegram video:", r.status_code)
        if r.status_code != 200:
            tg_text(f"Clip failed to send: {r.text[:200]}")
            return None
        return r.json().get("result", {}).get("message_id") or 1
    except Exception as e:
        print("telegram video error:", e)
        return None


def build_kb(cid):
    clip = CLIPS[cid]

    def label(key, text):
        return ("✅ " if key in clip["opts"] else "⬜ ") + text

    return {"inline_keyboard": [
        [{"text": label("v", "📱 Vertical"), "callback_data": f"t:{cid}:v"},
         {"text": label("c", "🎥 Cam split"), "callback_data": f"t:{cid}:c"}],
        [{"text": label("h", "🔥 Hook text"), "callback_data": f"t:{cid}:h"},
         {"text": label("b", "💬 Chat bubble"), "callback_data": f"t:{cid}:b"}],
        [{"text": "✏️ Edit hook text", "callback_data": f"e:{cid}"}],
        [{"text": "▶️ Make video", "callback_data": f"r:{cid}"},
         {"text": "📁 Make as file", "callback_data": f"f:{cid}"}],
    ]}


def refresh_kb(cid, message_id=None):
    clip = CLIPS.get(cid)
    mid = message_id or (clip or {}).get("msg_id")
    if clip and mid:
        tg_api("editMessageReplyMarkup", {"chat_id": CHAT_ID, "message_id": mid,
                                          "reply_markup": json.dumps(build_kb(cid))})


# ---------------- Recording ----------------
def start_recorder(video_id, buf_dir):
    os.makedirs(buf_dir, exist_ok=True)
    url = f"https://www.youtube.com/watch?v={video_id}"
    errf = open(os.path.join(buf_dir, "sl.log"), "w")
    sl = subprocess.Popen(["streamlink", "--stdout", url, QUALITY,
                           "--stream-timeout", "120",
                           "--hls-playlist-reload-attempts", "10"],
                          stdout=subprocess.PIPE, stderr=errf)
    prefix = f"seg_{int(time.time())}_"
    ff = subprocess.Popen([
        "ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-c", "copy",
        "-f", "segment", "-segment_time", "5", "-reset_timestamps", "1",
        os.path.join(buf_dir, prefix + "%05d.ts"),
    ], stdin=sl.stdout)
    return sl, ff


def kill_procs(*procs):
    for p in procs:
        try:
            if p:
                p.kill()
        except Exception:
            pass


def cleanup_loop(buf_dir, done):
    while not done.is_set():
        for f in glob.glob(os.path.join(buf_dir, "seg_*.ts")):
            try:
                if time.time() - os.path.getmtime(f) > 180:
                    os.remove(f)
            except Exception:
                pass
        done.wait(20)


# ---------------- Audio loudness ----------------
def measure_volume(path):
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-vn",
                            "-af", "volumedetect", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=30)
        m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", r.stderr)
        return float(m.group(1)) if m else None
    except Exception:
        return None


def audio_loop(buf_dir, done, levels):
    """Measures how loud each finished 5s segment is (laughs, screams = loud)."""
    seen = set()
    while not done.is_set():
        files = []
        try:
            files = sorted(glob.glob(os.path.join(buf_dir, "seg_*.ts")), key=os.path.getmtime)
        except OSError:
            pass
        for f in files[:-1]:          # the newest file is still being written
            if f in seen:
                continue
            seen.add(f)
            v = measure_volume(f)
            if v is not None:
                try:
                    levels.append((os.path.getmtime(f), v))
                except OSError:
                    pass
        now = time.time()
        while levels and now - levels[0][0] > BASELINE_WINDOW:
            levels.popleft()
        if len(seen) > 400:
            seen = set(files)
        done.wait(3)


def audio_state(levels, now):
    """Returns (score multiplier, how many dB louder than normal it is right now)."""
    snap = list(levels)
    if len(snap) < 8:
        return 1.0, 0.0
    recent = [v for t, v in snap if now - t <= 12]
    if not recent:
        return 1.0, 0.0
    jump = sum(recent) / len(recent) - statistics.median(v for _, v in snap)
    frac = min(1.0, max(0.0, (jump - 3) / 9))
    return 1.0 + AUDIO_MAX_BOOST * frac, jump


# ---------------- Chat text helpers ----------------
def clean_text(s, limit=80):
    s = re.sub(r"[^\x20-\x7E\u00C0-\u017F]", "", s or "")   # drops emojis and odd symbols
    return re.sub(r"\s+", " ", s).strip()[:limit]


def has_hype(msg):
    low = (msg or "").lower()
    return any(w in low for w in HYPE_WORDS)


def make_hook(log):
    msgs = [clean_text(m[3], 40) for m in log]
    msgs = [m for m in msgs if len(m) >= 2]
    if msgs:
        top, cnt = collections.Counter(m.lower() for m in msgs).most_common(1)[0]
        if cnt >= 3:
            return f'CHAT: "{top.upper()}"'
    if any(has_hype(m[3]) for m in log):
        return "CHAT IS LOSING IT"
    return "WAIT FOR IT..."


def pick_chat_lines(log, limit=2):
    cands = []
    for t, aid, aname, msg in log:
        m = clean_text(msg, 70)
        a = clean_text(aname, 18) or "viewer"
        if len(m) < 6:
            continue
        s = (2 if has_hype(msg) else 0) + min(len(m), 40) / 40
        cands.append((s, t, aid, a, m))
    cands.sort(reverse=True)
    out, used = [], set()
    for s, t, aid, a, m in cands:
        if aid in used:
            continue
        used.add(aid)
        out.append((t, a, m))
        if len(out) == limit:
            break
    out.sort()
    return [f"{a}: {m}" for t, a, m in out]


# ---------------- Clip making ----------------
def make_clip(name, buf_dir, trigger_time, n, score, jump, chat_log, cam_pref):
    # wait until the video after the peak has been recorded
    time.sleep(max(0, trigger_time + POST_SECONDS + 8 - time.time()))
    start, end = trigger_time - PRE_SECONDS, trigger_time + POST_SECONDS
    segs = []
    for f in glob.glob(os.path.join(buf_dir, "seg_*.ts")):
        try:
            t = os.path.getmtime(f)   # when this 5s segment was finished
        except OSError:
            continue
        if start <= t <= end + 5:
            segs.append((t, f))
    segs = [f for t, f in sorted(segs)]
    if not segs:
        print("no segments found")
        return
    print(f"[{name}] clip {n}: {len(segs)} segments (~{len(segs) * 5}s)")
    lst = os.path.join(buf_dir, f"list_{n}.txt")
    with open(lst, "w") as fh:
        for s in segs:
            fh.write(f"file '{os.path.abspath(s)}'\n")
    os.makedirs(CLIPS_DIR, exist_ok=True)
    out = os.path.join(CLIPS_DIR, f"raw_{name}_{n}_{int(trigger_time)}.mp4")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", lst, "-c", "copy", "-bsf:a", "aac_adtstoasc", out])
    if not os.path.exists(out):
        return
    cid = str(next(CLIP_IDS))
    CLIPS[cid] = {"path": out, "name": name, "n": n, "opts": set(),
                  "hook": make_hook(chat_log), "chat": pick_chat_lines(chat_log),
                  "cam_pref": cam_pref, "created": time.time(), "busy": False, "msg_id": None}
    loud = f", +{jump:.0f}dB loud" if jump >= 3 else ""
    dur = probe_duration(out)
    length = f"{dur:.0f}s" if dur else f"~{len(segs) * 5}s"
    cap = (f"{name} clip #{n} (score {score}{loud}, {length})\n"
           f"Hook: {CLIPS[cid]['hook']}\n"
           f"Pick features below, then tap Make video.")
    mid = tg_video(out, cap, kb=build_kb(cid))
    if mid:
        CLIPS[cid]["msg_id"] = mid
        STATE[name]["clips"] += 1
    else:
        CLIPS.pop(cid, None)
        try:
            os.remove(out)
        except OSError:
            pass


def janitor():
    now = time.time()
    for cid, clip in list(CLIPS.items()):
        if now - clip["created"] > CLIP_TTL and not clip.get("busy"):
            try:
                os.remove(clip["path"])
            except OSError:
                pass
            CLIPS.pop(cid, None)


# ---------------- Rendering the chosen features ----------------
def find_font():
    if FONT_PATH and os.path.exists(FONT_PATH):
        return FONT_PATH
    for pat in ("/system/fonts/Roboto-Bold.ttf", "/system/fonts/Roboto-Regular.ttf",
                "/system/fonts/Roboto*.ttf", "/system/fonts/DroidSans*.ttf",
                "/system/fonts/NotoSans-*.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/**/*.ttf", "/system/fonts/*.ttf"):
        hits = sorted(glob.glob(pat, recursive=True))
        if hits:
            return hits[0]
    return None


def ensure_font():
    """Copies a font to a plain filename so ffmpeg never trips on odd characters in the path."""
    dst = os.path.abspath(os.path.join(CLIPS_DIR, "font.ttf"))
    if os.path.exists(dst):
        return dst
    src = find_font()
    if not src:
        return None
    os.makedirs(CLIPS_DIR, exist_ok=True)
    try:
        shutil.copy(src, dst)
        return dst
    except Exception:
        return None


def probe_size(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
                           capture_output=True, text=True, timeout=30)
        w, h = r.stdout.strip().split(",")[:2]
        return int(w), int(h)
    except Exception:
        return None


def probe_duration(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", path], capture_output=True, text=True, timeout=30)
        return float(r.stdout.strip())
    except Exception:
        return None


def even(v):
    return int(v) // 2 * 2


def preset_cam_box(pref, sw, sh):
    """Cam box for a fixed corner (cam=br, bl, tr, tl): about a quarter of the screen width."""
    cw = even(sw * 0.26)
    ch = even(cw * PANE_H / OUT_W)
    x = 0 if pref in ("bl", "tl") else sw - cw
    y = 0 if pref in ("tl", "tr") else sh - ch
    return x, y, cw, ch


CASCADE_NAME = "haarcascade_frontalface_default.xml"
CASCADE_URL = "https://raw.githubusercontent.com/opencv/opencv/4.x/data/haarcascades/" + CASCADE_NAME


def get_cascade():
    """Finds OpenCV's face-detector file; downloads it once if the install doesn't include it."""
    cands = []
    try:
        import cv2
        cands.append(os.path.join(cv2.data.haarcascades, CASCADE_NAME))
    except Exception:
        pass
    cands += [f"/data/data/com.termux/files/usr/share/opencv4/haarcascades/{CASCADE_NAME}",
              f"/usr/share/opencv4/haarcascades/{CASCADE_NAME}",
              os.path.join(CLIPS_DIR, CASCADE_NAME)]
    for c in cands:
        if os.path.exists(c):
            return c
    try:
        os.makedirs(CLIPS_DIR, exist_ok=True)
        dst = os.path.join(CLIPS_DIR, CASCADE_NAME)
        r = requests.get(CASCADE_URL, timeout=30)
        if r.status_code == 200 and b"<opencv_storage" in r.content[:2000]:
            with open(dst, "wb") as f:
                f.write(r.content)
            return dst
    except Exception as e:
        print("cascade download error:", e)
    return None


def detect_faces(path, sw, sh):
    """Looks for the biggest face in a few frames of the clip. Returns a list of (x, y, w, h)."""
    try:
        import cv2
        cpath = get_cascade()
        casc = cv2.CascadeClassifier(cpath) if cpath else None
        if casc is None or casc.empty():
            return []
    except Exception:
        return []
    os.makedirs(CLIPS_DIR, exist_ok=True)
    faces = []
    for i, ts in enumerate((2, 6, 10, 14, 18)):
        frame = os.path.join(CLIPS_DIR, f"probe_{os.getpid()}_{i}.jpg")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(ts), "-i", path,
                        "-frames:v", "1", "-q:v", "3", frame])
        img = cv2.imread(frame) if os.path.exists(frame) else None
        if os.path.exists(frame):
            os.remove(frame)
        if img is None:
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        found = casc.detectMultiScale(gray, 1.1, 6, minSize=(max(24, sw // 40),) * 2)
        if len(found):
            faces.append(tuple(int(v) for v in max(found, key=lambda f: f[2] * f[3])))
    return faces


def cam_box_from_faces(faces, sw, sh):
    """Turns face detections into the crop box (x, y, w, h) for the bottom half of the split-screen."""
    if len(faces) < 2:
        return None
    cxs = sorted(f[0] + f[2] / 2 for f in faces)
    cys = sorted(f[1] + f[3] / 2 for f in faces)
    mcx, mcy = cxs[len(cxs) // 2], cys[len(cys) // 2]
    steady = [f for f in faces
              if abs(f[0] + f[2] / 2 - mcx) < sw * 0.05 and abs(f[1] + f[3] / 2 - mcy) < sh * 0.05]
    if len(steady) < 2:      # face moves around a lot = probably not a facecam
        return None
    fw = sorted(f[2] for f in steady)[len(steady) // 2]
    cw = fw * 2.8
    ch = cw * PANE_H / OUT_W
    if cw > sw:
        cw = sw
        ch = cw * PANE_H / OUT_W
    if ch > sh:
        ch = sh
        cw = ch * OUT_W / PANE_H
    cw, ch = even(cw), even(ch)
    x = max(0, min(sw - cw, int(mcx - cw / 2)))
    y = max(0, min(sh - ch, int(mcy - ch * 0.45)))
    return even(x), even(y), cw, ch


def find_cam(path, sw, sh):
    return cam_box_from_faces(detect_faces(path, sw, sh), sw, sh)


def drawtext(font, textfile, size, x, y, color="white", box="black@0.5", border=3):
    return (f"drawtext=fontfile={font}:textfile={textfile}:expansion=none:fontcolor={color}"
            f":fontsize={size}:borderw={border}:bordercolor=black:box=1:boxcolor={box}"
            f":boxborderw=16:x={x}:y={y}")


def run_ffmpeg(src, graph, out):
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-filter_complex", graph,
           "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("ffmpeg render error:", r.stderr[-400:])
    return r.returncode == 0 and os.path.exists(out)


def render_clip(cid, as_file):
    clip = CLIPS.get(cid)
    if not clip:
        return
    clip["busy"] = True
    tmp = []
    os.makedirs(CLIPS_DIR, exist_ok=True)
    try:
        opts, path = clip["opts"], clip["path"]
        label = f"{clip['name']} clip #{clip['n']}"
        if not opts:
            tg_video(path, f"{label} (original)", as_file=as_file)
            return
        size = probe_size(path)
        if not size:
            tg_text("Couldn't read that clip. Try another one.")
            return
        sw, sh = size
        notes = []
        layout, cam = "landscape", None
        if "c" in opts:
            pref = clip["cam_pref"]
            if pref in CAM_PRESETS:
                cam = preset_cam_box(pref, sw, sh)
            elif pref == "auto":
                cam = find_cam(path, sw, sh)
            layout = "split" if cam else "blur"
            if not cam:
                notes.append("cam not found, used plain vertical (try cam=br when starting the stream)"
                             if pref != "off" else "cam=off was set for this stream, used plain vertical")
        elif "v" in opts:
            layout = "blur"

        # geometry for each layout
        if layout == "split":
            gw = even(min(sw, sh * OUT_W / PANE_H))
            gx = even((sw - gw) / 2)
            cx, cy, cw, ch = cam
            graph = (f"[0:v]split=2[g][c];"
                     f"[g]crop={gw}:{sh}:{gx}:0,scale={OUT_W}:{PANE_H}[top];"
                     f"[c]crop={cw}:{ch}:{cx}:{cy},scale={OUT_W}:{PANE_H}[bot];"
                     f"[top][bot]vstack[v0]")
            W, H, hook_y, bubble_y = OUT_W, OUT_H, "60", f"{PANE_H}-text_h-20"
        elif layout == "blur":
            fg_h = int(OUT_W * sh / sw)
            top = (OUT_H - fg_h) // 2
            graph = ("[0:v]split=2[a][b];"
                     "[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
                     f"boxblur=8:2,scale={OUT_W}:{OUT_H}[bg];"
                     f"[b]scale={OUT_W}:-2[fg];"
                     "[bg][fg]overlay=(W-w)/2:(H-h)/2[v0]")
            W, H = OUT_W, OUT_H
            hook_y, bubble_y = f"({top}-text_h)/2", f"{top + fg_h}-text_h-20"
        else:
      
