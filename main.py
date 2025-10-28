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
    """Configuration settings"""
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
class TwitterError(Exception):
    pass

class ScrapingError(Exception):
    pass

class StateError(Exception):
    pass

# ============== STATE MANAGEMENT ==============
class StateManager:
    """Manage application state persistence"""
    
    def __init__(self, path: Path = Path("state.json")):
        self.path = path
        self._state = self._load_initial_state()
    
    def _load_initial_state(self) -> dict:
        """Load initial state from file or create default"""
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
            
            # Backward compatibility for old list format
            if isinstance(data, list):
                return {"last_id": None, "posted": data, "cooldown_until": None}
            
            # Merge with default to ensure all keys exist
            return {**default_state, **data}
            
        except (json.JSONDecodeError, KeyError, Exception) as e:
            logging.warning(f"State file corrupted, resetting: {e}")
            return default_state
    
    def save(self):
        """Save state to file"""
        try:
            # Ensure directory exists
            self.path.parent.mkdir(parents=True, exist_ok=True)
            
            with open(self.path, 'w', encoding='utf-8') as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
                
        except Exception as e:
            raise StateError(f"Could not save state: {e}")
    
    def is_posted(self, item_id: str) -> bool:
        """Check if item was already posted"""
        return item_id in self._state["posted"]
    
    def mark_posted(self, item_id: str):
        """Mark item as posted"""
        if not self.is_posted(item_id):
            self._state["posted"].append(item_id)
    
    def set_cooldown(self, minutes: int):
        """Set cooldown period"""
        self._state["cooldown_until"] = (
            datetime.now(timezone.utc) + timedelta(minutes=minutes)
        ).isoformat()
    
    def is_in_cooldown(self) -> bool:
        """Check if currently in cooldown period"""
        if not self._state["cooldown_until"]:
            return False
        
        try:
            cooldown_dt = datetime.fromisoformat(
                self._state["cooldown_until"].replace("Z", "+00:00")
            )
            return datetime.now(timezone.utc) < cooldown_dt
        except (ValueError, TypeError) as e:
            logging.warning(f"Invalid cooldown timestamp: {e}")
            self._state["cooldown_until"] = None
            return False
    
    @property
    def last_id(self) -> Optional[str]:
        return self._state["last_id"]
    
    @last_id.setter
    def last_id(self, value: Optional[str]):
        self._state["last_id"] = value
    
    def cleanup_old_entries(self, max_entries: int = 1000):
        """Clean up old posted entries to prevent infinite growth"""
        if len(self._state["posted"]) > max_entries:
            self._state["posted"] = self._state["posted"][-max_entries:]

# ============== LOGGING ==============
def setup_logging():
    """Setup logging configuration"""
    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] %(message)s',
        datefmt='%H:%M:%S'
    )

def log(msg: str, level: str = "info"):
    """Log message with timestamp"""
    logger = getattr(logging, level.lower())
    logger(msg)

# ============== TWITTER CLIENT ==============
def twitter_client(config: Config) -> Optional[tweepy.Client]:
    """Initialize Twitter client"""
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

STOP_PHRASES = [
    r"işbu açıklama.*?amaçla",
    r"yatırım tavsiyesi değildir", 
    r"kamunun bilgisine arz olunur",
    r"saygılarımızla",
    r"özel durum açıklaması",
    r"yatırımcılarımızın bilgisine",
    r"yasal uyarı",
    r"kişisel verilerin korunması",
    r"kvk"
]

REL_PREFIX = re.compile(r'^(?:dün|bugün|yesterday|today)\b[:\-–]?\s*', re.IGNORECASE)

# ============== CONTENT PROCESSING ==============
def clean_text(text: str) -> str:
    """Clean and normalize text content"""
    if not text or not isinstance(text, str):
        return ""
    
    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()
    
    # Remove stop phrases
    for pattern in STOP_PHRASES:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE | re.DOTALL)
    
    # Remove source identifiers
    text = re.sub(r"\b(Fintables|KAP)\b\s*[·\.\•]?\s*", "", text, flags=re.IGNORECASE)
    
    # Remove time prefixes
    text = REL_PREFIX.sub('', text).strip(" -–—:|•·")
    
    # Normalize conjunctions
    text = re.sub(r"\s+ile\s+", " ile ", text)
    text = re.sub(r"\s+ve\s+", " ve ", text)
    
    return text.strip()

def build_tweet(code: str, snippet: str) -> str:
    """Build tweet text from code and snippet"""
    base_text = clean_text(snippet)
    
    if not base_text:
        return f"Megafon #{code} | Yeni haber"
    
    # Extract first meaningful sentence
    sentences = [s.strip() for s in base_text.split('.') if s.strip() and len(s.strip()) > 20]
    first_sentence = sentences[0] if sentences else ' '.join(base_text.split()[:25])
    
    # Truncate if too long
    max_len = 230
    if len(first_sentence) > max_len:
        words = first_sentence.split()
        temp = ""
        for word in words:
            if len(temp + word + " ") <= max_len - 3:
                temp += word + " "
            else:
                break
        first_sentence = temp.strip() + "..."
    
    # Ensure proper ending
    if first_sentence and not first_sentence.endswith(('.', '!', '?')):
        first_sentence += "."
    
    tweet_text = f"Megafon #{code} | {first_sentence}"
    return tweet_text[:280]

