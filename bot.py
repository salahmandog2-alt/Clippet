import os, re, time, glob, subprocess, threading, collections
from datetime import datetime, timezone
import requests
import pytchat

# ---------------- CONFIG ----------------
QUALITY = "720p,best"   # for 1080p use "1080p60,1080p,best"
DEFAULT_MIN_MSGS = 60   # floor: never clip below this score
MULTIPLIER = 1.6        # must be this many times busier than normal
WARMUP = 120            # seconds of learning before any clip
PRE_SECONDS = 30
POST_SECONDS = 12
COOLDOWN = 60
MAX_RUNTIME = 5.5 * 3600
STATUS_EVERY = 30
MAX_RETRIES = 5
HYPE_WORDS = ["lmao", "lmfao", "clip", "pog", "omg", "wtf", "bruh", "💀", "😂", "🤣"]
# -----------------------------------------

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
START = time.time()
API = f"https://api.telegram.org/bot{TOKEN}"
VID_RE = re.compile(r"(?:v=|youtu\.be/|/live/|/shorts/)([\w-]{11})")
STATE = {}
ACTIVE = {}   # name -> stop Event
VIDS = {}     # name -> video id


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


def tg_video(path, caption):
    try:
        with open(path, "rb") as f:
            r = requests.post(f"{API}/sendVideo",
                              data={"chat_id": CHAT_ID, "caption": caption},
                              files={"video": f}, timeout=300)
        print("telegram video:", r.status_code)
        if r.status_code != 200:
            tg_text(f"Clip failed to send: {r.text[:200]}")
        return r.status_code == 200
    except Exception as e:
        print("telegram video error:", e)
        return False


def start_recorder(video_id, buf_dir):
    os.makedirs(buf_dir, exist_ok=True)
    url = f"https://www.youtube.com/watch?v={video_id}"
    errf = open(os.path.join(buf_dir, "sl.log"), "w")
    sl = subprocess.Popen(["streamlink", "--stdout", url, QUALITY,
                           "--stream-timeout", "120",
                           "--hls-playlist-reload-attempts", "10"],
                          stdout=subprocess.PIPE, stderr=errf)
    ff = subprocess.Popen([
        "ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-c", "copy",
        "-f", "segment", "-segment_time", "5", "-strftime", "1",
        os.path.join(buf_dir, "seg_%s.ts"),
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
                if time.time() - int(re.search(r"seg_(\d+)", f).group(1)) > 180:
                    os.remove(f)
            except Exception:
                pass
        done.wait(20)


def make_clip(name, buf_dir, trigger_time, n, score):
    time.sleep(POST_SECONDS + 8)
    start, end = trigger_time - PRE_SECONDS, trigger_time + POST_SECONDS
    segs = []
    for f in sorted(glob.glob(os.path.join(buf_dir, "seg_*.ts"))):
        t = int(re.search(r"seg_(\d+)", f).group(1))
        if start - 5 <= t <= end:
            segs.append(f)
    if not segs:
        print("no segments found")
        return
    lst = os.path.join(buf_dir, f"list_{n}.txt")
    with open(lst, "w") as fh:
        for s in segs:
            fh.write(f"file '{os.path.abspath(s)}'\n")
    out = f"clip_{name}_{n}.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", lst, "-c", "copy", "-bsf:a", "aac_adtstoasc", out])
    if os.path.exists(out):
        if tg_video(out, f"{name} clip #{n} (score {score})"):
            STATE[name]["clips"] += 1
        os.remove(out)


def compute(times, now, min_msgs, watch_start):
    recent = [t for t in times if now - t[0] <= 10]
    older_count = len(times) - len(recent)
    history = max(10.0, min(110.0, now - watch_start - 10))
    baseline = (older_count / history) * 10
    hype_recent = sum(1 for t in recent if t[1])
    score = len(recent) + hype_recent
    threshold = max(min_msgs, baseline * MULTIPLIER)
    return len(recent), score, threshold, baseline


