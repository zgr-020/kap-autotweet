# main.py
import os
import re
import json
import time
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, List
from datetime import datetime, timezone, timedelta

from playwright.sync_api import sync_playwright
import tweepy
from tweepy import TooManyRequests, TweepyException

# ===================== CONFIG =====================
@dataclass
class Config:
    api_key: Optional[str] = None
    api_key_secret: Optional[str] = None
    access_token: Optional[str] = None
    access_token_secret: Optional[str] = None
    max_per_run: int = 5
    cooldown_minutes: int = 15
    request_timeout: int = 30000
    browser_headless: bool = True

    @classmethod
    def from_env(cls):
        return cls(
            api_key=os.getenv("API_KEY"),
            api_key_secret=os.getenv("API_KEY_SECRET"),
            access_token=os.getenv("ACCESS_TOKEN"),
            access_token_secret=os.getenv("ACCESS_TOKEN_SECRET"),
        )

AKIS_URL = "https://fintables.com/borsa-haber-akisi"

# Kodu olmayan/olmaması gereken kelimeler
BANNED_CODES = {
    "AKIS","ILE","DUN","BUGUN","YER","SAHIP","ORTAKLARINDAN",
    "FINTABLES","BULTEN","GUNLUK","ANALIST","NOTLARI","YAYINDA",
    "DAYANIKLI","TUKETIM","URUNLERI","PIYASA","RAPOR","SEKTOR","HABERLER",
    "INFO"
}

# ===================== STATE =====================
class StateManager:
    def __init__(self, path: Path = Path("state.json")):
        self.path = path
        self._state = self._load_initial_state()

    def _load_initial_state(self) -> dict:
        default_state = {
            "last_id": None,
            "posted": [],
            "cooldown_until": None,
            "last_run": None,
        }
        if not self.path.exists():
            return default_state
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return {"last_id": None, "posted": data, "cooldown_until": None}
            return {**default_state, **data}
        except Exception:
            return default_state

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._state, f, ensure_ascii=False, indent=2)

    def is_posted(self, item_id: str) -> bool:
        return item_id in self._state["posted"]

    def mark_posted(self, item_id: str):
        if not self.is_posted(item_id):
            self._state["posted"].append(item_id)

    def set_cooldown(self, minutes: int):
        self._state["cooldown_until"] = (
            datetime.now(timezone.utc) + timedelta(minutes=minutes)
        ).isoformat()

    def is_in_cooldown(self) -> bool:
        cu = self._state.get("cooldown_until")
        if not cu:
            return False
        try:
            dt_ = datetime.fromisoformat(cu.replace("Z", "+00:00"))
            return datetime.now(timezone.utc) < dt_
        except Exception:
            self._state["cooldown_until"] = None
            return False

    @property
    def last_id(self) -> Optional[str]:
        return self._state["last_id"]

    @last_id.setter
    def last_id(self, v: Optional[str]):
        self._state["last_id"] = v

# ===================== LOG =====================
def setup_logging():
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")

def log(msg: str, level: str = "info"):
    getattr(logging, level.lower())(msg)

# ===================== TWITTER =====================
def twitter_client(config: Config) -> Optional[tweepy.Client]:
    if not all([config.api_key, config.api_key_secret, config.access_token, config.access_token_secret]):
        log("Twitter secrets missing, tweeting disabled", "warning")
        return None
    try:
        return tweepy.Client(
            consumer_key=config.api_key,
            consumer_secret=config.api_key_secret,
            access_token=config.access_token,
            access_token_secret=config.access_token_secret,
        )
    except Exception as e:
        log(f"Twitter client init failed: {e}", "error")
        return None

def send_tweet(client: Optional[tweepy.Client], tweet_text: str) -> bool:
    if not client:
        log(f"SIMULATION: {tweet_text}")
        return True
    try:
        client.create_tweet(text=tweet_text)
        log("Tweet sent")
        return True
    except TooManyRequests:
        log("Rate limit exceeded", "warning")
        raise
    except TweepyException as e:
        log(f"Twitter error: {e}", "error")
        return False
    except Exception as e:
        log(f"Tweet error: {e}", "error")
        return False

