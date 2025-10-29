import json, os, re, hashlib, time
from pathlib import Path
from playwright.sync_api import sync_playwright
import tweepy

STATE_PATH = Path("state.json")
MAX_TWEET_LEN = 279
URL = "https://fintables.com/borsa-haber-akisi"

# --- Twitter auth (secret adlarınla) ---
API_KEY = os.environ["API_KEY"]
API_SECRET = os.environ["API_KEY_SECRET"]
ACCESS_TOKEN = os.environ["ACCESS_TOKEN"]
ACCESS_SECRET = os.environ["ACCESS_TOKEN_SECRET"]

def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"hashes": []}

def save_state(state):
    state["hashes"] = state["hashes"][-2000:]
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def sha(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

def format_tweet(code: str, text: str) -> str:
    prefix = f"📰 #{code} | "
    room = MAX_TWEET_LEN - len(prefix)
    body = re.sub(r"\s+", " ", text.strip())
    if len(body) > room:
        body = body[: max(0, room - 1)].rstrip() + "…"
    return prefix + body

def is_kap_item(text: str) -> bool:
    return text.strip().upper().startswith("KAP")

def extract_code_from_row(row):
    # Öncelik: hisse linki
    links = row.query_selector_all("a[href*='/borsa/hisse/']")
    for a in links:
        t = a.inner_text().strip().upper()
        if re.fullmatch(r"[A-Z]{3,5}", t):
            return t
    # Yedek: metinden 3-5 harfli kod
    raw = row.inner_text().upper()
    m = re.search(r"\b([A-Z]{3,5})\b", raw)
    return m.group(1) if m else None

def scrape_featured_kap_items():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(locale="tr-TR", user_agent="Mozilla/5.0")
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded")

        # “Öne çıkanlar” filtresi
        try:
            page.get_by_role("button", name=re.compile("Öne ?çıkanlar", re.I)).click()
            page.wait_for_timeout(1200)
        except Exception:
            pass

        # KAP satırları (Fintables/Günlük Bülten hariç)
        rows = page.locator("li, article, div").all()
        items = []
        for row in rows:
            try:
                text_all = row.inner_text().strip()
            except Exception:
                continue
            if not re.search(r"\bKAP\b", text_all, re.I):
                continue
            if re.search(r"Fintables|G[üu]nl[üu]k B[üu]lten", text_all, re.I):
                continue
            # tarih-saat kırp
            text_clean = re.sub(r"\s+(Dün|Bugün)?\s*\d{1,2}[:.]\d{2}\b.*$", "", text_all, flags=re.I)
            # "KAP - " sonrası metni al
            parts = re.split(r"KAP\s*-\s*", text_clean, flags=re.I)
            detail = parts[1].strip() if len(parts) >= 2 else text_clean

            code = extract_code_from_row(row)
            if not code:
                m = re.search(r"\b([A-Z]{3,5})\b", detail)
                code = m.group(1) if m else None
            if not code:
                continue

            # tekrar “KAP - XXX” kalıntılarını temizle
            detail = re.sub(r"^([A-ZÇĞİÖŞÜ\s\-\|]+)?", "", detail).strip()
            detail = re.sub(r"\s+", " ", detail)

            items.append({"code": code, "detail": detail})

        browser.close()
        # en eskiden yeniye
        uniq, seen = [], set()
        for it in reversed(items):
            k = (it["code"], it["detail"])
            if k in seen: 
                continue
            seen.add(k)
            uniq.append(it)
        return uniq

def post_to_twitter(text: str):
    auth = tweepy.OAuth1UserHandler(API_KEY, API_SECRET, ACCESS_TOKEN, ACCESS_SECRET)
    api = tweepy.API(auth)
    api.update_status(status=text)

def main():
    state = load_state()
    items = scrape_featured_kap_items()

    to_post = []
    for it in items:
        h = sha(it["code"] + " | " + it["detail"])
        if h not in state["hashes"]:
            to_post.append((h, it))

    for h, it in to_post:
        tweet = format_tweet(it["code"], it["detail"])
        if len(tweet) < 10:
            continue
        post_to_twitter(tweet)
        state["hashes"].append(h)
        time.sleep(2)

    save_state(state)
    print(f"Scanned {len(items)}, posted {len(to_post)}")

if __name__ == "__main__":
    main()