def status_text(name):
    st = STATE[name]
    left = max(0, int((MAX_RUNTIME - (time.time() - START)) / 60))
    ago = f"{int(time.time() - st['last_clip'])}s ago" if st["last_clip"] else "none yet"
    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
    state = "LIVE 🔴" if st["live"] else "reconnecting / ended"
    warm = max(0, int(WARMUP - (time.time() - st["watch_start"]))) if st["live"] else 0
    if warm > 0:
        state += f" (warming up, {warm}s)"
    return (f"📊 {name} tracker\n"
            f"Status: {state}\n"
            f"Chat speed: {st['rate']} msgs / 10s\n"
            f"Normal speed: {st['baseline']:.0f} msgs / 10s\n"
            f"Score: {st['score']} (needs {st['threshold']:.0f} to clip)\n"
            f"Clips sent: {st['clips']}\n"
            f"Last clip: {ago}\n"
            f"Bot time left: {left} min\n"
            f"Updated: {now_str} UTC")


def status_loop(name, done):
    msg_id = tg_text(status_text(name))
    while not done.is_set():
        done.wait(STATUS_EVERY)
        if msg_id:
            tg_edit(msg_id, status_text(name))
    if msg_id:
        tg_edit(msg_id, status_text(name))


def read_err(buf_dir):
    try:
        return open(os.path.join(buf_dir, "sl.log")).read()[-400:]
    except Exception:
        return "unknown error"


def watch(name, vid, min_msgs, stop):
    buf_dir = f"buffer_{name}"
    watch_start = time.time()
    STATE[name] = {"live": False, "rate": 0, "score": 0, "threshold": min_msgs,
                   "baseline": 0, "clips": 0, "last_clip": 0, "watch_start": watch_start}
    done = threading.Event()
    times = collections.deque()
    last_clip = 0
    n = 0
    fails = 0
    first = True
    status_thread = None
    sl = ff = None
    final_msg = None
    try:
        while not stop.is_set() and time.time() - START < MAX_RUNTIME:
            sl, ff = start_recorder(vid, buf_dir)
            time.sleep(12)
            if sl.poll() is not None or ff.poll() is not None:
                err = read_err(buf_dir)
                kill_procs(sl, ff)
                fails += 1
                if first or fails >= MAX_RETRIES:
                    tg_text(f"{name}: couldn't open the stream.\n{err}")
                    break
                tg_text(f"{name}: couldn't reconnect (try {fails}/{MAX_RETRIES}). Retrying...")
                time.sleep(20)
                continue

            try:
                chat = pytchat.create(video_id=vid, interruptable=False)
                chat_ok = chat.is_alive()
                chat_err = "chat is not available for this stream"
            except Exception as e:
                chat_ok = False
                chat_err = f"couldn't read the chat: {str(e)[:150]}"
            if not chat_ok:
                kill_procs(sl, ff)
                fails += 1
                if first or fails >= MAX_RETRIES:
                    tg_text(f"{name}: {chat_err}")
                    break
                time.sleep(20)
                continue

            if first:
                first = False
                watch_start = time.time()
                STATE[name]["watch_start"] = watch_start
                tg_text(f"{name}: stream opened. Watching chat (learning for {WARMUP}s first)...")
                threading.Thread(target=cleanup_loop, args=(buf_dir, done), daemon=True).start()
                status_thread = threading.Thread(target=status_loop, args=(name, done))
                status_thread.start()
            STATE[name]["live"] = True
            run_started = time.time()

            reason = ""
            while True:
                if stop.is_set():
                    reason = "stop"
                    break
                if time.time() - START >= MAX_RUNTIME:
                    reason = "time limit"
                    break
                if sl.poll() is not None or ff.poll() is not None:
                    reason = "video feed dropped"
                    break
                if not chat.is_alive():
                    reason = "chat reader stopped"
                    break
                for c in chat.get().sync_items():
                    hype = any(w in c.message.lower() for w in HYPE_WORDS)
                    times.append((time.time(), hype))
                now = time.time()
                while times and now - times[0][0] > 120:
                    times.popleft()
                rate, score, threshold, baseline = compute(times, now, min_msgs, watch_start)
                STATE[name].update({"rate": rate, "score": score,
                                    "threshold": threshold, "baseline": baseline})
                warmed = now - watch_start >= WARMUP
                if warmed and score >= threshold and now - last_clip > COOLDOWN:
                    last_clip = now
                    n += 1
                    STATE[name]["last_clip"] = now
                    print(f"[{name}] spike! score={score} threshold={threshold:.1f}")
                    threading.Thread(target=make_clip,
                                     args=(name, buf_dir, now, n, score), daemon=True).start()
                time.sleep(1)

            kill_procs(sl, ff)
            STATE[name]["live"] = False
            if reason in ("stop", "time limit"):
                final_msg = f"{name}: stopped ({reason})."
                break
            if time.time() - run_started > 120:
                fails = 0
            fails += 1
            if fails >= MAX_RETRIES:
                final_msg = f"{name}: stopped (last reason: {reason}). The stream may have ended."
                break
            tg_text(f"{name}: {reason}. Reconnecting (try {fails}/{MAX_RETRIES})...")
            time.sleep(10)
        if final_msg is None and (stop.is_set() or time.time() - START >= MAX_RUNTIME):
            final_msg = f"{name}: stopped."
    except Exception as e:
        final_msg = f"{name}: error - {str(e)[:300]}"
    finally:
        STATE[name]["live"] = False
        done.set()
        kill_procs(sl, ff)
        if status_thread:
            status_thread.join(timeout=60)
        if final_msg:
            tg_text(final_msg)
        ACTIVE.pop(name, None)
        VIDS.pop(name, None)


