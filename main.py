import json
import time
import logging
from datetime import datetime
from typing import List, Dict, Optional

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# Twitter API Bilgileri - Kendi bilgilerinle değiştir!
TWITTER_API_KEY = "API_KEY"
TWITTER_API_SECRET = "API_SECRET"
TWITTER_ACCESS_TOKEN = "ACCESS_TOKEN"
TWITTER_ACCESS_TOKEN_SECRET = "ACCESS_TOKEN_SECRET"

# State dosyası
STATE_FILE = "state.json"

# Logging ayarları
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class FintablesTweetBot:
    def __init__(self):
        self.driver = None
        self.tweet_api = None
        self.last_processed_id = None
        self.load_state()

    def load_state(self):
        """state.json dosyasını yükle."""
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                state = json.load(f)
                self.last_processed_id = state.get('last_processed_id', None)
                logging.info(f"Son işlenen ID: {self.last_processed_id}")
        except FileNotFoundError:
            logging.info("state.json bulunamadı. Yeni bir dosya oluşturulacak.")
            self.last_processed_id = None

    def save_state(self, last_id: str):
        """state.json dosyasına son işlenen ID'yi kaydet."""
        state = {'last_processed_id': last_id}
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        logging.info(f"Yeni son işlenen ID kaydedildi: {last_id}")

    def setup_driver(self):
        """Selenium driver'ı başlat."""
        options = Options()
        options.add_argument("--headless")  # Arka planda çalıştır
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-web-security")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)

        service = Service(ChromeDriverManager().install())
        self.driver = webdriver.Chrome(service=service, options=options)
        self.driver.set_page_load_timeout(30)

    def get_news_items(self) -> List[Dict]:
        """Fintables'tan haberleri çek."""
        url = "https://fintables.com/borsa-haber-akisi"
        logging.info(f"Haberler çekiliyor: {url}")

        try:
            self.driver.get(url)
            # "Öne Çıkanlar" butonuna tıkla
            wait = WebDriverWait(self.driver, 10)
            prominent_button = wait.until(
                EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Öne Çıkanlar')]"))
            )
            prominent_button.click()
            time.sleep(2)  # Sayfanın yeniden yüklenmesini bekle

            # Sayfa kaynağını al
            page_source = self.driver.page_source
            soup = BeautifulSoup(page_source, 'html.parser')

            # Haberleri seç (KAP ile başlayan ve mavi kod içerenler)
            news_items = []
            news_rows = soup.find_all('div', class_='news-item')  # Fintables yapısına göre

            for row in news_rows:
                # KAP başlangıcı kontrolü
                header = row.find('div', class_='news-header')
                if not header:
                    continue

                # "KAP" ile başlıyor mu?
                header_text = header.get_text(strip=True)
                if not header_text.startswith('KAP'):
                    continue

                # Mavi kodu (hisse kodu) al
                code_span = header.find('span', class_='text-blue-500')  # Mavi renkli span
                if not code_span:
                    continue

                stock_code = code_span.get_text(strip=True).split('•')[-1].strip()  # "KAP • TERA" gibi
                if not stock_code:
                    continue

                # Haber metnini al (tarih/saat hariç)
                content_div = row.find('div', class_='news-content')
                if not content_div:
                    continue

                # Metni temizle: Sonundaki tarih/saat bilgisini kaldır
                full_text = content_div.get_text(strip=True)
                # Tarih/saat formatlarını kaldır (örn: "Dün 11:35", "Bugün 10:20", "29 Ekim 14:30")
                import re
                date_pattern = r'(?:Dün|Bugün|\d{1,2} \w+ \d{1,2}:\d{2})$'
                clean_text = re.sub(date_pattern, '', full_text).strip()

                # Haber metninde boşluk var mı kontrol et
                if not clean_text:
                    continue

                # Haber satırının ID'sini oluştur (benzersiz olmalı)
                # Örneğin: KAP_TERABVSN_20251029_1135
                # Veya sadece metnin hash'i
                import hashlib
                item_id = hashlib.md5(f"{stock_code}_{clean_text}".encode('utf-8')).hexdigest()[:12]

                news_items.append({
                    'id': item_id,
                    'code': stock_code,
                    'text': clean_text,
                    'raw_header': header_text,
                    'raw_content': full_text
                })

            logging.info(f"{len(news_items)} adet KAP haberi bulundu.")
            return news_items

        except Exception as e:
            logging.error(f"Haber çekme hatası: {e}")
            return []

    def tweet_news(self, news_item: Dict):
        """Bir haberi tweetle."""
        try:
            # Tweet şablonu: 📰 #KOD | Haber detayı
            tweet_text = f"📰 #{news_item['code']} | {news_item['text']}"
            # Karakter sınırı kontrolü (280 karakter)
            if len(tweet_text) > 280:
                tweet_text = tweet_text[:277] + "..."

            # Burada Twitter API'ye bağlan
            # NOT: Tweepy kullanıyoruz ama bu örnek için placeholder
            # Gerçek entegrasyon için aşağıda verdiğim Tweepy örneğini kullan

            # --- Tweepy Entegrasyonu (Aşağıdaki fonksiyonu kullan) ---
            # self.tweet_api.update_status(tweet_text)

            print(f"[TWEET] {tweet_text}")  # Test amaçlı
            logging.info(f"Tweet atıldı: {tweet_text}")

            # Durum kaydet
            self.save_state(news_item['id'])

        except Exception as e:
            logging.error(f"Tweet atma hatası: {e}")

    def run(self):
        """Botu çalıştır."""
        self.setup_driver()
        try:
            # Haberleri al
            news_list = self.get_news_items()

            if not news_list:
                logging.info("Yeni haber bulunamadı.")
                return

            # Son işlenen ID'den sonra gelenleri bul
            new_news = []
            found_last = False

            for item in reversed(news_list):  # En yeni haberden en eskiye doğru
                if self.last_processed_id is None or item['id'] != self.last_processed_id:
                    new_news.append(item)
                else:
                    found_last = True
                    break

            if not found_last and self.last_processed_id is not None:
                # Son işlenen ID listede yoksa, tüm listeyi yeni kabul et
                new_news = news_list

            # Yeni haberleri tweetle (en eski ilk, en yeni son)
            for item in reversed(new_news):
                self.tweet_news(item)
                time.sleep(2)  # Twitter API limitlerine uygun

            logging.info(f"{len(new_news)} adet yeni haber tweetlendi.")

        finally:
            if self.driver:
                self.driver.quit()


# Twitter API Entegrasyonu (Tweepy)
def setup_twitter_api():
    """Twitter API bağlantısı kur."""
    import tweepy

    auth = tweepy.OAuthHandler(TWITTER_API_KEY, TWITTER_API_SECRET)
    auth.set_access_token(TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_TOKEN_SECRET)
    api = tweepy.API(auth, wait_on_rate_limit=True)

    try:
        api.verify_credentials()
        logging.info("Twitter API kimlik doğrulaması başarılı.")
        return api
    except Exception as e:
        logging.error(f"Twitter API kimlik doğrulaması başarısız: {e}")
        return None


if __name__ == "__main__":
    bot = FintablesTweetBot()
    # Twitter API'yi başlat (opsiyonel)
    # bot.tweet_api = setup_twitter_api()
    # if bot.tweet_api is None:
    #     logging.error("Twitter API başlatılamadı. Program sonlandırılıyor.")
    #     exit(1)

    bot.run()
