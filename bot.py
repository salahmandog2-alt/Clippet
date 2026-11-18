import os, re, time, glob, subprocess, threading, collections
import requests
import pytchat

# ---------------- CONFIG ----------------
STREAMERS = [
    {"name": "Peanut", "handle": "theburntpeanut", "min_msgs": 15, "multiplier": 3.0},
    # Add more later, one line each:
    # {"name": "Speed", "handle": "ishowspeed", "min_msgs": 40, "multiplier": 3.0},
]
PRE_SECONDS = 30       # seconds before the moment
POST_SECONDS = 10      # seconds after the moment
COOLDOWN = 60          # min seconds between clips per streamer
MAX_RUNTIME = 5.5 * 3600  # stop before GitHub's 6h limit
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
