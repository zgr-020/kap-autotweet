# main.py
import os
import re
import json
import time
import logging
from pathlib import Path
from datetime import datetime as dt, timezone, timedelta

# ----- Zaman dilimini TR yap -----
os.environ["TZ"] = "Europe/Istanbul"
try:
    time.tzset()
except Exception:
    pass

from playwright.sync_api import sync_playwright
import tweepy

# ================== AYARLAR ==================
AKIS_URL = "https://fintables.com/borsa-haber-akisi"
STATE_PATH = Path("state.json")
MAX_PER_RUN = 5
MAX_TODAY = 25
COOLDOWN_MIN = 15

# ================== SECRETS ==================
API_KEY = os.getenv("API_KEY")
API_KEY_SECRET = os.getenv("API_KEY_SECRET")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
ACCESS_TOKEN_SECRET = os.getenv("ACCESS_TOKEN_SECRET")

# ================== LOG (dosya + console) ==================
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger().info

# ================== STATE ==================
def load_state():
    default = {
        "last_id": None,
        "posted": [],
        "cooldown_until": None,
        "count_today": 0,
        "day": None
    }
    if not STATE_PATH.exists():
        return default
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            default["posted"] = data
            return default
        for k, v in default.items():
            data.setdefault(k, v)
        return data
    except Exception as e:
        log(f"!! state.json okunamadı: {e}")
        return default

def save_state(s):
    try:
        STATE_PATH.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log(f"!! state.json kaydedilemedi: {e}")

# ================== TWITTER ==================
def twitter_client():
    if not all([API_KEY, API_KEY_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET]):
        log("!! Twitter anahtarları eksik → SIMÜLASYON modu")
        return None
    try:
        return tweepy.Client(
            consumer_key=API_KEY,
            consumer_secret=API_KEY_SECRET,
            access_token=ACCESS_TOKEN,
            access_token_secret=ACCESS_TOKEN_SECRET,
        )
    except Exception as e:
        log(f"!! Twitter client hatası: {e} → SIMÜLASYON")
        return None

def send_tweet(client, text: str) -> bool:
    if not client:
        log(f"SIMULATION TWEET: {text}")
        return True
    try:
        client.create_tweet(text=text)
        log("Tweet gönderildi")
        return True
    except Exception as e:
        log(f"Tweet hatası: {e}")
        if "429" in str(e) or "Too Many Requests" in str(e):
            raise RuntimeError("RATE_LIMIT")
        return False

# ================== EXTRACTOR (genişletilmiş + stabil ID) ==================
JS_EXTRACTOR = r"""
() => {
  const out = [];
  // Geniş selector: log'dan esinlenildi + olası item class'ları
  const nodes = Array.from(document.querySelectorAll(
    "main article, main li, main div[class*='item'], main div[class*='card'], main div[class*='news'], main div[class*='feed'], .native-scrollable > div, [data-testid*='post']"
  )).slice(0, 200);
  const skip = /(Fintables|Günlük Bülten|Analist|Bülten)/i;
  const kapRe = /\b[Kk][Aa][Pp](?::)?\b[^A-Za-zÇĞİÖŞÜ0-9]*([A-ZÇĞİÖŞÜ]{2,6})(?:\s*[•\/\-\|]\s*([A-ZÇĞİÖŞÜ]{2,6}))?/i;
  for (const el of nodes) {
    const text = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
    if (!text || text.length < 35) continue;
    if (skip.test(text)) continue;
    const m = text.match(kapRe);
    if (!m) continue;
    const codes = [];
    if (m[1]) codes.push(m[1].toUpperCase());
    if (m[2]) codes.push(m[2].toUpperCase());
    let content = text;
    const idx = text.toUpperCase().indexOf("KAP");
    if (idx >= 0) {
      const after = text.slice(idx);
      const mm = after.match(kapRe);
      if (mm) {
        const cut = after.indexOf(mm[0]) + mm[0].length;
        content = after.slice(cut).trim();
      }
    }
    content = content.replace(/^\p{P}+/u, "").replace(/\s+/g, " ").trim();
    if (content.length < 10) continue;
    // Stabil hash
    let hash = 0;
    for (let i = 0; i < text.length; i++) {
      const char = text.charCodeAt(i);
      hash = ((hash << 5) - hash + char) | 0;
    }
    out.push({ id: `kap-${codes.join("-")}-${Math.abs(hash)}`, codes, content, raw: text });
  }
  return out;
}
"""

def build_tweet(codes, content) -> str:
    codes_str = " ".join(f"#{c}" for c in codes)
    text = content.strip()
    if len(text) > 240:
        cutoff = text[:240].rfind(".")
        if cutoff > 180:
            text = text[:cutoff + 1] + "..."
        else:
            text = text[:237].rsplit(" ", 1)[0] + "..."
    return f"{codes_str} | {text}"[:280]

# ================== SAYFA İŞLEMLERİ ==================
def goto_with_retry(page, url, retries=3) -> bool:
    for i in range(retries):
        try:
            log(f"Sayfa yükleme deneme {i+1}/{retries}")
            page.goto(url, wait_until="networkidle", timeout=45000)
            page.wait_for_selector(".native-scrollable", timeout=20000)
            page.screenshot(path="debug-load.png")
            log("Screenshot: debug-load.png")
            return True
        except Exception as e:
            log(f"Yükleme hatası: {e}")
            if i < retries - 1:
                time.sleep(5)
    return False

