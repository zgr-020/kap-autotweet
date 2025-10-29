import json
import re
import hashlib
import os
import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

import tweepy

# === Twitter API ===
auth = tweepy.OAuthHandler(os.environ['API_KEY'], os.environ['API_KEY_SECRET'])
auth.set_access_token(os.environ['ACCESS_TOKEN'], os.environ['ACCESS_TOKEN_SECRET'])
api = tweepy.API(auth, wait_on_rate_limit=True)

STATE_FILE = 'state.json'

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {'last_id': None}

def save_state(last_id):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump({'last_id': last_id}, f, ensure_ascii=False, indent=2)

def fetch_fintables_news():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)

    try:
        # Bot izini sil
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        url = "https://fintables.com/borsa-haber-akisi"
        driver.get(url)

        # Sayfanın yüklenmesini bekle
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )

        # "Öne Çıkanlar" butonuna tıkla
        try:
            prominent_btn = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Öne Çıkanlar')]"))
            )
            prominent_btn.click()
            time.sleep(5)
        except:
            print("Öne Çıkanlar butonu bulunamadı. Devam ediliyor...")

        # Sayfa kaynağını al
        html = driver.page_source

        # Debug: HTML'i kaydet (test için)
        with open("debug.html", "w", encoding="utf-8") as f:
            f.write(html)

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, 'html.parser')

        news_items = []

        # Haber satırlarını bul — Fintables'in gerçek yapısı:
        # Her haber satırı: <div class="flex items-center justify-between ...">
        rows = soup.find_all('div', class_=lambda x: x and 'items-center' in x and 'justify-between' in x and 'py-3' in x)

        print(f"Bulunan satır sayısı: {len(rows)}")

        for row in rows:
            # Header: "KAP • TERA" gibi
            header_div = row.find('div', class_=lambda x: x and 'font-bold' in x)
            if not header_div:
                continue

            header_text = header_div.get_text(strip=True)
            if not header_text.startswith('KAP'):
                continue

            # Mavi hisse kodu: genelde span içinde, text-blue sınıfı
            code_span = row.find('span', class_=lambda x: x and 'text-blue' in x)
            if code_span:
                stock_code = code_span.get_text(strip=True)
            else:
                # Alternatif: "KAP • XXXX" formatından çıkar
                parts = header_text.split('•')
                if len(parts) < 2:
                    continue
                stock_code = parts[1].strip().split()[0]

            # İçerik metni
            content_div = row.find('div', class_=lambda x: x and 'text-gray-600' in x)
            if not content_div:
                continue

            full_text = content_div.get_text(strip=True)

            # Tarih/saat sonunda olur → kaldır
            clean_text = re.sub(r'(?:Dün|Bugün|\d{1,2}\s+\w+\s+\d{1,2}:\d{2})$', '', full_text).strip()

            if not clean_text or len(clean_text) < 10:
                continue

            item_id = hashlib.md5(f"{stock_code}|{clean_text}".encode()).hexdigest()[:16]
            news_items.append({
                'id': item_id,
                'code': stock_code,
                'text': clean_text
            })

        return list(reversed(news_items))

    except Exception as e:
        print(f"Hata: {e}")
        return []
    finally:
        driver.quit()

def main():
    state = load_state()
    news_list = fetch_fintables_news()

    if not news_list:
        print("Haber bulunamadı. debug.html dosyasını inceleyin.")
        return

    start_index = 0
    if state['last_id']:
        for i, item in enumerate(news_list):
            if item['id'] == state['last_id']:
                start_index = i + 1
                break
        else:
            start_index = max(0, len(news_list) - 5)

    new_items = news_list[start_index:]

    if not new_items:
        print("Yeni haber yok.")
        return

    for item in new_items:
        tweet = f"📰 #{item['code']} | {item['text']}"
        if len(tweet) > 280:
            tweet = tweet[:277] + "..."

        try:
            api.update_status(tweet)
            print(f"✅ Tweet: {tweet}")
            save_state(item['id'])
        except Exception as e:
            print(f"❌ Tweet hatası: {e}")

if __name__ == '__main__':
    main()
