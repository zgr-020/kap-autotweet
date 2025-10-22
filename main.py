import os, re, json, time
from pathlib import Path
from playwright.sync_api import sync_playwright
import tweepy

# ================== X (Twitter) anahtarları ==================
API_KEY = os.getenv("API_KEY")
API_KEY_SECRET = os.getenv("API_KEY_SECRET")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
ACCESS_TOKEN_SECRET = os.getenv("ACCESS_TOKEN_SECRET")

client = tweepy.Client(
    consumer_key=API_KEY,
    consumer_secret=API_KEY_SECRET,
    access_token=ACCESS_TOKEN,
    access_token_secret=ACCESS_TOKEN_SECRET,
)

# ================== Durum (aynı şeyi iki kez atma) ==========
STATE_FILE = Path("state.json")
posted = set(json.loads(STATE_FILE.read_text())) if STATE_FILE.exists() else set()
def save_state():
    STATE_FILE.write_text(json.dumps(sorted(list(posted)), ensure_ascii=False))

# ================== Yardımcılar ==============================
AKIS_URL = "https://fintables.com/borsa-haber-akisi"
UPPER_TR = "A-ZÇĞİÖŞÜ"
KAP_LINE_RE = re.compile(rf"^\s*KAP\s*·\s*([{UPPER_TR}0-9]{{3,6}})\b", re.M)  # tam 'KAP · KOD'

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
    if len(text) <= limit:
        return text
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
def rewrite_turkish_short(s: str) -> str:
    s = clean_text(s)
    s = re.sub(r"[“”\"']", "", s)
    s = re.sub(r"\(\s*\)", "", s)
    for pat, rep in REWRITE_MAP:
        s = re.sub(pat, rep, s, flags=re.I)
    s = re.sub(r"^\s*[-–—•·]\s*", "", s)
    return s.strip()

def is_pnl_news(text: str) -> bool:
    txt = text.lower()
    return any(k in txt for k in ["kâr", "kar", "zarar", "net dönem", "temettü", "temettu"])

def build_tweet(code: str, headline: str) -> str:
    base = rewrite_turkish_short(headline)
    base = summarize(base, 240)  # birkaç karakter buffer
    head = ("💰" if is_pnl_news(base) else "📰") + f" #{code} | "
    return (head + base)[:279]

# =============== Sayfada 'Öne çıkanlar' sekmesine geç =========
def go_highlights(page):
    """Akış sayfasında 'Öne çıkanlar' sekmesini açar."""
    for sel in [
        "button:has-text('Öne çıkanlar')",
        "[role='tab']:has-text('Öne çıkanlar')",
        "a:has-text('Öne çıkanlar')",
        "text=Öne çıkanlar",
    ]:
        try:
            loc = page.locator(sel)
            if loc.count():
                loc.first.click()
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(800)
                print(">> highlights ON")
                return True
        except Exception:
            continue
    print(">> highlights button not found; staying on 'Tümü'")
    return False

# =============== 'KAP · KOD' satırlarını çek ==================
def get_kap_rows(page):
    page.goto(AKIS_URL, wait_until="networkidle")
    page.wait_for_timeout(1200)
    go_highlights(page)

    containers = page.locator("li, div").filter(has_text="KAP")
    rows, seen = [], set()

    for i in range(min(250, containers.count())):
        row = containers.nth(i)
        text = row.inner_text().strip()

        m = KAP_LINE_RE.search(text)
        if not m:
            continue  # KAP değil → at

        code = m.group(1)

        link = row.locator("a").first
        if link.count() == 0:
            continue

        href = link.get_attribute("href") or f"row-{i}"
        mslug = re.search(r"([a-z0-9_-]{8,}|[0-9]{6,})", href, re.I)
        rid = (mslug.group(1) if mslug else href) + "_" + code

        if rid in seen:
            continue
        seen.add(rid)

        rows.append({"id": rid, "code": code, "link": link})

    return rows

# =============== Modal başlığını oku ==========================
def open_row_and_read_headline(page, link_locator):
    link_locator.scroll_into_view_if_needed(timeout=10000)
    link_locator.click()
    page.wait_for_selector("div[role='dialog'], .modal, .MuiDialog-root, .ant-modal", timeout=10000)

    headline = ""
    for sel in [
        "div[role='dialog'] h1",
        ".modal h1",
        ".MuiDialog-root h1",
        ".ant-modal h1",
        "div[role='dialog'] h2",
        ".modal h2",
        ".MuiDialog-root .MuiTypography-root.MuiTypography-h6",
        ".ant-modal .ant-modal-title",
    ]:
        loc = page.locator(sel)
        if loc.count():
            headline = loc.first.inner_text().strip()
            if len(headline) > 5:
                break

    if not headline:
        try:
            modal = page.locator("div[role='dialog'], .modal, .MuiDialog-root, .ant-modal").first
            ps = modal.locator("p")
            if ps.count():
                headline = " ".join(ps.nth(i).inner_text().strip() for i in range(min(2, ps.count())))
                headline = headline.strip()
        except Exception:
            pass

    # modalı kapat
    try:
        close_btn = page.locator("button:has-text('Kapat'), [aria-label='Close'], .ant-modal-close, .MuiDialog-root button[aria-label='close']")
        if close_btn.count():
            close_btn.first.click()
        else:
            page.keyboard.press("Escape")
    except Exception:
        pass

    return headline

# ================== ANA AKIŞ ==================================
def main():
    print(">> start")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
        )
        page = ctx.new_page()
        page.set_default_timeout(30000)

        rows = get_kap_rows(page)
        print(f">> kap rows: {len(rows)}")

        new_rows = [r for r in rows if r["id"] not in posted]
        print(f">> new: {len(new_rows)} (posted: {len(posted)})")
        new_rows.reverse()

        for r in new_rows:
            try:
                headline = open_row_and_read_headline(page, r["link"])
            except Exception as e:
                print("!! modal open/read error:", e)
                continue

            if not headline:
                print("!! headline not found, skipping")
                continue  # başlık yoksa tweet atma

            tweet = build_tweet(r["code"], headline)
            print(">> TWEET:", tweet)

            try:
                client.create_tweet(text=tweet)
                posted.add(r["id"]); save_state()
                print(">> tweet sent ✓")
                time.sleep(1.0)
            except Exception as e:
                print("!! tweet error:", e)

        browser.close()
        print(">> done")

if __name__ == "__main__":
    main()
