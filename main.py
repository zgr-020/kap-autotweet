import json, os, re, hashlib, time
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
import tweepy

STATE_PATH = Path("state.json")
MAX_TWEET_LEN = 279
URL = "https://fintables.com/borsa-haber-akisi"

# --- Twitter auth (secret adların) ---
API_KEY = os.environ["API_KEY"]
API_SECRET = os.environ["API_KEY_SECRET"]
ACCESS_TOKEN = os.environ["ACCESS_TOKEN"]
ACCESS_SECRET = os.environ["ACCESS_TOKEN_SECRET"]

# ------------ State ------------
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

# ------------ Tweet format ------------
def format_tweet(code: str, text: str) -> str:
    prefix = f"📰 #{code} | "
    room = MAX_TWEET_LEN - len(prefix)
    body = re.sub(r"\s+", " ", text.strip())
    if len(body) > room:
        body = body[: max(0, room - 1)].rstrip() + "…"
    return prefix + body

# ------------ Scrape helpers ------------
# Sadeltilmiş zaman/tarih temizleyici desenler
TIME_AT_END_RE = re.compile(r"\s*(?:Dün|Bugün|Az önce|Saat)?\s*\d{1,2}[:.]\d{2}\s*$", re.I)
DATE_TIME_AT_END_RE = re.compile(r"\s*\d{1,2}\s+[A-Za-zÇĞİÖŞÜçğıöşü]+\s+\d{1,2}[:.]\d{2}\s*$", re.I)

def is_kap_row_text(txt: str) -> bool:
    t = txt.strip()
    if not t:
        return False
    if re.search(r"Fintables|G[üu]nl[üu]k B[üu]lten|Bültenler", t, re.I):
        return False
    return bool(re.search(r"\bKAP\s*-\s*", t))

def extract_code_from_html(row):
    try:
        for a in row.query_selector_all("a[href*='/borsa/hisse/']"):
            t = a.inner_text().strip().upper()
            if re.fullmatch(r"[A-Z]{3,5}", t):
                return t
    except Exception:
        pass
    try:
        raw = row.inner_text().upper()
        m = re.search(r"KAP\s*-\s*([A-Z]{3,5})\b", raw)
        if m:
            return m.group(1)
        m2 = re.search(r"\b([A-Z]{3,5})\b", raw)
        if m2:
            return m2.group(1)
    except Exception:
        pass
    return None

def strip_time_parts(s: str) -> str:
    s = TIME_AT_END_RE.sub("", s)
    s = DATE_TIME_AT_END_RE.sub("", s)
    return s

def extract_detail_from_text(raw: str) -> str:
    s = strip_time_parts(raw)
    parts = re.split(r"KAP\s*-\s*[A-Z]{3,5}\s*", s, flags=re.I, maxsplit=1)
    if len(parts) == 2:
        s = parts[1]
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^[\-\|–—\s]+", "", s)
    return s

def click_featured(page):
    candidates = [
        "role=button[name=/Öne ?çıkanlar/i]",
        "text=Öne çıkanlar",
        "xpath=//*[contains(., 'Öne çıkanlar')]",
    ]
    for sel in candidates:
        try:
            page.locator(sel).first.click(timeout=1200)
            page.wait_for_timeout(1200)
            return True
        except Exception:
            continue
    return False

def scrape_featured_kap_items():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(locale="tr-TR", viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)

        click_featured(page)
        page.wait_for_timeout(800)

        try:
            rows = page.locator("li").all()
            if len(rows) < 5:
                rows = page.locator("article, div").all()
        except PWTimeout:
            rows = page.locator("article, div").all()

        items = []
        for row in rows:
            try:
                txt = row.inner_text().strip()
            except Exception:
                continue
            if not txt or not is_kap_row_text(txt):
                continue

            code = extract_code_from_html(row)
            if not code:
                continue

            detail = extract_detail_from_text(txt)
            if not detail or re.search(r"Fintables|G[üu]nl[üu]k B[üu]lten|Bültenler", detail, re.I):
                continue

            items.append({"code": code, "detail": detail})

        browser.close()

        uniq, seen = [], set()
        for it in reversed(items):  # en eskiden yeniye
            k = (it["code"], it["detail"])
            if k in seen:
                continue
            seen.add(k)
            uniq.append(it)
        return uniq

# ------------ Twitter ------------
def post_to_twitter(text: str):
    auth = tweepy.OAuth1UserHandler(API_KEY, API_SECRET, ACCESS_TOKEN, ACCESS_SECRET)
    api = tweepy.API(auth)
    api.update_status(status=text)

# ------------ Main ------------
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
