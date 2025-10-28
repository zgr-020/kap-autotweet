import os, re, json, time
from pathlib import Path
from playwright.sync_api import sync_playwright
import tweepy

# ============== X (Twitter) Secrets ==============
API_KEY = os.getenv("API_KEY")
API_KEY_SECRET = os.getenv("API_KEY_SECRET")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
ACCESS_TOKEN_SECRET = os.getenv("ACCESS_TOKEN_SECRET")

def twitter_client():
    if not all([API_KEY, API_KEY_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET]):
        print("!! Twitter secrets missing, tweeting disabled")
        return None
    return tweepy.Client(
        consumer_key=API_KEY,
        consumer_secret=API_KEY_SECRET,
        access_token=ACCESS_TOKEN,
        access_token_secret=ACCESS_TOKEN_SECRET,
    )

# ============== State ==============
STATE_PATH = Path("state.json")
def load_state():
    if not STATE_PATH.exists():
        return {"last_id": None, "posted": []}
    try:
        data = json.loads(STATE_PATH.read_text())
        if isinstance(data, list):
            return {"last_id": None, "posted": data}
        data.setdefault("last_id", None)
        data.setdefault("posted", [])
        return data
    except:
        return {"last_id": None, "posted": []}

def save_state(state):
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2))

state = load_state()
posted = set(state.get("posted", []))
last_id = state.get("last_id")

# ============== Constants ==============
AKIS_URL = "https://fintables.com/borsa-haber-akisi"
MAX_PER_RUN = 5
TICKER_RE = re.compile(r"^[A-ZÇĞİÖŞÜ]{3,6}[0-9]?$")
BANNED_TAGS = {"KAP", "FINTABLES", "FİNTABLES", "GÜNLÜK", "BÜLTEN", "BULTEN", "GUNLUK", "HABER"}
NON_NEWS_PATTERNS = [
    r"Günlük Bülten", r"Bülten", r"Piyasa temkini", r"Piyasa değerlendirmesi"
]
STOP_PHRASES = [
    r"işbu açıklama.*?amaçla", r"yatırım tavsiyesi değildir", r"kamunun bilgisine arz olunur",
    r"saygılarımızla", r"özel durum açıklaması", r"yatırımcılarımızın bilgisine",
]
REL_PREFIX = re.compile(r'^(?:dün|bugün|yesterday|today)\b[:\-–]?\s*', re.IGNORECASE)

# ============== JS Extractor (EN HIZLI YOL) ==============
JS_EXTRACTOR = """
() => {
    const rows = Array.from(document.querySelectorAll('main li, main div[role="listitem"], main div'))
        .slice(0, 300);  // max 300 satır

    const banned = new Set(['KAP', 'FINTABLES', 'FİNTABLES', 'GÜNLÜK', 'BÜLTEN', 'BULTEN', 'GUNLUK', 'HABER']);
    const tickerRe = /^[A-ZÇĞİÖŞÜ]{3,6}[0-9]?$/;
    const nonNewsRe = /(Günlük Bülten|Bülten|Piyasa temkini|Piyasa değerlendirmesi)/i;

    return rows.map(row => {
        const text = row.innerText || '';
        const norm = text.replace(/\\s+/g, ' ').trim();
        if (nonNewsRe.test(norm) || /Fintables/i.test(norm)) return null;

        const words = norm.split(/\\s+/);
        let code = '';
        for (const w of words) {
            const up = w.toUpperCase();
            if (banned.has(up)) continue;
            if (tickerRe.test(up)) { code = up; break; }
        }
        if (!code) return null;

        const pos = norm.toUpperCase().indexOf(code);
        let snippet = norm.slice(pos + code.length).trim();
        if (snippet.length < 15) return null;

        const id = `${code}-${norm.split('').reduce((a,b)=>a+(b.charCodeAt(0)<<5)+b.charCodeAt(0),0)}`;
        return { id, code, snippet, raw: norm };
    }).filter(Boolean);
}
"""

