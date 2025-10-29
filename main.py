import os, re, json, time, hashlib
from pathlib import Path
from playwright.sync_api import sync_playwright
import tweepy

# ================== X (Twitter) SECRETS ==================
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

# ================== STATE ==================
STATE_FILE = Path("state.json")
posted = set(json.loads(STATE_FILE.read_text())) if STATE_FILE.exists() else set()
def save_state():
    keep = sorted(list(posted))[-5000:]
    STATE_FILE.write_text(json.dumps(keep, ensure_ascii=False))

# ================== HELPERS ==================
AKIS_URL = "https://fintables.com/borsa-haber-akisi"

UPPER_TR = "A-ZÇĞİÖŞÜ"
# 3–6 TR büyük harf + opsiyonel 1 rakam
TICKER_RE = re.compile(rf"^[{UPPER_TR}]{{3,6}}[0-9]?$")

BANNED_TAGS = {"KAP","FINTABLES","FİNTABLES","GÜNLÜK","BÜLTEN","BULTEN","GUNLUK","HABER"}
NON_NEWS_PATTERNS = [
    r"\bGünlük Bülten\b", r"\bBülten\b", r"\bPiyasa temkini\b", r"\bPiyasa değerlendirmesi\b"
]
STOP_PHRASES = [
    r"işbu açıklama.*?amaçla", r"yatırım tavsiyesi değildir",
    r"kamunun bilgisine arz olunur", r"saygılarımızla",
    r"özel durum açıklaması", r"yatırımcılarımızın bilgisine",
]
TIME_PATTERNS = [
    r"\b\d{1,2}:\d{2}\b", r"\bDün\s+\d{1,2}:\d{2}\b", r"\bBugün\b", r"\bAz önce\b"
]

def clean_text(t: str) -> str:
    t = re.sub(r"\s+", " ", (t or "")).strip()
    for p in STOP_PHRASES: t = re.sub(p, "", t, flags=re.I)
    for p in TIME_PATTERNS: t = re.sub(p, "", t, flags=re.I)
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
    (r"\bbildirdi\b","duyurdu"),
    (r"\bbildirimi\b","açıklaması"),
    (r"\bilgisine\b","paylaştı"),
    (r"\bgerçekleştirdi\b","tamamladı"),
    (r"\bbaşladı\b","başlattı"),
    (r"\bdevam ediyor\b","sürdürülüyor"),
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
    out = f"📰 #{code} | {base}"
    return out[:279]

def infinite_scroll_a_bit(page, steps=3, pause_ms=400):
    for _ in range(steps):
        page.mouse.wheel(0, 1600)
        page.wait_for_timeout(pause_ms)

# >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
# GÜÇLENDİRİLMİŞ "Öne çıkanlar" tıklama
def go_highlights(page):
    # olası tüm güvenli seçiciler + xpath
    selectors = [
        "button:has-text('Öne çıkanlar')",
        "role=button[name='Öne çıkanlar']",
        "text='Öne çıkanlar'",
        "xpath=//button[contains(normalize-space(.),'Öne çıkanlar')]",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc and loc.count() > 0:
                loc.click(timeout=1500)
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(400)
                print(">> highlights ON")
                return True
        except Exception:
            continue
    print(">> highlights button not found; staying on 'Tümü'")
    return False
# <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<

def best_ticker_in_row(row) -> str:
    code = ""
    anchors = row.locator("a, span, div")
    for j in range(min(40, anchors.count())):
        tt = (anchors.nth(j).inner_text() or "").strip()
        tt_up = tt.upper()
        if tt_up in BANNED_TAGS:
            continue
        if TICKER_RE.fullmatch(tt_up):
            code = tt_up
            break
    return code

def extract_company_rows(page, max_collect=40):
    rows = page.locator("main li, main div[role='listitem'], main div")
    total = min(500, rows.count())
    print(">> raw rows:", total)

    items = []
    for i in range(total):
        if len(items) >= max_collect: break
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
        if not re.search(r"\bKAP\b", text_norm, flags=re.I):
            continue

        pos = text_norm.upper().find(code)
        snippet = text_norm[pos + len(code):].strip()
        snippet = clean_text(snippet)
        if len(snippet) < 15:
            continue

        rid = f"{code}-{hash(text_norm)}"
        items.append({"id": rid, "code": code, "snippet": snippet})

    return items  # yeni→eski

# ================== ANA AKIŞ ==================
def main():
    print(">> start")
    tw = twitter_client()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-gpu","--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
            locale="tr-TR", timezone_id="Europe/Istanbul"
        )
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page = ctx.new_page(); page.set_default_timeout(30000)

        page.goto(AKIS_URL, wait_until="networkidle")
        page.wait_for_timeout(600)
        go_highlights(page)  # <<< güçlendirilmiş tıklama
        infinite_scroll_a_bit(page, steps=2, pause_ms=350)

        items = extract_company_rows(page, max_collect=40)
        if not items:
            print(">> no eligible rows"); browser.close(); return

        new_items = [it for it in items if it["id"] not in posted]
        if not new_items:
            print(">> nothing new to post"); browser.close(); return

        # Eskiden→yeniye
        new_items.reverse()

        for it in new_items:
            tweet = build_tweet(it["code"], it["snippet"])
            print(">> TWEET:", tweet)
            if tw:
                try:
                    tw.create_tweet(text=tweet)
                    print(">> tweet sent ✓")
                except Exception as e:
                    print("!! tweet error:", e); continue
            posted.add(it["id"]); save_state()
            time.sleep(2)

        browser.close()
        print(">> done (posted:", len(new_items), ")")

if __name__ == "__main__":
    main()
