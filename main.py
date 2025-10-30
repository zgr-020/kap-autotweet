# main.py
import os
import re
import json
import time
import logging
from pathlib import Path
from datetime import datetime as dt, timezone, timedelta
# ----- Zaman dilimini TR yap (log ve Playwright bağlamı için) -----
os.environ["TZ"] = "Europe/Istanbul"
try:
    time.tzset() # Linux'ta çalışır
except Exception:
    pass
from playwright.sync_api import sync_playwright
import tweepy
# ================== AYARLAR ==================
AKIS_URL = "https://fintables.com/borsa-haber-akisi"
STATE_PATH = Path("state.json")
MAX_PER_RUN = 5 # Bir çalıştırmada en fazla kaç tweet
MAX_TODAY = 25 # Günlük üst limit
COOLDOWN_MIN = 15 # 429 sonrası bekleme dk
# ================== SECRETS ==================
API_KEY = os.getenv("API_KEY")
API_KEY_SECRET = os.getenv("API_KEY_SECRET")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
ACCESS_TOKEN_SECRET = os.getenv("ACCESS_TOKEN_SECRET")
# ================== LOG ==================
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
log = logging.info
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
            # Eski format desteği
            default["posted"] = data
            return default
        for k, v in default.items():
            data.setdefault(k, v)
        return data
    except Exception as e:
        log(f"!! state.json okunamadı, sıfırlanıyor: {e}")
        return default
def save_state(s):
    try:
        STATE_PATH.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log(f"!! state.json kaydedilemedi: {e}")
# ================== TWITTER ==================
def twitter_client():
    if not all([API_KEY, API_KEY_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET]):
        log("!! Twitter anahtarları eksik → SIMÜLASYON modunda tweetlenecek.")
        return None
    try:
        return tweepy.Client(
            consumer_key=API_KEY,
            consumer_secret=API_KEY_SECRET,
            access_token=ACCESS_TOKEN,
            access_token_secret=ACCESS_TOKEN_SECRET,
        )
    except Exception as e:
        log(f"!! Twitter client kurulamadı: {e} → SIMÜLASYON")
        return None
def send_tweet(client, text: str) -> bool:
    if not client:
        log(f"SIMULATION TWEET: {text}")
        return True
    try:
        client.create_tweet(text=text)
        log("Tweet sent")
        return True
    except Exception as e:
        log(f"Tweet error: {e}")
        if "429" in str(e) or "Too Many Requests" in str(e):
            raise RuntimeError("RATE_LIMIT")
        return False
# ================== EXTRACTOR (geliştirilmiş: selector daralt, regex esnek) ==================
JS_EXTRACTOR = r"""
() => {
  const out = [];
  // Selector'ları daralt: sadece olası haber item'ları (performans ↑)
  const nodes = Array.from(document.querySelectorAll(
    "main article, main li[role='listitem'], main .news-item, main .feed-item, main .card, main div[class*='item']"
  )).slice(0, 200); // 200'ye düşür
  const skip = /(Fintables|Günlük Bülten|Analist|Bülten)/i;
  // Regex esnet: kAp/KAP: destekli, çift kod için optional separator
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
    // İçerik: KAP + KOD(lar) ifadesinden sonrasını al
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
    // Temizlik
    content = content.replace(/^\p{P}+/u, "").replace(/\s+/g, " ").trim();
    if (content.length < 10) continue; // Boş içerik filtrele
    // ID: Daha stabil (text'in MD5-like hash'i)
    let hash = 0;
    for (let i = 0; i < text.length; i++) {
      const char = text.charCodeAt(i);
      hash = ((hash << 5) - hash + char) | 0; // djb2 hash
    }
    out.push({ id: `kap-${codes.join("-")}-${Math.abs(hash)}`, codes, content, raw: text });
  }
  return out;
}
"""
def build_tweet(codes, content) -> str:
    """Quanta, Fin. haber tweet formatı (çift kod destekli)"""
    codes_str = " ".join(f"#{c}" for c in codes)
    text = content.strip()
    # Geliştirilmiş kısaltma: Cümle sonuna göre
    if len(text) > 240:
        cutoff = text[:240].rfind(".")
        if cutoff > 180:  # En az %75 al
            text = text[:cutoff + 1] + "..."
        else:
            text = text[:237].rsplit(" ", 1)[0] + "..."
    return f"📰 {codes_str} | {text}"[:280]
# ================== SAYFA İŞLEMLERİ ==================
def goto_with_retry(page, url, retries=3) -> bool:
    for i in range(retries):
        try:
            log(f"Sayfa yükleme deneme {i+1}/{retries}")
            page.goto(url, wait_until="networkidle", timeout=45000)  # networkidle ekle (JS load bekle)
            page.wait_for_selector("main", timeout=20000)
            # Screenshot for debug (GitHub'da artifact olarak yükle)
            page.screenshot(path="debug-load.png")
            log("Screenshot saved: debug-load.png")
            return True
        except Exception as e:
            log(f"Yükleme hatası: {e}")
            if i < retries - 1:
                time.sleep(5)  # Beklemeyi artır
    return False
