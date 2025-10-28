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
            
            if isinstance(data, list):
                return {"last_id": None, "posted": data, "cooldown_until": None}
            
            return {**default_state, **data}
            
        except (json.JSONDecodeError, KeyError, Exception) as e:
            logging.warning(f"State file corrupted, resetting: {e}")
            return default_state
    
    def save(self):
        """Save state to file"""
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
        except (ValueError, TypeError):
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
    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] %(message)s',
        datefmt='%H:%M:%S'
    )

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
def extract_codes_from_kap_text(text: str) -> List[str]:
    """Extract multiple stock codes from KAP text"""
    # KAP-TERA/BVSAN veya KAP-TERA formatını yakala
    matches = re.findall(r'KAP\s*[•·\-\.]?\s*([A-ZÇĞİÖŞÜ]{3,5})(?:[/\s]([A-ZÇĞİÖŞÜ]{3,5}))?', text, re.IGNORECASE)
    
    codes = []
    for match in matches:
        if match[0]:
            codes.append(match[0].upper())
        if match[1]:
            codes.append(match[1].upper())
    
    return list(set(codes))

def extract_clean_content(text: str) -> str:
    """Extract clean content for a single news item"""
    if not text:
        return ""
    
    # KAP başlığını temizle
    text = re.sub(r'KAP\s*[•·\-\.]?\s*[A-Z/]+\s*\d{1,2}:\d{2}\s*', '', text)
    
    # "Şirket" ile başlayan ön ekleri temizle
    text = re.sub(r'^\s*Şirket\s*', '', text, flags=re.IGNORECASE)
    
    # Fintables ile başlayan diğer haberleri kes
    text = re.split(r'\s*Fintables\s*[•·\-\.]\s*', text)[0]
    
    # Noktaya kadar olan kısmı al
    sentences = re.split(r'[.!?]+', text)
    if sentences and sentences[0].strip():
        first_sentence = sentences[0].strip()
        # Eğer ilk cümle çok kısaysa, ikinci cümleyi de al
        if len(first_sentence) < 40 and len(sentences) > 1:
            combined = (first_sentence + '. ' + sentences[1].strip()).strip()
            # Nokta ile bitirmeyi garantile
            if not combined.endswith('.'):
                combined += '.'
            return combined
        else:
            if not first_sentence.endswith('.'):
                first_sentence += '.'
            return first_sentence
    
    return text.strip()

def build_tweet_quanta_style(codes: List[str], content: str) -> str:
    """Build tweet in Quanta Finance style"""
    if not codes:
        return ""
    
    codes_str = " ".join([f"#{code}" for code in codes])
    clean_content = extract_clean_content(content)
    
    if not clean_content:
        return ""
    
    base_tweet = f"📰 {codes_str} | {clean_content}"
    
    if len(base_tweet) <= 280:
        return base_tweet
    
    # Çok uzunsa kısalt
    max_content_length = 280 - len(f"📰 {codes_str} | ...") - 3
    if max_content_length > 10:  # Minimum içerik uzunluğu
        # Son tam kelimeyi bul
        shortened = clean_content[:max_content_length]
        last_space = shortened.rfind(' ')
        if last_space > len(shortened) * 0.7:  # Makul bir noktada kes
            shortened = shortened[:last_space]
        
        return f"📰 {codes_str} | {shortened}..."[:280]
    
    return ""

def is_valid_news_content(text: str) -> bool:
    """Validate if content is a single complete news item"""
    if not text or len(text) < 30:
        return False
    
    spam_phrases = [
        "yatırım tavsiyesi değildir",
        "yasal uyarı", 
        "kişisel veri",
        "kvk",
        "saygılarımızla",
        "kamunun bilgisine"
    ]
    
    text_lower = text.lower()
    if any(phrase in text_lower for phrase in spam_phrases):
        return False
    
    return True

