import os, re, json, time, logging
from pathlib import Path
from typing import List, Dict, Optional, Set
from dataclasses import dataclass
from playwright.sync_api import sync_playwright, Page, Locator
import tweepy

# ================== LOGGING KONFİGÜRASYONU ==================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('kap_bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ================== KONFİGÜRASYON ==================
@dataclass
class TwitterConfig:
    api_key: str = os.getenv("API_KEY")
    api_secret: str = os.getenv("API_KEY_SECRET") 
    access_token: str = os.getenv("ACCESS_TOKEN")
    access_token_secret: str = os.getenv("ACCESS_TOKEN_SECRET")
    
    def validate(self):
        if not all([self.api_key, self.api_secret, self.access_token, self.access_token_secret]):
            raise ValueError("Twitter API bilgileri eksik!")

class BotConfig:
    AKIS_URL = "https://fintables.com/borsa-haber-akisi"
    STATE_FILE = Path("state.json")
    MAX_TWEET_LENGTH = 279
    HEADLINE_SUMMARY_LIMIT = 240
    REQUEST_TIMEOUT = 30000
    SCROLL_TIMEOUT = 10000
    
    # Regex patterns
    UPPER_TR = "A-ZÇĞİÖŞÜ"
    KAP_LINE_RE = re.compile(rf"^\s*KAP\s*[·:]\s*([{UPPER_TR}0-9]{{3,6}})\b", re.M)
    
    STOP_PHRASES = [
        r"işbu açıklama.*?amaçla", r"yatırım tavsiyesi değildir", r"kamunun bilgisine arz olunur",
        r"saygılarımızla", r"özel durum açıklaması", r"yatırımcılarımızın bilgisine",
    ]
    
    REWRITE_MAP = [
        (r"\bbildirdi\b", "duyurdu"),
        (r"\bbildirimi\b", "açıklaması"),
        (r"\bilgisine\b", "paylaştı"),
        (r"\bgerçekleştirdi\b", "tamamladı"),
        (r"\bbaşladı\b", "başlattı"),
        (r"\bdevam ediyor\b", "sürdürülüyor"),
    ]
    
    HIGHLIGHTS_SELECTORS = [
        "button:has-text('Öne çıkanlar')",
        "[role='tab']:has-text('Öne çıkanlar')", 
        "a:has-text('Öne çıkanlar')",
        "text=Öne çıkanlar",
    ]

