import os, re, json, time, random, datetime as dt
from pathlib import Path
from playwright.sync_api import sync_playwright
import tweepy

# ================== X (Twitter) Secrets ==================
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

# ================== Config / Limits ==================
AKIS_URL = "https://fintables.com/borsa-haber-akisi"

MAX_PER_RUN    = 4            # bir çalıştırmada en fazla kaç tweet
TWEET_SLEEP_LO = 35           # tweet arası min bekleme (sn)
TWEET_SLEEP_HI = 55           # tweet arası max bekleme (sn)
MAX_PER_DAY    = 120          # günlük üst limit (free plan / güvenli)

# 429 sonrası soğuma (dakika)
COOLDOWN_MIN_FIRST = 20
COOLDOWN_MIN_NEXT  = 60

# ================== State (duplicate & cooldown) ==================
STATE_PATH = Path("state.json")

def _default_state():
    return {
        "last_id": None,
        "posted": [],
        "cooldown_until": None,     # ISO timestamp
        "count_today": 0,
        "day": dt.date.today().isoformat(),
    }

def load_state():
    if not STATE_PATH.exists():
        return _default_state()
    try:
        data = json.loads(STATE_PATH.read_text())
        if isinstance(data, list):
            # ultra eski format
            s = _default_state()
            s["posted"] = data
            return s
        # alanları tamamla
        base = _default_state()
        base.update(data)
        # bugün reset
        if base.get("day") != dt.date.today().isoformat():
            base["day"] = dt.date.today().isoformat()
            base["count_today"] = 0
        return base
    except Exception:
        return _default_state()

def save_state(state):
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2))

state   = load_state()
posted  = set(state.get("posted", []))
last_id = state.get("last_id")

def now_utc():
    return dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)

def in_cooldown(state) -> bool:
    cu = state.get("cooldown_until")
    if not cu:
        return False
    try:
        until = dt.datetime.fromisoformat(cu)
        return now_utc() < until
    except Exception:
        return False

def set_cooldown(state, minutes: int):
    until = now_utc() + dt.timedelta(minutes=minutes)
    state["cooldown_until"] = until.isoformat()
    save_state(state)
    print(f">> enter cooldown until {until.isoformat()} (for {minutes} min)")

def clear_cooldown(state):
    if state.get("cooldown_until"):
        state["cooldown_until"] = None
        save_state(state)

# ================== Parsing helpers ==================
UPPER_TR = "A-ZÇĞİÖŞÜ"
TICKER_RE = re.compile(rf"^[{UPPER_TR}]{{3,6}}[0-9]?$")

BANNED_TAGS = {"KAP", "FINTABLES", "FİNTABLES", "GÜNLÜK", "BÜLTEN", "BULTEN", "GUNLUK", "HABER"}

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
    for p in STOP_PHRASES:  t = re.sub(p, "", t, flags=re.I)
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
    anchors = row.locator("a, span, div")
    for j in range(min(40, anchors.count())):
        tt = (anchors.nth(j).inner_text() or "").strip().upper()
        if tt in BANNED_TAGS: continue
        if TICKER_RE.fullmatch(tt): return tt
    return ""

def extract_company_rows_list(page, max_scan=400):
    rows = page.locator("main li, main div[role='listitem'], main div")
    total = min(max_scan, rows.count())
    print(">> raw rows:", total)

    items = []
    for i in range(total):
        row = rows.nth(i)
        code = best_ticker_in_row(row)
        if not code: continue

        text = row.inner_text().strip()
        text_norm = re.sub(r"\s+", " ", text)

        if any(re.search(p, text_norm, flags=re.I) for p in NON_NEWS_PATTERNS):
            continue
        if re.search(r"\bFintables\b", text_norm, flags=re.I):
            continue

        pos = text_norm.upper().find(code)
        snippet = text_norm[pos + len(code):].strip()
        snippet = clean_text(snippet)
        if len(snippet) < 15: continue

        rid = f"{code}-{hash(text_norm)}"
        items.append({"id": rid, "code": code, "snippet": snippet})

    print(">> eligible items:", len(items))
    return items

# ================== MAIN ==================
def main():
    print(">> entry", flush=True)

    # 1) Cooldown kontrol
    if in_cooldown(state):
        print(">> in cooldown window; skipping run")
        return

    # 2) Günlük limit
    if state.get("count_today", 0) >= MAX_PER_DAY:
        print(f">> daily cap reached ({state['count_today']}/{MAX_PER_DAY}); skipping")
        return

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
        page = ctx.new_page(); page.set_default_timeout(30000)
        page.goto(AKIS_URL, wait_until="networkidle")
        page.wait_for_timeout(600)
        go_highlights(page)

        items = extract_company_rows_list(page)
        if not items:
            print(">> done (no items)")
            browser.close(); return

        newest_seen_id = items[0]["id"]

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

        # sırayı eski → yeni ve limit
        to_tweet = to_tweet[:MAX_PER_RUN]
        to_tweet.reverse()

        sent = 0
        consecutive_429 = 0

        for it in to_tweet:
            if it["id"] in posted:
                print(">> already posted, skip")
                continue

            if state["count_today"] >= MAX_PER_DAY:
                print(">> daily cap reached during loop; stop")
                break

            tweet = build_tweet(it["code"], it["snippet"])
            print(">> TWEET:", tweet)

            try:
                if tw:
                    tw.create_tweet(text=tweet)
                posted.add(it["id"])
                state["count_today"] = state.get("count_today", 0) + 1
                sent += 1
                consecutive_429 = 0
                print(">> tweet sent ✓")

                # anında kalıcılaştır
                state["posted"] = sorted(list(posted))
                state["last_id"] = newest_seen_id
                clear_cooldown(state)
                save_state(state)

                # jitter
                time.sleep(random.randint(TWEET_SLEEP_LO, TWEET_SLEEP_HI))

            except Exception as e:
                msg = str(e)
                print("!! tweet error:", msg)

                if "429" in msg or "Too Many Requests" in msg:
                    consecutive_429 += 1
                    # ilk 429’da kısa, tekrarlarsa uzun cooldown
                    minutes = COOLDOWN_MIN_FIRST if consecutive_429 == 1 else COOLDOWN_MIN_NEXT
                    set_cooldown(state, minutes)
                    print(">> stop run due to 429; cooldown set")
                    break
                else:
                    # başka hata → bu tweet'i atla, devam et
                    continue

        # run bitimi
        state["posted"] = sorted(list(posted))
        state["last_id"] = newest_seen_id
        save_state(state)

        browser.close()
        print(f">> done (sent: {sent})")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print("!! UNCAUGHT ERROR !!\n", tb)
        try:
            with open("debug.log", "a", encoding="utf-8") as f:
                f.write(tb + "\n")
        except Exception:
            pass
        raise
