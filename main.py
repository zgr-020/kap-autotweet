import os, re, json, hashlib, time
from pathlib import Path
import requests
from bs4 import BeautifulSoup
import tweepy

FOREKS_URL = "https://www.foreks.com/analizler/piyasa-analizleri/sirket"
STATE_PATH = Path("data/posted.json")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)

TICKER_RE = re.compile(r"\b[A-ZÇĞİÖŞÜ]{3,5}\b")

def load_state():
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if STATE_PATH.exists():
        try:
            return set(json.loads(STATE_PATH.read_text()))
        except Exception:
            return set()
    return set()

def save_state(ids):
    STATE_PATH.write_text(json.dumps(sorted(list(ids)), ensure_ascii=False, indent=2))

def sha(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:24]

def fetch_html():
    r = requests.get(
        FOREKS_URL,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8"},
        timeout=20,
    )
    r.raise_for_status()
    return r.text

def extract_rows(html: str):
    """
    Foreks sayfasındaki kutudaki satırları mümkün olduğunca dayanıklı şekilde ayrıştırır.
    - Her satırda sağda 'etiket' gibi görünen bir A etiketi (ticker) var.
    - Satır başlığını aynı satırın ilk linkinden/strong/span'ından alıyoruz.
    """
    soup = BeautifulSoup(html, "lxml")

    # Ana liste kartını bul (başlık 'Piyasa Analizleri' sayfasındaki tek büyük liste)
    # Satırlar genelde <li> veya <div class="list-item"> benzeri; iki yöntemi de deneriz.
    container = None
    for candidate in soup.find_all(["div", "section"]):
        if candidate.get_text(strip=True).startswith("ŞİRKET HABERLERİ") or "Şirket Haberleri" in candidate.get_text():
            container = candidate.parent  # satırların olduğu üst kapsayıcı
            break
    if container is None:
        # Alternatif: sayfadaki tüm olası satır bloklarını tara
        container = soup

    rows = []
    # 1) <li> tabanlı liste
    for li in container.find_all(["li", "div"], recursive=True):
        # Satırın içinde başlık olabilecek ilk link/strong/span
        # ve sağda kod olabilecek link (tam büyük harf 3-5 harf)
        # Başlık linki
        title = None
        title_link = None
        # başlığı tutarlı almak için en uzun metinli <a>’yı seç
        a_tags = [a for a in li.find_all("a", recursive=True) if a.get_text(strip=True)]
        if not a_tags:
            continue
        title_link = max(a_tags, key=lambda a: len(a.get_text(strip=True)))
        title = " ".join(title_link.get_text(" ", strip=True).split())

        # Hisse kodu adayları: satır içindeki tüm <a> ve <span> metinlerinde regex araması
        codes = []
        for el in li.find_all(["a", "span", "div"], recursive=True):
            text = el.get_text(strip=True)
            # sağdaki etiketler genelde kısa ve TAM BÜYÜK HARF; yabancı kelimeleri elemeye çalış
            for m in TICKER_RE.findall(text):
                # Sık çıkan ama kod olmayan kısaltmaları ele
                if m in {"TCMB", "CEO", "NVIDIA", "NVDIA", "BIST", "FOREKS"}:
                    continue
                # Türkçe büyük harfleri normalize edip sadece A-Z yapalım
                norm = (m
                        .replace("Ç","C").replace("Ğ","G").replace("İ","I")
                        .replace("Ö","O").replace("Ş","S").replace("Ü","U"))
                if 3 <= len(norm) <= 5 and norm.isupper():
                    codes.append(norm)
        codes = list(dict.fromkeys(codes))  # uniq, sıra koru

        # Bu satır bir haber kartı mı? Başlıkta 'Şirket Haberleri' yazıyorsa atla
        if not title or "ŞİRKET HABERLERİ" in title.upper():
            continue

        # Eğer hiç kod yoksa bu haberi tweetlemeyeceğiz
        if not codes:
            continue

        # İlk kodu seç
        ticker = codes[0]
        rows.append({"title": title, "ticker": ticker})

    # Düşük kaliteli gürültüyü ele: çok kısa başlıklar, duyuru olmayanlar
    cleaned = []
    for r in rows:
        if len(r["title"]) >= 20:  # çok kısa başlıkları at
            cleaned.append(r)
    return cleaned

def compose_tweet(ticker: str, title: str) -> str:
    base = f"📰 #{ticker} | {title}"
    # Kullanıcının 279 karakter sınırı
    if len(base) <= 279:
        return base
    # Gerekirse akıllı kes: önce parantez/alt açıklamaları kısalt
    trimmed = base[:276] + "…"
    return trimmed

def twitter_client():
    api_key = os.getenv("API_KEY")
    api_secret = os.getenv("API_KEY_SECRET")
    access_token = os.getenv("ACCESS_TOKEN")
    access_secret = os.getenv("ACCESS_TOKEN_SECRET")

    auth = tweepy.OAuth1UserHandler(api_key, api_secret, access_token, access_secret)
    api = tweepy.API(auth)
    # basit doğrulama
    api.verify_credentials()
    return api

def main():
    print(">> start (Foreks BIST Şirketleri)")
    html = fetch_html()
    rows = extract_rows(html)
    print(f">> parsed rows: {len(rows)}")

    if not rows:
        print(">> no eligible rows")
        return

    seen = load_state()
    api = twitter_client()

    posted_any = False
    # En yeni en üste geliyor; sondan başa gidip eskinin önce atılmasını isteyebilirsin.
    for r in rows:
        tweet = compose_tweet(r["ticker"], r["title"])
        uid = sha(tweet)  # aynı tweet bir daha atılmasın
        if uid in seen:
            continue
        try:
            api.update_status(status=tweet)
            print(">> tweeted:", tweet)
            seen.add(uid)
            posted_any = True
            time.sleep(3)  # hız limiti için küçük bekleme
        except Exception as e:
            print("!! tweet error:", e)

    if posted_any:
        save_state(seen)
        print(">> state saved")
    else:
        print(">> nothing new to tweet")

if __name__ == "__main__":
    main()
