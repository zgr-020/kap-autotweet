import os, re, json, time, datetime
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
        log("!! Twitter secrets missing, tweeting disabled")
        return None
    return tweepy.Client(
        consumer_key=API_KEY,
        consumer_secret=API_KEY_SECRET,
        access_token=ACCESS_TOKEN,
        access_token_secret=ACCESS_TOKEN_SECRET,
    )

# ============== Logging ==============
def log(msg: str):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {msg}")

# ============== State ==============
STATE_PATH = Path("state.json")

def load_state():
    if not STATE_PATH.exists():
        return {"last_id": None, "posted": []}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {"last_id": None, "posted": data}
        data.setdefault("last_id", None)
        data.setdefault("posted", [])
        return data
    except Exception as e:
        log(f"!! state.json bozuk: {e}, sıfırlanıyor")
        return {"last_id": None, "posted": []}

def save_state(state):
    try:
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log(f"!! state.json kaydedilemedi: {e}")

state = load_state()
posted = set(state.get("posted", []))
last_id = state.get("last_id")

# ============== Constants ==============
AKIS_URL = "https://fintables.com/borsa-haber-akisi"
MAX_PER_RUN = 5

# Güçlü ticker regex + banned list
TICKER_RE = re.compile(r"\b([A-ZÇĞİÖŞÜ]{2,5})(?=[0-9]?\b|$)")
BANNED_WORDS = {
    "BURADA", "KVKK", "FINTABLES", "BÜLTEN", "GÜNLÜK", "BULTEN", "KAP", "FİNTABLES",
    "POLİTİKASI", "YASAL", "UYARI", "BİLGİLENDİRME", "GUNLUK", "HABER", "BULTEN"
}
NON_NEWS_PATTERNS = [
    r"Günlük Bülten", r"Bülten", r"Piyasa temkini", r"Piyasa değerlendirmesi",
    r"yatırım bilgi", r"yasal uyarı", r"kişisel veri", r"kvk"
]
STOP_PHRASES = [
    r"işbu açıklama.*?amaçla", r"yatırım tavsiyesi değildir", r"kamunun bilgisine arz olunur",
    r"saygılarımızla", r"özel durum açıklaması", r"yatırımcılarımızın bilgisine",
    r"yasal uyarı", r"kişisel verilerin korunması", r"kvk"
]
REL_PREFIX = re.compile(r'^(?:dün|bugün|yesterday|today)\b[:\-–]?\s*', re.IGNORECASE)

# ============== JS Extractor (GÜÇLENDİRİLMİŞ) ==============
JS_EXTRACTOR = """
() => {
    const rows = Array.from(document.querySelectorAll('main li, main div[role="listitem"], main div'))
        .slice(0, 300);
    const banned = new Set(['BURADA','KVKK','FINTABLES','BÜLTEN','GÜNLÜK','BULTEN','KAP','FİNTABLES','POLİTİKASI','YASAL','UYARI','BİLGİLENDİRME','GUNLUK','HABER','BULTEN']);
    const nonNewsRe = /(Günlük Bülten|Bülten|Piyasa temkini|Piyasa değerlendirmesi|yatırım bilgi|yasal uyarı|kişisel veri|kvk)/i;

    return rows.map(row => {
        const text = row.innerText || '';
        const norm = text.replace(/\\s+/g, ' ').trim();
        if (nonNewsRe.test(norm) || /Fintables/i.test(norm)) return null;

        const words = norm.split(/\\s+/);
        let code = '';

        for (let i = 0; i < words.length; i++) {
            let w = words[i].replace(/[.:,]$/, '');
            let up = w.toUpperCase();
            if (banned.has(up)) continue;
            if (up.length < 2 || up.length > 6) continue;
            if (!/^[A-ZÇĞİÖŞÜ]+[0-9]?$/.test(up)) continue;

            const next = i + 1 < words.length ? words[i + 1].toUpperCase() : '';
            const prev = i > 0 ? words[i - 1].toUpperCase() : '';

            if (prev === 'KAP' ||
                ['PAY', 'HİSSE', 'ADET', 'TL', '%', 'FİYAT', 'TL', 'YÜZDE'].includes(next) ||
                norm.includes(`KAP - ${up}`) ||
                norm.includes(`KAP • ${up}`) ||
                norm.includes(`KAP · ${up}`)) {
                code = up;
                break;
            }
        }

        if (!code) return null;
        const pos = norm.toUpperCase().indexOf(code);
        let snippet = norm.slice(pos + code.length).trim();
        if (snippet.length < 20) return null;
        if (/yatırım bilgi|yasal uyarı|kişisel veri|kvk|politikası/i.test(snippet)) return null;

        const hash = norm.split('').reduce((a, c) => (a * 31 + c.charCodeAt(0)) & 0xFFFFFFFF, 0);
        const id = `${code}-${hash}`;
        return { id, code, snippet, raw: norm };
    }).filter(Boolean);
}
"""

