"""
Twitch Chat → Sensational News Ticker (All Local LLM)

Requirements:
  pip install flask requests twitchio python-dotenv
  Ollama locally installed & model pulled
"""

import os
import re
import json
import threading
import random
from collections import deque
from flask import Flask, jsonify, render_template, make_response
import requests
from twitchio.ext import commands
from dotenv import load_dotenv



# ---------------------- Load .env ----------------------
load_dotenv()
TWITCH_TOKEN = os.getenv("TWITCH_TOKEN")
TWITCH_CHANNEL = os.getenv("TWITCH_CHANNEL")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "phi3")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
PORT = int(os.getenv("PORT", 5100))
HEADLINE_MAX_WORDS = int(os.getenv("HEADLINE_MAX_WORDS", 14))
MESSAGE_RATE = float(os.getenv("MESSAGE_RATE", 0.25))
LANGUAGE = os.getenv("LANGUAGE", "english")

if not TWITCH_TOKEN or not TWITCH_CHANNEL:
    print("[SETUP] Please set TWITCH_TOKEN and TWITCH_CHANNEL environment variables before running.")

# ---------------------- Headlines ----------------------
MAX_HEADLINES = 100
headlines = deque(maxlen=MAX_HEADLINES)
_update_counter = 0
_update_lock = threading.Lock()


def mark_updated():
    global _update_counter
    with _update_lock:
        _update_counter += 1
        return _update_counter


def get_update_counter():
    with _update_lock:
        return _update_counter


# ---------------------- Text Cleaning ----------------------
def clean_message(msg: str) -> str:
    if not msg:
        return ""
    msg = re.sub(r"https?://\S+", "", msg)
    msg = re.sub(r"\s+", " ", msg).strip()
    if len(msg) > 300:
        msg = msg[:300] + "…"
    return msg


def template_fallback(username: str, message: str) -> str:
    base = f"BREAKING: {username} {message[:80]}".strip()
    options = [
        f"BREAKING: {username} Stuns The Internet — {message}",
        f"ALERT: {username} Sparks Frenzy — {message}",
        f"LIVE NOW: {username} Shocks Chat — {message}",
        f"HEADS UP: {username} Just Dropped This — {message}",
    ]
    best = max(options, key=len)
    return smart_title(best)


def smart_title(s: str) -> str:
    def fix(word: str) -> str:
        if word.isupper():
            return word
        return word[:1].upper() + word[1:]
    return " ".join(fix(w) for w in s.split())


# ---------------------- Local LLM ----------------------
PROMPT_TEMPLATE = (
    "Rewrite '{message}' into playful, short, sensational but harmless tabloid headline\n"
    f"- Max {HEADLINE_MAX_WORDS} words.\n"
    "- Title Case.\n"
    "- Keep it short and simple\n"
    "- Make sure the headline is complete, no cut-offs\n"
    "- No emojis.\n"
    "- No adult, political, or toxic content.\n"
    "- Base only on provided username + message.\n\n"
    "- Never add 'notes'\n"
    "Username: {username}\n"
    "Message: {message}\n"
    "Always keep {username} intact!\n"
    "Final headline in {LANGUAGE} only.\n"
)

def generate_headline_local_llm(username: str, message: str) -> str:
    prompt = PROMPT_TEMPLATE.format(username=username, message=message, HEADLINE_MAX_WORDS=HEADLINE_MAX_WORDS, LANGUAGE=LANGUAGE)
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=60,
        )
        resp.raise_for_status()
        text = resp.json().get("response", "").strip()
        if not text:
            raise ValueError("Empty response")
        
        # Clean up the response - take only the first line to avoid meta-commentary
        lines = text.split('\n')
        headline = lines[0].strip()
        
        # Remove quotes if the LLM added them
        headline = headline.strip('"').strip("'")
        
        # Enforce word limit
        words = headline.split()
        if len(words) > HEADLINE_MAX_WORDS:
            headline = " ".join(words[:HEADLINE_MAX_WORDS])
        
        # Force fix username if LLM changed it (post-processing safety net)
        if username.lower() in headline.lower():
            # Find and replace any case variations of the username
            headline = re.sub(re.escape(username), username, headline, flags=re.IGNORECASE)
        
        headline = re.sub(r"\s+", " ", headline)
        return smart_title(headline)
    except Exception as e:
        print(f"[LLM] Falling back to template due to error: {e}")
        return template_fallback(username, message)


# ---------------------- Twitch Bot ----------------------
class ChatToTickerBot(commands.Bot):
    def __init__(self):
        super().__init__(
            token=TWITCH_TOKEN,
            prefix="!",
            initial_channels=[TWITCH_CHANNEL],
            fetch_self=False,
        )

    async def event_ready(self):
        print(f"[TWITCH] Logged in as: {self.nick}")
        print(f"[TWITCH] Monitoring: #{TWITCH_CHANNEL}")

    async def event_message(self, message):
        username = message.author.name if message.author else "someone"
        raw = message.content or ""
        cleaned = clean_message(raw)
        if not cleaned:
            return
        
        # Configurable random chance to process each message
        if random.random() > MESSAGE_RATE:
            return
            
        headline = generate_headline_local_llm(username, cleaned)
        headlines.appendleft(headline)
        mark_updated()
        print(f"[HEADLINE] {headline}")


def run_twitch_bot():
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot = ChatToTickerBot()
    loop.run_until_complete(bot.start())


# ---------------------- Flask Web ----------------------
app = Flask(__name__, template_folder="templates")

@app.get("/")
def index():
    config = {
        "API_POLL_INTERVAL": int(os.getenv("API_POLL_INTERVAL", 2000)),
        "API_MAX_FAILURES": int(os.getenv("API_MAX_FAILURES", 5)),
        "TICKER_SPEED": int(os.getenv("TICKER_SPEED", 50)),
    }
    resp = make_response(render_template("index.html", config=json.dumps(config)))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/api/headlines")
def api_headlines():
    return jsonify({
        "version": get_update_counter(),
        "items": list(headlines)
    })


# ---------------------- Main ----------------------
def main():
    # Start with empty headlines for a cleaner experience
    t = threading.Thread(target=run_twitch_bot, name="twitch-bot", daemon=True)
    t.start()
    print(f"[WEB] Serving ticker at http://localhost:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()