def is_valid_ticker(code: str, text: str) -> bool:
    """Validate stock ticker and content"""
    if not code or not text:
        return False
    
    # Length and format check
    if len(code) < 3 or len(code) > 5:
        return False
    
    if not re.match(r"^[A-ZÇĞİÖŞÜ]{3,5}$", code):
        return False
    
    # Content validation
    forbidden_phrases = ["YATIRIM", "TAVSİYE", "UYARI", "KİŞİSEL", "POLİTİKASI", "KVK"]
    text_upper = text.upper()
    
    if any(phrase in text_upper for phrase in forbidden_phrases):
        return False
    
    return True

# ============== BROWSER & SCRAPING ==============
JS_EXTRACTOR = """
() => {
    try {
        const rows = Array.from(document.querySelectorAll('main li, main div[role="listitem"], main div'))
            .slice(0, 300);
        if (!rows.length) return [];

        const banned = new Set(['ADET','TEK','MİLYON','TL','YÜZDE','PAY','HİSSE','ŞİRKET','BİST','KAP','FİNTABLES','BÜLTEN','GÜNLÜK','BURADA','KVKK','POLİTİKASI','YASAL','UYARI','BİLGİLENDİRME','GUNLUK','HABER','ALTNY','YBTAS','RODRG','MAGEN','TERA']);
        const nonNewsRe = /(Günlük Bülten|Bülten|Piyasa temkini|yatırım bilgi|yasal uyarı|kişisel veri|kvk)/i;

        return rows.map(row => {
            const text = row.innerText || '';
            if (!text.trim()) return null;
            const norm = text.replace(/\\s+/g, ' ').trim();
            if (nonNewsRe.test(norm) || /Fintables/i.test(norm)) return null;

            const kapMatch = norm.match(/\\bKAP\\s*[•·\\-\\.]\\s*([A-ZÇĞİÖŞÜ]{3,5})(?:[0-9]?\\b)/i);
            if (!kapMatch) return null;

            const code = kapMatch[1].toUpperCase();
            if (banned.has(code)) return null;
            if (!/^[A-ZÇĞİÖŞÜ]{3,5}$/.test(code)) return null;

            const pos = norm.toUpperCase().indexOf(code) + code.length;
            let snippet = norm.slice(pos).trim();
            if (snippet.length < 40) return null;
            if (/yatırım bilgi|yasal uyarı|kişisel veri|kvk|politikası/i.test(snippet)) return null;

            const timestamp = Date.now();
            const hash = norm.split('').reduce((a, c) => (a * 31 + c.charCodeAt(0)) & 0xFFFFFFFF, 0);
            const id = `${code}-${hash}-${timestamp}`;
            return { id, code, snippet, raw: norm };
        }).filter(Boolean);
    } catch (e) {
        console.error("JS Extractor Error:", e);
        return [];
    }
}
"""

class BrowserManager:
    """Manage browser lifecycle and operations"""
    
    def __init__(self, config: Config):
        self.config = config
    
    def __enter__(self):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=self.config.browser_headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox", 
                "--disable-dev-shm-usage",
                "--disable-gpu"
            ]
        )
        self.context = self.browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            locale="tr-TR",
            timezone_id="Europe/Istanbul"
        )
        self.page = self.context.new_page()
        self.page.set_default_timeout(self.config.request_timeout)
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if hasattr(self, 'context'):
            self.context.close()
        if hasattr(self, 'browser'):
            self.browser.close()
        if hasattr(self, 'playwright'):
            self.playwright.stop()
    
    def goto_with_retry(self, url: str, retries: int = 3) -> bool:
        """Navigate to URL with retry logic"""
        for attempt in range(retries):
            try:
                log(f"Navigation attempt {attempt + 1}/{retries}")
                self.page.goto(url, wait_until="domcontentloaded")
                self.page.wait_for_selector("main", timeout=15000)
                self.page.wait_for_load_state("networkidle")
                self.page.wait_for_timeout(2000)
                return True
                
            except PlaywrightTimeoutError as e:
                log(f"Timeout on attempt {attempt + 1}: {e}", "warning")
                if attempt < retries - 1:
                    time.sleep(5)
            except Exception as e:
                log(f"Error on attempt {attempt + 1}: {e}", "warning")
                if attempt < retries - 1:
                    time.sleep(5)
        
        return False
    
    def extract_items(self) -> List[dict]:
        """Extract news items using JavaScript"""
        try:
            log("Evaluating JS extractor...")
            raw_items = self.page.evaluate(JS_EXTRACTOR)
            log(f"Extracted {len(raw_items)} items")
            
            # Debug first few items
            for i, item in enumerate(raw_items[:3]):
                log(f"DEBUG ITEM {i+1}: {item['raw'][:100]}...")
                
            return raw_items
            
        except Exception as e:
            log(f"JS evaluation failed: {e}", "error")
            return []