def handle_message(text):
    low = text.lower().strip()
    if low.startswith("/stop"):
        for ev in list(ACTIVE.values()):
            ev.set()
        tg_text("Stopping all streams.")
        return
    if low.startswith("/help"):
        tg_text("Just send a YouTube live link and I'll start clipping.\n"
                "Optional: add a name after it (Speed) and min=250 to make it less sensitive.\n"
                "Example: https://youtube.com/live/xxxx Speed min=250\n/stop stops everything.")
        return
    m = VID_RE.search(text)
    if not m:
        return
    vid = m.group(1)
    if vid in VIDS.values():
        tg_text("Already watching that stream.")
        return
    tokens = text.split()
    min_msgs = DEFAULT_MIN_MSGS
    words = []
    for t in tokens:
        if VID_RE.search(t):
            continue
        mm = re.fullmatch(r"min=(\d+)", t.lower())
        if mm:
            min_msgs = int(mm.group(1))
        else:
            words.append(t)
    name = re.sub(r"[^\w-]", "", "_".join(words)) or f"Stream{len(ACTIVE) + 1}"
    while name in ACTIVE:
        name += "2"
    stop = threading.Event()
    ACTIVE[name] = stop
    VIDS[name] = vid
    tg_text(f"Got it. Starting {name} (min={min_msgs})...")
    threading.Thread(target=watch, args=(name, vid, min_msgs, stop), daemon=True).start()


