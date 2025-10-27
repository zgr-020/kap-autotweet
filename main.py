# main.py — Fintables "Öne çıkanlar" -> X otomatik tweet (sağlamlaştırılmış)
import os, re, json, time, random
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

# ============== Ayarlar ==============
AKIS_URL = "https://fintables.com/borsa-haber-akisi"
MAX_PER_RUN = 5
SLEEP_BETWEEN_TWEETS = 15  # saniye
COOLDOWN_SECONDS = 10 * 60  # 429 rate-limitte bekleme
PAGE_RELOAD_RETRIES = 3

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

# Göreli tarih öneklerini temizle (Dün/Bugün/Yesterday/Today)
REL_PREFIX = re.compile(r'^(?:dün|bugün|yesterday|today)\b[:\-–]?\s*', re.IGNORECASE)
def strip_relative_prefix(text: str) -> str:
    return REL_PREFIX.sub('', text).lstrip('-–: ').strip()

def clean_text(t: str) -> str:
    t = re.sub(r"\s+", " ", (t or "")).strip()
    for p in STOP_PHRASES: t = re.sub(p, "", t, flags=re.I)
    for p in TIME_PATTERNS: t = re.sub(p, "", t, flags=re.I)
    t = re.sub(r"\b(Fintables|KAP)\b\s*[·\.]?\s*", "", t, flags=re.I)
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
    base = strip_relative_prefix(base)
    return (f"📰 #{code} | " + base)[:279]

# --- küçük yardımcılar: görünen mi? güvenli iç metin al ---
def is_vis(loc):
    try:
        box = loc.bounding_box()
        return box is not None and box["width"] > 0 and box["height"] > 0
    except Exception:
        return True  # en kötü ihtimal görünür varsay

def safe_text(loc, timeout=800):
    try:
        return (loc.inner_text(timeout=timeout) or "").strip()
    except Exception:
        return ""

def go_highlights(page):
    # çerez banner’ı/kaplamaları kapat
    for sel in ["button:has-text('Kabul')", "button:has-text('Kabul et')", "button:has-text('Anladım')"]:
        try:
            btn = page.locator(sel)
            if btn.count():
                btn.first.click(timeout=1000)
                page.wait_for_timeout(200)
        except Exception:
            pass

    # sekmeyi 'Öne çıkanlar' yap
    for sel in [
        "button:has-text('Öne çıkanlar')",
        "[role='tab']:has-text('Öne çıkanlar')",
        "a:has-text('Öne çıkanlar')",
        "text=Öne çıkanlar",
    ]:
        try:
            loc = page.locator(sel)
            if loc.count():
                loc.first.click(timeout=1500)
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(800)
                print(">> highlights ON")
                break
        except Exception:
            continue

    # miniscroll ile içerik tetikle
    try:
        page.mouse.wheel(0, 400)
        page.wait_for_timeout(300)
        page.mouse.wheel(0, -400)
        page.wait_for_timeout(300)
    except Exception:
        pass

def extract_company_rows_list(page, max_scan=400):
    """Modal açmadan listede şirket etiketi olan satırları döndürür (en yeni → eski)."""
    try:
        page.wait_for_selector("main", state="attached", timeout=15000)
    except Exception:
        print(">> no <main> found; returning empty")
        return []

    selector_candidates = [
        "main li",
        "main [role='listitem']",
        "main article",
        "main div[role='row']",
        "main div"
    ]
    rows = None
    rows_sel = None
    for sel in selector_candidates:
        try:
            loc = page.locator(sel)
            cnt = loc.count()
            if cnt and cnt > 0:
                rows = loc
                rows_sel = sel
                break
        except Exception:
            continue

    if rows is None:
        print(">> no row selector matched; returning empty")
        return []

    try:
        total = rows.count()
    except Exception:
        total = 0
    total = min(max_scan, total)
    print(f">> rows selector: {rows_sel}  |  raw rows: {total}")

    items = []
    for i in range(total):
        row = rows.nth(i)
        if not is_vis(row):
            continue
        code = best_ticker_in_row(row)
        if not code:
            continue

        text = safe_text(row, timeout=700)
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
    return items

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

# ============== MAIN ==============
def main():
    print(">> entry", flush=True)
    tw = twitter_client()

    # Rastgele ama gerçekçi bir UA havuzu
    ua_pool = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
    ]
    ua = random.choice(ua_pool)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox","--disable-dev-shm-usage","--disable-gpu",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=site-per-process,IsolateOrigins",
            ],
        )
        ctx = browser.new_context(
            user_agent=ua,
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            viewport={"width": 1366, "height": 900},
            java_script_enabled=True,
            ignore_https_errors=True,
            extra_http_headers={
                "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        # webdriver izini azalt
        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = { runtime: {} };
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
              parameters.name === 'notifications' ?
              Promise.resolve({ state: Notification.permission }) :
              originalQuery(parameters)
            );
        """)

        page = ctx.new_page()
        page.set_default_timeout(30000)

        # Sayfayı güvenli şekilde 3 denemeye kadar yükle
        items = []
        for attempt in range(1, PAGE_RELOAD_RETRIES+1):
            try:
                print(f">> load attempt {attempt}")
                page.goto(AKIS_URL, wait_until="networkidle")
                page.wait_for_timeout(800)
                go_highlights(page)
                items = extract_company_rows_list(page)
                if items:
                    break
                # hiç item gelmediyse ufak bekle + bir kez daha miniscroll dene
                page.wait_for_timeout(800)
            except Exception as e:
                print(f"!! page load error (attempt {attempt}): {e}")
                continue

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

        # Run başına limit ve eski → yeni
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

                # Anında state yaz
                state["posted"] = sorted(list(posted))
                state["last_id"] = newest_seen_id
                save_state(state)

                time.sleep(SLEEP_BETWEEN_TWEETS)
            except Exception as e:
                print("!! tweet error:", e)
                # 429: tek retry, yine olmazsa cooldown
                if "429" in str(e) or "Too Many Requests" in str(e):
                    try:
                        print(">> hit rate limit; waiting 60s then retry once…")
                        time.sleep(60)
                        if tw:
                            tw.create_tweet(text=tweet)
                        posted.add(it["id"])
                        sent += 1
                        print(">> tweet sent (after retry) ✓")
                        state["posted"] = sorted(list(posted))
                        state["last_id"] = newest_seen_id
                        save_state(state)
                        time.sleep(SLEEP_BETWEEN_TWEETS)
                    except Exception as e2:
                        print("!! retry failed:", e2)
                        if "429" in str(e2) or "Too Many Requests" in str(e2):
                            until = time.strftime('%H:%M:%S', time.localtime(time.time()+COOLDOWN_SECONDS))
                            print(f">> enter cooldown until {until} (for {COOLDOWN_SECONDS//60} min)")
                        continue

        # Son görüleni kaydet
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