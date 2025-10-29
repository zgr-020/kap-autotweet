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
TICKER_RE = re.compile(rf"^[{UPPER_TR}]{{3,6}}[0-9]?$")

BANNED_TAGS = {"KAP","FINTABLES","FİNTABLES","GÜNLÜK","BÜLTEN","BULTEN","GUNLUK","HABER"}
NON_NEWS_PATTERNS = [r"\bGünlük Bülten\b", r"\bBülten\b"]

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

def build_tweet(code: str, snippet: str) -> str:
    base = summarize(snippet, 240)
    return (f"📰 #{code} | " + base)[:279]

def infinite_scroll(page, steps=3, pause_ms=350):
    for _ in range(steps):
        page.mouse.wheel(0, 1600)
        page.wait_for_timeout(pause_ms)

def go_highlights(page):
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
                page.wait_for_timeout(300)
                print(">> highlights ON")
                return True
        except Exception:
            pass
    print(">> highlights button not found; staying on 'Tümü'")
    return False

# --- yalnızca metin düğümlerini al (button/svg hariç) ---
def get_text_only(div_locator):
    return div_locator.evaluate("""
        el => Array.from(el.childNodes)
                  .filter(n => n.nodeType === Node.TEXT_NODE)
                  .map(n => n.textContent)
                  .join(' ')
    """)

# ================== SCRAPE (KAP etiketi + mavi kod) ==================
def extract_company_rows(page, max_collect=60):
    """
    Sadece KAP etiketli satırlar:
      - KAP etiketi:  div.text-utility-02.text-fg-03  (innerText == 'KAP')
      - Hisse kodu:   span.text-shared-brand-01        (örn. ONCSM, YBTAS)
      - Haber detayı: div.font-medium.text-body-sm     (yalın metin)
    Dönüş: yeni→eski (ekranda görünen sıra)
    """
    row_xpath = (
        "//li[.//div[contains(@class,'text-utility-02') and contains(@class,'text-fg-03') and normalize-space()='KAP']"
        "    and .//span[contains(@class,'text-shared-brand-01')]]"
        " | "
        "//div[.//div[contains(@class,'text-utility-02') and contains(@class,'text-fg-03') and normalize-space()='KAP']"
        "     and .//span[contains(@class,'text-shared-brand-01')]]"
    )
    rows = page.locator(f"xpath={row_xpath}")
    total = min(600, rows.count())
    print(">> raw rows (KAP-filtered):", total)

    items = []
    for i in range(total):
        if len(items) >= max_collect:
            break
        row = rows.nth(i)

        # 1) Kod
        try:
            code = row.locator("span.text-shared-brand-01").first.inner_text().strip().upper()
        except Exception:
            code = ""
        if not code or not TICKER_RE.fullmatch(code) or code in BANNED_TAGS:
            continue

        # 2) Detay
        detail_loc = row.locator("div.font-medium.text-body-sm").first
        if detail_loc.count() == 0:
            continue
        try:
            detail = get_text_only(detail_loc)
        except Exception:
            detail = detail_loc.inner_text()
        detail = clean_text(detail)
        if len(detail) < 10:
            continue

        # 3) Güvenlik: bülten vs ele
        if any(re.search(p, detail, re.I) for p in NON_NEWS_PATTERNS):
            continue

        rid = f"{code}-{hash(code + '|' + detail)}"
        items.append({"id": rid, "code": code, "snippet": detail})

    return items

# ================== MAIN ==================
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
        page.wait_for_timeout(500)
        go_highlights(page)
        infinite_scroll(page, steps=3, pause_ms=350)

        items = extract_company_rows(page, max_collect=60)
        if not items:
            print(">> no eligible rows"); browser.close(); return

        # state filtresi
        new_items = [it for it in items if it["id"] not in posted]
        if not new_items:
            print(">> nothing new to post"); browser.close(); return

        # eskiden → yeniye sırala
        new_items.reverse()

        sent = 0
        for it in new_items:
            text = build_tweet(it["code"], it["snippet"])
            print(">> TWEET:", text)
            if tw:
                try:
                    tw.create_tweet(text=text)
                    print(">> tweet sent ✓")
                except Exception as e:
                    print("!! tweet error:", e); continue
            posted.add(it["id"]); save_state()
            sent += 1
            time.sleep(2)

        browser.close()
        print(">> done (posted:", sent, ")")

if __name__ == "__main__":
    main()
