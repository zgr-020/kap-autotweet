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

# ============== CONTENT PROCESSING ==============
def extract_codes_from_kap_text(text: str) -> List[str]:
    """Extract multiple stock codes from KAP text"""
    # KAP-TERA/BVSAN formatını yakala
    matches = re.findall(r'KAP\s*[•·\-\.]?\s*([A-ZÇĞİÖŞÜ]{3,5})(?:[/\s]([A-ZÇĞİÖŞÜ]{3,5}))?', text, re.IGNORECASE)
    
    codes = []
    for match in matches:
        if match[0]:  # İlk kod
            codes.append(match[0].upper())
        if match[1]:  # İkinci kod (varsa)
            codes.append(match[1].upper())
    
    # Benzersiz kodları döndür
    return list(set(codes))

def extract_clean_single_news_content(text: str) -> str:
    """Extract clean content for a SINGLE news item"""
    if not text:
        return ""
    
    # KAP başlığını temizle (KAP-TERA/BVSAN 11:35 gibi)
    text = re.sub(r'KAP\s*[•·\-\.]?\s*[A-Z/]+\s*\d{1,2}:\d{2}\s*', '', text)
    
    # "Şirket" ile başlayan ön ekleri temizle
    text = re.sub(r'^\s*Şirket\s*(?:emti|iştiraki|ortaklığı|hissedarı)?\s*', '', text, flags=re.IGNORECASE)
    
    # Fintables ile başlayan diğer haberleri kes
    text = re.split(r'Fintables\s*[•·\-\.]', text)[0]
    
    # Noktaya kadar olan kısmı al (ilk cümle)
    sentences = re.split(r'[.!?]+', text)
    if sentences and sentences[0].strip():
        first_sentence = sentences[0].strip()
        # Eğer ilk cümle çok kısaysa, ikinci cümleyi de al
        if len(first_sentence) < 30 and len(sentences) > 1:
            text = (first_sentence + '. ' + sentences[1].strip()).strip()
        else:
            text = first_sentence
    
    # Fazla boşlukları temizle
    text = re.sub(r'\s+', ' ', text).strip()
    
    return text

def build_tweet_quanta_style(codes: List[str], content: str) -> str:
    """Build tweet in Quanta Finance style - 📰 #KOD | içerik"""
    if not codes:
        return ""
    
    # Kodları # ile birleştir
    codes_str = " ".join([f"#{code}" for code in codes])
    
    # İçeriği temizle
    clean_content = extract_clean_single_news_content(content)
    
    if not clean_content:
        return ""
    
    # Tweet uzunluğunu kontrol et
    base_tweet = f"📰 {codes_str} | {clean_content}"
    
    if len(base_tweet) <= 280:
        return base_tweet
    
    # Çok uzunsa kısalt
    max_content_length = 280 - len(f"📰 {codes_str} | ...") - 3
    if max_content_length > 0:
        clean_content = clean_content[:max_content_length].rstrip()
        # Son kelimeyi tamamla
        if clean_content and not clean_content.endswith(('.', '!', '?')):
            clean_content = clean_content.rsplit(' ', 1)[0] + "..."
        else:
            clean_content = clean_content + "..."
    
    return f"📰 {codes_str} | {clean_content}"[:280]

def is_valid_news_content(text: str) -> bool:
    """Validate if content is a single complete news item"""
    if not text or len(text) < 30:
        return False
    
    # Spam/legal içerik kontrolü
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
    
    # Birden fazla haber içeriyorsa geçersiz
    if len(re.findall(r'Fintables\s*[•·\-\.]', text)) > 1:
        return False
    
    return True

