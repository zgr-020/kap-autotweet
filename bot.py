import json
import re
import hashlib
import os
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager
import tweepy

# === Twitter API ===
auth = tweepy.OAuthHandler(os.environ['API_KEY'], os.environ['API_KEY_SECRET'])
auth.set_access_token(os.environ['ACCESS_TOKEN'], os.environ['ACCESS_TOKEN_SECRET'])
api = tweepy.API(auth, wait_on_rate_limit=True)

# === State Yönetimi ===
STATE_FILE = 'state.json'

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {'last_id': None}

def save_state(last_id):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump({'last_id': last_id}, f, ensure_ascii=False, indent=2)

# === Haberleri Çek (Selenium ile) ===
def fetch_kap_news_selenium():
    options = Options()
    options.add_argument("--headless")
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
    driver = webdriver.Chrome(service=service, options=options)

    try:
        url = "https://fintables.com/borsa-haber-akisi"
        driver.get(url)

        # "Öne Çıkanlar" butonuna tıkla
        wait = WebDriverWait(driver, 10)
        prominent_button = wait.until(
            EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Öne Çıkanlar')]"))
        )
        prominent_button.click()
        time.sleep(3)  # Sayfanın yeniden yüklenmesini bekle

        page_source = driver.page_source
        soup = BeautifulSoup(page_source, 'html.parser')

        news_items = []

        # Haber satırlarını bul (Fintables yapısına göre)
        rows = soup.find_all('div', class_='flex items-center justify-between py-3 border-b border-gray-100')
        if not rows:
            rows = soup.find_all('div', attrs={'class': lambda x: x and 'news-item' in x})

        for row in rows:
            # Header: "KAP • TERA" gibi
            header = row.find('div', class_=lambda x: x and 'font-bold' in x)
            if not header:
                continue

            header_text = header.get_text(strip=True)
            if not header_text.startswith('KAP'):
                continue

            # Mavi hisse kodunu al
            code_span = row.find('span', class_=lambda x: x and ('text-blue' in x or 'blue' in x))
            if not code_span:
                parts = header_text.split('•')
                if len(parts) < 2:
                    continue
                stock_code = parts[1].strip().split()[0]
            else:
                stock_code = code_span.get_text(strip=True).split()[0]

            # İçerik metni
            content_div = row.find('div', class_=lambda x: x and 'text-gray' in x)
            if not content_div:
                continue

            full_text = content_div.get_text(strip=True)

            # Tarih/saat bilgisini kaldır
            clean_text = re.sub(r'(?:Dün|Bugün|\d{1,2}\s+\w+\s+\d{1,2}:\d{2})$', '', full_text).strip()

            if not clean_text or len(clean_text) < 5:
                continue

            item_id = hashlib.md5(f"{stock_code}|{clean_text}".encode()).hexdigest()[:16]

            news_items.append({
                'id': item_id,
                'code': stock_code,
                'text': clean_text
            })

        return list(reversed(news_items))

    except Exception as e:
        print(f"Selenium hatası: {e}")
        return []
    finally:
        driver.quit()

# === Ana İşlem ===
def main():
    state = load_state()
    news_list = fetch_kap_news_selenium()

    if not news_list:
        print("Haber bulunamadı.")
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
            print(f"✅ Tweet atıldı: {tweet}")
            save_state(item['id'])
        except Exception as e:
            print(f"❌ Hata: {e}")

if __name__ == '__main__':
    import time  # time.sleep için gerekli
    main()
