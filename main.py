import os, re, json, time
from pathlib import Path
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright
import tweepy

# ================== X (Twitter) anahtarları ==================
API_KEY = os.getenv("API_KEY")
API_KEY_SECRET = os.getenv("API_KEY_SECRET")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
ACCESS_TOKEN_SECRET = os.getenv("ACCESS_TOKEN_SECRET")

client = tweepy.Client(
    consumer_key=API_KEY,
    consumer_secret=API_KEY_SECRET,
    access_token=ACCESS_TOKEN,
    access_token_secret=ACCESS_TOKEN_SECRET,
)

# ================== Durum (aynı şeyi iki kez atma) ==========
STATE_FILE = Path("state.json")
posted = set(json.loads(STATE_FILE.read_text())) if STATE_FILE.exists() else set()
def save_state():
    STATE_FILE.write_text(json.dumps(sorted(list(posted)), ensure_ascii=False))

# ================== Yardımcılar ==============================
AKIS_URL = "https://fintables.com/borsa-haber-akisi"
UPPER_TR = "A-ZÇĞİÖŞÜ"
CODE_RE = re.compile(rf"\b[{UPPER_TR}0-9]{{3,6}}\b")

STOP_PHRASES = [
    r"işbu açıklama.*?amaçla", r"yatırım tavsiyesi değildir", r"kamunun bilgisine arz olunur",
    r"saygılarımızla", r"özel durum açıklaması", r"yatırımcılarımızın bilgisine",
]
def clean_text(t: str) -> str:
    t = re.sub(r"\s+", " ", (t or "")).strip()
    for p in STOP_PHRASES:
        t = re.sub(p, "", t, flags=re.I)
    return t.strip(" -–—:.")

def summarize(text: str, limit: int) -> str:
    text = clean_text(text)
    # modal başlığı zaten kısa olur; yine de emniyet
    if len(text) <= limit:
        return text
    # cümle sonuna kadar kes
    parts = re.split(r"(?<=[.!?])\s+", text)
    out = ""
    for s in parts:
        if not s: continue
        cand = (out + " " + s).strip()
        if len(cand) > limit: break
        out = cand
    return out or text[:limit]

# Basit bir “özgünleştirici”: çekirdek anlam ve sayıları korur, kalıpları sadeleştirir
REWRITE_MAP = [
    (r"\bbildirdi\b", "duyurdu"),
    (r"\bbildirimi\b", "açıklaması"),
    (r"\bilgisine\b", "paylaştı"),
    (r"\bgerçekleştirdi\b", "tamamladı"),
    (r"\bbaşladı\b", "başlattı"),
    (r"\bdevam ediyor\b", "sürdürülüyor"),
    (r"\butağında\b", "kapsamında"),
]
def rewrite_turkish_short(s: str) -> str:
    s = clean_text(s)
    # tırnak/boş parantez/tekrar temizliği
    s = re.sub(r"[“”\"']", "", s)
    s = re.sub(r"\(\s*\)", "", s)
    # bazı kalıpları sadeleştir
    for pat, rep in REWRITE_MAP:
        s = re.sub(pat, rep, s, flags=re.I)
    # baştaki “Şirket/…;” gibi etiketleri kırp
    s = re.sub(r"^\s*[-–—•·]\s*", "", s)
    return s.strip()

def is_pnl_news(text: str) -> bool:
    txt = text.lower()
    return any(k in txt for k in ["kâr", "kar", "zarar", "net dönem", "temettü", "temettu"])

def build_tweet(code: str, headline: str) -> str:
    base = rewrite_turkish_short(headline)
    base = summarize(base, 240)  # biraz pay bırakalım
    head = ("💰" if is_pnl_news(base) else "📰") + f" #{code} | "
    return (head + base)[:279]

