import os, re, json, time, glob, subprocess, threading, collections
from datetime import datetime, timezone
import requests
import pytchat

# ---------------- CONFIG ----------------
STREAMERS = [
    {"name": "Peanut", "handle": "theburntpeanut", "min_msgs": 15, "multiplier": 3.0},
    # Add more later, one line each:
    # {"name": "Speed", "handle": "ishowspeed", "min_msgs": 40, "multiplier": 3.0},
]
PRE_SECONDS = 30
POST_SECONDS = 10
COOLDOWN = 60
MAX_RUNTIME = 5.5 * 3600
STATUS_EVERY = 30
HYPE_WORDS = ["lmao", "lmfao", "clip", "pog", "omg", "wtf", "bruh", "💀", "😂", "🤣"]
# -----------------------------------------

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
START = time.time()
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
API = f"https://api.telegram.org/bot{TOKEN}"
STATE = {}


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
        return r.status_code == 200
    except Exception as e:
        print("telegram video error:", e)
        return False


def find_live(handle):
    url = f"https://www.youtube.com/@{handle}/live"
    try:
        p = subprocess.run(["streamlink", "--json", url],
                           capture_output=True, text=True, timeout=90)
        data = json.loads(p.stdout or "{}")
        if data.get("streams") and not data.get("error"):
            vid = (data.get("metadata") or {}).get("id")
            if vid:
                return vid
            m = re.search(r"v=([\w-]{11})", p.stdout)
            if m:
                return m.group(1)
        print("streamlink says:", str(data.get("error") or p.stderr)[:300])
    except Exception as e:
        print("find_live error:", e)
    try:
        r = requests.get(url, headers=UA,
                         cookies={"CONSENT": "YES+1", "SOCS": "CAI"}, timeout=20)
        m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})"', r.text)
        if m and '"isLive":true' in r.text:
            return m.group(1)
    except Exception as e:
        print("find_live fallback error:", e)
    return None


def start_recorder(video_id, buf_dir):
    os.makedirs(buf_dir, exist_ok=True)
    url = f"https://www.youtube.com/watch?v={video_id}"
    sl = subprocess.Popen(["streamlink", "--stdout", url, "720p,best"], stdout=subprocess.PIPE)
    ff = subprocess.Popen([
        "ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-c", "copy",
        "-f", "segment", "-segment_time", "5", "-strftime", "1",
        os.path.join(buf_dir, "seg_%s.ts"),
    ], stdin=sl.stdout)
    return sl, ff


def cleanup_loop(buf_dir, stop):
    while not stop.is_set():
        for f in glob.glob(os.path.join(buf_dir, "seg_*.ts")):
            try:
                if time.time() - int(re.search(r"seg_(\d+)", f).group(1)) > 180:
                    os.remove(f)
            except Exception:
                pass
        time.sleep(20)


def make_clip(name, buf_dir, trigger_time, n):
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
        if tg_video(out, f"{name} clip #{n}"):
            STATE[name]["clips"] += 1
        os.remove(out)


def compute(times, now, s):
    recent = [t for t in times if now - t[0] <= 10]
    older = len(times) - len(recent)
    baseline = (older / 110.0) * 10
    hype_recent = sum(1 for t in recent if t[1])
    score = len(recent) + hype_recent
    threshold = max(s["min_msgs"], baseline * s["multiplier"])
    return len(recent), score, threshold


def status_text(name):
    st = STATE[name]
    left = max(0, int((MAX_RUNTIME - (time.time() - START)) / 60))
    if st["last_clip"]:
        ago = f"{int(time.time() - st['last_clip'])}s ago"
    else:
        ago = "none yet"
    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
    state = "LIVE 🔴" if st["live"] else "ended / offline"
    return (f"📊 {name} tracker\n"
            f"Status: {state}\n"
            f"Chat speed: {st['rate']} msgs / 10s\n"
            f"Score: {st['score']} (needs {st['threshold']:.0f} to clip)\n"
            f"Clips sent: {st['clips']}\n"
            f"Last clip: {ago}\n"
            f"Bot time left: {left} min\n"
            f"Updated: {now_str} UTC")


def status_loop(name, stop):
    msg_id = tg_text(status_text(name))
    while not stop.is_set():
        stop.wait(STATUS_EVERY)
        if msg_id:
            tg_edit(msg_id, status_text(name))
    if msg_id:
        tg_edit(msg_id, status_text(name))


def run_streamer(s):
    name = s["name"]
    buf_dir = f"buffer_{name}"
    STATE[name] = {"live": False, "rate": 0, "score": 0, "threshold": s["min_msgs"],
                   "clips": 0, "last_clip": 0}
    n = 0
    announced_offline = False
    while time.time() - START < MAX_RUNTIME:
        vid = find_live(s["handle"])
        if not vid:
            if not announced_offline:
                tg_text(f"{name} is offline. Waiting for the stream...")
                announced_offline = True
            print(f"[{name}] not live, checking again in 60s")
            time.sleep(60)
            continue

        announced_offline = False
        tg_text(f"{name} is live. Watching chat... ({vid})")
        STATE[name]["live"] = True
        sl, ff = start_recorder(vid, buf_dir)
        stop = threading.Event()
        threading.Thread(target=cleanup_loop, args=(buf_dir, stop), daemon=True).start()
        status_thread = threading.Thread(target=status_loop, args=(name, stop))
        status_thread.start()

        chat = pytchat.create(video_id=vid)
        times = collections.deque()
        last_clip = 0
        time.sleep(15)

        while chat.is_alive() and time.time() - START < MAX_RUNTIME:
            for c in chat.get().sync_items():
                hype = any(w in c.message.lower() for w in HYPE_WORDS)
                times.append((time.time(), hype))
            now = time.time()
            while times and now - times[0][0] > 120:
                times.popleft()
            rate, score, threshold = compute(times, now, s)
            STATE[name].update({"rate": rate, "score": score, "threshold": threshold})
            if score >= threshold and now - last_clip > COOLDOWN:
                last_clip = now
                n += 1
                STATE[name]["last_clip"] = now
                print(f"[{name}] spike! score={score} threshold={threshold:.1f}")
                threading.Thread(target=make_clip, args=(name, buf_dir, now, n), daemon=True).start()
            time.sleep(1)

        STATE[name]["live"] = False
        stop.set()
        status_thread.join(timeout=60)
        sl.kill()
        ff.kill()
        tg_text(f"{name} stream ended or time limit reached.")
        time.sleep(30)


if __name__ == "__main__":
    tg_text("Clip bot started.")
    threads = [threading.Thread(target=run_streamer, args=(s,)) for s in STREAMERS]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
