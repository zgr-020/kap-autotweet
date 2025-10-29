import os, re, json, hashlib, time
from pathlib import Path
import requests
from bs4 import BeautifulSoup
import tweepy

# Playwright
from playwright.sync_api import sync_playwright

BASE_URL = "https://www.foreks.com/analizler/piyasa-analizleri/sirket"
AMP_URL = BASE_URL.rstrip("/") + "/amp"

STATE_PATH = Path("data/posted.json")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36")

TICKER_RE = re.compile(r"\b[A-ZÇĞİÖŞÜ]{3,5}\b", re.UNICODE)

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
    import hashlib
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:24]

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

def normalize_ticker(m: str) -> str:
    return (m.replace("Ç","C").replace("Ğ","G").replace("İ","I")
              .replace("Ö","O").replace("Ş","S").replace("Ü","U"))

def compose_tweet(ticker: str, title: str) -> str:
    base = f"📰 #{ticker} | {title}"
    return base if len(base) <= 279 else base[:276] + "…"

# ---------- Parsers ----------
def extract_rows_from_html(html: str):
    soup = BeautifulSoup(html, "lxml")
    rows = []

    # Sayfadaki listeleri kaba tarama – etiket (kod) + başlık aynı blokta
    for blk in soup.find_all(["li", "article", "div", "section"]):
        a_tags = [a for a in blk.find_all("a") if a.get_text(strip=True)]
        if not a_tags:
            continue
        title_link = max(a_tags, key=lambda a: len(a.get_text(strip=True)))
        title = " ".join(title_link.get_text(" ", strip=True).split())
        if not title or "ŞİRKET HABERLERİ" in title.upper():
            continue

        # Etiket/kod: kısa ve TAM büyük harfli link/etiket
        codes = []
        for el in blk.find_all(["a", "span", "div"]):
            text = el.get_text(strip=True)
            if not text or len(text) > 8:  # sağdaki chip kısadır
                continue
            for m in TICKER_RE.findall(text):
                n = normalize_ticker(m)
                if 3 <= len(n) <= 5 and n.isupper() and n not in {"TCMB","CEO","BIST"}:
                    codes.append(n)
        codes = list(dict.fromkeys(codes))
        if not codes:
            continue

        rows.append({"title": title, "ticker": codes[0]})

    # Çok kısa başlıkları ele
    rows = [r for r in rows if len(r["title"]) >= 20]
    return rows

def fetch_rendered_with_playwright(url: str) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA, locale="tr-TR")
        page = context.new_page()
        page.set_default_timeout(25000)

        # Bazı siteler cookie banner koyar; önce direkt git
        page.goto(url, wait_until="networkidle")
        # İçerik akışı yüklensin diye az beklet
        page.wait_for_timeout(2000)

        # "BIST Şirketleri" filtresi otomatik aktif ama garanti olsun:
        # Eğer bir filtre sekmesi görünüyorsa, metin içeren butonu tıkla
        try:
            tab = page.get_by_role("button", name=re.compile("BIST Şirketleri", re.I))
            if tab.is_visible():
                tab.click()
                page.wait_for_timeout(500)
        except Exception:
            pass

        html = page.content()
        browser.close()
        return html

# ---------- Twitter ----------
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

    # 1) AMP dene
    try:
        amp_html = http_get(AMP_URL)
        print(f">> fetched AMP html: {len(amp_html)} bytes")
        rows = extract_rows_from_html(amp_html)
        if rows:
            print(f">> amp rows: {len(rows)}")
        else:
            print(">> amp gave 0 rows")
    except Exception as e:
        print("!! amp error:", e)
        rows = []

    # 2) Normal sayfa
    if not rows:
        try:
            norm_html = http_get(BASE_URL)
            print(f">> fetched normal html: {len(norm_html)} bytes")
            rows = extract_rows_from_html(norm_html)
            print(f">> normal rows: {len(rows)}")
        except Exception as e:
            print("!! normal error:", e)
            rows = []

    # 3) Render (Playwright)
    if not rows:
        print(">> trying Playwright rendered DOM…")
        try:
            rend_html = fetch_rendered_with_playwright(BASE_URL)
            print(f">> fetched rendered html: {len(rend_html)} bytes")
            rows = extract_rows_from_html(rend_html)
            print(f">> rendered rows: {len(rows)}")
        except Exception as e:
            print("!! render error:", e)
            rows = []

    print(f">> parsed rows: {len(rows)}")
    if not rows:
        print(">> no eligible rows")
        return

    seen = load_state()
    api = twitter_client()
    posted_any = False

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
