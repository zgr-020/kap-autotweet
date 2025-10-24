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

# ================== Durum (tekrarları önleme) ==========================
STATE_PATH = Path("state.json")

def load_state():
    """
    Eski sürümlerde state.json bir liste olabilirdi.
    Yeni format:
    {
      "last_id": "en_son_görülen_haber_idsi",
      "posted": ["id1","id2",...]
    }
    """
    if not STATE_PATH.exists():
        return {"last_id": None, "posted": []}
    try:
        data = json.loads(STATE_PATH.read_text())
        if isinstance(data, list):  # eski formatı dönüştür
            return {"last_id": None, "posted": data}
        if "last_id" not in data: data["last_id"] = None
        if "posted" not in data: data["posted"] = []
        return data
    except Exception:
        return {"last_id": None, "posted": []}

def save_state(state):
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2))

state = load_state()
posted = set(state.get("posted", []))
last_id = state.get("last_id")

# ================== Yardımcılar =======================================
AKIS_URL = "https://fintables.com/borsa-haber-akisi"

UPPER_TR = "A-ZÇĞİÖŞÜ"
TICKER_RE = re.compile(rf"^[{UPPER_TR}]{{3,6}}[0-9]?$")  # BIST kodu

# Kod OLAMAYACAK sabit etiketler (şirket kodu olmayan ifadeler)
BANNED_TAGS = {"KAP", "FINTABLES", "FİNTABLES", "GÜNLÜK", "BÜLTEN", "BULTEN", "GUNLUK", "HABER"}

# Haber dışı satırları ele
NON_NEWS_PATTERNS = [
    r"\bGünlük Bülten\b", r"\bBülten\b", r"\bPiyasa temkini\b", r"\bPiyasa değerlendirmesi\b"
]

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
    return (f"📰 #{code} | " + base)[:279]

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

def best_ticker_in_row(row) -> str:
    """Satırdaki etiketlerden gerçek hisse kodunu seç (KAP/Fintables vb. hariç)."""
    anchors = row.locator("a, span, div")
    for j in range(min(40, anchors.count())):
        tt = (anchors.nth(j).inner_text() or "").strip().upper()
        if tt in BANNED_TAGS:
            continue
        if TICKER_RE.fullmatch(tt):
            return tt
    return ""

def extract_company_rows_list(page):
    """
    Modal açmadan, listede şirket etiketi olan **bütün** satırları (en yeni → eski)
    döndürür. KAP içermeyen veya Fintables içeriği olanları eler.
    """
    rows = page.locator("main li, main div[role='listitem'], main div")
    total = min(400, rows.count())
    print(">> raw rows:", total)

    items = []
    for i in range(total):   # üstten aşağı = en yeni → eski
        row = rows.nth(i)

        code = best_ticker_in_row(row)
        if not code:
            continue

        text = row.inner_text().strip()
        text_norm = re.sub(r"\s+", " ", text)

        # Haber dışı ve Fintables ele
        if any(re.search(p, text_norm, flags=re.I) for p in NON_NEWS_PATTERNS):
            continue
        if re.search(r"\bFintables\b", text_norm, flags=re.I):
            continue

        # Sadece KAP içerikleri
        if not re.search(r"\bKAP\b", text_norm, flags=re.I):
            continue

        # koddan sonrası snippet
        pos = text_norm.upper().find(code)
        snippet = text_norm[pos + len(code):].strip()
        snippet = clean_text(snippet)
        if len(snippet) < 15:
            continue

        rid = f"{code}-{hash(text_norm)}"
        items.append({"id": rid, "code": code, "snippet": snippet})

    return items  # en yeni → eski

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

        items = extract_company_rows_list(page)  # en yeni → eski
        print(f">> eligible items: {len(items)}")
        if not items:
            browser.close(); print(">> done (no items)"); return

        global last_id, posted, state

        # 1) En yeni görülen id (liste başı)
        newest_seen_id = items[0]["id"]

        # 2) En son gördüğümüz habere kadar olan kısmı al (yeni gelenlerin tamamı)
        to_tweet = []
        for it in items:
            if last_id and it["id"] == last_id:
                break  # buradan sonrası önceki taramada görülmüştü
            to_tweet.append(it)

        if not to_tweet:
            print(">> no new items since last run")
            # yine de last_id'i güncelle (sayfa farklı sırada gelebilir)
            state["last_id"] = newest_seen_id
            save_state(state)
            browser.close(); print(">> done"); return

        # 3) Sırayı korumak için eski → yeni gönder
        to_tweet.reverse()

        sent = 0
        for it in to_tweet:
            if it["id"] in posted:
                print(">> already posted, skip and stop (safety)")
                break  # güvenlik: beklenmedik tekrar varsa dur
            tweet = build_tweet(it["code"], it["snippet"])
            print(">> TWEET:", tweet)
            try:
                if tw:
                    tw.create_tweet(text=tweet)
                posted.add(it["id"])
                sent += 1
                print(">> tweet sent ✓")
                time.sleep(1.0)
            except Exception as e:
                print("!! tweet error:", e)

        # 4) last_id'i bu çalışmada görülen **en yeni** habere ayarla
        state["posted"] = sorted(list(posted))
        state["last_id"] = newest_seen_id
        save_state(state)

        browser.close()
        print(f">> done (sent: {sent})")

if __name__ == "__main__":
    main()