# ================== ANA BOT SINIFI ==================
class KapNewsBot:
    def __init__(self, config: TwitterConfig):
        self.config = config
        self.client = tweepy.Client(
            consumer_key=config.api_key,
            consumer_secret=config.api_secret,
            access_token=config.access_token,
            access_token_secret=config.access_token_secret,
        )
        self.posted: Set[str] = self._load_state()
        self.cfg = BotConfig()
    
    def _load_state(self) -> Set[str]:
        """State dosyasını yükle"""
        try:
            if BotConfig.STATE_FILE.exists():
                with open(BotConfig.STATE_FILE, 'r', encoding='utf-8') as f:
                    return set(json.load(f))
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"State file okunamadı, yeni oluşturulacak: {e}")
        return set()
    
    def save_state(self):
        """State'i kaydet"""
        try:
            with open(BotConfig.STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(sorted(list(self.posted)), f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.error(f"State kaydedilemedi: {e}")
    
    def clean_text(self, text: str) -> str:
        """Metni temizle"""
        if not text:
            return ""
        text = re.sub(r"\s+", " ", text).strip()
        for pattern in self.cfg.STOP_PHRASES:
            text = re.sub(pattern, "", text, flags=re.IGNORECASE)
        return text.strip(" -–—:.")
    
    def summarize(self, text: str, limit: int) -> str:
        """Metni özetle"""
        text = self.clean_text(text)
        if len(text) <= limit:
            return text
        
        # Cümle sınırlarından böl
        sentences = re.split(r"(?<=[.!?])\s+", text)
        result = ""
        
        for sentence in sentences:
            if not sentence:
                continue
            candidate = (result + " " + sentence).strip()
            if len(candidate) > limit:
                break
            result = candidate
        
        return result or text[:limit]
    
    def rewrite_turkish_short(self, text: str) -> str:
        """Türkçe metni yeniden yaz"""
        text = self.clean_text(text)
        text = re.sub(r"[“”\"']", "", text)
        text = re.sub(r"\(\s*\)", "", text)
        
        for pattern, replacement in self.cfg.REWRITE_MAP:
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
        
        text = re.sub(r"^\s*[-–—•·]\s*", "", text)
        return text.strip()
    
    def is_pnl_news(self, text: str) -> bool:
        """Kâr/zarar haberi mi?"""
        text_lower = text.lower()
        keywords = ["kâr", "kar", "zarar", "net dönem", "temettü", "temettu", "finansal", "faaliyet"]
        return any(keyword in text_lower for keyword in keywords)
    
    def build_tweet(self, code: str, headline: str) -> str:
        """Tweet metnini oluştur"""
        base = self.rewrite_turkish_short(headline)
        base = self.summarize(base, self.cfg.HEADLINE_SUMMARY_LIMIT)
        
        emoji = "💰" if self.is_pnl_news(base) else "📰"
        tweet_text = f"{emoji} #{code} | {base}"
        
        return tweet_text[:self.cfg.MAX_TWEET_LENGTH]
    
    def go_highlights(self, page: Page) -> bool:
        """Öne çıkanlar sekmesine geç"""
        logger.info("Öne çıkanlar sekmesi aranıyor...")
        
        for selector in self.cfg.HIGHLIGHTS_SELECTORS:
            try:
                locator = page.locator(selector)
                if locator.count() > 0:
                    locator.first.click()
                    page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(2000)  # Artırıldı
                    logger.info("Öne çıkanlar sekmesine geçildi")
                    return True
            except Exception as e:
                logger.debug(f"Selector {selector} çalışmadı: {e}")
                continue
        
        logger.warning("Öne çıkanlar sekmesi bulunamadı, 'Tümü' sekmesinde devam ediliyor")
        return False
    
    def debug_page_content(self, page: Page):
        """Debug için sayfa içeriğini kontrol et"""
        logger.info("Sayfa debug ediliyor...")
        
        # Sayfanın screenshot'ını al
        page.screenshot(path="debug_screenshot.png")
        logger.info("Sayfa screenshot'ı alındı: debug_screenshot.png")
        
        # Sayfa HTML'sini kaydet
        html_content = page.content()
        with open("debug_page.html", "w", encoding="utf-8") as f:
            f.write(html_content)
        logger.info("Debug HTML kaydedildi: debug_page.html")
        
        # TÜM KAP elementlerini detaylı logla
        all_kap_elements = page.locator(':has-text("KAP")')
        logger.info(f"Sayfada toplam {all_kap_elements.count()} KAP elementi var")
        
        # İlk 10 KAP elementinin detayını logla
        for i in range(min(10, all_kap_elements.count())):
            try:
                element = all_kap_elements.nth(i)
                text = element.inner_text()
                logger.info(f"KAP Element {i+1}: {text[:200]}...")
            except Exception as e:
                logger.error(f"KAP Element {i+1} okunamadı: {e}")
    
    def get_kap_rows(self, page: Page) -> List[Dict]:
        """KAP satırlarını bul - GÜNCELLENMİŞ VERSİYON"""
        logger.info("KAP haberleri aranıyor...")
        
        try:
            page.goto(self.cfg.AKIS_URL, wait_until="networkidle")
            
            # DAHA UZUN BEKLE - JavaScript'in yüklenmesi için
            logger.info("Sayfanın yüklenmesi bekleniyor...")
            page.wait_for_timeout(5000)
            
            # Debug için mevcut durumu kaydet
            self.debug_page_content(page)
            
            # "Öne çıkanlar" sekmesine geç
            self.go_highlights(page)
            
            # HABERLERİN GELMESİ İÇİN EK BEKLEME
            logger.info("Haberlerin yüklenmesi bekleniyor...")
            page.wait_for_timeout(4000)
            
            # HABER CONTAINER'INI BEKLE
            try:
                # Haber container'ı için farklı selector'lar dene
                news_selectors = [
                    "[class*='news']", "[class*='haber']", "[class*='flow']",
                    "[class*='list']", "[class*='container']", "ul", "ol", "li"
                ]
                
                for selector in news_selectors:
                    elements = page.locator(selector)
                    if elements.count() > 0:
                        logger.info(f"Haber container bulundu: {selector} ({elements.count()} element)")
                        break
                else:
                    logger.warning("Hiçbir haber container selector'ı çalışmadı")
                        
            except Exception as e:
                logger.warning(f"Haber container beklenirken hata: {e}")

            # ANA KAP Arama kodu
            rows = []
            seen = set()
            
            # Farklı container selector'ları deneyelim
            container_selectors = [
                "div", "li", "article", "section", 
                "[class*='news']", "[class*='haber']", "[class*='card']",
                "[class*='item']", "[class*='flow']", "[class*='list']"
            ]
            
            for selector in container_selectors:
                try:
                    containers = page.locator(selector).filter(has_text="KAP")
                    count = containers.count()
                    logger.info(f"Selector '{selector}': {count} element bulundu")
                    
                    for i in range(min(25, count)):  # Limit artırıldı
                        try:
                            container = containers.nth(i)
                            text = container.inner_text().strip()
                            
                            # DEBUG: Tüm KAP text'lerini görelim
                            if "KAP" in text:
                                logger.info(f"KAP Text Örneği: {text[:150]}...")
                            
                            # KAP pattern'ını ara
                            match = self.cfg.KAP_LINE_RE.search(text)
                            if match:
                                code = match.group(1)
                                logger.info(f"KAP EŞLEŞTİ: {code}")
                                
                                # Link bul
                                link = container.locator("a").first
                                if link.count() > 0:
                                    href = link.get_attribute("href") or f"row-{i}"
                                    unique_id = f"{href}_{code}"
                                    
                                    if unique_id not in seen:
                                        seen.add(unique_id)
                                        rows.append({
                                            "id": unique_id,
                                            "code": code, 
                                            "link": link,
                                            "raw_text": text
                                        })
                                        logger.info(f"KAP eklendi: {code} - {text[:50]}...")
                                else:
                                    logger.warning(f"KAP {code} için link bulunamadı")
                            else:
                                logger.debug(f"KAP bulundu ama regex eşleşmedi: {text[:80]}...")
                                
                        except Exception as e:
                            logger.debug(f"Container {i} işlenirken hata: {e}")
                            continue
                            
                except Exception as e:
                    logger.error(f"Selector {selector} işlenirken hata: {e}")
                    continue
            
            logger.info(f"Toplam {len(rows)} KAP haberi bulundu")
            return rows
            
        except Exception as e:
            logger.error(f"KAP satırları alınırken hata: {e}")
            return []
    
    def open_row_and_read_headline(self, page: Page, link_locator: Locator) -> Optional[str]:
        """Modal aç ve başlığı oku"""
        logger.info("Modal açılıyor...")
        
        try:
            link_locator.scroll_into_view_if_needed(timeout=self.cfg.SCROLL_TIMEOUT)
            link_locator.click()
            
            # Modal'ın yüklenmesini bekle
            page.wait_for_selector(
                "div[role='dialog'], .modal, .MuiDialog-root, .ant-modal", 
                timeout=10000
            )
            page.wait_for_timeout(1500)  # Artırıldı
            
            headline = ""
            
            # Başlık selector'ları
            headline_selectors = [
                "div[role='dialog'] h1", ".modal h1", ".MuiDialog-root h1",
                ".ant-modal h1", "div[role='dialog'] h2", ".modal h2",
                ".MuiDialog-root .MuiTypography-h6", ".ant-modal .ant-modal-title",
                "[class*='title']", "[class*='headline']", "h1, h2, h3"
            ]
            
            for selector in headline_selectors:
                try:
                    loc = page.locator(selector)
                    if loc.count() > 0:
                        headline = loc.first.inner_text().strip()
                        if len(headline) > 10:  # Anlamlı bir başlık uzunluğu
                            logger.info(f"Başlık bulundu: {headline[:50]}...")
                            break
                except Exception:
                    continue
            
            # Başlık bulunamazsa fallback
            if not headline:
                try:
                    modal = page.locator("div[role='dialog'], .modal, .MuiDialog-root, .ant-modal").first
                    paragraphs = modal.locator("p")
                    if paragraphs.count() > 0:
                        headline_parts = []
                        for i in range(min(3, paragraphs.count())):
                            para_text = paragraphs.nth(i).inner_text().strip()
                            if para_text:
                                headline_parts.append(para_text)
                        headline = " ".join(headline_parts)
                        logger.info(f"Fallback başlık: {headline[:50]}...")
                except Exception as e:
                    logger.warning(f"Fallback başlık alınamadı: {e}")
            
            # Modal'ı kapat
            self.safe_modal_close(page)
            
            return headline if headline else None
            
        except Exception as e:
            logger.error(f"Modal açma/okuma hatası: {e}")
            self.safe_modal_close(page)
            return None
    
    def safe_modal_close(self, page: Page):
        """Modal'ı güvenle kapat"""
        try:
            close_selectors = [
                "button[aria-label='Kapat']",
                "button[aria-label='Close']", 
                ".ant-modal-close",
                ".modal-close",
                "[data-testid='close-button']",
                "button:has-text('Kapat')",
                "button:has-text('Close')",
            ]
            
            for selector in close_selectors:
                try:
                    close_btn = page.locator(selector)
                    if close_btn.count() > 0:
                        close_btn.first.click()
                        page.wait_for_timeout(500)
                        return
                except Exception:
                    continue
            
            # Son çare Escape
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            
        except Exception as e:
            logger.debug(f"Modal kapatma hatası: {e}")
    
    def send_tweet(self, tweet_text: str) -> bool:
        """Tweet gönder"""
        try:
            self.client.create_tweet(text=tweet_text)
            return True
        except Exception as e:
            logger.error(f"Tweet gönderilemedi: {e}")
            return False
    
    def process_news_flow(self, page: Page):
        """Ana haber işleme akışı"""
        logger.info("Haber akışı işleniyor...")
        
        # KAP satırlarını al
        rows = self.get_kap_rows(page)
        logger.info(f"Bulunan KAP satırları: {len(rows)}")
        
        # Yeni haberleri filtrele
        new_rows = [r for r in rows if r["id"] not in self.posted]
        logger.info(f"Yeni haberler: {len(new_rows)} (Önceden gönderilmiş: {len(self.posted)})")
        
        # Yeniden eskiye doğru işle
        new_rows.reverse()
        
        successful_tweets = 0
        
        for row in new_rows:
            try:
                logger.info(f"Haber işleniyor: {row['code']}")
                
                headline = self.open_row_and_read_headline(page, row["link"])
                if not headline:
                    logger.warning(f"Başlık bulunamadı: {row['code']}")
                    continue
                
                # Tweet oluştur
                tweet = self.build_tweet(row["code"], headline)
                logger.info(f"Oluşturulan Tweet: {tweet}")
                
                # Tweet gönder
                if self.send_tweet(tweet):
                    self.posted.add(row["id"])
                    self.save_state()
                    successful_tweets += 1
                    logger.info("Tweet başarıyla gönderildi ✓")
                    
                    # Rate limiting
                    time.sleep(2.0)
                else:
                    logger.error("Tweet gönderilemedi")
                    
            except Exception as e:
                logger.error(f"Haber işleme hatası {row['code']}: {e}")
                continue
        
        logger.info(f"İşlem tamamlandı. Başarılı tweetler: {successful_tweets}/{len(new_rows)}")

# ================== ANA FONKSİYON ==================
def main():
    logger.info("=== KAP BOT BAŞLATILIYOR ===")
    
    try:
        # Konfigürasyonu yükle ve validate et
        config = TwitterConfig()
        config.validate()
        
        # Bot'u oluştur
        bot = KapNewsBot(config)
        
        # Playwright ile tarayıcıyı başlat
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-gpu", 
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled"
                ]
            )
            
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                locale="tr-TR",
                timezone_id="Europe/Istanbul",
                viewport={"width": 1920, "height": 1080}
            )
            
            page = context.new_page()
            page.set_default_timeout(30000)
            
            # Ana işlemi çalıştır
            bot.process_news_flow(page)
            
            # Temizlik
            browser.close()
            
    except Exception as e:
        logger.error(f"Bot çalışırken kritik hata: {e}")
        raise
    
    logger.info("=== KAP BOT SONLANDI ===")

if __name__ == "__main__":
    main()