# ================== Fintables → “KAP” satırları ==================
def get_kap_rows(page):
    """
    Akış sayfasındaki 'KAP' etiketli satırlardan:
    - benzersiz id (satır metni + zaman damgasından türetilir)
    - hisse kodu (mavi chip/etiket)
    - modalı açmak için tıklanacak anchor
    döndürür.
    """
    page.goto(AKIS_URL, wait_until="networkidle")
    page.wait_for_timeout(1500)

    # Satır kapsayıcıları: her satır genelde <li> veya <div> blok
    candidates = page.locator("li, div").filter(has_text=re.compile(r"\bKAP\b"))
    rows = []
    seen_ids = set()

    for i in range(min(200, candidates.count())):  # ilk 200 satır yeter
        row = candidates.nth(i)
        text = row.inner_text().strip()
        if "KAP" not in text:
            continue

        # hisse kodu: mavi etiketin metni (regex + yakın çevre fallback)
        m = CODE_RE.search(text)
        code = m.group(0) if m else ""
        if not code:
            inner_tags = row.locator("a, span, div")
            for j in range(min(10, inner_tags.count())):
                t = (inner_tags.nth(j).inner_text() or "").strip()
                mm = CODE_RE.search(t)
                if mm:
                    code = mm.group(0); break
        if not code:
            continue

        # tıklanacak link (aynı satır içindeki ilk anchor)
        link = row.locator("a").first
        if link.count() == 0:
            continue

        # benzersiz id oluştur: link href + görünen metinden
        href = link.get_attribute("href") or f"row-{i}"
        mslug = re.search(r"([a-z0-9_-]{8,}|[0-9]{6,})", href, re.I)
        rid = (mslug.group(1) if mslug else href) + "_" + code

        if rid in seen_ids:
            continue
        seen_ids.add(rid)

        rows.append({"id": rid, "code": code, "link": link})

    return rows

# ================== Modal başlığını çek ========================
def open_row_and_read_headline(page, link_locator):
    """
    Satır linkine tıklar, modal açılınca başlık metnini döndürür.
    """
    # Modal açtır
    link_locator.scroll_into_view_if_needed()
    link_locator.click()
    # Modal köşesindeki kapat/çarpı ikonuna göre bekle
    page.wait_for_selector("div[role='dialog'], .modal, .MuiDialog-root, .ant-modal", timeout=10000)

    # Başlık: modal içindeki ilk <h> veya güçlü başlık alanı
    headline = ""
    for sel in [
        "div[role='dialog'] h1, .modal h1, .MuiDialog-root h1, .ant-modal h1",
        "div[role='dialog'] h2, .modal h2, .MuiDialog-root h2, .ant-modal h2",
        "div[role='dialog'] .title, .modal .title, .MuiDialog-root .MuiTypography-root",
    ]:
        loc = page.locator(sel)
        if loc.count():
            headline = loc.first.inner_text().strip()
            break
    if not headline:
        # fallback: modal içindeki ilk satır
        modal = page.locator("div[role='dialog'], .modal, .MuiDialog-root, .ant-modal").first
        if modal.count():
            headline = modal.inner_text().split("\n")[0].strip()

    # modalı kapat (sağ üst çarpı)
    try:
        close_btn = page.locator("button:has-text('Kapat'), [aria-label='Close'], .ant-modal-close, .MuiDialog-root button[aria-label='close']")
        if close_btn.count():
            close_btn.first.click()
        else:
            page.keyboard.press("Escape")
    except Exception:
        pass

    return headline

# ================== ANA AKIŞ ==================================
def main():
    print(">> start")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
        )
        page = ctx.new_page()
        page.set_default_timeout(30000)

        # 1) KAP satırlarını çek
        rows = get_kap_rows(page)
        print(f">> kap rows: {len(rows)}")

        # 2) yeni olanları filtrele
        new_rows = [r for r in rows if r["id"] not in posted]
        print(f">> new: {len(new_rows)} (posted: {len(posted)})")

        # eskiden yeniye
        new_rows.reverse()

        # 3) her satır için modal başlığını al → özgünleştir → tweet
        for r in new_rows:
            try:
                headline = open_row_and_read_headline(page, r["link"])
            except Exception as e:
                print("!! modal open/read error:", e)
                continue

            if not headline:
                continue

            tweet = build_tweet(r["code"], headline)
            print(">> TWEET:", tweet)

            try:
                client.create_tweet(text=tweet)
                posted.add(r["id"]); save_state()
                print(">> tweet sent ✓")
                time.sleep(1.0)
            except Exception as e:
                print("!! tweet error:", e)

        browser.close()
        print(">> done")

if __name__ == "__main__":
    main()
