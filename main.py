import os, re, json, time
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
import tweepy

# ==== X (Twitter) erişimi ====
API_KEY = os.getenv("API_KEY")
API_KEY_SECRET = os.getenv("API_KEY_SECRET")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
ACCESS_TOKEN_SECRET = os.getenv("ACCESS_TOKEN_SECRET")

client = tweepy.Client(
    consumer_key=API_KEY,
    consumer_secret=API_KEY_SECRET,
    access_token=ACCESS_TOKEN,
    access_token_secret=ACCESS_TOKEN_SECRET
)

# ==== Kalıcı durum (aynı KAP id'sini ikinci kez atma) ====
STATE_FILE = Path("state.json")
posted = set(json.loads(STATE_FILE.read_text())) if STATE_FILE.exists() else set()
def save_state():
    STATE_FILE.write_text(json.dumps(sorted(list(posted)), ensure_ascii=False))

# ==== Metin yardımcıları ====
STOP_PHRASES = [
    r"işbu açıklama.*?amaçla", r"bu açıklama.*?kapsamında",
    r"kamunun bilgisine arz olunur", r"saygılarımızla",
    r"yatırımcılarımızın bilgisine", r"özel durum açıklaması",
    r"yatırım tavsiyesi", r"işbu .*? kapsamındadır"
]
def clean_text(t: str) -> str:
    t = re.sub(r"\s+", " ", (t or "")).strip()
    for p in STOP_PHRASES:
        t = re.sub(p, "", t, flags=re.I)
    return t.strip(" -–—:.")

def summarize(text: str, limit: int) -> str:
    text = clean_text(text)
    sents = re.split(r"(?<=[.!?])\s+", text)
    out = ""
    for s in sents:
        if not s: continue
        cand = (out + " " + s).strip()
        if len(cand) > limit: break
        out = cand
        if len(out) >= limit * 0.7: break
    return out or text[:limit]

def is_pnl_news(title: str, body: str) -> bool:
    blob = (title or "") + " " + (body or "")
    return bool(re.search(r"\b(kâr|kar|zarar|net dönem kar|dönem karı|zararı|finansal sonuç)\b", blob.lower()))

def format_tweet(code: str, title: str, body: str) -> str:
    head_emoji = "💰" if is_pnl_news(title, body) else "📰"
    head = f"{head_emoji} #{code} | "
    return (head + summarize(body or title, 279 - len(head)))[:279]

# ==== Detay sayfası okuyucular ====
def extract_kv_table(page) -> dict:
    """th/td tablosunu sözlüğe çevirir (alan adı -> değer)."""
    kv = {}
    rows = page.locator("table tr")
    for i in range(rows.count()):
        th = rows.nth(i).locator("th")
        td = rows.nth(i).locator("td")
        if th.count() > 0 and td.count() > 0:
            key = th.first.inner_text().strip().lower()
            val = td.first.inner_text().strip()
            if key and val:
                kv[key] = val
    return kv

# Anahtar seçiminde öncelik vereceğimiz (ama bununla sınırlı olmayan) kelimeler
KEY_PRIORITIES = [
    "konu", "işlem türü", "sözleşme", "iş ilişkisi", "taraf", "karar", "tarih",
    "tutar", "fiyat", "adet", "oran", "pay", "vade", "faiz", "temettü",
    "ihale", "alım", "satış", "devralma", "devretme", "yatırım", "proje",
    "isin", "aracı", "müşteri", "tedarikçi"
]
KEY_BLACKLIST = [
    "gönderim tarihi", "yıl", "periyot", "bildirim tipi", "yapılan açıklamanın",
    "yapılan açıklama ertelenmiş", "ekler", "dil", "referans", "versiyon"
]

def pick_informative_pairs(kv: dict, max_pairs: int = 4):
    """Anahtar-değer sözlüğünden en bilgilendirici birkaç çifti seç."""
    if not kv: return []
    scored = []
    for k, v in kv.items():
        if any(b in k for b in KEY_BLACKLIST): 
            continue
        score = 0
        for i, word in enumerate(KEY_PRIORITIES[::-1]):  # sondakilere az, baştakilere çok puan
            if word in k:
                score += (i + 1)
        # uzun ama gereksiz anahtarları bastır
        score += min(len(v), 40) / 40.0
        scored.append((score, k, v))
    scored.sort(reverse=True)
    out = []
    for _, k, v in scored:
        out.append(f"{k}: {v}")
        if len(out) >= max_pairs:
            break
    return out