# ============== Helpers ==============
def clean_text(t: str) -> str:
    t = re.sub(r"\s+", " ", t).strip()
    for p in STOP_PHRASES: t = re.sub(p, "", t, flags=re.I)
    t = re.sub(r"\b(Fintables|KAP)\b\s*[·\.]?\s*", "", t, flags=re.I)
    return REL_PREFIX.sub('', t).strip(" -–—:|•·")

def rewrite_tr_short(s: str) -> str:
    s = clean_text(s)
    s = s.replace('bildirdi', 'duyurdu')\
         .replace('bildirimi', 'açıklaması')\
         .replace('bilgisine', 'paylaştı')\
         .replace('gerçekleştirdi', 'tamamladı')\
         .replace('başladı', 'başlattı')\
         .replace('devam ediyor', 'sürdürülüyor')
    return re.sub(r"^\s*[-–—•·]\s*", "", s).strip()

def build_tweet(code: str, snippet: str) -> str:
    base = rewrite_tr_short(snippet)
    base = base[:240] if len(base) > 240 else base
    return (f"#{code} | " + base)[:279]

# ============== MAIN (HIZLANDIRILMIŞ) ==============
def main():
    print(">> start (fast mode)")
    tw = twitter_client()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            locale="tr-TR", timezone_id="Europe/Istanbul"
        )
        page = ctx.new_page()
        page.set_default_timeout(30000)

        # 1. Hızlı yükleme
        page.goto(AKIS_URL, wait_until="domcontentloaded")
        page.wait_for_selector("main", timeout=15000)

        # 2. Öne çıkanlar (opsiyonel, hızlı)
        try:
            page.click("text=Öne çıkanlar", timeout=3000)
            page.wait_for_load_state("domcontentloaded")
            print(">> highlights ON")
        except:
            print(">> highlights not found, using default")

        # 3. JS ile tek seferde tüm veriyi çek
        raw_items = page.evaluate(JS_EXTRACTOR)
        print(f">> extracted {len(raw_items)} items in JS")

        if not raw_items:
            print(">> no items")
            browser.close(); return

        # En yeni ID
        newest_id = raw_items[0]["id"]

        # Yeni olanlar
        to_tweet = []
        for it in raw_items:
            if last_id and it["id"] == last_id:
                break
            to_tweet.append(it)
        to_tweet = to_tweet[:MAX_PER_RUN]

        if not to_tweet:
            state["last_id"] = newest_id
            save_state(state)
            print(">> no new items")
            browser.close(); return

        # Eski → Yeni
        to_tweet.reverse()

        sent = 0
        for it in to_tweet:
            if it["id"] in posted:
                continue

            tweet = build_tweet(it["code"], it["snippet"])
            print(">> TWEET:", tweet)

            try:
                if tw:
                    tw.create_tweet(text=tweet)
                posted.add(it["id"])
                sent += 1
                print(">> sent ✓")

                # Hemen kaydet
                state["posted"] = sorted(list(posted))
                state["last_id"] = newest_id
                save_state(state)

                # Rate limit'e göre dinamik bekleme
                if sent >= 4:  # 5. tweet'te biraz bekle
                    time.sleep(3)
            except Exception as e:
                if "429" in str(e) or "Too Many Requests" in str(e):
                    print(">> rate limit, waiting 65s...")
                    time.sleep(65)
                    try:
                        if tw: tw.create_tweet(text=tweet)
                        posted.add(it["id"])
                        state["posted"] = sorted(list(posted))
                        state["last_id"] = newest_id
                        save_state(state)
                        sent += 1
                    except:
                        pass
                else:
                    print("!! error:", e)

        # Son kaydet
        state["last_id"] = newest_id
        save_state(state)
        browser.close()
        print(f">> done (sent: {sent})")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print("!! ERROR !!")
        print(tb)
        with open("debug.log", "a", encoding="utf-8") as f:
            f.write(tb + "\n")
