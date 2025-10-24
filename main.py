import os, re, json, time
from pathlib import Path
from playwright.sync_api import sync_playwright
import tweepy

# ============== X (Twitter) Secrets ==============
API_KEY = os.getenv("API_KEY")
API_KEY_SECRET = os.getenv("API_KEY_SECRET")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
ACCESS_TOKEN_SECRET = os.getenv("ACCESS_TOKEN_SECRET")

def twitter_client():
    if not all([API_KEY, API_KEY_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET]):
        print("!! Twitter secrets missing, tweeting disabled")
        return None
    return tweepy.Client(
        consumer_key=API_KEY,
        consumer_secret=API_KEY_SECRET,
        access_token=ACCESS_TOKEN,
        access_token_secret=ACCESS_TOKEN_SECRET,
    )

# ============== State (duplicate koruması) ==============
STATE_PATH = Path("state.json")

def load_state():
    if not STATE_PATH.exists():
        return {"last_id": None, "posted": []}
    try:
        data = json.loads(STATE_PATH.read_text())
        # eski biçim liste ise dönüştür
        if isinstance(data, list):
            return {"last_id": None, "posted": data}
        data.setdefault("last_id", None)
        data.setdefault("posted", [])
        return data
    except Exception:
        return {"last_id": None, "posted": []}

def save_state(state):
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2))

state = load_state()
posted = set(state.get("posted", []))
last_id = state.get("last_id")

# ============== Parsing helpers ==============
AKIS_URL = "https://fintables.com/borsa-haber-akisi"
MAX_PER_RUN = 5
SLEEP_BETWEEN_TWEETS = 15  # saniye

UPPER_TR = "A-ZÇĞİÖŞÜ"
TICKER_RE = re.compile(rf"^[{UPPER_TR}]{{3,6}}[0-9]?$")  # BIST kodu

# şirket kodu olamayacak etiketler
BANNED_TAGS = {"KAP", "FINTABLES", "FİNTABLES", "GÜNLÜK", "BÜLTEN", "BULTEN", "GUNLUK", "HABER"}

# bülten/günlük içerikleri ele
NON_NEWS_PATTERNS = [
    r"\bGünlük Bülten\b", r"\bBülten\b", r"\bPiyasa temkini\b", r"\bPiyasa değerlendirmesi\b"
]

STOP_PHRASES = [
    r"işbu açıklama.*?amaçla", r"yatırım tavsiyesi değildir", r"kamunun bilgisine arz olunur",
    r"saygılarımızla", r"özel durum açıklaması", r"yatırımcılarımızın bilgisine",
]
TIME_PATTERNS = [r"\b\d{1,2}:\d{2}\b", r"\bDün\s+\d{1,2}:\d{2}\b", r"\bBugün\b", r"\bAz önce\b"]

# -------- Göreli tarih öneklerini temizle (Dün/Bugün/Yesterday/Today) --------
REL_PREFIX = re.compile(r'^(?:dün|bugün|yesterday|today)\b[:\-–]?\s*', re.IGNORECASE)
def strip_relative_prefix(text: str) -> str:
    # "Dün:", "Bugün -", "Yesterday " vb. önekleri ve yan ayıracı temizle
    t = REL_PREFIX.sub('', text).lstrip('-–: ').strip()
    return t
# ---------------------------------------------------------------------------

def clean_text(t: str) -> str:
    t = re.sub(r"\s+", " ", (t or "")).strip()
    for p in STOP_PHRASES: t = re.sub(p, "", t, flags=re.I)
    for p in TIME_PATTERNS: t = re.sub(p, "", t, flags=re.I)
    t = re.sub(r"\b(Fintables|KAP)\b\s*[·\.]?\s*", "", t, flags=re.I)  # kaynak kırpıntısı
    return t.strip(" -–—:|•·")

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