# ===================== EXTRACTOR (DOM) =====================
JS_EXTRACTOR_DOM = """
() => {
  try {
    const items = [];
    const rows = Array.from(document.querySelectorAll('main li, main article, main div'))
      .filter(el => /\\bKAP\\b\\s*[•·\\-\\.]/i.test(el.innerText || ''));

    const banned = new Set([
      "AKIS","ILE","DUN","BUGUN","YER","SAHIP","ORTAKLARINDAN",
      "FINTABLES","BULTEN","GUNLUK","ANALIST","NOTLARI","YAYINDA",
      "DAYANIKLI","TUKETIM","URUNLERI","PIYASA","RAPOR","SEKTOR","HABERLER","INFO"
    ]);
    const isCodeLike = (t) => /^[A-ZÇĞİÖŞÜ]{3,5}$/.test(t) && !banned.has(t);

    for (const row of rows) {
      const text = (row.innerText || '').replace(/\\s+/g,' ').trim();

      let codes = Array.from(row.querySelectorAll('a'))
        .map(a => (a.textContent || '').trim().toUpperCase())
        .filter(isCodeLike);

      if (codes.length === 0) {
        const head = text.split(/[|–—-]/)[0];
        const toks = head.replace(/.*\\bKAP\\b\\s*[•·\\-\\.]/i,'')
                         .trim()
                         .split(/[\\s\\/]+/)
                         .map(t => t.toUpperCase());
        codes = toks.filter(isCodeLike).slice(0, 2);
      }
      if (!codes.length) continue;

      const last = codes[codes.length - 1];
      const start = text.toUpperCase().indexOf(last) + last.length;
      let content = text.slice(start).trim();
      content = content
        .replace(/^[:\\-–—\\|\\.]\\s*/,'')
        .replace(/\\b(Dün|Bugün)\\b.*$/i,'')
        .replace(/\\b\\d{1,2}:\\d{2}\\b.*$/,'')
        .trim();

      if (!content || content.length < 30) continue;
      if (/yatırım tavsiyesi|yasal uyarı|kişisel veri|kvk/i.test(content)) continue;

      const hash = (text.split('').reduce((a,c)=>(a*31 + c.charCodeAt(0))>>>0,0)).toString(16);
      const id = `kap-${codes.join('-')}-${hash}`;

      if (!items.find(x => x.id === id)) {
        items.push({ id, codes, content, raw: text });
      }
    }
    return items;
  } catch(e) { console.error(e); return []; }
}
"""

# ===================== CONTENT =====================
def extract_clean_content(text: str) -> str:
    if not text:
        return ""
    sentences = re.split(r"[.!?]+", text)
    if sentences and sentences[0].strip():
        s0 = sentences[0].strip()
        if len(s0) < 40 and len(sentences) > 1:
            combined = (s0 + ". " + sentences[1].strip()).strip()
            if not combined.endswith("."):
                combined += "."
            return combined
        if not s0.endswith("."):
            s0 += "."
        return s0
    return text.strip()

def build_tweet_quanta_style(codes: List[str], content: str) -> str:
    if not codes:
        return ""
    codes_str = " ".join([f"#{c}" for c in codes])
    clean = extract_clean_content(content)
    if not clean:
        return ""
    base = f"📰 {codes_str} | {clean}"
    if len(base) <= 280:
        return base
    max_len = 280 - len(f"📰 {codes_str} | ") - 3
    shortened = clean[:max_len]
    sp = shortened.rfind(" ")
    if sp > max_len * 0.7:
        shortened = shortened[:sp]
    return f"📰 {codes_str} | {shortened}..."

def is_valid_news_content(text: str) -> bool:
    if not text or len(text) < 30:
        return False
    tl = text.lower()
    for p in ["yatırım tavsiyesi değildir", "yasal uyarı", "kişisel veri", "kvk", "saygılarımızla", "kamunun bilgisine"]:
        if p in tl:
            return False
    return True

