import os, re, time, glob, subprocess, threading, collections
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
HYPE_WORDS = ["lmao", "lmfao", "clip", "pog", "omg", "wtf", "bruh", "💀", "😂", "🤣"]
# -----------------------------------------

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
START = time.time()
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}


def tg_text(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": msg}, timeout=30)
    except Exception as e:
        print("telegram text error:", e)


def tg_video(path, caption):
    try:
        with open(path, "rb") as f:
            r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendVideo",
                              data={"chat_id": CHAT_ID, "caption": caption},
                              files={"video": f}, timeout=300)
        print("telegram video:", r.status_code)
    except Exception as e:
        print("telegram video error:", e)


def find_live(handle):
    try:
        r = requests.get(f"https://www.youtube.com/@{handle}/live", headers=UA,
                         cookies={"CONSENT": "YES+1", "SOCS": "CAI"}, timeout=20)
        m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})"', r.text)
        if m and '"isLive":true' in r.text:
            return m.group(1)
    except Exception as e:
        print("find_live error:", e)
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
        tg_video(out, f"{name} clip #{n}")
        os.remove(out)


def run_streamer(s):
    name = s["name"]
    buf_dir = f"buffer_{name}"
    n = 0
    while time.time() - START < MAX_RUNTIME:
        vid = find_live(s["handle"])
        if not vid:
            print(f"[{name}] not live, checking again in 60s")
            time.sleep(60)
            continue

        tg_text(f"{name} is live. Watching chat... ({vid})")
        sl, ff = start_recorder(vid, buf_dir)
        stop = threading.Event()
        threading.Thread(target=cleanup_loop, args=(buf_dir, stop), daemon=True).start()

        chat = pytchat.create(video_id=vid)
        times = collections.deque()
        last_clip = 0
        time.sleep(15)

        while chat.is_alive() and time.time() - START < MAX_RUNTIME:
            for c in chat.get().sync_items():
                now = time.time()
                hype = any(w in c.message.lower() for w in HYPE_WORDS)
                times.append((now, hype))
                while times and now - times[0][0] > 120:
                    times.popleft()
                recent = [t for t in times if now - t[0] <= 10]
                older_count = len(times) - len(recent)
                baseline = (older_count / 110.0) * 10
                hype_recent = sum(1 for t in recent if t[1])
                score = len(recent) + hype_recent
                threshold = max(s["min_msgs"], baseline * s["multiplier"])
                if score >= threshold and now - last_clip > COOLDOWN:
                    last_clip = now
                    n += 1
                    print(f"[{name}] spike! score={score} threshold={threshold:.1f}")
                    threading.Thread(target=make_clip, args=(name, buf_dir, now, n), daemon=True).start()
            time.sleep(1)

        stop.set()
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
