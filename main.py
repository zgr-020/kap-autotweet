import os, re, json, hashlib, time
from pathlib import Path
import requests
from bs4 import BeautifulSoup
import tweepy

BASE_URL = "https://www.foreks.com/analizler/piyasa-analizleri/sirket"
AMP_URL = BASE_URL.rstrip("/") + "/amp"

STATE_PATH = Path("data/posted.json")
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)

# 3–5 harfli, TR büyük harfleri de kapsayan hisse kodu
TICKER_RE = re.compile(r"\b[A-ZÇĞİÖŞÜ]{3,5}\b", re.UNICODE)

def http_get(url):
    r = requests.get(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
        timeout=25,
    )
    r.raise_for_status()
    return r.text

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

def sha24(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:24]

def normalize_ticker(m: str) -> str:
    return (m.replace("Ç","C").replace("Ğ","G").replace("İ","I")
              .replace("Ö","O").replace("Ş","S").replace("Ü","U"))

def compose_tweet(ticker: str, title: str) -> str:
    base = f"📰 #{ticker} | {title}"
    if len(base) <= 279:
        return base
    return base[:276] + "…"

# ---------- PARSERS ----------

def parse_amp(html: str):
    """
    AMP sayfası statik olur. AMP’de genelde haber kartları <article>, <li> ya da
    <a class="..."> ile gelir; sağdaki etiketler küçük 'chip/tag' linkleridir.
    """
    soup = BeautifulSoup(html, "lxml")
    rows = []

    # AMP’de ana içerik çoğunlukla <main> altında
    container = soup.find("main") or soup

    # Kart benzeri bloklar
    for blk in container.find_all(["article", "li", "div", "section"], recursive=True):
        # Başlık adayı: en uzun metinli <a> veya <h*> içindeki <a>
        a_tags = [a for a in blk.find_all("a", recursive=True) if a.get_text(strip=True)]
        if not a_tags:
            continue
        title_link = max(a_tags, key=lambda a: len(a.get_text(strip=True)))
        title = " ".join(title_link.get_text(" ", strip=True).split())
        if not title or "ŞİRKET HABERLERİ" in title.upper():
            continue

        # Etiket/kod adayı: kısa metinli <a>/<span>’larda 3–5 harf
        codes = []
        for el in blk.find_all(["a", "span", "div"], recursive=True):
            text = el.get_text(strip=True)
            if not text or len(text) > 8:  # etiketler kısa olur
                continue
            for m in TICKER_RE.findall(text):
                n = normalize_ticker(m)
                if 3 <= len(n) <= 5 and n.isupper():
                    codes.append(n)
        codes = list(dict.fromkeys(codes))
        if not codes:
            continue

        rows.append({"title": title, "ticker": codes[0]})

    return rows

def parse_nuxt_json(html: str):
    """
    Foreks Vue/Nuxt tabanlı olabilir. HTML içinde window.__NUXT__ veya benzeri
    bir JSON’da liste olur. Buradan title ve ticker çıkarırız.
    """
    rows = []
    # __NUXT__ gömülü JSON’u çek
    m = re.search(r"window\.__NUXT__\s*=\s*(\{.*?\});", html, re.DOTALL)
    if not m:
        return rows
    try:
        nuxt = json.loads(m.group(1))
    except Exception:
        return rows

    # JSON yapısı olası: nuxt['state'] / ['data'] içinde liste
    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                yield k, v
                yield from walk(v)
        elif isinstance(x, list):
            for i in x:
                yield from walk(i)

    candidates = []
    for k, v in walk(nuxt):
        if isinstance(v, list) and v and isinstance(v[0], dict) and ("title" in v[0] or "name" in v[0]):
            candidates.append(v)

    for arr in candidates:
        for item in arr:
            title = (item.get("title") or item.get("name") or "").strip()
            if not title:
                continue
            text_blob = json.dumps(item, ensure_ascii=False)
            codes = []
            for m2 in TICKER_RE.findall(text_blob):
                n = normalize_ticker(m2)
                if 3 <= len(n) <= 5 and n.isupper():
                    codes.append(n)
            codes = list(dict.fromkeys(codes))
            if not codes:
                continue
            if "ŞİRKET HABERLERİ" in title.upper():
                continue
            rows.append({"title": title, "ticker": codes[0]})

    return rows

def extract_rows_resilient():
    # 1) AMP dene
    try:
        html = http_get(AMP_URL)
        print(f">> fetched AMP html: {len(html)} bytes")
        rows = parse_amp(html)
        if rows:
            print(f">> amp rows: {len(rows)}")
            return rows
        else:
            print(">> amp parse yielded 0 rows, falling back to normal page JSON…")
    except Exception as e:
        print("!! amp fetch error:", e)

    # 2) Normal sayfa + NUXT JSON dene
    html = http_get(BASE_URL)
    print(f">> fetched normal html: {len(html)} bytes")
    rows = parse_nuxt_json(html)
    if rows:
        print(f">> nuxt rows: {len(rows)}")
        return rows

    # 3) Son çare: normal DOM’dan kaba ayrıştırma (bazı durumlar yine işe yarar)
    soup = BeautifulSoup(html, "lxml")
    fallback = []
    for li in soup.find_all(["li", "article", "div"]):
        a_tags = [a for a in li.find_all("a") if a.get_text(strip=True)]
        if not a_tags:
            continue
        title_link = max(a_tags, key=lambda a: len(a.get_text(strip=True)))
        title = " ".join(title_link.get_text(" ", strip=True).split())
        if not title or "ŞİRKET HABERLERİ" in title.upper():
            continue
        codes = []
        for el in li.find_all(["a", "span", "div"]):
            for m in TICKER_RE.findall(el.get_text(strip=True)):
                n = normalize_ticker(m)
                if 3 <= len(n) <= 5 and n.isupper():
                    codes.append(n)
        codes = list(dict.fromkeys(codes))
        if codes:
            fallback.append({"title": title, "ticker": codes[0]})
    print(f">> fallback rows: {len(fallback)}")
    return fallback

# ---------- TWITTER ----------

def twitter_client():
    api_key = os.getenv("API_KEY")
    api_secret = os.getenv("API_KEY_SECRET")
    access_token = os.getenv("ACCESS_TOKEN")
    access_secret = os.getenv("ACCESS_TOKEN_SECRET")

    auth = tweepy.OAuth1UserHandler(api_key, api_secret, access_token, access_secret)
    api = tweepy.API(auth)
    api.verify_credentials()
    return api

def main():
    print(">> start (Foreks BIST Şirketleri)")
    rows = extract_rows_resilient()
    print(f">> parsed rows: {len(rows)}")

    if not rows:
        print(">> no eligible rows")
        return

    seen = load_state()
    api = twitter_client()
    posted_any = False

    # Eski → yeni sırasıyla atmak istersen: rows = rows[::-1]
    for r in rows:
        tweet = compose_tweet(r["ticker"], r["title"])
        uid = sha24(tweet)
        if uid in seen:
            continue
        try:
            api.update_status(status=tweet)
            print(">> tweeted:", tweet)
            seen.add(uid)
            posted_any = True
            time.sleep(3)
        except Exception as e:
            print("!! tweet error:", e)

    if posted_any:
        save_state(seen)
        print(">> state saved")
    else:
        print(">> nothing new to tweet")

if __name__ == "__main__":
    main()