# ============== TWITTER OPERATIONS ==============
def send_tweet(client: Optional[tweepy.Client], tweet_text: str) -> bool:
    """Send tweet with error handling"""
    if not client:
        log("No Twitter client, running in simulation mode")
        return True
    
    try:
        response = client.create_tweet(text=tweet_text)
        log(f"Tweet sent successfully: {tweet_text[:80]}...")
        return True
        
    except TooManyRequests:
        log("Rate limit exceeded - need cooldown", "warning")
        raise TwitterError("Rate limit exceeded")
    except TweepyException as e:
        log(f"Twitter API error: {e}", "error")
        raise TwitterError(f"Twitter API error: {e}")
    except Exception as e:
        log(f"Unexpected error while tweeting: {e}", "error")
        return False

# ============== MAIN LOGIC ==============
def process_new_items(items: List[dict], state: StateManager, config: Config, 
                     twitter_client: Optional[tweepy.Client]) -> int:
    """Process new items and send tweets"""
    sent_count = 0
    
    for item in items:
        if sent_count >= config.max_per_run:
            log(f"Reached maximum tweets per run ({config.max_per_run})")
            break
        
        if state.is_posted(item["id"]):
            continue
            
        if not is_valid_ticker(item["code"], item["snippet"]):
            log(f"Invalid ticker/content: {item['code']}")
            continue
        
        try:
            tweet_text = build_tweet(item["code"], item["snippet"])
            log(f"Attempting tweet: {tweet_text}")
            
            if send_tweet(twitter_client, tweet_text):
                state.mark_posted(item["id"])
                sent_count += 1
                
                # Small delay between tweets to be respectful
                if sent_count < config.max_per_run and twitter_client:
                    time.sleep(3)
                    
        except TwitterError as e:
            if "Rate limit" in str(e):
                state.set_cooldown(config.cooldown_minutes)
                log(f"Rate limit hit, cooldown activated for {config.cooldown_minutes} minutes")
                break
            else:
                log(f"Twitter error for {item['code']}: {e}", "warning")
        except Exception as e:
            log(f"Unexpected error processing {item['code']}: {e}", "error")
    
    return sent_count

def main():
    """Main application entry point"""
    setup_logging()
    log("Application starting...")
    
    config = Config.from_env()
    state_manager = StateManager()
    twitter = twitter_client(config)
    
    # Check cooldown
    if state_manager.is_in_cooldown():
        log("Currently in cooldown period, exiting")
        return
    
    try:
        with BrowserManager(config) as browser:
            if not browser.goto_with_retry(AKIS_URL):
                log("Failed to load page after retries", "error")
                return
            
            # Try to click highlights if available
            try:
                if browser.page.get_by_text("Öne çıkanlar", exact=True).is_visible(timeout=3000):
                    browser.page.click("text=Öne çıkanlar")
                    browser.page.wait_for_load_state("networkidle")
                    browser.page.wait_for_timeout(2000)
                    log("Highlights section activated")
            except Exception as e:
                log(f"Could not activate highlights: {e}", "debug")
            
            # Extract items
            all_items = browser.extract_items()
            if not all_items:
                log("No items extracted")
                return
            
            # Get newest ID for state tracking
            newest_id = all_items[0]["id"] if all_items else None
            
            # Filter new items
            new_items = []
            for item in all_items:
                if state_manager.last_id and item["id"].startswith(
                    state_manager.last_id.split('-')[0] + '-' + state_manager.last_id.split('-')[1]
                ):
                    break
                new_items.append(item)
            
            new_items = new_items[:config.max_per_run * 2]  # Get some buffer
            
            if not new_items:
                log("No new items to process")
                if newest_id:
                    state_manager.last_id = newest_id
                    state_manager.save()
                return
            
            log(f"Found {len(new_items)} new items to process")
            
            # Process in chronological order (oldest first)
            new_items.reverse()
            
            # Send tweets
            sent_count = process_new_items(new_items, state_manager, config, twitter)
            
            # Update state
            if newest_id:
                state_manager.last_id = newest_id
            
            state_manager.cleanup_old_entries()
            state_manager.save()
            
            log(f"Completed successfully. Sent {sent_count} tweets")
            
    except Exception as e:
        log(f"Fatal error in main execution: {e}", "error")
        raise

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Interrupted by user")
    except Exception as e:
        log(f"Fatal error: {e}", "error")
        # Log full traceback for debugging
        import traceback
        traceback_str = traceback.format_exc()
        log(f"Traceback: {traceback_str}", "error")
        
        # Write to debug log file
        debug_log = Path("debug.log")
        with open(debug_log, "a", encoding="utf-8") as f:
            f.write(f"\n--- {datetime.now()} ---\n{traceback_str}\n")
