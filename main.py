# main.py — Fintables "Öne çıkanlar" -> X otomatik tweet (anchor-temelli çıkarım)
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
COOLDOWN_SECONDS = 10 * 60
PAGE_RELOAD_RETRIES = 5

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

def close_banners(page):
    for sel in ["button:has-text('Kabul')", "button:has-text('Anladım')", "button:has-text('Kapat')"]:
        try:
            btn = page.locator(sel)
            if btn.count():
                btn.first.click(timeout=1200)
                page.wait_for_timeout(200)
        except Exception:
            pass

def go_highlights(page):
    close_banners(page)
    for sel in [
        "button:has-text('Öne çıkanlar')",
        "[role='tab']:has-text('Öne çıkanlar')",
        "a:has-text('Öne çıkanlar')",
        "text=Öne çıkanlar",
    ]:
        try:
            loc = page.locator(sel)
            if loc.count():
                loc.first.click(timeout=1800)
                page.wait_for_timeout(600)
                print(">> highlights ON")
                break
        except Exception:
            continue
    try:
        page.mouse.wheel(0, 600); page.wait_for_timeout(250)
        page.mouse.wheel(0, -600); page.wait_for_timeout(250)
    except Exception:
        pass

def get_container_text(page, anchor):
    """Hisse linkinden yukarı en yakın satır kapsayıcısına çık ve metni al."""
    try:
        h = anchor.element_handle()
        if not h:
            return ""
        # Yakın kapsayıcı: article > li > div.card vs.
        container = h.evaluate_handle("""
            el => el.closest('article, li, [role=listitem], .card, .group, .feed-item, .flex, .grid, section') || el.parentElement
        """)
        if not container:
            return ""
        txt = container.evaluate("(n)=> (n.innerText || '').trim()")
        return txt or ""
    except Exception:
        # Anchor metnini son çare olarak dön
        try:
            return (anchor.inner_text() or "").strip()
        except Exception:
            return ""

def extract_company_rows_list(page, max_scan=120):
    """
    — Önce hisse etiket linklerini topla: a[href^='/hisse/'] (en güvenilir sinyal)
    — Her link için en yakın satır kapsayıcısının metnini al.
    — Filtrele ve (code, snippet) oluştur.
    """
    try:
        page.wait_for_selector("main", state="attached", timeout=20000)
    except Exception:
        print(">> no <main> found; returning empty")
        return []

    anchors = page.locator("a[href^='/hisse/']")
    try:
        cnt = anchors.count()
    except Exception:
        cnt = 0
    cnt = min(max_scan, max(0, cnt))
    print(f">> hisse anchors: {cnt}")

    seen_ids = set()
    items = []

    for i in range(cnt):
        a = anchors.nth(i)
        try:
            href = a.get_attribute("href") or ""
        except Exception:
            href = ""
        m = re.search(r"/hisse/([A-Za-zÇĞİÖŞÜçğıöşü0-9]{3,6})", href)
        if not m:
            continue
        code = m.group(1).upper()
        if not TICKER_RE.fullmatch(code):
            continue
        if code in BANNED_TAGS:
            continue

        text = get_container_text(page, a)
        if not text:
            continue

        # Filtreler
        text_norm = re.sub(r"\s+", " ", text)
        if any(re.search(p, text_norm, flags=re.I) for p in NON_NEWS_PATTERNS):
            continue
        # Eğer konteyner metninde "Fintables" geçse bile, bu kez sadece satır metnine bakıyoruz.
        # Çoğu zaman satırda başlık + kısa açıklama olacak.

        # Snippet'i koda göre kes (koddan sonrası)
        pos = text_norm.upper().find(code)
        snippet = text_norm[pos + len(code):].strip() if pos >= 0 else text_norm
        snippet = clean_text(snippet)
        if len(snippet) < 15:
            # çok kısaysa satır başlığını tamamını kullan
            snippet = clean_text(text_norm)
        if len(snippet) < 15:
            continue

        rid = f"{code}-{hash(text_norm)}"
        if rid in seen_ids:
            continue
        seen_ids.add(rid)
        items.append({"id": rid, "code": code, "snippet": snippet})

    print(">> eligible items:", len(items))
    return items

# ============== MAIN ==============
def main():
    print(">> entry", flush=True)
    tw = twitter_client()

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
        page.set_default_navigation_timeout(90000)

        items = []
        for attempt in range(1, PAGE_RELOAD_RETRIES+1):
            try:
                print(f">> load attempt {attempt}")
                page.goto(AKIS_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(800)

                go_highlights(page)

                # anchor tabanlı çıkarım
                items = extract_company_rows_list(page)
                if items:
                    break

                # hiç item yoksa bir miniscroll daha dene
                page.wait_for_timeout(1000)
                go_highlights(page)
            except Exception as e:
                print(f"!! page load error (attempt {attempt}): {e}")
                page.wait_for_timeout(1500)
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
