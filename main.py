import json
import re
import hashlib
import os
from datetime import datetime

import requests
from bs4 import BeautifulSoup
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

# === Haberleri Çek ===
def fetch_kap_news():
    url = "https://fintables.com/borsa-haber-akisi"
    response = requests.get(url)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    news_items = []

    # Tüm haber satırlarını bul (Fintables yapısına göre)
    # Örnek: <div class="flex items-center justify-between py-3 border-b border-gray-100">
    rows = soup.find_all('div', class_=lambda x: x and 'py-3' in x and 'border-b' in x)

    for row in rows:
        # Header kısmını al
        header = row.find('div', class_=lambda x: x and 'font-bold' in x)
        if not header:
            continue

        header_text = header.get_text(strip=True)
        if not header_text.startswith('KAP'):
            continue

        # Mavi renkte hisse kodunu al
        code_span = row.find('span', class_=lambda x: x and ('text-blue' in x or 'blue' in x))
        if not code_span:
            # Alternatif: header içinde "•" sonrası kısım
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

        # Tarih/saat bilgisini kaldır (örn: "Dün 11:35", "Bugün 10:20")
        clean_text = re.sub(r'(?:Dün|Bugün|\d{1,2}\s+\w+\s+\d{1,2}:\d{2})$', '', full_text).strip()

        if not clean_text or len(clean_text) < 5:
            continue

        # Benzersiz ID oluştur
        item_id = hashlib.md5(f"{stock_code}|{clean_text}".encode()).hexdigest()[:16]

        news_items.append({
            'id': item_id,
            'code': stock_code,
            'text': clean_text
        })

    # En yeni ilk → ters çevir
    return list(reversed(news_items))

# === Ana İşlem ===
def main():
    state = load_state()
    news_list = fetch_kap_news()

    if not news_list:
        print("Haber bulunamadı.")
        return

    # Son işlenen ID'den sonraki haberleri al
    start_index = 0
    if state['last_id']:
        for i, item in enumerate(news_list):
            if item['id'] == state['last_id']:
                start_index = i + 1
                break
        else:
            # ID bulunamadıysa, en fazla son 5 haberi tweetle
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
    main()
