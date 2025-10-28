import os
import re
import json
import time
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, List, Set
from datetime import datetime, timezone, timedelta

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import tweepy
from tweepy import TooManyRequests, TweepyException

# ============== CONFIGURATION ==============
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
            access_token_secret=os.getenv("ACCESS_TOKEN_SECRET")
        )

# ============== CUSTOM EXCEPTIONS ==============
class TwitterError(Exception): ...
class ScrapingError(Exception): ...
class StateError(Exception): ...

# ============== STATE MANAGEMENT ==============
class StateManager:
    def __init__(self, path: Path = Path("state.json")):
        self.path = path
        self._state = self._load_initial_state()

    def _load_initial_state(self) -> dict:
        default_state = {
            "last_id": None,
            "posted": [],
            "cooldown_until": None,
            "last_run": None
        }
        if not self.path.exists():
            return default_state
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, list):
                return {"last_id": None, "posted": data, "cooldown_until": None}
            return {**default_state, **data}
        except Exception as e:
            logging.warning(f"State file corrupted, resetting: {e}")
            return default_state

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, 'w', encoding='utf-8') as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            raise StateError(f"Could not save state: {e}")

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
        if not self._state["cooldown_until"]:
            return False
        try:
            cooldown_dt = datetime.fromisoformat(
                self._state["cooldown_until"].replace("Z", "+00:00")
            )
            return datetime.now(timezone.utc) < cooldown_dt
        except Exception:
            self._state["cooldown_until"] = None
            return False

    @property
    def last_id(self) -> Optional[str]:
        return self._state["last_id"]

    @last_id.setter
    def last_id(self, value: Optional[str]):
        self._state["last_id"] = value

    def cleanup_old_entries(self, max_entries: int = 1000):
        if len(self._state["posted"]) > max_entries:
            self._state["posted"] = self._state["posted"][-max_entries:]

# ============== LOGGING ==============
def setup_logging():
    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s', datefmt='%H:%M:%S')

def log(msg: str, level: str = "info"):
    logger = getattr(logging, level.lower())
    logger(msg)

# ============== TWITTER CLIENT ==============
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
        log(f"Twitter client initialization failed: {e}", "error")
        return None

# ============== CONSTANTS ==============
AKIS_URL = "https://fintables.com/borsa-haber-akisi"

# ============== CONTENT PROCESSING ==============
TR_UP = "A-ZÇĞİÖŞÜ"
CODE_RE = rf"[{TR_UP}]{{3,5}}(?:[0-9])?"

def _uniq_in_order(seq: List[str]) -> List[str]:
    seen = set(); out=[]
    for s in seq:
        if s not in seen:
            seen.add(s); out.append(s)
    return out

def extract_codes_from_kap_text(text: str) -> List[str]:
    """
    KAP haber metninden yalnızca geçerli Borsa İstanbul hisse kodlarını çeker.
    Örnekler:
        "KAP - TERA/BVSAN"  →  ["TERA", "BVSAN"]
        "KAP • HDFGS"       →  ["HDFGS"]
        "KAP TERA ve BVSAN" →  ["TERA", "BVSAN"]
    """
    if not text:
        return []

    t = re.sub(r"\s+", " ", text.upper())

    # 'KAP' kelimesinden sonra gelen kısmı al
    after = re.split(r"\bKAP\b", t, maxsplit=1)
    cand = after[1] if len(after) > 1 else t

    # ayırıcıları normalize et
    cand = re.sub(r"[•·,;:|\\-]", " ", cand)
    cand = cand.replace("/", " ")
    cand = re.sub(r"VE", " ", cand)

    # olası kod adaylarını bul
    raw_codes = re.findall(r"\b[A-ZÇĞİÖŞÜ]{2,5}[0-9]?\b", cand)

    # filtre: yalnızca büyük harflerden oluşan, anlamlı uzunlukta olan kodlar
    banned = {
        "KAP","ADET","TEK","MİLYON","MILYON","TL","YÜZDE","PAY","HİSSE",
        "SIRKET","ŞİRKET","ORTAKLARINDAN","SAHIP","SAHİP","YATIRIM",
        "BORSASI","BİST","BIST","KAMU","BILGILENDIRME","FINANCIAL"
    }

    codes = [c for c in raw_codes if c not in banned and 2 < len(c) <= 6]

    # Türkçe harfleri büyük Latin’e çevir
    tr_map = str.maketrans("ÇĞİÖŞÜ", "CGIOSU")
    codes = [c.translate(tr_map) for c in codes]

    # sıralı benzersiz
    seen = set()
    unique = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            unique.append(c)

    # en fazla 3 kod
    return unique[:3]