def click_highlights(page):
    """Geliştirilmiş: Daha fazla selector + wait + case-insensitive"""
    # Text-based
    selectors = [
        "text=/öne[\\s]*çıkanlar/i",  # Regex case-insensitive
        "button:has-text('Öne çıkanlar')",
        "a:has-text('Öne çıkanlar')",
        "[role='tab']:has-text('Öne çıkanlar')",
        "div[role='button']:has-text('Öne çıkanlar')"
    ]
    # Icon'lu butonlar için (eğer text yoksa)
    icon_selectors = [
        "button[aria-label*='öne' i]",
        ".filter-tab:has(svg)",  # Icon varsa
        "[data-testid='highlights-tab']"
    ]
    all_selectors = selectors + icon_selectors
    page.wait_for_timeout(2000)  # Sayfa tam yüklenene kadar bekle
    for sel in all_selectors:
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                loc.first.wait_for(state="visible", timeout=5000)  # Explicit wait
                if loc.first.is_visible():
                    loc.first.click()
                    page.wait_for_timeout(1500)  # Tıklama sonrası load
                    log(">> Öne çıkanlar sekmesi aktif")
                    # Screenshot after click
                    page.screenshot(path="debug-highlights.png")
                    return True
        except Exception as e:
            log(f"Selector '{sel}' hatası: {e}")
            continue
    log(">> Öne çıkanlar butonu bulunamadı (Tümü'nde kalındı)")
    page.screenshot(path="debug-no-highlights.png")
    return False
def scroll_warmup(page):
    """Güçlendirilmiş: Infinite scroll için wait_for_function"""
    log(">> Scroll warmup başlıyor")
    for y in [0, 400, 800, 1200, 1600]:  # Daha fazla adım
        try:
            page.evaluate(f"window.scrollTo(0,{y})")
            page.wait_for_timeout(800)  # Beklemeyi artır
        except Exception:
            pass
    # Yeni item'lar gelene kadar bekle (dynamic load detect)
    try:
        page.wait_for_function("document.querySelectorAll('main article, main li').length > 10", timeout=10000)
        log(">> Yeterli item yüklendi")
    except Exception:
        log(">> Item load timeout, devam")
    page.evaluate("window.scrollTo(0,0)")
    page.wait_for_timeout(1000)
# ================== ANA AKIŞ ==================
def main():
    log("Başladı")
    state = load_state()
    # Günlük sayaç reset
    today = dt.now().strftime("%Y-%m-%d")
    if state.get("day") != today:
        state["count_today"] = 0
        state["day"] = today
    # Cooldown kontrol (UTC tutarlı)
    if state.get("cooldown_until"):
        try:
            cd = dt.fromisoformat(state["cooldown_until"])
            cd = cd.replace(tzinfo=timezone.utc) if cd.tzinfo is None else cd
            if dt.now(timezone.utc) < cd:
                log("In cooldown, exiting")
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
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",  # Güncel UA
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            viewport={"width": 1920, "height": 1080},  # GitHub için büyük viewport
        )
        page = ctx.new_page()
        page.set_default_timeout(45000)
        if not goto_with_retry(page, AKIS_URL):
            log("!! Sayfa açılamadı")
            browser.close()
            return
        # Öne çıkanlar
        click_highlights(page)
        scroll_warmup(page)
        # Çıkarım
        try:
            items = page.evaluate(JS_EXTRACTOR) or []
            # Debug: Raw HTML snippet logla
            main_html = page.evaluate("document.querySelector('main').innerHTML.substring(0, 1000)")
            log(f"Main HTML snippet: {main_html}")
        except Exception as e:
            log(f"JS extractor hatası: {e}")
            items = []
        log(f"Bulunan KAP haberleri: {len(items)}")
        if not items:
            log("!! Items boş - Screenshot'lar kontrol et: debug-*.png")
            browser.close()
            return
        # En yeni → en eski; last_id filtrele (reverse kaldır: yeni önce at)
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
            log("Yeni öğe yok")
            return
        # Yeni önce at (reverse kaldırıldı)
        sent = 0
        for it in to_send:  # Artık reversed(items) mantığı yok, direkt yeni
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
                        time.sleep(3)  # Beklemeyi artır
            except RuntimeError as e:
                if str(e) == "RATE_LIMIT":
                    now_utc = dt.now(timezone.utc)
                    state["cooldown_until"] = (now_utc + timedelta(minutes=COOLDOWN_MIN)).isoformat()
                    save_state(state)
                    log(f"Cooldown activated for {COOLDOWN_MIN} minutes")
                    break
                else:
                    log("Beklenmeyen hata (tweet): " + str(e))
        browser.close()
        log(f"Done. Sent {sent} tweets")
if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        log("!! FATAL !!")
        log(str(e))
        log(traceback.format_exc())