# ============== BROWSER & SCRAPING ==============
JS_EXTRACTOR_CLEAN_HIGHLIGHTS = """
() => {
    try {
        console.log("Extracting CLEAN highlights content...");
        const items = [];
        
        // Her bir haber kartını bul
        const selectors = [
            'div[class*="card"]',
            'div[class*="item"]', 
            'div[class*="news"]',
            'li',
            'article'
        ];
        
        for (const selector of selectors) {
            const elements = document.querySelectorAll(selector);
            for (const el of elements) {
                try {
                    const text = el.innerText || el.textContent || '';
                    const cleanText = text.replace(/\\s+/g, ' ').trim();
                    
                    // SADECE KAP haberleri ve yeterli uzunluk
                    if (cleanText.length > 50 && /KAP\\s*[•·\\-\\.]\\s*[A-Z]{3,5}/i.test(cleanText)) {
                        console.log("Found potential KAP item:", cleanText.substring(0, 100));
                        
                        // Spam/legal içerik filtreleme
                        if (/yatırım tavsiyesi|yasal uyarı|kişisel veri|kvk|saygılarımızla/i.test(cleanText)) {
                            continue;
                        }
                        
                        // Birden fazla haber içeriyorsa atla (Fintables• ile başlayan diğer haberler)
                        const newsSections = cleanText.split(/Fintables\\s*[•·\\-\\.]/);
                        if (newsSections.length > 2) {
                            continue; // Çok fazla haber içeriyor
                        }
                        
                        // İlk haber bölümünü al
                        const firstNews = newsSections[0].trim();
                        
                        // KAP kodunu çıkar
                        const kapMatch = firstNews.match(/KAP\\s*[•·\\-\\.]?\\s*([A-ZÇĞİÖŞÜ]{3,5})(?:[\\/\\s]([A-ZÇĞİÖŞÜ]{3,5}))?/i);
                        if (kapMatch) {
                            const codes = [];
                            if (kapMatch[1]) codes.push(kapMatch[1].toUpperCase());
                            if (kapMatch[2]) codes.push(kapMatch[2].toUpperCase());
                            
                            if (codes.length === 0) continue;
                            
                            // Geçersiz kodları filtrele
                            const invalidCodes = ['ADET', 'TEK', 'MİLYON', 'TL', 'YÜZDE', 'PAY', 'HİSSE', 'ŞİRKET', 'BİST', 'KAP'];
                            const validCodes = codes.filter(code => !invalidCodes.includes(code) && /^[A-ZÇĞİÖŞÜ]{3,5}$/.test(code));
                            
                            if (validCodes.length === 0) continue;
                            
                            // Benzersiz ID oluştur (sadece ilk haber için)
                            const contentForHash = firstNews.replace(/\\s*\\d{1,2}:\\d{2}\\s*/, '');
                            const hash = contentForHash.split('').reduce((a, c) => (a * 31 + c.charCodeAt(0)) & 0xFFFFFFFF, 0);
                            const id = `highlight-${validCodes.join('-')}-${hash}`;
                            
                            // Duplicate kontrolü
                            if (!items.find(item => item.id === id)) {
                                items.push({
                                    id: id,
                                    codes: validCodes,
                                    content: firstNews,
                                    raw: cleanText
                                });
                                console.log(`Added CLEAN highlight: ${validCodes.join('/')} - ${firstNews.substring(0, 80)}`);
                            }
                        }
                    }
                } catch (e) {
                    console.log("Error processing element:", e);
                    continue;
                }
            }
        }
        
        console.log(`Total CLEAN highlights found: ${items.length}`);
        return items;
    } catch (e) {
        console.error("Clean highlights extractor error:", e);
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
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled"
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
        
        # Stealth settings
        self.page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        """)
        
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
                self.page.goto(url, wait_until="networkidle")
                self.page.wait_for_timeout(3000)
                
                if self.page.locator("body").is_visible():
                    log("Page loaded successfully")
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
    
    def click_highlights_tab(self) -> bool:
        """Click on 'Öne Çıkanlar' tab to show highlights"""
        try:
            log("Looking for 'Öne Çıkanlar' tab...")
            
            selectors = [
                "button:has-text('Öne Çıkanlar')",
                "a:has-text('Öne Çıkanlar')",
                "div:has-text('Öne Çıkanlar')",
                "[class*='highlight']:has-text('Öne Çıkanlar')",
                "[class*='tab']:has-text('Öne Çıkanlar')",
                "text=Öne Çıkanlar"
            ]
            
            for selector in selectors:
                try:
                    if self.page.locator(selector).is_visible(timeout=5000):
                        log(f"Found highlights tab with selector: {selector}")
                        self.page.click(selector)
                        self.page.wait_for_timeout(3000)
                        self.page.wait_for_load_state("networkidle")
                        log("Successfully clicked 'Öne Çıkanlar' tab")
                        return True
                except Exception as e:
                    log(f"Selector {selector} failed: {e}", "debug")
                    continue
            
            log("Trying to find tabs by listing all clickable elements...")
            all_buttons = self.page.locator("button, a, div[role='button']")
            count = all_buttons.count()
            
            for i in range(count):
                try:
                    button = all_buttons.nth(i)
                    text = button.text_content()
                    if text and "Öne Çıkanlar" in text:
                        button.click()
                        self.page.wait_for_timeout(3000)
                        log("Found and clicked 'Öne Çıkanlar' by text content")
                        return True
                except:
                    continue
            
            log("Could not find 'Öne Çıkanlar' tab", "warning")
            return False
            
        except Exception as e:
            log(f"Error clicking highlights tab: {e}", "error")
            return False
    
    def extract_clean_highlight_items(self) -> List[dict]:
        """Extract clean news items from highlights section"""
        try:
            log("Evaluating CLEAN highlights extractor...")
            
            self.page.evaluate("window.scrollTo(0, 500)")
            self.page.wait_for_timeout(2000)
            
            raw_items = self.page.evaluate(JS_EXTRACTOR_CLEAN_HIGHLIGHTS)
            
            log(f"Extracted {len(raw_items)} CLEAN highlight items")
            
            # Debug için ilk birkaç item'ı göster
            for i, item in enumerate(raw_items[:5]):
                log(f"CLEAN HIGHLIGHT {i+1}: {item['codes']} - {item['content'][:100]}...")
                
            return raw_items
            
        except Exception as e:
            log(f"Clean highlights extraction failed: {e}", "error")
            return []

