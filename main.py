import json, os, re, hashlib, time
from pathlib import Path
from playwright.sync_api import sync_playwright
import tweepy

STATE_PATH = Path("state.json")
MAX_TWEET_LEN = 279
URL = "https://fintables.com/borsa-haber-akisi"

API_KEY = os.environ["API_KEY"]
API_SECRET = os.environ["API_KEY_SECRET"]
ACCESS_TOKEN = os.environ["ACCESS_TOKEN"]
ACCESS_SECRET = os.environ["ACCESS_TOKEN_SECRET"]

# ------------ state ------------
def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"hashes": []}

def save_state(state):
    state["hashes"] = state["hashes"][-5000:]
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def sha(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

# ------------ utils ------------
def format_tweet(code: str, text: str) -> str:
    prefix = f"📰 #{code} | "
    room = MAX_TWEET_LEN - len(prefix)
    body = re.sub(r"\s+", " ", text.strip())
    if len(body) > room:
        body = body[: max(0, room - 1)].rstrip() + "…"
    return prefix + body

TIME_END_1 = re.compile(r"\s*(?:Dün|Bugün|Az önce|Saat)?\s*\d{1,2}[:.]\d{2}\s*$", re.I)
TIME_END_2 = re.compile(r"\s*\d{1,2}\s+[A-Za-zÇĞİÖŞÜçğıöşü]+\s+\d{1,2}[:.]\d{2}\s*$", re.I)

def strip_time_parts(s: str) -> str:
    s = TIME_END_1.sub("", s)
    s = TIME_END_2.sub("", s)
    return s

def extract_code_from_row(row):
    # 1) mavi linkteki kod
    for a in row.query_selector_all("a[href*='/borsa/hisse/']"):
        t = a.inner_text().strip().upper()
        if re.fullmatch(r"[A-Z]{3,5}", t):
            return t
    # 2) "KAP - KOD" paterninden
    raw = row.inner_text().upper()
    m = re.search(r"KAP\s*-\s*([A-Z]{3,5})\b", raw)
    if m:
        return m.group(1)
    # 3) fallback: ilk 3–5 harfli kod benzeri
    m2 = re.search(r"\b([A-Z]{3,5})\b", raw)
    return m2.group(1) if m2 else None

def extract_detail_from_row(row_text: str) -> str:
    txt = strip_time_parts(row_text)
    parts = re.split(r"KAP\s*-\s*[A-Z]{3,5}\s*", txt, flags=re.I, maxsplit=1)
    if len(parts) == 2:
        txt = parts[1]
    txt = re.sub(r"\s+", " ", txt).strip()
    txt = re.sub(r"^[\-\|–—\s]+", "", txt)
    return txt

def dismiss_cookies(page):
    selectors = [
        "button:has-text('Kabul et')", "button:has-text('Kabul')",
        "text=Kabul et", "text=Kabul", "button:has-text('Accept')",
        "button:has-text('Accept all')"
    ]
    for s in selectors:
        try:
            el = page.locator(s).first
            if el and el.is_visible():
                el.click(timeout=800)
                page.wait_for_timeout(300)
                return True
        except Exception:
            pass
    return False

def click_featured(page):
    selectors = [
        "role=button[name=/Öne ?çıkanlar/i]",
        "text=Öne çıkanlar",
        "xpath=//*[contains(normalize-space(.), 'Öne çıkanlar')]"
    ]
    for s in selectors:
        try:
            page.locator(s).first.click(timeout=1200)
            page.wait_for_timeout(800)
            return True
        except Exception:
            continue
    return False

def lazy_scroll(page, steps=4):
    for i in range(steps):
        page.mouse.wheel(0, 2000)
        page.wait_for_timeout(600)

# ------------ scrape ------------
def scrape_featured_kap_items():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            locale="tr-TR",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127 Safari/537.36",
            viewport={"width": 1440, "height": 900},
        )
        page = ctx.new_page()

        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1800)
        dismiss_cookies(page)

        # başlık görünsün
        try:
            page.get_by_role("heading", name=re.compile("^Akış$", re.I)).wait_for(timeout=4000)
        except Exception:
            pass

        clicked = click_featured(page)
        print(f"[log] featured clicked: {clicked}")

        # içerik yüklensin
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1000)
        # "KAP -" görünene dek biraz bekle + kaydır
        for _ in range(3):
            if page.get_by_text(re.compile(r"\bKAP\s*-\s*", re.I)).count() > 0:
                break
            lazy_scroll(page, steps=2)

        # KAP satırları: li altında KAP metni olanlar
        kap_rows = page.locator("xpath=//li[.//*[contains(normalize-space(.),'KAP -')]]").all()
        if not kap_rows:
            # fallback: article/div
            kap_rows = page.locator("xpath=//article[.//*[contains(normalize-space(.),'KAP -')]] | //div[.//*[contains(normalize-space(.),'KAP -')]]").all()

        print(f"[log] candidate rows: {len(kap_rows)}")

        items = []
        for row in kap_rows:
            try:
                txt = row.inner_text().strip()
            except Exception:
                continue
            if not txt:
                continue
            if re.search(r"Fintables|G[üu]nl[üu]k B[üu]lten|Bültenler", txt, re.I):
                continue

            code = extract_code_from_row(row)
            if not code:
                continue

            detail = extract_detail_from_row(txt)
            if not detail:
                continue

            items.append({"code": code, "detail": detail})

        browser.close()

        # en eskiden yeniye, duplikeleri temizle
        uniq, seen = [], set()
        for it in reversed(items):
            k = (it["code"], it["detail"])
            if k in seen: 
                continue
            seen.add(k)
            uniq.append(it)
        print(f"[log] parsed items: {len(uniq)}")
        return uniq

# ------------ twitter ------------
def post_to_twitter(text: str):
    auth = tweepy.OAuth1UserHandler(API_KEY, API_SECRET, ACCESS_TOKEN, ACCESS_SECRET)
    api = tweepy.API(auth)
    api.update_status(status=text)

# ------------ main ------------
def main():
    state = load_state()
    items = scrape_featured_kap_items()

    new_items = []
    for it in items:
        h = sha(f"{it['code']}|{it['detail']}")
        if h not in state["hashes"]:
            new_items.append((h, it))

    posted = 0
    for h, it in new_items:
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
