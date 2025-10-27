# main.py  —  Fintables "Öne çıkanlar" -> X (Twitter) autopost
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
        if isinstance(data, list):  # çok eski sürümden kalma
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

# ============== Ayarlar ==============
AKIS_URL = "https://fintables.com/borsa-haber-akisi"
MAX_PER_RUN = 5
SLEEP_BETWEEN_TWEETS = 15  # saniye
COOLDOWN_SECONDS = 10 * 60  # 429 yersek bir sonraki denemeye kadar bekleme (workflow zaten 10dk)

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
    return REL_PREFIX.sub('', (text or "")).lstrip('-–: ').strip()
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
    for pat, rep in REWRITE_MAP:
        s = re.sub(pat, rep, s, flags=re.I)
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
    base = strip_relative_prefix(base)   # göreli tarih öneklerini sil
    return (f"📰 #{code} | " + base)[:279]

# --------- Playwright "safe" yardımcıları (timeout/kilitlenme önleme) --------
def safe_text(loc, timeout=300):
    """Element görünmüyorsa/detached ise boş döner, timeout'u kısa tutar."""
    try:
        t = loc.text_content(timeout=timeout)
        return (t or "").strip()
    except Exception:
        return ""

def is_vis(loc) -> bool:
    try:
        return loc.is_visible(timeout=0)
    except Exception:
        return False
# ---------------------------------------------------------------------------

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
    try:
        cnt = anchors.count()
    except Exception:
        cnt = 0
    cnt = min(40, max(0, cnt))
    for j in range(cnt):
        item = anchors.nth(j)
        if not is_vis(item):
            continue
        tt = safe_text(item)  # kilitlenmez
        tt = tt.upper()
        if not tt:
            continue
        if tt in BANNED_TAGS:
            continue
        if TICKER_RE.fullmatch(tt):
            return tt
    return ""

def extract_company_rows_list(page, max_scan=400):
    """Modal açmadan listede şirket etiketi olan satırları döndürür (en yeni → eski)."""
    rows_sel = "main li:visible, main [role='listitem']:visible"
    page.wait_for_selector("main", state="attached", timeout=15000)
    page.wait_for_selector(rows_sel, timeout=15000)

    rows = page.locator(rows_sel)
    try:
        total = rows.count()
    except Exception:
        total = 0
    total = min(max_scan, total)
    print(">> raw rows:", total)

    items = []
    for i in range(total):  # en üstten aşağı = en yeni → eski
        row = rows.nth(i)
        if not is_vis(row):
            continue

        code = best_ticker_in_row(row)
        if not code:
            continue

        text = safe_text(row, timeout=500)
        if not text:
            continue
        text_norm = re.sub(r"\s+", " ", text)

        if any(re.search(p, text_norm, flags=re.I) for p in NON_NEWS_PATTERNS):
            continue
        if re.search(r"\bFintables\b", text_norm, flags=re.I):
            continue

        pos = text_norm.upper().find(code)
        snippet = text_norm[pos + len(code):].strip() if pos >= 0 else text_norm
        snippet = clean_text(snippet)
        if len(snippet) < 15:
            continue

        rid = f"{code}-{hash(text_norm)}"
        items.append({"id": rid, "code": code, "snippet": snippet})

    print(">> eligible items:", len(items))
    return items  # en yeni → eski

# ============== MAIN ==============
def main():
    print(">> entry", flush=True)
    tw = twitter_client()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
            locale="tr-TR", timezone_id="Europe/Istanbul"
        )
        page = ctx.new_page()
        page.set_default_timeout(30000)

        page.goto(AKIS_URL, wait_until="networkidle")
        page.wait_for_timeout(600)
        go_highlights(page)

        items = extract_company_rows_list(page)
        if not items:
            print(">> done (no items)")
            browser.close()
            return

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
            browser.close()
            print(">> done")
            return

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

                # BAŞARILI GÖNDERİMDEN HEMEN SONRA STATE'İ KAYDET
                state["posted"] = sorted(list(posted))
                state["last_id"] = newest_seen_id
                save_state(state)

                time.sleep(SLEEP_BETWEEN_TWEETS)  # rate-limit güvenlik
            except Exception as e:
                print("!! tweet error:", e)
                # 429 veya benzeri rate limit: tek kez bekle/denemeyi bırak (cooldown)
                if "429" in str(e) or "Too Many Requests" in str(e):
                    until = time.strftime('%H:%M:%S', time.localtime(time.time()+COOLDOWN_SECONDS))
                    print(f">> enter cooldown until {until} (for {COOLDOWN_SECONDS//60} min)")
                    break  # döngüyü kır, bir sonraki workflow tetiklemesinde devam
                else:
                    # başka hata: bu item’i atla
                    continue

        # son görüleni kaydet
        state["posted"] = sorted(list(posted))
        state["last_id"] = newest_seen_id
        save_state(state)

        browser.close()
        print(f">> done (sent: {sent})")

if __name__ == "__main__":
    try:
        main()
        print(">> main() finished", flush=True)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print("!! UNCAUGHT ERROR !!")
        print(tb)
        try:
            with open("debug.log", "a", encoding="utf-8") as f:
                f.write(tb + "\n")
        except Exception:
            pass
        raise
