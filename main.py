import json, os, re, hashlib, time
from pathlib import Path
from playwright.sync_api import sync_playwright
import tweepy

STATE_PATH = Path("state.json")
MAX_TWEET_LEN = 279
URL = "https://fintables.com/borsa-haber-akisi?tab=featured"

# --- Twitter secrets ---
API_KEY = os.environ["API_KEY"]
API_SECRET = os.environ["API_KEY_SECRET"]
ACCESS_TOKEN = os.environ["ACCESS_TOKEN"]
ACCESS_SECRET = os.environ["ACCESS_TOKEN_SECRET"]

# --- Optional cookie (put full cookie string as secret) ---
FINTABLES_COOKIE = os.environ.get("FINTABLES_COOKIE", "").strip()

# --------------- state ---------------
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

# --------------- utils ---------------
TIME_END_1 = re.compile(r"\s*(?:Dün|Bugün|Az önce|Saat)?\s*\d{1,2}[:.]\d{2}\s*$", re.I)
TIME_END_2 = re.compile(r"\s*\d{1,2}\s+[A-Za-zÇĞİÖŞÜçğıöşü]+\s+\d{1,2}[:.]\d{2}\s*$", re.I)

def strip_time_parts(s: str) -> str:
    s = TIME_END_1.sub("", s)
    s = TIME_END_2.sub("", s)
    return s

def format_tweet(code: str, text: str) -> str:
    prefix = f"📰 #{code} | "
    room = MAX_TWEET_LEN - len(prefix)
    body = re.sub(r"\s+", " ", text.strip())
    if len(body) > room:
        body = body[: max(0, room - 1)].rstrip() + "…"
    return prefix + body

def extract_code_and_detail(line: str):
    """
    Bir metin satırından 'KAP - KOD ....' desenini alır.
    """
    if not re.search(r"\bKAP\s*[-–]\s*", line, re.I):
        return None
    # Fintables/Günlük Bültenleri ele
    if re.search(r"Fintables|G[üu]nl[üu]k B[üu]lten|Bültenler", line, re.I):
        return None
    # Kod
    m = re.search(r"KAP\s*[-–]\s*([A-Z]{3,5})\b", line, re.I)
    if not m:
        return None
    code = m.group(1).upper()
    # Detay
    txt = strip_time_parts(line)
    parts = re.split(r"KAP\s*[-–]\s*[A-Z]{3,5}\s*", txt, flags=re.I, maxsplit=1)
    detail = parts[1] if len(parts) == 2 else txt
    detail = re.sub(r"\s+", " ", detail).strip()
    detail = re.sub(r"^[\-\|–—\s]+", "", detail)
    if not detail:
        return None
    return {"code": code, "detail": detail}

# --------------- scrape ---------------
def scrape_featured_kap_items():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context(
            locale="tr-TR",
            viewport={"width": 1440, "height": 900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/127.0.0.0 Safari/537.36"),
        )
        # stealth
        ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        # optional cookie
        if FINTABLES_COOKIE:
            cookies = []
            for part in FINTABLES_COOKIE.split(";"):
                if "=" in part:
                    name, value = part.strip().split("=", 1)
                    cookies.append({"name": name.strip(), "value": value.strip(), "domain": ".fintables.com", "path": "/"})
            if cookies:
                ctx.add_cookies(cookies)

        page = ctx.new_page()

        # Ağ yakalama: hem JSON hem metin yanıtları tara
        captured = []
        def on_response(resp):
            try:
                ct = resp.headers.get("content-type", "").lower()
            except Exception:
                ct = ""
            try:
                if "application/json" in ct:
                    data = resp.json()
                    # JSON içindeki tüm stringleri tara
                    strings = []
                    def walk(x):
                        if isinstance(x, dict):
                            for v in x.values(): walk(v)
                        elif isinstance(x, list):
                            for v in x: walk(v)
                        elif isinstance(x, str):
                            strings.append(x)
                    walk(data)
                    for s in strings:
                        if "KAP" in s:
                            item = extract_code_and_detail(s)
                            if item:
                                captured.append(item)
                else:
                    # metin içerikler
                    text = resp.text() if ("text" in ct or ct == "") else ""
                    if "KAP" in text:
                        for line in text.splitlines():
                            item = extract_code_and_detail(line)
                            if item:
                                captured.append(item)
            except Exception:
                pass
        page.on("response", on_response)

        # sayfayı yükle
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        # biraz kaydır
        for _ in range(10):
            page.mouse.wheel(0, 2400)
            page.wait_for_timeout(300)

        # 1) body.innerText’ten tara
        all_text = page.evaluate("document.body && document.body.innerText || ''")
        dom_items = []
        if all_text:
            for ln in all_text.split("\n"):
                item = extract_code_and_detail(ln)
                if item:
                    dom_items.append(item)

        print(f"[log] dom_text items: {len(dom_items)}")
        print(f"[log] net_captured items: {len(captured)}")

        browser.close()

        # birleşik sonuç
        all_items = dom_items + captured
        # uniq + eskiden yeniye
        uniq, seen = [], set()
        for it in all_items:
            k = (it["code"], it["detail"])
            if k in seen: 
                continue
            seen.add(k)
            uniq.append(it)
        return uniq[::-1]

# --------------- twitter ---------------
def post_to_twitter(text: str):
    auth = tweepy.OAuth1UserHandler(API_KEY, API_SECRET, ACCESS_TOKEN, ACCESS_SECRET)
    api = tweepy.API(auth)
    api.update_status(status=text)

# --------------- main ---------------
def main():
    state = load_state()
    items = scrape_featured_kap_items()

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
