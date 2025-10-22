import os, re, json, time
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
import tweepy

# --- X anahtarları ---
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

# --- gönderilmiş KAP id'leri ---
STATE_FILE = Path("state.json")
posted = set(json.loads(STATE_FILE.read_text())) if STATE_FILE.exists() else set()
def save_state(): STATE_FILE.write_text(json.dumps(sorted(list(posted)), ensure_ascii=False))

# --- metin yardımcıları ---
STOP_PHRASES = [
    r"işbu açıklama.*?amaçla", r"bu açıklama.*?kapsamında",
    r"kamunun bilgisine arz olunur", r"saygılarımızla",
    r"yatırımcılarımızın bilgisine", r"özel durum açıklaması"
]
def clean_text(t: str) -> str:
    t = re.sub(r"\s+", " ", t or "").strip()
    for p in STOP_PHRASES: t = re.sub(p, "", t, flags=re.I)
    return t.strip(" -–—:.")

def summarize(text: str, limit: int) -> str:
    text = clean_text(text)
    sents = re.split(r"(?<=[.!?])\s+", text)
    out = ""
    for s in sents:
        if not s: continue
        cand = (out + " " + s).strip()
        if len(cand) > limit: break
        out = cand
        if len(out) >= limit * 0.7: break
    return out or text[:limit]

def format_tweet(code: str, title: str, body: str) -> str:
    full = clean_text(body) or clean_text(title)
    is_pnl = bool(re.search(r"\b(kâr|kar|zarar|net dönem kar|dönem karı|zararı)\b",
                            (title + " " + full).lower()))
    emoji = "💰" if is_pnl else "📰"
    head = f"{emoji} #{code} | "
    text = summarize(full, limit=279 - len(head))
    return (head + text)[:279]

def main():
    print(">> start")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        try:
            page.goto("https://www.kap.org.tr/tr/bildirim-sorgu", wait_until="load")

            # KVKK/çerez bandı görünürse kabul et
            try:
                cookie_btn = page.locator("button:has-text('Kabul Et'), button:has-text('KABUL ET')")
                if cookie_btn.count() > 0:
                    cookie_btn.first.click()
                    print(">> cookie accepted")
            except Exception: pass

            # "Bildirmler" sekmesi açık değilse tıkla (metin İngilizce/tema değişirse sorun olmasın diye alternatif)
            try:
                page.locator("text=Bildirimler").first.click(timeout=2000)
            except Exception: pass

            page.wait_for_selector("table tbody tr", timeout=15000)
        except PWTimeout:
            print("!! tablo gelmedi (timeout)")
            browser.close(); return

        rows = page.locator("table tbody tr")
        n = rows.count()
        print(f">> rows found: {n}")

        items = []
        for i in range(n):
            row = rows.nth(i)
            tds = row.locator("td")
            if tds.count() < 5: continue
            code  = tds.nth(1).inner_text().strip()
            title = tds.nth(4).inner_text().strip()

            # ikonlu/isimli fark etmeksizin Bildirim linkini yakala
            link = ""
            link_candidate = row.locator('a[href*="/tr/Bildirim/"]')
            if link_candidate.count() > 0:
                link = link_candidate.first.get_attribute("href")
            if not link:
                # fallback: satırdaki ilk <a>
                a = row.locator("a")
                if a.count() > 0:
                    link = a.first.get_attribute("href")

            if not (code and title and link): 
                continue

            m = re.search(r"(\d{6,})", link)
            kap_id = m.group(1) if m else link
            items.append({"id": kap_id, "code": code, "title": title, "url": link})

        print(f">> parsed items: {len(items)}")

        # sadece yeni olanlar
        new_items = [it for it in items if it["id"] not in posted]
        print(f">> new items: {len(new_items)} (posted: {len(posted)})")

        # kronolojik sırayla gönder
        new_items.reverse()
        for it in new_items:
            page.goto(it["url"], wait_until="load")
            page.wait_for_timeout(1000)

            # özet bilgi -> yoksa ilk paragraflar
            detail = ""
            try:
                el = page.locator("xpath=//th[contains(.,'Özet Bilgi')]/following-sibling::td")
                if el.count() > 0:
                    detail = el.first.inner_text()
                if not detail:
                    p_tags = page.locator("article p, .content p, p")
                    if p_tags.count() > 0:
                        detail = " ".join(p_tags.nth(j).inner_text() for j in range(min(3, p_tags.count())))
            except Exception as e:
                print(".. detail parse error:", e)

            tweet = format_tweet(it["code"], it["title"], detail or it["title"])
            print(">> TWEET:", tweet)

            try:
                client.create_tweet(text=tweet)
                posted.add(it["id"]); save_state()
                print(">> tweet sent ✓")
                time.sleep(2)
            except Exception as e:
                print("!! tweet error:", e)

        browser.close()
        print(">> done")

if __name__ == "__main__":
    main()
