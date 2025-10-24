import os, re, json, time
from pathlib import Path
from playwright.sync_api import sync_playwright
import tweepy

# ================== X (Twitter) anahtarları (SECRETS) ==================
API_KEY = os.getenv("API_KEY")
API_KEY_SECRET = os.getenv("API_KEY_SECRET")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
ACCESS_TOKEN_SECRET = os.getenv("ACCESS_TOKEN_SECRET")

def twitter_client():
    if not all([API_KEY, API_KEY_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET]):
        print("!! Twitter secrets missing; tweets will be skipped")
        return None
    return tweepy.Client(
        consumer_key=API_KEY,
        consumer_secret=API_KEY_SECRET,
        access_token=ACCESS_TOKEN,
        access_token_secret=ACCESS_TOKEN_SECRET,
    )

# ================== Durum (aynı şeyi iki kez atma) =====================
STATE_FILE = Path("state.json")
posted = set(json.loads(STATE_FILE.read_text())) if STATE_FILE.exists() else set()
def save_state():
    STATE_FILE.write_text(json.dumps(sorted(list(posted)), ensure_ascii=False))

# ================== Yardımcılar =======================================
AKIS_URL = "https://fintables.com/borsa-haber-akisi"

UPPER_TR = "A-ZÇĞİÖŞÜ"
# Hisse etiketi için geniş desen (3–6 harf; sonda sayi/uzanti gelebilir)
TICKER_RE = re.compile(rf"\b[{UPPER_TR}]{{3,6}}[0-9]?\b")

# Haber dışı kalıplar
NON_NEWS_PATTERNS = [
    r"\bGünlük Bülten\b",
    r"\bBülten\b",
    r"\bPiyasa temkini\b",
    r"\bPiyasa değerlendirmesi\b",
]

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
    if len(text) <= limit: return text
    parts = re.split(r"(?<=[.!?])\s+", text)
    out = ""
    for s in parts:
        if not s: continue
        cand = (out + " " + s).strip()
        if len(cand) > limit: break
        out = cand
    return out or text[:limit]

REWRITE_MAP = [
    (r"\bbildirdi\b", "duyurdu"),
    (r"\bbildirimi\b", "açıklaması"),
    (r"\bilgisine\b", "paylaştı"),
    (r"\bgerçekleştirdi\b", "tamamladı"),
    (r"\bbaşladı\b", "başlattı"),
    (r"\bdevam ediyor\b", "sürdürülüyor"),
]
def rewrite_tr_short(s: str) -> str:
    s = clean_text(s)
    s = re.sub(r"[“”\"']", "", s)
    s = re.sub(r"\(\s*\)", "", s)
    for pat, rep in REWRITE_MAP:
        s = re.sub(pat, rep, s, flags=re.I)
    s = re.sub(r"^\s*[-–—•·]\s*", "", s)
    return s.strip()

def is_pnl_news(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in ["kâr", "kar", "zarar", "net dönem", "temettü", "temettu"])

def build_tweet(code: str, snippet: str) -> str:
    base = rewrite_tr_short(snippet)
    base = summarize(base, 240)   # buffer
    head = ("💰" if is_pnl_news(base) else "📰") + f" #{code} | "
    return (head + base)[:279]

def go_highlights(page):
    for sel in [
        "button:has-text('Öne çıkanlar')",
        "[role='tab']:has-text('Öne çıkanlar')",
        "a:has-text('Öne çıkanlar')",
        "text=Öne çıkanlar",
    ]:
        loc = page.locator(sel)
        if loc.count():
            loc.first.click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(600)
            print(">> highlights ON")
            return True
    print(">> highlights button not found; staying on 'Tümü'")
    return False

def infinite_scroll_a_bit(page, steps=5, pause_ms=450):
    # Liste kısa ise birkaç ekran aşağı kaydırıp daha fazla satır yükletelim
    for _ in range(steps):
        page.mouse.wheel(0, 2200)
        page.wait_for_timeout(pause_ms)

def extract_company_rows(page):
    """
    Modal AÇMADAN, listede şirket etiketi (hisse kodu) olan satırları topla.
    Dönüş: [{'id', 'code', 'snippet'}]
    """
    # Akış alanındaki liste satırları (olası kapsayıcılar)
    rows = page.locator("main li, main div[role='listitem'], main div")
    total = min(600, rows.count())
    print(">> raw rows:", total)

    items, seen = [], set()
    for i in range(total):
        row = rows.nth(i)

        # Satırda görünen şirket etiketi: çoğu zaman <a> veya <span> içinde mavi chip
        code = ""
        anchors = row.locator("a, span, div")
        acount = min(20, anchors.count())
        for j in range(acount):
            tt = (anchors.nth(j).inner_text() or "").strip()
            m = TICKER_RE.fullmatch(tt) or TICKER_RE.search(tt)
            if m:
                code = m.group(0)
                break
        if not code:
            continue

        # Satırın tüm metni
        text = row.inner_text().strip()
        text_norm = re.sub(r"\s+", " ", text)

        # Haber dışı kalıpları ele
        if any(re.search(p, text_norm, flags=re.I) for p in NON_NEWS_PATTERNS):
            continue

        # Etiket ve “KAP · / Fintables ·” gibi önekleri kırp
        # “KOD”dan sonraki kısmı kısa özet olarak al
        pos = text_norm.find(code)
        snippet = text_norm[pos + len(code):].strip(" -–—•·:|")
        if len(snippet) < 15:  # çok kısa/boşsa kullanma
            continue

        rid = f"{code}-{hash(text_norm)}"
        if rid in seen or rid in posted:
            continue
        seen.add(rid)
        items.append({"id": rid, "code": code, "snippet": snippet})

    return items

# ================== ANA AKIŞ ==================================
def main():
    print(">> start")
    tw = twitter_client()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-gpu","--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
            locale="tr-TR", timezone_id="Europe/Istanbul"
        )
        page = ctx.new_page(); page.set_default_timeout(30000)

        page.goto(AKIS_URL, wait_until="networkidle")
        page.wait_for_timeout(700)
        go_highlights(page)
        infinite_scroll_a_bit(page, steps=5, pause_ms=400)

        items = extract_company_rows(page)
        print(f">> company-tag rows: {len(items)}")

        new_items = [it for it in items if it["id"] not in posted]
        print(f">> new: {len(new_items)} (posted: {len(posted)})")
        new_items.reverse()  # eskiden yeniye

        for it in new_items:
            tweet = build_tweet(it["code"], it["snippet"])
            print(">> TWEET:", tweet)
            try:
                if tw:
                    tw.create_tweet(text=tweet)
                posted.add(it["id"]); save_state()
                print(">> tweet sent ✓")
                time.sleep(1.0)
            except Exception as e:
                print("!! tweet error:", e)

        browser.close()
        print(">> done")

if __name__ == "__main__":
    main()
