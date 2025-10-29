import json, os, re, hashlib, time
from pathlib import Path
from playwright.sync_api import sync_playwright
import tweepy

STATE_PATH = Path("state.json")
MAX_TWEET_LEN = 279
BASE = "https://fintables.com/borsa-haber-akisi?tab=featured"

API_KEY = os.environ["API_KEY"]
API_SECRET = os.environ["API_KEY_SECRET"]
ACCESS_TOKEN = os.environ["ACCESS_TOKEN"]
ACCESS_SECRET = os.environ["ACCESS_TOKEN_SECRET"]

# ---------- State ----------
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

# ---------- Utils ----------
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

def extract_code_from_text(txt: str) -> str | None:
    # Öncelik: "KAP - KOD"
    m = re.search(r"KAP\s*[-–]\s*([A-Z]{3,5})\b", txt, re.I)
    if m:
        return m.group(1).upper()
    # Yedek: mavi kod genelde linktir, ama metinden de 3-5 harfli ilk adayı al
    m2 = re.search(r"\b([A-Z]{3,5})\b", txt)
    return m2.group(1).upper() if m2 else None

def extract_detail_from_text(txt: str) -> str:
    t = strip_time_parts(txt)
    parts = re.split(r"KAP\s*[-–]\s*[A-Z]{3,5}\s*", t, flags=re.I, maxsplit=1)
    if len(parts) == 2:
        t = parts[1]
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"^[\-\|–—\s]+", "", t)
    return t

def dismiss_cookies(page):
    for s in ["button:has-text('Kabul')", "button:has-text('Kabul et')",
              "text=Kabul et", "button:has-text('Accept')", "button:has-text('Accept all')"]:
        try:
            el = page.locator(s).first
            if el and el.is_visible():
                el.click(timeout=800)
                page.wait_for_timeout(200)
                return True
        except Exception:
            pass
    return False

def lazy_scroll(page, steps=10):
    for _ in range(steps):
        page.mouse.wheel(0, 2200)
        page.wait_for_timeout(500)

# ---------- DOM parse ----------
def parse_dom(page):
    # "KAP -" içeren tüm elemanları yakala (en-dash varyantı dahil)
    nodes = page.locator("xpath=//*[contains(normalize-space(.),'KAP -') or contains(normalize-space(.),'KAP –')]").all()
    print(f"[log] dom nodes with 'KAP -/–': {len(nodes)}")
    items = []
    for n in nodes:
        try:
            txt = n.inner_text().strip()
        except Exception:
            continue
        # Gürültüyü ele
        if not txt or re.search(r"Fintables|G[üu]nl[üu]k B[üu]lten|Bültenler", txt, re.I):
            continue
        if not re.search(r"\bKAP\s*[-–]\s*", txt, re.I):
            continue
        code = extract_code_from_text(txt)
        if not code:
            continue
        detail = extract_detail_from_text(txt)
        if not detail:
            continue
        items.append({"code": code, "detail": detail})
    # Duplike temizle
    uniq, seen = [], set()
    for it in items:
        k = (it["code"], it["detail"])
        if k in seen: 
            continue
        seen.add(k)
        uniq.append(it)
    return uniq

# ---------- Network capture (Plan-B) ----------
def parse_from_json_payload(payload) -> list[dict]:
    out = []
    # payload bir dict/list olabilir; içindeki tüm string alanlarda “KAP -” ara
    def walk(x):
        if isinstance(x, dict):
            for v in x.values(): walk(v)
        elif isinstance(x, list):
            for v in x: walk(v)
        elif isinstance(x, str):
            s = x.strip()
            if re.search(r"\bKAP\s*[-–]\s*[A-Z]{3,5}\b", s):
                code = extract_code_from_text(s)
                detail = extract_detail_from_text(s)
                if code and detail and not re.search(r"Fintables|Bülten", s, re.I):
                    out.append({"code": code, "detail": detail})
    walk(payload)
    # uniq
    uniq, seen = [], set()
    for it in out:
        k = (it["code"], it["detail"])
        if k in seen: 
            continue
        seen.add(k)
        uniq.append(it)
    return uniq

def scrape_featured_kap_items():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context(
            locale="tr-TR",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/127.0.0.0 Safari/537.36"),
            viewport={"width": 1440, "height": 900},
        )
        # Stealth: webdriver bayrağını gizle
        ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = ctx.new_page()

        # Ağdan JSON yakalama
        captured = []
        def on_response(resp):
            ct = resp.headers.get("content-type", "")
            if "application/json" in ct:
                try:
                    data = resp.json()
                    found = parse_from_json_payload(data)
                    if found:
                        captured.extend(found)
                except Exception:
                    pass
        page.on("response", on_response)

        # git
        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_timeout(1800)
        dismiss_cookies(page)

        # network sakinleşsin
        page.wait_for_load_state("networkidle")
        # lazy-load için kaydır
        lazy_scroll(page, steps=12)
        page.wait_for_timeout(800)

        # 1) DOM’dan dene
        dom_items = parse_dom(page)
        print(f"[log] dom extracted: {len(dom_items)}")

        # 2) Plan-B: JSON’dan (varsa)
        print(f"[log] captured via json: {len(captured)}")

        browser.close()

        # birleşik sonuç
        all_items = dom_items + captured
        # en eskiden yeniye ve uniq
        uniq, seen = [], set()
        for it in all_items:
            k = (it["code"], it["detail"])
            if k in seen: 
                continue
            seen.add(k)
            uniq.append(it)
        return uniq[::-1]  # eskiden yeniye

# ---------- Twitter ----------
def post_to_twitter(text: str):
    auth = tweepy.OAuth1UserHandler(API_KEY, API_SECRET, ACCESS_TOKEN, ACCESS_SECRET)
    api = tweepy.API(auth)
    api.update_status(status=text)

# ---------- Main ----------
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