# ============== BROWSER & SCRAPING ==============
JS_EXTRACTOR_SIMPLE = """
() => {
    try {
        console.log("=== SIMPLE EXTRACTOR STARTED ===");
        const items = [];
        
        // Tüm metin içeren elementleri bul
        const allElements = document.querySelectorAll('div, li, article, section, p');
        console.log(`Total elements: ${allElements.length}`);
        
        for (const el of allElements) {
            try {
                const text = el.innerText || el.textContent || '';
                const cleanText = text.replace(/\\s+/g, ' ').trim();
                
                // Basit KAP haberi kontrolü
                if (cleanText.length > 40 && cleanText.includes('KAP') && /[A-Z]{3,5}/.test(cleanText)) {
                    console.log("Found KAP text:", cleanText.substring(0, 80));
                    
                    // Spam kontrolü
                    if (/yatırım tavsiyesi|yasal uyarı|kişisel veri|kvk/i.test(cleanText)) {
                        continue;
                    }
                    
                    // KAP kodlarını çıkar
                    const kapRegex = /KAP\\s*[•·\\-\\.]?\\s*([A-Z]{3,5})(?:[\\/\\s]([A-Z]{3,5}))?/i;
                    const match = cleanText.match(kapRegex);
                    
                    if (match) {
                        const codes = [];
                        if (match[1]) codes.push(match[1].toUpperCase());
                        if (match[2]) codes.push(match[2].toUpperCase());
                        
                        // Basit geçersiz kod filtresi
                        const invalidCodes = ['ADET', 'TEK', 'MİLYON', 'TL', 'YÜZDE', 'PAY', 'HİSSE', 'ŞİRKET', 'BİST', 'KAP'];
                        const validCodes = codes.filter(code => !invalidCodes.includes(code) && code.length >= 3);
                        
                        if (validCodes.length > 0) {
                            // Benzersiz ID
                            const hash = cleanText.split('').reduce((a, c) => (a * 31 + c.charCodeAt(0)) & 0xFFFFFFFF, 0);
                            const id = `simple-${validCodes.join('-')}-${hash}`;
                            
                            // Duplicate kontrolü
                            if (!items.find(item => item.id === id)) {
                                items.push({
                                    id: id,
                                    codes: validCodes,
                                    content: cleanText,
                                    raw: cleanText
                                });
                                console.log(`✅ Added: ${validCodes.join('/')}`);
                            }
                        }
                    }
                }
            } catch (e) {
                // Hata durumunda devam et
                continue;
            }
        }
        
        console.log(`=== EXTRACTION COMPLETE: ${items.length} items ===`);
        return items;
    } catch (e) {
        console.error("Simple extractor error:", e);
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
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            viewport={"width": 1920, "height": 1080}
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
        for attempt in range(retries):
            try:
                log(f"Navigation attempt {attempt + 1}/{retries}")
                self.page.goto(url, wait_until="networkidle")
                self.page.wait_for_timeout(3000)
                return True
            except Exception as e:
                log(f"Attempt {attempt + 1} failed: {e}", "warning")
                if attempt < retries - 1:
                    time.sleep(5)
        return False
    
    def click_highlights_tab(self) -> bool:
        try:
            log("Looking for 'Öne Çıkanlar' tab...")
            
            selectors = [
                "button:has-text('Öne Çıkanlar')",
                "a:has-text('Öne Çıkanlar')", 
                "div:has-text('Öne Çıkanlar')",
                "text=Öne Çıkanlar"
            ]
            
            for selector in selectors:
                try:
                    if self.page.locator(selector).is_visible(timeout=5000):
                        log(f"Found tab: {selector}")
                        self.page.click(selector)
                        self.page.wait_for_timeout(3000)
                        return True
                except:
                    continue
            
            return False
        except Exception as e:
            log(f"Error clicking tab: {e}", "error")
            return False
    
    def extract_simple_items(self) -> List[dict]:
        """Extract items using simple method"""
        try:
            log("Using SIMPLE extractor...")
            
            # Sayfayı biraz kaydır
            self.page.evaluate("window.scrollTo(0, 400)")
            self.page.wait_for_timeout(2000)
            
            raw_items = self.page.evaluate(JS_EXTRACTOR_SIMPLE)
            log(f"Simple extractor found {len(raw_items)} items")
            
            for i, item in enumerate(raw_items[:3]):
                log(f"Item {i+1}: {item['codes']} - {item['content'][:80]}...")
                
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
        response = client.create_tweet(text=tweet_text)
        log(f"Tweet sent successfully")
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
def process_new_items(items: List[dict], state: StateManager, config: Config, 
                     twitter_client: Optional[tweepy.Client]) -> int:
    sent_count = 0
    
    for item in items:
        if sent_count >= config.max_per_run:
            break
        
        if state.is_posted(item["id"]):
            continue
            
        if not is_valid_news_content(item["content"]):
            continue
        
        try:
            tweet_text = build_tweet_quanta_style(item["codes"], item["content"])
            
            if not tweet_text:
                continue
                
            log(f"Tweeting: {tweet_text}")
            
            if send_tweet(twitter_client, tweet_text):
                state.mark_posted(item["id"])
                sent_count += 1
                
                if sent_count < config.max_per_run and twitter_client:
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
        log("In cooldown, exiting")
        return
    
    try:
        with BrowserManager(config) as browser:
            if not browser.goto_with_retry(AKIS_URL):
                log("Page load failed")
                return
            
            browser.click_highlights_tab()
            browser.page.wait_for_timeout(5000)
            
            items = browser.extract_simple_items()
            if not items:
                log("No items found")
                return
            
            log(f"Found {len(items)} items")
            
            new_items = [item for item in items if not state_manager.is_posted(item["id"])]
            if not new_items:
                log("No new items")
                return
            
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
        traceback_str = traceback.format_exc()
        log(f"Traceback: {traceback_str}", "error")

if __name__ == "__main__":
    main()