def _smart_first_sentence(text: str, min_len: int = 40, max_len: int = 240) -> str:
    """
    Sayılardaki 1.125.000 gibi noktalara/dakika saatlerine takılmadan ilk cümleyi seçer.
    Gerekirse ikinci güvenli cümle sınırına kadar genişletir.
    """
    t = re.sub(r"\s+", " ", (text or "").strip())
    end = None
    for i, ch in enumerate(t):
        if ch in ".!?":
            prev_is_digit = (i > 0 and t[i-1].isdigit())
            next_is_digit = (i+1 < len(t) and t[i+1].isdigit())
            if not (prev_is_digit and next_is_digit):
                end = i
                break
    first = t if end is None else t[:end+1].strip()

    if len(first) < min_len:
        for j in range((end or 0)+1, len(t)):
            ch = t[j]
            if ch in ".!?":
                prev_is_digit = (j > 0 and t[j-1].isdigit())
                next_is_digit = (j+1 < len(t) and t[j+1].isdigit())
                if not (prev_is_digit and next_is_digit):
                    first = t[:j+1].strip()
                    break

    if len(first) > max_len:
        cut = first[:max_len]
        cut = cut[:cut.rfind(" ")] if " " in cut else cut
        first = cut.rstrip(".,;:!?"+" ") + "..."
    return first

def extract_clean_content(text: str) -> str:
    if not text:
        return ""
    t = text
    # KAP başlığı / saat / etiket kırpma
    t = re.sub(r'KAP\s*[•·\-\.\:]*\s*[A-ZÇĞİÖŞÜ/]{3,20}\s*(\d{1,2}:\d{2})?', '', t, flags=re.I)
    t = re.sub(r'\bFintables\b.*$', '', t, flags=re.I)
    t = re.sub(r'^\s*Şirket\s*', '', t, flags=re.I)
    # Gereksiz uyarılar
    t = re.sub(r'yatırım tavsiyesi değildir|yasal uyarı|kişisel veri|kvk|kamunun bilgisine|saygılarımızla',
               '', t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip()
    first = _smart_first_sentence(t)
    if first and first[-1] not in ".!?…":
        first += "."
    return first

def build_tweet_quanta_style(codes: List[str], content: str) -> str:
    if not codes:
        return ""
    codes_str = " ".join(f"#{c}" for c in codes)
    clean_content = extract_clean_content(content)
    if not clean_content:
        return ""
    tweet = f"📰 {codes_str} | {clean_content}"
    if len(tweet) <= 280:
        return tweet
    max_content = 280 - len(f"📰 {codes_str} | ")
    cut = clean_content[:max_content]
    cut = cut[:cut.rfind(" ")] if " " in cut else cut
    return f"📰 {codes_str} | {cut.rstrip('.,;:! ')}..."

def is_valid_news_content(text: str) -> bool:
    if not text or len(text) < 30:
        return False
    spam = ["yatırım tavsiyesi değildir", "yasal uyarı", "kişisel veri", "kvk", "saygılarımızla", "kamunun bilgisine"]
    tl = text.lower()
    return not any(s in tl for s in spam)

# ============== BROWSER & SCRAPING ==============
JS_EXTRACTOR_SIMPLE = """
() => {
    try {
        const items = [];
        const all = document.querySelectorAll('div, li, article, section, p');
        for (const el of all) {
            const text = (el.innerText || el.textContent || '').replace(/\\s+/g,' ').trim();
            if (!text || text.length < 40) continue;
            if (!/\\bKAP\\b/i.test(text)) continue;
            if (/yatırım tavsiyesi|yasal uyarı|kişisel veri|kvk/i.test(text)) continue;

            // kaba kod çıkarımı (Python tarafında tekrar düzelteceğiz)
            const m = text.match(/KAP\\s*[•·\\-\\.]?\\s*([A-ZÇĞİÖŞÜ]{3,5})(?:[\\/\\s]([A-ZÇĞİÖŞÜ]{3,5}))?/i);
            let codes = [];
            if (m) {
                if (m[1]) codes.push(m[1].toUpperCase());
                if (m[2]) codes.push(m[2].toUpperCase());
            }
            const hash = text.split('').reduce((a,c)=>(a*31+c.charCodeAt(0))>>>0,0);
            const id = `simple-${hash}`;
            items.push({ id, codes, content: text, raw: text });
        }
        return items;
    } catch(e) { return []; }
}
"""

class BrowserManager:
    def __init__(self, config: Config):
        self.config = config

    def __enter__(self):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=self.config.browser_headless,
            args=["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage","--disable-gpu"]
        )
        self.context = self.browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
            locale="tr-TR", timezone_id="Europe/Istanbul",
            viewport={"width": 1920, "height": 1080}
        )
        self.page = self.context.new_page()
        self.page.set_default_timeout(self.config.request_timeout)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if hasattr(self, 'context'): self.context.close()
        if hasattr(self, 'browser'): self.browser.close()
        if hasattr(self, 'playwright'): self.playwright.stop()

    def goto_with_retry(self, url: str, retries: int = 3) -> bool:
        for attempt in range(retries):
            try:
                log(f"Navigation attempt {attempt+1}/{retries}")
                self.page.goto(url, wait_until="networkidle")
                self.page.wait_for_timeout(2000)
                return True
            except Exception as e:
                log(f"Attempt {attempt+1} failed: {e}", "warning")
                if attempt < retries-1: time.sleep(5)
        return False

    def click_highlights_tab(self) -> bool:
        try:
            for sel in ["button:has-text('Öne Çıkanlar')","a:has-text('Öne Çıkanlar')","div:has-text('Öne Çıkanlar')","text=Öne Çıkanlar"]:
                try:
                    if self.page.locator(sel).is_visible(timeout=3000):
                        self.page.click(sel)
                        self.page.wait_for_timeout(1500)
                        return True
                except: continue
            return False
        except Exception as e:
            log(f"Error clicking tab: {e}", "error")
            return False

    def extract_simple_items(self) -> List[dict]:
        try:
            self.page.evaluate("window.scrollTo(0, 400)")
            self.page.wait_for_timeout(1200)
            raw_items = self.page.evaluate(JS_EXTRACTOR_SIMPLE)
            log(f"Simple extractor found {len(raw_items)} items")
            return raw_items
        except Exception as e:
            log(f"Simple extraction failed: {e}", "error")
            return []

