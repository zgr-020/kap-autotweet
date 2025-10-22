import os, re, json, time, tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from pdfminer.high_level import extract_text
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

# ==== Kalıcı durum ====
STATE_FILE = Path("state.json")
posted = set(json.loads(STATE_FILE.read_text())) if STATE_FILE.exists() else set()
def save_state(): STATE_FILE.write_text(json.dumps(sorted(list(posted)), ensure_ascii=False))

# ==== Metin yardımcıları ====
STOP_PHRASES = [
    r"işbu açıklama.*?amaçla", r"bu açıklama.*?kapsamında", r"kamunun bilgisine arz olunur",
    r"saygılarımızla", r"yatırımcılarımızın bilgisine", r"özel durum açıklaması",
    r"yatırım tavsiyesi", r"işbu .*? kapsamındadır"
]
def clean_text(t: str) -> str:
    t = re.sub(r"\s+", " ", (t or "")).strip()
    for p in STOP_PHRASES: t = re.sub(p, "", t, flags=re.I)
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

# ==== Listeyi al (XHR ya da DOM) ====
def fetch_list_items(page):
    # Sayfaya git → “Kabul Et” → “Ara”
    page.goto("https://www.kap.org.tr/tr/bildirim-sorgu", wait_until="networkidle")
    try:
        btn = page.get_by_role("button", name=re.compile("Kabul Et", re.I))
        if btn.count(): btn.first.click()
    except Exception: pass
    try:
        page.get_by_role("button", name=re.compile("^Ara$", re.I)).first.click()
    except Exception:
        page.locator("button:has-text('Ara'), [role='button']:has-text('Ara')").first.click()

    # 1) XHR yanıtından yakalamayı dene
    def is_resp(r):
        u = r.url.lower()
        return r.status==200 and ("api" in u) and ("disclosure" in u or "bildirim" in u or "search" in u)
    items = []
    try:
        resp = page.wait_for_response(lambda r: is_resp(r), timeout=30000)
        j = resp.json()
        data = j.get("data") if isinstance(j, dict) else j
        for it in (data or []):
            code = it.get("companyCode") or it.get("code") or ""
            title = it.get("title") or it.get("subject") or ""
            href  = it.get("detailUrl") or ""
            _id   = it.get("id") or ""
            if not href and _id:
                href = f"https://www.kap.org.tr/tr/Bildirim/{_id}"
            if not (_id or href) or not code or not title: continue
            if not _id:
                m = re.search(r"(\d{6,})", href); _id = m.group(1) if m else href
            items.append({"id": _id, "code": code, "title": title, "url": href})
    except Exception:
        pass

    # 2) Olmazsa DOM’dan al
    if not items:
        page.wait_for_selector("table tbody tr, .table tbody tr", timeout=30000)
        rows = page.locator("table tbody tr, .table tbody tr")
        for i in range(rows.count()):
            row = rows.nth(i); tds = row.locator("td")
            if tds.count() < 5: continue
            code = tds.nth(1).inner_text().strip()
            title = tds.nth(4).inner_text().strip()
            link = ""
            lc = row.locator('a[href*="/tr/Bildirim/"], a[href*="/tr/bildirim/"]')
            if lc.count(): link = lc.first.get_attribute("href")
            if not link:
                a = row.locator("a"); 
                if a.count(): link = a.first.get_attribute("href")
            if not (code and title and link): continue
            m = re.search(r"(\d{6,})", link); _id = m.group(1) if m else link
            items.append({"id": _id, "code": code, "title": title, "url": link})
    return items

# ==== Detay PDF → metin ====
def extract_text_from_pdf(page) -> str:
    """
    Detay sayfasının sağındaki 'PDF' tuşunu tıkla,
    PDF response'ını yakala ve metni çıkar.
    """
    text = ""
    # 1) PDF düğmesini bul
    pdf_btn = None
    for sel in [
        "a:has-text('PDF')",
        "button:has-text('PDF')",
        "a[title*='PDF' i]",
        "a[href*='.pdf']",
    ]:
        try:
            cand = page.locator(sel)
            if cand.count():
                pdf_btn = cand.first; break
        except Exception:
            continue
    if not pdf_btn:
        return text

    # 2) PDF response'u bekle (bazı sayfalarda indirme olabiliyor)
    try:
        with page.expect_response(lambda r: "application/pdf" in (r.headers.get("content-type","").lower()), timeout=20000) as resp_info:
            pdf_btn.click()
        resp = resp_info.value
        pdf_bytes = resp.body()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as f:
            f.write(pdf_bytes); pdf_path = f.name
        text = extract_text(pdf_path) or ""
        try: os.remove(pdf_path)
        except Exception: pass
        if text: return text
    except Exception:
        pass

    # 3) Fallback: indirme olayı (download) ile yakala
    try:
        with page.expect_download(timeout=20000) as dl_info:
            pdf_btn.click()
        download = dl_info.value
        pdf_path = download.path()
        if not pdf_path:
            pdf_path = download.save_as(str(Path(tempfile.gettempdir()) / f"kap_{int(time.time())}.pdf"))
        text = extract_text(pdf_path) or ""
        return text
    except Exception:
        return ""

def main():
    print(">> start")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox","--disable-gpu","--disable-dev-shm-usage"])
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
            locale="tr-TR",
            timezone_id="Europe/Istanbul"
        )
        page = context.new_page(); page.set_default_timeout(30000)

        # 1) Listeyi al
        try:
            items = fetch_list_items(page)
        except PWTimeout:
            print("!! page timeout (list)"); browser.close(); return

        if not items:
            print("!! no items from xhr/dom"); browser.close(); return

        print(f">> parsed items: {len(items)}")
        new_items = [it for it in items if it["id"] not in posted]
        print(f">> new items: {len(new_items)} (posted: {len(posted)})")
        new_items.reverse()  # eskiden yeniye

        # 2) Her yeni ilanın detayına gir → PDF → özet → tweet
        for it in new_items:
            page.goto(it["url"], wait_until="load")
            page.wait_for_timeout(1200)

            pdf_text = extract_text_from_pdf(page)  # PDF metni (varsa)
            body = pdf_text or it["title"]
            tweet = format_tweet(it["code"], it["title"], body)

            print(">> TWEET:", tweet)
            try:
                client.create_tweet(text=tweet)
                posted.add(it["id"]); save_state()
                print(">> tweet sent ✓")
                time.sleep(1.5)
            except Exception as e:
                print("!! tweet error:", e)

        browser.close(); print(">> done")

if __name__ == "__main__":
    main()
