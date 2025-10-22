import os, re, json, time
from pathlib import Path
from playwright.sync_api import sync_playwright
import tweepy

API_KEY = os.getenv("API_KEY")
API_KEY_SECRET = os.getenv("API_KEY_SECRET")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
ACCESS_TOKEN_SECRET = os.getenv("ACCESS_TOKEN_SECRET")

client = tweepy.Client(
    consumer_key=API_KEY,
    consumer_secret=API_KEY_SECRET,
    access_token=ACCESS_TOKEN,
    access_token_secret=ACCESS_TOKEN_SECRET
)

STATE_FILE = Path("state.json")
posted = set(json.loads(STATE_FILE.read_text())) if STATE_FILE.exists() else set()
def save_state(): STATE_FILE.write_text(json.dumps(sorted(list(posted)), ensure_ascii=False))

STOP_PHRASES = [
    r"işbu açıklama.*?amaçla", r"bu açıklama.*?kapsamında", r"kamunun bilgisine arz olunur",
    r"saygılarımızla", r"yatırımcılarımızın bilgisine", r"özel durum açıklaması"
]
def clean_text(t: str) -> str:
    t = re.sub(r"\s+", " ", t).strip()
    for p in STOP_PHRASES: t = re.sub(p, "", t, flags=re.I)
    return t.strip(" -–—:.")

def summarize(text: str, limit: int = 220) -> str:
    text = clean_text(text)
    sents = re.split(r"(?<=[.!?])\s+", text)
    out = ""
    for s in sents:
        if not s: continue
        cand = (out + " " + s).strip()
        if len(cand) > limit: break
        out = cand
        if len(out) >= limit * 0.7: break
    if not out: out = text[:limit]
    return out

def format_tweet(code: str, title: str, body: str) -> str:
    full = clean_text(body) or clean_text(title)
    is_pnl = bool(re.search(r"\b(kâr|kar|zarar|net dönem kar)\b", (title + " " + full).lower()))
    emoji = "💰" if is_pnl else "📰"
    text = summarize(full, limit=279 - (len(code) + 6))
    return f"{emoji} #{code} | {text}"[:279]

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("https://www.kap.org.tr/tr/bildirim-sorgu", wait_until="load")
        page.wait_for_timeout(2500)

        rows = page.locator("table tbody tr")
        for i in range(rows.count()):
            tds = rows.nth(i).locator("td")
            if tds.count() < 5: continue
            code = tds.nth(1).inner_text().strip()
            title = tds.nth(4).inner_text().strip()
            link_el = rows.nth(i).locator("a", has_text=re.compile("Detay|İncele", re.I))
            href = link_el.first.get_attribute("href") if link_el.count() > 0 else ""
            if not (code and title and href): continue
            m = re.search(r"(\d{6,})", href)
            kap_id = m.group(1) if m else href
            if kap_id in posted: continue

            page.goto(href, wait_until="load")
            page.wait_for_timeout(1000)
            detail = ""
            p_tags = page.locator("p")
            if p_tags.count() > 0:
                detail = " ".join(p_tags.nth(i).inner_text() for i in range(min(3, p_tags.count())))
            tweet = format_tweet(code, title, detail)
            client.create_tweet(text=tweet)
            print("Tweet gönderildi:", tweet)
            posted.add(kap_id)
            save_state()
            time.sleep(2)

        browser.close()

if __name__ == "__main__":
    main()