# ============== Helpers ==============
def clean_text(t: str) -> str:
    t = re.sub(r"\s+", " ", t).strip()
    for p in STOP_PHRASES:
        t = re.sub(p, "", t, flags=re.I | re.DOTALL)
    t = re.sub(r"\b(Fintables|KAP)\b\s*[·\.\•]?\s*", "", t, flags=re.I)
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
    base = base[:235] if len(base) > 235 else base  # #KOD | için yer
    tweet = f"#{code} | {base}"
    return tweet[:280]

def is_valid_ticker(code: str, text: str) -> bool:
    if code in BANNED_WORDS:
        return False
    if not (2 <= len(code) <= 6):
        return False
    if not re.match(r"^[A-ZÇĞİÖŞÜ]+[0-9]?$", code):
        return False
    forbidden = ["YATIRIM BİLGİ", "TAVSİYE", "YASAL UYARI", "KİŞİSEL VERİ", "POLİTİKASI", "KVK"]
    if any(phrase in text.upper() for phrase in forbidden):
        return False
    return True

def goto_with_retry(page, url, retries=3):
    for i in range(retries):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_selector("main", timeout=15000)
            return True
        except Exception as e:
            log(f">> goto retry {i+1}/{retries}: {e}")
            time.sleep(5)
    return False

# ============== MAIN ==============
def main():
    log(">> start (fast mode)")
    tw = twitter_client()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            locale="tr-TR",
            timezone_id="Europe/Istanbul"
        )
        page = ctx.new_page()
        page.set_default_timeout(30000)

        # 1. Güvenli yükleme
        if not goto_with_retry(page, AKIS_URL):
            log("!! Sayfa yüklenemedi, çıkılıyor")
            browser.close()
            return

        # 2. Öne çıkanlar (güvenli)
        try:
            if page.get_by_text("Öne çıkanlar", exact=True).is_visible(timeout=3000):
                page.click("text=Öne çıkanlar")
                page.wait_for_load_state("domcontentloaded", timeout=10000)
                log(">> highlights ON")
        except:
            log(">> highlights not available")

        # 3. JS ile veri çek
        raw_items = page.evaluate(JS_EXTRACTOR)
        log(f">> extracted {len(raw_items)} items in JS")

        if not raw_items:
            log(">> no items")
            browser.close()
            return

        newest_id = raw_items[0]["id"]
        to_tweet = []
        for it in raw_items:
            if last_id and it["id"] == last_id:
                break
            to_tweet.append(it)
        to_tweet = to_tweet[:MAX_PER_RUN]

        if not to_tweet:
            state["last_id"] = newest_id
            save_state(state)
            log(">> no new items")
            browser.close()
            return

        to_tweet.reverse()
        sent = 0
        for it in to_tweet:
            if it["id"] in posted:
                continue

            # Ekstra güvenlik: Python tarafında da kontrol
            if not is_valid_ticker(it["code"], it["snippet"]):
                log(f">> SKIP: #{it['code']} (geçersiz haber)")
                continue

            tweet = build_tweet(it["code"], it["snippet"])
            log(f">> TWEET: {tweet}")

            try:
                if tw:
                    tw.create_tweet(text=tweet)
                posted.add(it["id"])
                sent += 1
                state["posted"] = sorted(list(posted))
                state["last_id"] = newest_id
                save_state(state)
                log(">> sent")

                if sent >= 4:
                    time.sleep(3)
            except Exception as e:
                if "429" in str(e) or "Too Many Requests" in str(e):
                    log(">> rate limit, waiting 65s...")
                    time.sleep(65)
                    try:
                        if tw: tw.create_tweet(text=tweet)
                        posted.add(it["id"])
                        state["posted"] = sorted(list(posted))
                        state["last_id"] = newest_id
                        save_state(state)
                        sent += 1
                        log(">> sent (retry)")
                    except:
                        log("!! retry failed")
                else:
                    log(f"!! error: {e}")

        state["last_id"] = newest_id
        save_state(state)
        browser.close()
        log(f">> done (sent: {sent})")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        log("!! FATAL ERROR !!")
        log(tb)
        with open("debug.log", "a", encoding="utf-8") as f:
            f.write(f"\n--- {datetime.datetime.now()} ---\n{tb}\n")
