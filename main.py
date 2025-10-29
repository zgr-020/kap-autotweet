import os, re, json, time, hashlib
from pathlib import Path
from playwright.sync_api import sync_playwright
import tweepy

URL = "https://fintables.com/borsa-haber-akisi?tab=featured"
STATE_PATH = Path("state.json")
MAX_TWEET_LEN = 279

# --- X (Twitter) Secrets ---
API_KEY = os.environ["API_KEY"]
API_SECRET = os.environ["API_KEY_SECRET"]
ACCESS_TOKEN = os.environ["ACCESS_TOKEN"]
ACCESS_SECRET = os.environ["ACCESS_TOKEN_SECRET"]

# ---------------- State ----------------
def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"hashes": []}

def save_state(s):
    s["hashes"] = s["hashes"][-5000:]
    STATE_PATH.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")

def sha(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

# ---------------- Helpers ----------------
TIME_PATTS = [
    re.compile(r"\s*(?:Dün|Bugün|Az önce|Saat)?\s*\d{1,2}[:.]\d{2}\s*$", re.I),
    re.compile(r"\s*\d{1,2}\s+[A-Za-zÇĞİÖŞÜçğıöşü]+\s+\d{1,2}[:.]\d{2}\s*$", re.I),
]

def strip_time(s: str) -> str:
    s = s.strip()
    for p in TIME_PATTS:
        s = p.sub("", s)
    return s.strip()

def format_tweet(code: str, text: str) -> str:
    prefix = f"📰 #{code} | "
    body = re.sub(r"\s+", " ", text).strip()
    room = MAX_TWEET_LEN - len(prefix)
    if len(body) > room:
        body = body[:room-1].rstrip() + "…"
    return prefix + body

def dismiss_banners(page):
    for sel in ["button:has-text('Kabul et')", "button:has-text('Kabul')",
                "button:has-text('Accept')", "button:has-text('Accept all')",
                "text=Kabul et"]:
        try:
            el = page.locator(sel).first
            if el and el.is_visible():
                el.click(timeout=800)
                page.wait_for_timeout(200)
                break
        except Exception:
            pass

# ---------------- Scrape ----------------
def scrape():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            locale="tr-TR",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/127.0 Safari/537.36"),
            viewport={"width": 1440, "height": 900},
        )
        # stealth
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page = ctx.new_page()

        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        dismiss_banners(page)

        # Lazy içerik için biraz kaydır
        for _ in range(6):
            page.mouse.wheel(0, 2200)
            page.wait_for_timeout(300)

        # KAP satırlarını bekle (li altında hisse linki olan satırlar)
        row_locator = page.locator("xpath=//li[.//a[contains(@href,'/borsa/hisse/')]]")
        try:
            row_locator.first.wait_for(timeout=8000)
        except Exception:
            pass

        rows = row_locator.all()
        items = []
        for r in rows:
            try:
                # Sol etiket metni: "KAP - TERA BVSAN" gibi
                row_text = re.sub(r"\s+", " ", r.inner_text()).strip()
            except Exception:
                continue

            # Yalnızca KAP olanlar; Fintables/Günlük Bülten hariç
            if not row_text.upper().startswith("KAP -"):
                continue
            if re.search(r"Fintables|G[üu]nl[üu]k B[üu]lten|Bültenler", row_text, re.I):
                continue

            # Kodu mavi linkten al
            try:
                code = r.locator("a[href*='/borsa/hisse/']").first.inner_text().strip().upper()
            except Exception:
                # yedek: “KAP - KOD …”
                m = re.search(r"KAP\s*-\s*([A-Z]{3,5})\b", row_text, re.I)
                code = m.group(1).upper() if m else None
            if not code:
                continue

            # Detay: “KAP - KOD” sonrası tüm cümle
            # bazen “KAP - TERA BVSAN …” olabilir → KOD’dan sonrasını al
            detail = row_text
            # önce KAP - KOD kırp
            detail = re.split(rf"KAP\s*-\s*{re.escape(code)}\s*", detail, flags=re.I, maxsplit=1)
            detail = detail[1] if len(detail) == 2 else row_text
            detail = strip_time(detail)
            detail = re.sub(r"^[\-\|–—\s]+", "", detail).strip()
            if not detail:
                continue

            items.append({"code": code, "detail": detail})

        browser.close()

        # Aynı tetiklemede uniq + en eskiden yeniye
        uniq, seen = [], set()
        for it in reversed(items):
            k = (it["code"], it["detail"])
            if k in seen: 
                continue
            seen.add(k)
            uniq.append(it)
        return uniq

# ---------------- Twitter ----------------
def post_to_twitter(text: str):
    auth = tweepy.OAuth1UserHandler(API_KEY, API_SECRET, ACCESS_TOKEN, ACCESS_SECRET)
    api = tweepy.API(auth)
    api.update_status(status=text)

# ---------------- Main ----------------
def main():
    state = load_state()
    items = scrape()

    to_post = []
    for it in items:
        h = sha(f"{it['code']}|{it['detail']}")
        if h not in state["hashes"]:
            to_post.append((h, it))

    posted = 0
    for h, it in to_post:
        tweet = format_tweet(it["code"], it["detail"])
        if len(tweet) < 10:
            continue
        post_to_twitter(tweet)
        state["hashes"].append(h)
        posted += 1
        time.sleep(2)

    save_state(state)
    print(f"Scanned {len(items)}, posted {posted}")

if __name__ == "__main__":
    main()
