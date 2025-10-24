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

# BIST kodu: 3–6 Türkçe büyük harf + opsiyonel 1 rakam (örn. ISCTR, HEKTS, TUPRS, SISE, VESTL, KCHOL, KONTR, ALARK, BERA, etc.)
UPPER_TR = "A-ZÇĞİÖŞÜ"
TICKER_RE = re.compile(rf"^[{UPPER_TR}]{{3,6}}[0-9]?$")

# Kod OLAMAYACAK sabit etiketler
BANNED_TAGS = {
    "KAP", "FINTABLES", "FİNTABLES", "GÜNLÜK", "BÜLTEN", "BULTEN", "GUNLUK",
    "HABER"
}

# Haber dışı satırları ele
NON_NEWS_PATTERNS = [
    r"\bGünlük Bülten\b", r"\bBülten\b", r"\bPiyasa temkini\b", r"\bPiyasa değerlendirmesi\b"
]

# snippet temizliği
STOP_PHRASES = [
    r"işbu açıklama.*?amaçla", r"yatırım tavsiyesi değildir", r"kamunun bilgisine arz olunur",
    r"saygılarımızla", r"özel durum açıklaması", r"yatırımcılarımızın bilgisine",
]
TIME_PATTERNS = [
    r"\b\d{1,2}:\d{2}\b",            # 10:45
    r"\bDün\s+\d{1,2}:\d{2}\b",      # Dün 20:17
    r"\bBugün\b", r"\bAz önce\b"
]

def clean_text(t: str) -> str:
    t = re.sub(r"\s+", " ", (t or "")).strip()
    for p in STOP_PHRASES: t = re.sub(p, "", t, flags=re.I)
    for p in TIME_PATTERNS: t = re.sub(p, "", t, flags=re.I)
    # kaynak kırpıntıları
    t = re.sub(r"\b(Fintables|KAP)\b\s*[·\.]?\s*", "", t, flags=re.I)
    return t.strip(" -–—:|•·")

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
    for pat, rep in REWRITE_MAP: s = re.sub(pat, rep, s, flags=re.I)
    s = re.sub(r"^\s*[-–—•·]\s*", "", s)
    return s.strip()

def build_tweet(code: str, snippet: str) -> str:
    base = rewrite_tr_short(snippet)
    base = summarize(base, 240)   # buffer
    return (f"#{code} | " + base)[:279]

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

def infinite_scroll_a_bit(page, steps=3, pause_ms=400):
    # Yeterince satır gelsin diye az kaydırıyoruz (çok kaydırırsan eskileri de getirir)
    for _ in range(steps):
        page.mouse.wheel(0, 1600)
        page.wait_for_timeout(pause_ms)

def best_ticker_in_row(row) -> str:
    """Satırdaki etiketlerden gerçek hisse kodunu seç (KAP vb. hariç)."""
    code = ""
    anchors = row.locator("a, span, div")
    for j in range(min(30, anchors.count())):
        tt = (anchors.nth(j).inner_text() or "").strip()
        tt_up = tt.upper()
        if tt_up in BANNED_TAGS:    # KAP / Fintables / Bülten vs. değil
            continue
        # yalnızca düz kodu al (örn. ALARK, TUPRS, ISCTR, SISE gibi)
        if TICKER_RE.fullmatch(tt_up):
            code = tt_up
            break
    return code

def extract_company_rows(page):
    """
    Modal açmadan, listede şirket etiketi (hisse kodu) olan satırlardan
    EN YENİ (ilk görünen) haberi döndür.
    """
    rows = page.locator("main li, main div[role='listitem'], main div")
    total = min(300, rows.count())
    print(">> raw rows:", total)

    for i in range(total):  # üstten aşağı — ilk uygun satır yeter
        row = rows.nth(i)

        code = best_ticker_in_row(row)
        if not code:
            continue

        text = row.inner_text().strip()
        text_norm = re.sub(r"\s+", " ", text)

        if any(re.search(p, text_norm, flags=re.I) for p in NON_NEWS_PATTERNS):
            continue

        # koddan sonrası snippet
        pos = text_norm.upper().find(code)
        snippet = text_norm[pos + len(code):].strip()
        snippet = clean_text(snippet)

        if len(snippet) < 15:
            continue

        rid = f"{code}-{hash(text_norm)}"
        return {"id": rid, "code": code, "snippet": snippet}  # sadece ilk uygun haber

    return None

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
        page.wait_for_timeout(600)
        go_highlights(page)
        infinite_scroll_a_bit(page, steps=2, pause_ms=350)

        item = extract_company_rows(page)
        if not item:
            print(">> no eligible row"); browser.close(); return

        if item["id"] in posted:
            print(">> newest is already posted"); browser.close(); return

        tweet = build_tweet(item["code"], item["snippet"])
        print(">> TWEET:", tweet)

        try:
            if tw:
                tw.create_tweet(text=tweet)
            posted.add(item["id"]); save_state()
            print(">> tweet sent ✓")
        except Exception as e:
            print("!! tweet error:", e)

        browser.close()
        print(">> done")

if __name__ == "__main__":
    main()
