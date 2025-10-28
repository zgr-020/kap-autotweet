import os, re, json, time, datetime
from pathlib import Path
from playwright.sync_api import sync_playwright
import tweepy
from datetime import datetime as dt, timezone, timedelta

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

# ============== Logging (SADECE KONSOLA) ==============
def log(msg: str):
    timestamp = dt.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {msg}")

# ============== State ==============
STATE_PATH = Path("state.json")

def load_state():
    if not STATE_PATH.exists():
        return {"last_id": None, "posted": [], "cooldown_until": None, "count_today": 0, "day": None}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {"last_id": None, "posted": data, "cooldown_until": None, "count_today": 0, "day": None}
        data.setdefault("last_id", None)
        data.setdefault("posted", [])
        data.setdefault("cooldown_until", None)
        data.setdefault("count_today", 0)
        data.setdefault("day", None)
        return data
    except Exception as e:
        log(f"!! state.json bozuk: {e}, sıfırlanıyor")
        return {"last_id": None, "posted": [], "cooldown_until": None, "count_today": 0, "day": None}

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
MAX_TODAY = 10

STOP_PHRASES = [
    r"işbu açıklama.*?amaçla", r"yatırım tavsiyesi değildir", r"kamunun bilgisine arz olunur",
    r"saygılarımızla", r"özel durum açıklaması", r"yatırımcılarımızın bilgisine",
    r"yasal uyarı", r"kişisel verilerin korunması", r"kvk"
]
REL_PREFIX = re.compile(r'^(?:dün|bugün|yesterday|today)\b[:\-–]?\s*', re.IGNORECASE)

# ============== JS Extractor (SADECE KAP - XXXX) ==============
JS_EXTRACTOR = r"""
() => {
  try {
    const rows = Array.from(document.querySelectorAll('main li, main div[role="listitem"], main div')).slice(0, 500);
    if (!rows.length) return [];

    const banned = new Set(['ADET','TEK','MİLYON','TL','YÜZDE','PAY','HİSSE','ŞİRKET','BİST','KAP','FİNTABLES','BÜLTEN','GÜNLÜK','BURADA','KVKK','POLİTİKASI','YASAL','UYARI','BİLGİLENDİRME','GUNLUK','HABER']);
    const nonNewsRe = /(Günlük Bülten|Bülten|Piyasa temkini|yatırım bilgi|yasal uyarı|kişisel veri|kvk)/i;
    const codeRe = /([A-ZÇĞİÖŞÜ]{3,6}[0-9]?)/g;

    function pickAllCodes(afterKapText) {
      const set = new Set();
      if (!afterKapText) return [];
      const up = afterKapText.toUpperCase();
      for (const m of up.matchAll(codeRe)) {
        const tok = (m[1] || '').trim();
        if (!tok) continue;
        if (banned.has(tok)) continue;
        if (/^[A-ZÇĞİÖŞÜ]{3,6}[0-9]?$/.test(tok)) set.add(tok);
        if (set.size >= 4) break; // güvenlik
      }
      return Array.from(set);
    }

    return rows.map(row => {
      const text = row.innerText || '';
      if (!text.trim()) return null;
      const norm = text.replace(/\s+/g, ' ').trim();
      if (nonNewsRe.test(norm) || /Fintables/i.test(norm)) return null;

      const up = norm.toUpperCase();
      const kapIdx = up.indexOf('KAP');
      if (kapIdx === -1) return null;

      const afterKap = norm.slice(kapIdx + 3).replace(/^[\s•·\-\.:|]+/, '');
      const codes = pickAllCodes(afterKap);
      if (!codes.length) return null;

      // açıklama: ilk kodun geçtiği yerden sonrası
      const pos = up.indexOf(codes[0]);
      let snippet = norm.slice(pos + codes[0].length).trim();
      if (snippet.length < 30) return null;
      if (/yatırım bilgi|yasal uyarı|kişisel veri|kvk|politikası/i.test(snippet)) return null;

      // stabil id
      let h = 0; for (let i = 0; i < norm.length; i++) h = (h * 31 + norm.charCodeAt(i)) >>> 0;
      const id = `${codes[0]}-${h}`;

      return { id, code: codes[0], codes, snippet, raw: norm };
    }).filter(Boolean);
  } catch (e) {
    console.error('JS Extractor Error:', e);
    return [];
  }
}
"""

# ============== Helpers ==============
def clean_text(t: str) -> str:
    t = re.sub(r"\s+", " ", t).strip()
    for p in STOP_PHRASES:
        t = re.sub(p, "", t, flags=re.I | re.DOTALL)
    t = re.sub(r"\b(Fintables|KAP)\b\s*[·\.\•]?\s*", "", t, flags=re.I)
    t = REL_PREFIX.sub('', t).strip(" -–—:|•·")
    t = re.sub(r"\s+ile\s+", " ile ", t)
    t = re.sub(r"\s+ve\s+", " ve ", t)
    return t