def click_highlights(page):
    """Tümü > Öne çıkanlar önceliği"""
    selectors = [
        "text=/tümü/i",
        "button:has-text('Tümü')",
        "a:has-text('Tümü')",
        "text=/öne[\\s]*çıkanlar/i",
        "button:has-text('Öne çıkanlar')"
    ]
    page.wait_for_timeout(2000)
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                loc.first.wait_for(state="visible", timeout=5000)
                if loc.first.is_visible():
                    loc.first.click()
                    page.wait_for_timeout(2000)
                    log(f">> '{sel}' sekmesi aktif")
                    page.screenshot(path="debug-highlights.png")
                    return True
        except Exception as e:
            log(f"Selector hatası '{sel}': {e}")
            continue
    log(">> Sekme bulunamadı, mevcut sayfada kal")
    page.screenshot(path="debug-no-highlights.png")
    return False

def scroll_warmup(page):
    log(">> Scroll warmup (gelişmiş) başlıyor")
    for y in [0, 300, 600, 900, 1200, 1500, 1800]:
        try:
            page.evaluate(f"window.scrollTo(0,{y})")
            page.wait_for_timeout(1000)
        except Exception:
            pass

    # MutationObserver: Yeni item gelene kadar bekle
    observer_js = """
    () => {
      return new Promise((resolve) => {
        const target = document.querySelector('main') || document.querySelector('.native-scrollable');
        if (!target) return resolve(false);
        let itemCount = target.querySelectorAll('article, li, div[class*="item"]').length;
        const observer = new MutationObserver(() => {
          const newCount = target.querySelectorAll('article, li, div[class*="item"]').length;
          if (newCount > itemCount + 2) {
            observer.disconnect();
            resolve(true);
          }
          itemCount = newCount;
        });
        observer.observe(target, { childList: true, subtree: true });
        setTimeout(() => {
          observer.disconnect();
          resolve(target.querySelectorAll('article, li, div[class*="item"]').length > 5);
        }, 15000);
      });
    }
    """
    try:
        loaded = page.evaluate(observer_js)
        log(">> Yeni item'lar yüklendi" if loaded else ">> Observer timeout")
    except Exception as e:
        log(f">> Observer hatası: {e}")
    page.evaluate("window.scrollTo(0,0)")
    page.wait_for_timeout(1500)

# ================== ANA AKIŞ ==================
def main():
    log("Bot başladı")
    state = load_state()
    today = dt.now().strftime("%Y-%m-%d")
    if state.get("day") != today:
        state["count_today"] = 0
        state["day"] = today

    if state.get("cooldown_until"):
        try:
            cd = dt.fromisoformat(state["cooldown_until"])
            cd = cd.replace(tzinfo=timezone.utc) if cd.tzinfo is None else cd
            if dt.now(timezone.utc) < cd:
                log("Cooldown aktif, çıkılıyor")
                return
            else:
                state["cooldown_until"] = None
        except Exception:
            state["cooldown_until"] = None

    if state["count_today"] >= MAX_TODAY:
        log(f"Günlük limit ({MAX_TODAY}) aşıldı")
        return

    tw = twitter_client()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--disable-web-security"],
        )
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            viewport={"width": 1920, "height": 1080},
        )
        page = ctx.new_page()
        page.set_default_timeout(45000)

        if not goto_with_retry(page, AKIS_URL):
            log("!! Sayfa açılamadı")
            browser.close()
            return

        click_highlights(page)
        scroll_warmup(page)

        # EK DEBUG
        try:
            main_html = page.evaluate("document.querySelector('main')?.innerHTML.substring(0, 2000) || 'Main yok'")
            log(f"Main HTML (uzun): {main_html}")
            item_count = page.evaluate("document.querySelectorAll('article, li, div[class*=\"item\"], div[class*=\"news\"]').length")
            log(f"Potansiyel item sayısı: {item_count}")
            items = page.evaluate(JS_EXTRACTOR) or []
        except Exception as e:
            log(f"JS extractor hatası: {e}")
            items = []

        log(f"KAP haberleri bulundu: {len(items)}")
        if not items:
            body_snippet = page.evaluate("document.body.innerHTML.substring(0, 500)")
            log(f"Body snippet: {body_snippet}")
            log("!! Items boş → debug-*.png ve bot.log kontrol et")
            browser.close()
            return

        posted_set = set(state.get("posted", []))
        newest_id = items[0]["id"]
        to_send = []
        last_id = state.get("last_id")
        for it in items:
            if last_id and it["id"] == last_id:
                break
            to_send.append(it)

        if not to_send:
            state["last_id"] = newest_id
            save_state(state)
            browser.close()
            log("Yeni haber yok")
            return

        sent = 0
        for it in to_send:
            if sent >= MAX_PER_RUN:
                break
            if it["id"] in posted_set:
                continue
            if not it.get("codes") or not it.get("content"):
                continue
            tweet = build_tweet(it["codes"], it["content"])
            log(f"Tweeting: {tweet}")
            try:
                ok = send_tweet(tw, tweet)
                if ok:
                    posted_set.add(it["id"])
                    state["posted"] = sorted(list(posted_set))
                    state["last_id"] = newest_id
                    state["count_today"] += 1
                    save_state(state)
                    sent += 1
                    if tw and sent < MAX_PER_RUN:
                        time.sleep(3)
            except RuntimeError as e:
                if str(e) == "RATE_LIMIT":
                    now_utc = dt.now(timezone.utc)
                    state["cooldown_until"] = (now_utc + timedelta(minutes=COOLDOWN_MIN)).isoformat()
                    save_state(state)
                    log(f"Rate limit → {COOLDOWN_MIN} dk cooldown")
                    break
                else:
                    log(f"Tweet hatası: {e}")

        browser.close()
        log(f"İşlem bitti. Gönderilen: {sent}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        log("!! FATAL HATA !!")
        log(str(e))
        log(traceback.format_exc())