# ============== TWITTER OPERATIONS ==============
def send_tweet(client: Optional[tweepy.Client], tweet_text: str) -> bool:
    """Send tweet with error handling"""
    if not client:
        log(f"SIMULATION: {tweet_text}")
        return True
    
    try:
        response = client.create_tweet(text=tweet_text)
        log(f"Tweet sent: {tweet_text}")
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
            log(f"Already posted: {item['codes']}")
            continue
            
        if not is_valid_news_content(item["content"]):
            log(f"Invalid content: {item['codes']} - {item['content'][:100]}...")
            continue
        
        try:
            # QUANTA STYLE TWEET - 📰 #KOD1 #KOD2 | içerik
            tweet_text = build_tweet_quanta_style(item["codes"], item["content"])
            
            if not tweet_text:
                log(f"Empty tweet for: {item['codes']}")
                continue
                
            log(f"Attempting tweet: {tweet_text}")
            
            if send_tweet(twitter_client, tweet_text):
                state.mark_posted(item["id"])
                sent_count += 1
                
                # Küçük gecikme
                if sent_count < config.max_per_run and twitter_client:
                    time.sleep(2)
                    
        except TwitterError as e:
            if "Rate limit" in str(e):
                state.set_cooldown(config.cooldown_minutes)
                log(f"Rate limit hit, cooldown activated for {config.cooldown_minutes} minutes")
                break
            else:
                log(f"Twitter error for {item['codes']}: {e}", "warning")
        except Exception as e:
            log(f"Unexpected error processing {item['codes']}: {e}", "error")
    
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
            
            # Öne Çıkanlar tab'ına tıkla
            if not browser.click_highlights_tab():
                log("Failed to click highlights tab, but continuing...")
            
            browser.page.wait_for_timeout(5000)
            
            # Extract CLEAN items from highlights
            all_items = browser.extract_clean_highlight_items()
            if not all_items:
                log("No clean highlight items extracted")
                return
            
            log(f"Successfully extracted {len(all_items)} clean highlight items")
            
            # Yeni item'ları filtrele
            new_items = []
            for item in all_items:
                if not state_manager.is_posted(item["id"]):
                    new_items.append(item)
            
            if not new_items:
                log("No new clean highlight items to process")
                return
            
            log(f"Found {len(new_items)} new clean highlight items to process")
            
            # En yeni haberler önce gelsin
            new_items = new_items[:config.max_per_run]
            
            # Send tweets
            sent_count = process_new_items(new_items, state_manager, config, twitter)
            
            # Update state
            if new_items:
                state_manager.last_id = new_items[-1]["id"]
            
            state_manager.cleanup_old_entries()
            state_manager.save()
            
            log(f"Completed successfully. Sent {sent_count} clean highlight tweets")
            
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
        import traceback
        traceback_str = traceback.format_exc()
        log(f"Traceback: {traceback_str}", "error")
        
        debug_log = Path("debug.log")
        with open(debug_log, "a", encoding="utf-8") as f:
            f.write(f"\n--- {datetime.now()} ---\n{traceback_str}\n")