def build_tweet(code: str, snippet: str, codes=None) -> str:
    codes = codes or [code]
    base = clean_text(snippet)

    # ikinci/üçüncü kodları metin içinde hashtag'le
    for extra in codes[1:]:
        # zaten hashtag'li değilse kelime bazlı değiştir
        pat = re.compile(rf"\b{re.escape(extra)}\b", flags=re.I)
        if pat.search(base):
            base = pat.sub(f"#{extra}", base)
        else:
            # metinde yoksa, sona ekleyelim
            base = base + f" #{extra}"

    # ilk cümleyi al ve kısalt
    sentences = [s.strip() for s in base.split('.') if s.strip()]
    first_sentence = sentences[0] if sentences else base.split(' ', 25)[0]
    if len(first_sentence) < 30:
        words = base.split()
        first_sentence = ' '.join(words[:30])
    if len(first_sentence) > 230:
        first_sentence = first_sentence[:227].rsplit(' ', 1)[0] + "..."
    if not first_sentence.endswith(('.', '!', '?')):
        first_sentence += "."

    tweet = f"📰 #{code} | {first_sentence}"
    return tweet[:279]  # X sınırımız (279)

def is_valid_ticker(code: str, text: str) -> bool:
    if len(code) < 2 or len(code) > 6: return False
    if not re.match(r"^[A-ZÇĞİÖŞÜ]+(?:[0-9])?$", code): return False
    forbidden = ["YATIRIM BİLGİ", "TAVSİYE", "YASAL UYARI", "KİŞİSEL VERİ", "POLİTİKASI", "KVK"]
    if any(phrase in text.upper() for phrase in forbidden): return False
    return True

def goto_with_retry(page, url, retries=3):
    for i in range(retries):
        try:
            log(f">> goto attempt {i+1}/{retries}")
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_selector("main", timeout=15000)
            page.wait_for_load_state("networkidle", timeout=30000)
            page.wait_for_timeout(2000)
            return True
        except Exception as e:
            log(f">> goto retry {i+1}/{retries} failed: {e}")
            if i < retries - 1:
                time.sleep(5)
    return False

# ============== MAIN ==============
def main():
    log(">> start (GitHub Actions)")

    # COOLDOWN KONTROLÜ
    if state.get("cooldown_until"):
        try:
            cooldown_dt = dt.fromisoformat(state["cooldown_until"].replace("Z", "+00:00"))
            if dt.now(timezone.utc) < cooldown_dt:
                log(f">> cooldown aktif: {cooldown_dt.isoformat()}")
                return
        except Exception as e:
            log(f"!! cooldown parse hatası: {e}")

    # GÜNLÜK LİMİT
    today = dt.now().strftime("%Y-%m-%d")
    if state.get("day") != today:
        state["count_today"] = 0
        state["day"] = today
    if state.get("count_today", 0) >= MAX_TODAY:
        log(f">> günlük limit ({MAX_TODAY}) aşıldı")
        return

    tw = twitter_client()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            locale="tr-TR",
            timezone_id="Europe/Istanbul"
        )
        page = ctx.new_page()
        page.set_default_timeout(30000)

        if not goto_with_retry(page, AKIS_URL):
            log("!! Sayfa yüklenemedi")
            browser.close()
            return

        try:
            if page.get_by_text("Öne çıkanlar", exact=True).is_visible(timeout=3000):
                page.click("text=Öne çıkanlar")
                page.wait_for_load_state("networkidle", timeout=15000)
                page.wait_for_timeout(2000)
                log(">> highlights ON")
        except:
            log(">> highlights not available")

        try:
            log(">> evaluating JS extractor...")
            raw_items = page.evaluate(JS_EXTRACTOR)
            log(f">> extracted {len(raw_items)} items in JS")
        except Exception as e:
            log(f"!! JS evaluation failed: {e}")
            raw_items = []

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
            if not is_valid_ticker(it["code"], it["snippet"]):
                log(f">> SKIP: #{it['code']} (geçersiz)")
                continue

            tweet = build_tweet(it["code"], it["snippet"], it.get("codes"))
            log(f">> TWEET: {tweet}")

            try:
                if tw:
                    tw.create_tweet(text=tweet)
                posted.add(it["id"])
                sent += 1
                state["count_today"] = state.get("count_today", 0) + 1
                state["posted"] = sorted(list(posted))
                state["last_id"] = newest_id
                save_state(state)
                log(">> sent")
                if sent >= 4:
                    time.sleep(3)
            except Exception as e:
                if "429" in str(e) or "Too Many Requests" in str(e):
                    log(">> rate limit → 15 dk cooldown")
                    state["cooldown_until"] = (dt.now(timezone.utc) + timedelta(minutes=15)).isoformat()
                    save_state(state)
                    time.sleep(65)
                    try:
                        if tw: tw.create_tweet(text=tweet)
                        posted.add(it["id"])
                        state["count_today"] = state.get("count_today", 0) + 1
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
            f.write(f"\n--- {dt.now()} ---\n{tb}\n")