# ===================== BROWSER =====================
class BrowserManager:
    def __init__(self, config: Config):
        self.config = config

    def __enter__(self):
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(
            headless=self.config.browser_headless,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        self.ctx = self.browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36"),
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            viewport={"width": 1920, "height": 1080},
        )
        self.page = self.ctx.new_page()
        self.page.set_default_timeout(self.config.request_timeout)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self.ctx.close()
        finally:
            try:
                self.browser.close()
            finally:
                self.pw.stop()

    def goto_with_retry(self, url: str, retries: int = 3) -> bool:
        for i in range(retries):
            try:
                log(f"Navigation attempt {i+1}/{retries}")
                self.page.goto(url, wait_until="networkidle")
                self.page.wait_for_timeout(1200)
                return True
            except Exception as e:
                log(f"Attempt {i+1} failed: {e}", "warning")
                if i < retries - 1:
                    time.sleep(5)
        return False

    def click_highlights_tab(self) -> bool:
        sels = [
            "button:has-text('Öne Çıkanlar')",
            "a:has-text('Öne Çıkanlar')",
            "div:has-text('Öne Çıkanlar')",
            "text=Öne Çıkanlar",
        ]
        for s in sels:
            try:
                if self.page.locator(s).is_visible(timeout=2000):
                    self.page.click(s)
                    self.page.wait_for_timeout(1000)
                    return True
            except Exception:
                continue
        return False

    def extract_simple_items(self) -> List[dict]:
        try:
            log("Using DOM extractor (KAP + blue codes)…")
            self.page.evaluate("window.scrollTo(0, 400)")
            self.page.wait_for_timeout(1200)
            items = self.page.evaluate(JS_EXTRACTOR_DOM)
            log(f"DOM extractor found {len(items)} items")
            return items
        except Exception as e:
            log(f"DOM extraction failed: {e}", "error")
            return []

# ===================== PROCESS =====================
def process_new_items(items: List[dict], state: StateManager, config: Config,
                      tw_client: Optional[tweepy.Client]) -> int:
    sent = 0
    for it in items:
        if sent >= config.max_per_run:
            break
        if state.is_posted(it["id"]):
            continue
        if not is_valid_news_content(it["content"]):
            continue

        tweet = build_tweet_quanta_style(it["codes"], it["content"])
        if not tweet:
            continue

        log(f"Tweeting: {tweet}")
        try:
            ok = send_tweet(tw_client, tweet)
            if ok:
                state.mark_posted(it["id"])
                sent += 1
                if sent < config.max_per_run and tw_client:
                    time.sleep(2)
        except TooManyRequests:
            state.set_cooldown(config.cooldown_minutes)
            log(f"Cooldown activated for {config.cooldown_minutes} minutes")
            break
        except Exception as e:
            log(f"Error while tweeting: {e}", "warning")
            continue
    return sent

# ===================== MAIN =====================
def main():
    setup_logging()
    log("Starting...")

    config = Config.from_env()
    state = StateManager()
    twitter = twitter_client(config)

    if state.is_in_cooldown():
        log("In cooldown, exiting")
        return

    try:
        with BrowserManager(config) as br:
            if not br.goto_with_retry(AKIS_URL):
                log("Page load failed")
                return

            br.click_highlights_tab()
            br.page.wait_for_timeout(1200)

            items = br.extract_simple_items()
            if not items:
                log("No items found")
                return

            # last_id mantığı: en üstteki en yeni
            if state.last_id:
                # state.last_id görülene kadar al
                new_items = []
                for it in items:
                    if it["id"] == state.last_id:
                        break
                    new_items.append(it)
            else:
                new_items = items

            if not new_items:
                log("No new items")
                return

            # Eskiden yeniye
            new_items = new_items[::-1]
            sent_count = process_new_items(new_items[:config.max_per_run], state, config, twitter)

            # son çalıştırmada gördüğümüz en yeni id
            state.last_id = items[0]["id"]
            state.save()
            log(f"Done. Sent {sent_count} tweets")
    except Exception as e:
        log(f"Fatal error: {e}", "error")

if __name__ == "__main__":
    main()