def build_tweet(code: str, snippet: str) -> str:
    base = rewrite_tr_short(snippet)
    base = summarize(base, 240)
    base = strip_relative_prefix(base)   # 👈 göreli tarih öneklerini sil
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
    """Satırdaki etiketler içinden gerçek hisse kodunu bul (KAP/Fintables vb. hariç)."""
    anchors = row.locator("a, span, div")
    for j in range(min(40, anchors.count())):
        tt = (anchors.nth(j).inner_text() or "").strip().upper()
        if tt in BANNED_TAGS:
            continue
        if TICKER_RE.fullmatch(tt):
            return tt
    return ""

def extract_company_rows_list(page, max_scan=400):
    """Modal açmadan listede şirket etiketi olan satırları döndürür (en yeni → eski)."""
    rows = page.locator("main li, main div[role='listitem'], main div")
    total = min(max_scan, rows.count())
    print(">> raw rows:", total)

    items = []
    for i in range(total):  # en üstten aşağı = en yeni → eski
        row = rows.nth(i)
        code = best_ticker_in_row(row)
        if not code:
            continue

        text = row.inner_text().strip()
        text_norm = re.sub(r"\s+", " ", text)

        if any(re.search(p, text_norm, flags=re.I) for p in NON_NEWS_PATTERNS):
            continue
        if re.search(r"\bFintables\b", text_norm, flags=re.I):
            continue

        pos = text_norm.upper().find(code)
        snippet = text_norm[pos + len(code):].strip()
        snippet = clean_text(snippet)
        if len(snippet) < 15:
            continue

        rid = f"{code}-{hash(text_norm)}"
        items.append({"id": rid, "code": code, "snippet": snippet})

    print(">> eligible items:", len(items))
    return items  # en yeni → eski

# ============== MAIN ==============
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

        items = extract_company_rows_list(page)
        if not items:
            print(">> done (no items)")
            browser.close(); return

        # en yeni görülen
        newest_seen_id = items[0]["id"]

        # önceki run'dan bu yana gelenler (last_id görünene kadar)
        to_tweet = []
        for it in items:
            if last_id and it["id"] == last_id:
                break
            to_tweet.append(it)

        if not to_tweet:
            print(">> no new items since last run")
            state["last_id"] = newest_seen_id
            save_state(state)
            browser.close(); print(">> done"); return

        # Run başına üst limit ve eski → yeni sırası
        to_tweet = to_tweet[:MAX_PER_RUN]
        to_tweet.reverse()

        sent = 0
        for it in to_tweet:
            if it["id"] in posted:
                print(">> already posted, skip and continue")
                continue

            tweet = build_tweet(it["code"], it["snippet"])
            print(">> TWEET:", tweet)

            try:
                if tw:
                    tw.create_tweet(text=tweet)
                posted.add(it["id"])
                sent += 1
                print(">> tweet sent ✓")
                time.sleep(SLEEP_BETWEEN_TWEETS)  # rate-limit güvenlik
            except Exception as e:
                print("!! tweet error:", e)
                if "429" in str(e) or "Too Many Requests" in str(e):
                    try:
                        print(">> hit rate limit; waiting 60s then retry once…")
                        time.sleep(60)
                        if tw:
                            tw.create_tweet(text=tweet)
                        posted.add(it["id"])
                        sent += 1
                        print(">> tweet sent (after retry) ✓")
                        time.sleep(SLEEP_BETWEEN_TWEETS)
                    except Exception as e2:
                        print("!! retry failed:", e2)
                        continue

        # son görüleni kaydet
        state["posted"] = sorted(list(posted))
        state["last_id"] = newest_seen_id
        save_state(state)

        browser.close()
        print(f">> done (sent: {sent})")

if __name__ == "__main__":
    print(">> entry", flush=True)
    try:
        main()
        print(">> main() finished", flush=True)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print("!! UNCAUGHT ERROR !!")
        print(tb)
        # ayrıca dosyaya bırak (artifacts ile görebilirsin)
        try:
            with open("debug.log", "a", encoding="utf-8") as f:
                f.write(tb + "\n")
        except Exception:
            pass
        raise
