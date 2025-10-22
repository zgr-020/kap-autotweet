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
    r"işbu açıklama.*?amaçla", r"bu açıklama.*?kapsamında", r"kamunun bilgisine arz olunur",
    r"saygılarımızla", r"yatırımcılarımızın bilgisine", r"özel durum açıklaması",
    r"yatırım tavsiyesi", r"işbu .*? kapsamındadır"
]
def clean_text(t: str) -> str:
    t = re.sub(r"\s+", " ", (t or "")).strip()
    for p in STOP_PHRASES:
        t = re.sub(p, "", t, flags=re.I)
    return t.strip(" -–—:.")

def summarize(text: str, limit: int) -> str:
    """Kısa ve doğal bir özet: ilk 1–2 anlamlı cümleyi al, limite yaklaşınca dur."""
    text = clean_text(text)
    sents = re.split(r"(?<=[.!?])\s+", text)
    out = ""
    for s in sents:
        if not s: 
            continue
        cand = (out + " " + s).strip()
        if len(cand) > limit:
            break
        out = cand
        if len(out) >= limit * 0.7:
            break
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
    n = rows.count()
    for i in range(n):
        th = rows.nth(i).locator("th")
        td = rows.nth(i).locator("td")
        if th.count() > 0 and td.count() > 0:
            key = th.first.inner_text().strip().lower()
            val = td.first.inner_text().strip()
            if key and val:
                kv[key] = val
    return kv

def build_finance_summary(kv: dict) -> str:
    """Tutar, vade, faiz, ISIN, aracı vb. alanlardan kısa özet üretir."""
    # isim varyasyonları
    tutar = kv.get("tutar") or kv.get("gerçekleştirilen nominal tutar") or kv.get("satışa konu nominal tutar") or kv.get("ihraç tavanı tutarı")
    vade  = kv.get("vade tarihi") or kv.get("vade") or kv.get("vade (gün)")
    faiz  = kv.get("faiz oranı - yıllık basit (%)") or kv.get("faiz oranı (%)") or kv.get("faiz oranı") or kv.get("kupon faizi")
    isin  = kv.get("isin kodu") or kv.get("isin kod") or kv.get("isin")
    arac  = kv.get("aracılık hizmeti alınan yatırım kuruluşu") or kv.get("aracı kurum") or kv.get("aracı kurum/kuruluş")
    tip   = kv.get("bildirim konusu") or kv.get("konu") or kv.get("işlem türü")

    parts = []
    if tip:   parts.append(tip)
    if tutar: parts.append(tutar)
    if vade:  parts.append(f"vade {vade}")
    if faiz:  parts.append(f"faiz %{faiz}")
    if isin:  parts.append(f"ISIN {isin}")
    if arac:  parts.append(f"aracı: {arac}")
    return ", ".join([p for p in parts if p])

def extract_company_note(page) -> str:
    """
    Sayfanın altındaki 'Ek Açıklamalar' / 'Açıklamalar' vb. serbest metni döndürür.
    Yoksa boş string.
    """
    # 1) 'Ek Açıklamalar' bir th/td satırı olarak gelebilir
    try:
        el = page.locator("xpath=//th[contains(.,'Ek Açıklamalar')]/following-sibling::td")
        if el.count() > 0:
            txt = el.first.inner_text().strip()
            if len(txt) > 20:
                return txt
    except Exception:
        pass
    # 2) En alttaki paragraflar
    try:
        p_tags = page.locator("article p, .content p, p")
        n = p_tags.count()
        if n > 0:
            # sondan birkaç paragrafı birleştir (genelde not en altta)
            tail = " ".join(p_tags.nth(i).inner_text() for i in range(max(0, n-3), n))
            tail = clean_text(tail)
            # çok uzunsa kısalacak zaten; yeter ki en az 20 karakter olsun
            if len(tail) > 20:
                return tail
    except Exception:
        pass
    return ""

# ==== Liste sayfasından satır çekme ====
def fetch_list_items(page):
    """
    Bildirim listesinde her satır için:
    - code (hisse kodu)
    - title (Konu)
    - url (Detay linki)
    - id  (linkten çıkarılan numerik id)
    döner.
    """
    items = []
    rows = page.locator("table tbody tr")
    n = rows.count()
    for i in range(n):
        row = rows.nth(i)
        tds = row.locator("td")
        if tds.count() < 5:
            continue
        code  = tds.nth(1).inner_text().strip()
        title = tds.nth(4).inner_text().strip()

        # Detay linki; ikon veya metin olabilir
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
        page.set_default_timeout(20000)

        # Liste sayfasını aç
        try:
            page.goto("https://www.kap.org.tr/tr/bildirim-sorgu", wait_until="load")
            # Çerez/KVKK bandı varsa kapat
            try:
                btn = page.locator("button:has-text('Kabul Et'), button:has-text('KABUL ET')")
                if btn.count() > 0:
                    btn.first.click()
                    print(">> cookie accepted")
            except Exception:
                pass
            page.wait_for_selector("table tbody tr", timeout=15000)
        except PWTimeout:
            print("!! tablo gelmedi (timeout)")
            browser.close()
            return

        items = fetch_list_items(page)
        print(f">> parsed items: {len(items)}")

        # Sadece yeni olanlar
        new_items = [it for it in items if it["id"] not in posted]
        print(f">> new items: {len(new_items)} (posted: {len(posted)})")

        # Kronolojik sırayla gönder (eskiden yeniye)
        new_items.reverse()

        for it in new_items:
            # Detay sayfasını aç
            page.goto(it["url"], wait_until="load")
            page.wait_for_timeout(1200)

            # 1) KV tablo → finansal özet
            kv = extract_kv_table(page)
            fin_sum = build_finance_summary(kv)

            # 2) Özet Bilgi (varsa)
            ozet = ""
            try:
                el = page.locator("xpath=//th[contains(.,'Özet Bilgi')]/following-sibling::td")
                if el.count() > 0:
                    ozet = el.first.inner_text().strip()
            except Exception:
                pass

            # 3) Şirketin serbest "açıklama notu" (en altta)
            note = extract_company_note(page)

            # Öncelik: açıklama notu > finansal özet > özet bilgi > başlık
            body = note or fin_sum or ozet or it["title"]

            tweet = format_tweet(it["code"], it["title"], body)
            print(">> TWEET:", tweet)

            try:
                client.create_tweet(text=tweet)
                posted.add(it["id"])
                save_state()
                print(">> tweet sent ✓")
                time.sleep(1.5)  # nazikçe
            except Exception as e:
                print("!! tweet error:", e)

        browser.close()
        print(">> done")

if __name__ == "__main__":
    main()