def main():
    tg_text("Clip bot started. Send me a YouTube live link to start clipping (send /help for tips).")
    offset = 0
    while time.time() - START < MAX_RUNTIME:
        try:
            r = requests.get(f"{API}/getUpdates", params={"offset": offset, "timeout": 25}, timeout=40)
            updates = r.json().get("result", [])
        except Exception as e:
            print("poll error:", e)
            time.sleep(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
                continue
            text = (msg.get("text") or "").strip()
            if text:
                try:
                    handle_message(text)
                except Exception as e:
                    tg_text(f"Error: {str(e)[:200]}")
    for ev in list(ACTIVE.values()):
        ev.set()
    time.sleep(15)
    tg_text("Bot time limit reached. Start it again when you need it.")


if __name__ == "__main__":
    main()import os, re, time, glob, subprocess, threading, collections
from datetime import datetime, timezone
import requests
import pytchat

# ---------------- CONFIG ----------------
QUALITY = "720p,best"   # for 1080p use "1080p60,1080p,best"
DEFAULT_MIN_MSGS = 60   # floor: never clip below this score
MULTIPLIER = 1.6        # must be this many times busier than normal
WARMUP = 120            # seconds of learning before any clip
PRE_SECONDS = 30
POST_SECONDS = 12
COOLDOWN = 60
MAX_RUNTIME = 5.5 * 3600
STATUS_EVERY = 30
MAX_RETRIES = 5
HYPE_WORDS = ["lmao", "lmfao", "clip", "pog", "omg", "wtf", "bruh", "💀", "😂", "🤣"]
# -----------------------------------------

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
START = time.time()
API = f"https://api.telegram.org/bot{TOKEN}"
VID_RE = re.compile(r"(?:v=|youtu\.be/|/live/|/shorts/)([\w-]{11})")
STATE = {}
ACTIVE = {}   # name -> stop Event
VIDS = {}     # name -> video id


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


def tg_video(path, caption):
    try:
        with open(path, "rb") as f:
            r = requests.post(f"{API}/sendVideo",
                              data={"chat_id": CHAT_ID, "caption": caption},
                              files={"video": f}, timeout=300)
        print("telegram video:", r.status_code)
        if r.status_code != 200:
            tg_text(f"Clip failed to send: {r.text[:200]}")
        return r.status_code == 200
    except Exception as e:
        print("telegram video error:", e)
        return False


def start_recorder(video_id, buf_dir):
    os.makedirs(buf_dir, exist_ok=True)
    url = f"https://www.youtube.com/watch?v={video_id}"
    errf = open(os.path.join(buf_dir, "sl.log"), "w")
    sl = subprocess.Popen(["streamlink", "--stdout", url, QUALITY,
                           "--stream-timeout", "120",
                           "--hls-playlist-reload-attempts", "10"],
                          stdout=subprocess.PIPE, stderr=errf)
    ff = subprocess.Popen([
        "ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-c", "copy",
        "-f", "segment", "-segment_time", "5", "-strftime", "1",
        os.path.join(buf_dir, "seg_%s.ts"),
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
                if time.time() - int(re.search(r"seg_(\d+)", f).group(1)) > 180:
                    os.remove(f)
            except Exception:
                pass
        done.wait(20)


def make_clip(name, buf_dir, trigger_time, n, score):
    time.sleep(POST_SECONDS + 8)
    start, end = trigger_time - PRE_SECONDS, trigger_time + POST_SECONDS
    segs = []
    for f in sorted(glob.glob(os.path.join(buf_dir, "seg_*.ts"))):
        t = int(re.search(r"seg_(\d+)", f).group(1))
        if start - 5 <= t <= end:
            segs.append(f)
    if not segs:
        print("no segments found")
        return
    lst = os.path.join(buf_dir, f"list_{n}.txt")
    with open(lst, "w") as fh:
        for s in segs:
            fh.write(f"file '{os.path.abspath(s)}'\n")
    out = f"clip_{name}_{n}.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", lst, "-c", "copy", "-bsf:a", "aac_adtstoasc", out])
    if os.path.exists(out):
        if tg_video(out, f"{name} clip #{n} (score {score})"):
            STATE[name]["clips"] += 1
        os.remove(out)


def compute(times, now, min_msgs, watch_start):
    recent = [t for t in times if now - t[0] <= 10]
    older_count = len(times) - len(recent)
    history = max(10.0, min(110.0, now - watch_start - 10))
    baseline = (older_count / history) * 10
    hype_recent = sum(1 for t in recent if t[1])
    score = len(recent) + hype_recent
    threshold = max(min_msgs, baseline * MULTIPLIER)
    return len(recent), score, threshold, baseline


def status_text(name):
    st = STATE[name]
    left = max(0, int((MAX_RUNTIME - (time.time() - START)) / 60))
    ago = f"{int(time.time() - st['last_clip'])}s ago" if st["last_clip"] else "none yet"
    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
    state = "LIVE 🔴" if st["live"] else "reconnecting / ended"
    warm = max(0, int(WARMUP - (time.time() - st["watch_start"]))) if st["live"] else 0
    if warm > 0:
        state += f" (warming up, {warm}s)"
    return (f"📊 {name} tracker\n"
            f"Status: {state}\n"
            f"Chat speed: {st['rate']} msgs / 10s\n"
            f"Normal speed: {st['baseline']:.0f} msgs / 10s\n"
            f"Score: {st['score']} (needs {st['threshold']:.0f} to clip)\n"
            f"Clips sent: {st['clips']}\n"
            f"Last clip: {ago}\n"
            f"Bot time left: {left} min\n"
            f"Updated: {now_str} UTC")


def status_loop(name, done):
    msg_id = tg_text(status_text(name))
    while not done.is_set():
        done.wait(STATUS_EVERY)
        if msg_id:
            tg_edit(msg_id, status_text(name))
    if msg_id:
        tg_edit(msg_id, status_text(name))


def read_err(buf_dir):
    try:
        return open(os.path.join(buf_dir, "sl.log")).read()[-400:]
    except Exception:
        return "unknown error"


def watch(name, vid, min_msgs, stop):
    buf_dir = f"buffer_{name}"
    watch_start = time.time()
    STATE[name] = {"live": False, "rate": 0, "score": 0, "threshold": min_msgs,
                   "baseline": 0, "clips": 0, "last_clip": 0, "watch_start": watch_start}
    done = threading.Event()
    times = collections.deque()
    last_clip = 0
    n = 0
    fails = 0
    first = True
    status_thread = None
    sl = ff = None
    final_msg = None
    try:
        while not stop.is_set() and time.time() - START < MAX_RUNTIME:
            sl, ff = start_recorder(vid, buf_dir)
            time.sleep(12)
            if sl.poll() is not None or ff.poll() is not None:
                err = read_err(buf_dir)
                kill_procs(sl, ff)
                fails += 1
                if first or fails >= MAX_RETRIES:
                    tg_text(f"{name}: couldn't open the stream.\n{err}")
                    break
                tg_text(f"{name}: couldn't reconnect (try {fails}/{MAX_RETRIES}). Retrying...")
                time.sleep(20)
                continue

            try:
                chat = pytchat.create(video_id=vid, interruptable=False)
                chat_ok = chat.is_alive()
                chat_err = "chat is not available for this stream"
            except Exception as e:
                chat_ok = False
                chat_err = f"couldn't read the chat: {str(e)[:150]}"
            if not chat_ok:
                kill_procs(sl, ff)
                fails += 1
                if first or fails >= MAX_RETRIES:
                    tg_text(f"{name}: {chat_err}")
                    break
                time.sleep(20)
                continue

            if first:
                first = False
                watch_start = time.time()
                STATE[name]["watch_start"] = watch_start
                tg_text(f"{name}: stream opened. Watching chat (learning for {WARMUP}s first)...")
                threading.Thread(target=cleanup_loop, args=(buf_dir, done), daemon=True).start()
                status_thread = threading.Thread(target=status_loop, args=(name, done))
                status_thread.start()
            STATE[name]["live"] = True
            run_started = time.time()

            reason = ""
            while True:
                if stop.is_set():
                    reason = "stop"
                    break
                if time.time() - START >= MAX_RUNTIME:
                    reason = "time limit"
                    break
                if sl.poll() is not None or ff.poll() is not None:
                    reason = "video feed dropped"
                    break
                if not chat.is_alive():
                    reason = "chat reader stopped"
                    break
                for c in chat.get().sync_items():
                    hype = any(w in c.message.lower() for w in HYPE_WORDS)
                    times.append((time.time(), hype))
                now = time.time()
                while times and now - times[0][0] > 120:
                    times.popleft()
                rate, score, threshold, baseline = compute(times, now, min_msgs, watch_start)
                STATE[name].update({"rate": rate, "score": score,
                                    "threshold": threshold, "baseline": baseline})
                warmed = now - watch_start >= WARMUP
                if warmed and score >= threshold and now - last_clip > COOLDOWN:
                    last_clip = now
                    n += 1
                    STATE[name]["last_clip"] = now
                    print(f"[{name}] spike! score={score} threshold={threshold:.1f}")
                    threading.Thread(target=make_clip,
                                     args=(name, buf_dir, now, n, score), daemon=True).start()
                time.sleep(1)

            kill_procs(sl, ff)
            STATE[name]["live"] = False
            if reason in ("stop", "time limit"):
                final_msg = f"{name}: stopped ({reason})."
                break
            if time.time() - run_started > 120:
                fails = 0
            fails += 1
            if fails >= MAX_RETRIES:
                final_msg = f"{name}: stopped (last reason: {reason}). The stream may have ended."
                break
            tg_text(f"{name}: {reason}. Reconnecting (try {fails}/{MAX_RETRIES})...")
            time.sleep(10)
        if final_msg is None and (stop.is_set() or time.time() - START >= MAX_RUNTIME):
            final_msg = f"{name}: stopped."
    except Exception as e:
        final_msg = f"{name}: error - {str(e)[:300]}"
    finally:
        STATE[name]["live"] = False
        done.set()
        kill_procs(sl, ff)
        if status_thread:
            status_thread.join(timeout=60)
        if final_msg:
            tg_text(final_msg)
        ACTIVE.pop(name, None)
        VIDS.pop(name, None)


def handle_message(text):
    low = text.lower().strip()
    if low.startswith("/stop"):
        for ev in list(ACTIVE.values()):
            ev.set()
        tg_text("Stopping all streams.")
        return
    if low.startswith("/help"):
        tg_text("Just send a YouTube live link and I'll start clipping.\n"
                "Optional: add a name after it (Speed) and min=250 to make it less sensitive.\n"
                "Example: https://youtube.com/live/xxxx Speed min=250\n/stop stops everything.")
        return
    m = VID_RE.search(text)
    if not m:
        return
    vid = m.group(1)
    if vid in VIDS.values():
        tg_text("Already watching that stream.")
        return
    tokens = text.split()
    min_msgs = DEFAULT_MIN_MSGS
    words = []
    for t in tokens:
        if VID_RE.search(t):
            continue
        mm = re.fullmatch(r"min=(\d+)", t.lower())
        if mm:
            min_msgs = int(mm.group(1))
        else:
            words.append(t)
    name = re.sub(r"[^\w-]", "", "_".join(words)) or f"Stream{len(ACTIVE) + 1}"
    while name in ACTIVE:
        name += "2"
    stop = threading.Event()
    ACTIVE[name] = stop
    VIDS[name] = vid
    tg_text(f"Got it. Starting {name} (min={min_msgs})...")
    threading.Thread(target=watch, args=(name, vid, min_msgs, stop), daemon=True).start()


def main():
    tg_text("Clip bot started. Send me a YouTube live link to start clipping (send /help for tips).")
    offset = 0
    while time.time() - START < MAX_RUNTIME:
        try:
            r = requests.get(f"{API}/getUpdates", params={"offset": offset, "timeout": 25}, timeout=40)
            updates = r.json().get("result", [])
        except Exception as e:
            print("poll error:", e)
            time.sleep(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
                continue
            text = (msg.get("text") or "").strip()
            if text:
                try:
                    handle_message(text)
                except Exception as e:
                    tg_text(f"Error: {str(e)[:200]}")
    for ev in list(ACTIVE.values()):
        ev.set()
    time.sleep(15)
    tg_text("Bot time limit reached. Start it again when you need it.")


if __name__ == "__main__":
    main()