def build_generic_summary(kv: dict) -> str:
    """Her ilan türü için çalışacak genel özet: en iyi 3–4 anahtar=değer."""
    pairs = pick_informative_pairs(kv, max_pairs=4)
    return ", ".join(pairs)

def extract_company_note(page) -> str:
    """Sayfanın altındaki serbest metni döndürür (Ek Açıklamalar / paragraflar)."""
    try:
        el = page.locator("xpath=//th[contains(.,'Ek Açıklamalar')]/following-sibling::td")
        if el.count() > 0:
            txt = el.first.inner_text().strip()
            if len(txt) > 20:
                return txt
    except Exception:
        pass
    try:
        p_tags = page.locator("article p, .content p, p")
        n = p_tags.count()
        if n > 0:
            tail = " ".join(p_tags.nth(i).inner_text() for i in range(max(0, n-3), n))
            tail = clean_text(tail)
            if len(tail) > 20:
                return tail
    except Exception:
        pass
    return ""

# ==== Liste sayfasından satır çekme ====
def fetch_list_items(page):
    items = []
    rows = page.locator("table tbody tr, .table tbody tr")
    for i in range(rows.count()):
        row = rows.nth(i)
        tds = row.locator("td")
        if tds.count() < 5: 
            continue
        code  = tds.nth(1).inner_text().strip()
        title = tds.nth(4).inner_text().strip()

        link = ""
        lc = row.locator('a[href*="/tr/Bildirim/"], a[href*="/tr/bildirim/"]')
        if lc.count() > 0:
            link = lc.first.get_attribute("href")
        if not link:
            a = row.locator("a")
            if a.count() > 0:
                link = a.first.get_attribute("href")

        if not (code and title and link):
            continue
        m = re.search(r"(\d{6,})", link)
        kap_id = m.group(1) if m else link
        items.append({"id": kap_id, "code": code, "title": title, "url": link})
    return items

# ==== Ana akış ====
def main():
    print(">> start")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]
        )
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
            locale="tr-TR",
            timezone_id="Europe/Istanbul"
        )
        page = context.new_page()
        page.set_default_timeout(30000)

        try:
            # 1) Liste sayfası
            page.goto("https://www.kap.org.tr/tr/bildirim-sorgu", wait_until="networkidle")

            # 2) Çerez/KVKK bandı varsa kapat
            try:
                btn = page.locator("button:has-text('Kabul Et'), button:has-text('KABUL ET')")
                if btn.count() > 0:
                    btn.first.click()
                    print(">> cookie accepted")
            except Exception:
                pass

            # 3) 'Ara' butonuna bas (tablo bu tıkla geliyor)
            try:
                search_btn = page.locator("button:has-text('Ara'), [role='button']:has-text('Ara')")
                search_btn.first.click()
                print(">> search clicked")
            except Exception as e:
                print("!! search click failed:", e)

            # 4) Tabloyu bekle
            page.wait_for_selector("table tbody tr, .table tbody tr", timeout=30000)

        except PWTimeout:
            print("!! tablo gelmedi (timeout)")
            browser.close()
            return

        # 5) Satırları topla
        items = fetch_list_items(page)
        print(f">> parsed items: {len(items)}")

        # 6) Sadece yeni olanları işle
        new_items = [it for it in items if it["id"] not in posted]
        print(f">> new items: {len(new_items)} (posted: {len(posted)})")

        new_items.reverse()  # eskiden yeniye

        for it in new_items:
            # Detay sayfasını aç
            page.goto(it["url"], wait_until="load")
            page.wait_for_timeout(1200)

            # a) KV tablo (varsa) → genel özet
            kv = extract_kv_table(page)
            kv_sum = build_generic_summary(kv) if kv else ""

            # b) Özet Bilgi (varsa)
            ozet = ""
            try:
                el = page.locator("xpath=//th[contains(.,'Özet Bilgi')]/following-sibling::td")
                if el.count() > 0:
                    ozet = el.first.inner_text().strip()
            except Exception:
                pass

            # c) Şirketin serbest "açıklama notu" (en altta)
            note = extract_company_note(page)

            # Öncelik: açıklama notu > kv özet > özet bilgi > başlık
            body = note or kv_sum or ozet or it["title"]

            tweet = format_tweet(it["code"], it["title"], body)
            print(">> TWEET:", tweet)

            try:
                client.create_tweet(text=tweet)
                posted.add(it["id"])
                save_state()
                print(">> tweet sent ✓")
                time.sleep(1.5)
            except Exception as e:
                print("!! tweet error:", e)

        browser.close()
        print(">> done")

if __name__ == "__main__":
    main()