# ============== TWITTER OPERATIONS ==============
def send_tweet(client: Optional[tweepy.Client], tweet_text: str) -> bool:
    if not client:
        log(f"SIMULATION: {tweet_text}")
        return True
    try:
        client.create_tweet(text=tweet_text)
        log("Tweet sent successfully")
        return True
    except TooManyRequests:
        log("Rate limit exceeded", "warning")
        raise TwitterError("Rate limit exceeded")
    except TweepyException as e:
        log(f"Twitter error: {e}", "error")
        raise TwitterError(f"Twitter error: {e}")
    except Exception as e:
        log(f"Tweet error: {e}", "error")
        return False

# ============== MAIN LOGIC ==============
def process_new_items(items: List[dict], state: StateManager, config: Config, twitter_client_obj: Optional[tweepy.Client]) -> int:
    sent_count = 0
    for item in items:
        if sent_count >= config.max_per_run: break
        if state.is_posted(item["id"]): continue
        if not is_valid_news_content(item.get("content","")): continue

        # >>> Kodları Python tarafında kesinleştir <<<
        fixed_codes = extract_codes_from_kap_text(item.get("content","") or item.get("raw",""))
        if not fixed_codes and item.get("codes"):
            fixed_codes = extract_codes_from_kap_text("KAP " + " ".join(item["codes"]) + " " + (item.get("content","")))
        fixed_codes = _uniq_in_order(fixed_codes)[:3]
        if not fixed_codes:
            log("SKIP: no valid codes found")
            continue

        try:
            tweet_text = build_tweet_quanta_style(fixed_codes, item["content"])
            if not tweet_text: continue
            log(f"Tweeting: {tweet_text}")
            if send_tweet(twitter_client_obj, tweet_text):
                state.mark_posted(item["id"])
                sent_count += 1
                if sent_count < config.max_per_run and twitter_client_obj:
                    time.sleep(2)
        except TwitterError as e:
            if "Rate limit" in str(e):
                state.set_cooldown(config.cooldown_minutes)
                log(f"Cooldown activated for {config.cooldown_minutes} minutes")
                break
        except Exception as e:
            log(f"Error: {e}", "warning")
    return sent_count

def main():
    setup_logging()
    log("Starting...")
    config = Config.from_env()
    state_manager = StateManager()
    twitter = twitter_client(config)

    if state_manager.is_in_cooldown():
        log("In cooldown, exiting"); return

    try:
        with BrowserManager(config) as browser:
            if not browser.goto_with_retry(AKIS_URL):
                log("Page load failed"); return
            browser.click_highlights_tab()
            browser.page.wait_for_timeout(2000)

            items = browser.extract_simple_items()
            if not items:
                log("No items found"); return

            log(f"Found {len(items)} items")
            new_items = [it for it in items if not state_manager.is_posted(it["id"])]
            if not new_items:
                log("No new items"); return

            log(f"Processing {len(new_items)} new items")
            new_items = new_items[:config.max_per_run]

            sent_count = process_new_items(new_items, state_manager, config, twitter)

            if new_items:
                state_manager.last_id = new_items[-1]["id"]

            state_manager.save()
            log(f"Done. Sent {sent_count} tweets")

    except Exception as e:
        log(f"Fatal error: {e}", "error")
        import traceback
        log(f"Traceback: {traceback.format_exc()}", "error")

if __name__ == "__main__":
    main()
