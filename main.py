import os
import re
import json
import time
import logging
from pathlib import Path
from datetime import datetime as dt, timezone, timedelta

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

# ================== LOG ==================
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)
log = logging.getLogger().info

# ================== STATE ==================
def load_state():
    default = {"last_id": None, "posted": [], "cooldown_until": None, "count_today": 0, "day": None}
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
        if "posted" in s and isinstance(s["posted"], list):
            s["posted"] = s["posted"][-5000:]
        STATE_PATH.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"state.json güncellendi: {len(s.get('posted', []))} tweet kaydedildi")
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
        err_msg = str(e).lower()
        if "duplicate content" in err_msg:
            log("Twitter: Duplicate content → zaten atılmış, atlanıyor")
            return True
        if "429" in err_msg or "too many requests" in err_msg:
            log("Rate limit → 15 dk cooldown")
            raise RuntimeError("RATE_LIMIT")
        log(f"Tweet hatası: {e}")
        return False

# ================== EXTRACTOR ==================
JS_EXTRACTOR = r"""
() => {
  const out = [];
  const nodes = Array.from(document.querySelectorAll('a.block[href^="/borsa-haber-akisi/"]')).slice(0, 200);
  const skip = /(Fintables|Günlük Bülten|Analist|Bülten|Fintables Akış)/i;

  // Zaman başını (Dün/Bugün/Yarın + HH:MM ya da sadece Dün/Bugün/Yarın) temizle
  const stripTimeHead = (s) => {
    if (!s) return "";
    return s
      .replace(/^\s*/, "")
      .replace(
        /^(?:(?:dün|bugün|yarın|pazartesi|salı|çarşamba|perşembe|cuma|cumartesi|pazar)\s*)?\d{1,2}:\d{2}\s*|^(?:dün|bugün|yarın)\s+/i,
        ""
      )
      .trim();
  };

  for (const a of nodes) {
    const text = a.textContent || "";
    const href = (a.href || a.getAttribute('href') || "").split('?')[0];
    const match = text.match(/KAP\s*[:•·]\s*([A-ZÇĞİÖŞÜ]{2,6})\s*([^]+?)(?=\n|$)/i);
    if (!match) continue;

    // 👇 sadece ilk geçerli 2–6 harfli kodu al
    let code = (match[1] || "").toUpperCase();
    code = (code.match(/[A-ZÇĞİÖŞÜ]{2,6}/) || [""])[0];
    if (!code) continue;

    let content = (match[2] || "").trim();
    if (content.length < 20 || skip.test(content)) continue;

    content = content.replace(/^[^\wÇĞİÖŞÜçğıöşü]+/u, '').replace(/\s+/g, ' ').trim();

    // ⬇️ ID için: varsa href kullan; yoksa zaman başı temizlenmiş metni kullan
    let hash = 0;
    const rawForHash = href || stripTimeHead(text);
    for (let i = 0; i < rawForHash.length; i++) {
      hash = ((hash << 5) - hash + rawForHash.charCodeAt(i)) | 0;
    }

    out.push({
      id: `kap-${code}-${Math.abs(hash)}`,
      codes: [code],
      content: content,
      raw: text
    });
  }
  return out;
}
"""

# MEGAFON + ESTETİK
TWEET_EMOJI = "📣"
ADD_UNIQ = False

def build_tweet(codes, content, tweet_id="") -> str:
    codes_str = " ".join(f"#{c}" for c in codes)
    # 👇 YENİ: “dün/bugün + saat” başlarını temizle
    text = re.sub(
        r'^(?:(?:dün|bugün|yarın|pazartesi|salı|çarşamba|perşembe|cuma|cumartesi|pazar)\s*)?\d{1,2}:\d{2}\s*|^(?:dün|bugün|yarın)\s+',
        '',
        content.strip(),
        flags=re.IGNORECASE
    ).strip()

    prefix = f"{TWEET_EMOJI} {codes_str} | "
    suffix = ""
    if ADD_UNIQ and tweet_id:
        uniq = tweet_id[-4:]
        suffix = f" [K{uniq}]"

    max_len = 279 - len(prefix) - len(suffix)
    if len(text) > max_len:
        cut = text[:max_len]
        dot = cut.rfind(".")
        if dot >= 0 and dot >= max_len - 120:
            cut = cut[:dot + 1]
        else:
            cut = cut.rsplit(" ", 1)[0] if " " in cut else cut
        text = cut.rstrip() + "..."

    return (prefix + text + suffix)[:279]

# ================== SAYFA İŞLEMLERİ ==================
def goto_with_retry(page, url, retries=3) -> bool:
    for i in range(retries):
        try:
            log(f"Sayfa yükleme deneme {i+1}/{retries}")
            page.goto(url, wait_until="networkidle", timeout=45000)
            page.wait_for_selector('a.block[href^="/borsa-haber-akisi/"]', timeout=20000)
            page.screenshot(path="debug-load.png")
            log("Screenshot: debug-load.png")
            return True
        except Exception as e:
            log(f"Yükleme hatası: {e}")
            if i < retries - 1:
                time.sleep(5)
    return False

def click_highlights(page):
    selectors = [
        "text=/öne[\\s]*çıkanlar/i",
        "button:has-text('Öne çıkanlar')",
        "a:has-text('Öne çıkanlar')",
        "[role='tab']:has-text('Öne çıkanlar')",
        "div[role='button']:has-text('Öne çıkanlar')"
    ]
    page.wait_for_timeout(2500)
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible(timeout=5000):
                loc.first.click()
                page.wait_for_timeout(2500)
                log(">> 'ÖNE ÇIKANLAR' sekmesi aktif!")
                page.screenshot(path="debug-one-cikanlar.png")
                return True
        except Exception as e:
            log(f"Selector hatası '{sel}': {e}")
    log(">> 'ÖNE ÇIKANLAR' butonu BULUNAMADI")
    page.screenshot(path="debug-one-cikanlar-yok.png")
    return False

def scroll_warmup(page):
    log(">> Scroll warmup başlıyor")
    for y in [0, 300, 600, 900, 1200]:
        page.evaluate(f"window.scrollTo(0,{y})")
        page.wait_for_timeout(1200)
    try:
        page.wait_for_function("document.querySelectorAll('a.block[href^=\"/borsa-haber-akisi/\"]').length > 10", timeout=15000)
        log(">> Yeterli haber yüklendi")
    except:
        log(">> Scroll timeout")
    page.evaluate("window.scrollTo(0,0)")
    page.wait_for_timeout(1000)

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
                log("Cooldown aktif")
                return
            state["cooldown_until"] = None
        except:
            state["cooldown_until"] = None

    if state["count_today"] >= MAX_TODAY:
        log(f"Günlük limit aşıldı")
        return

    tw = twitter_client()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
            locale="tr-TR", timezone_id="Europe/Istanbul", viewport={"width": 1920, "height": 1080}
        )
        page = ctx.new_page()
        page.set_default_timeout(45000)

        if not goto_with_retry(page, AKIS_URL):
            browser.close()
            return

        click_highlights(page)
        scroll_warmup(page)

        item_count = page.evaluate("document.querySelectorAll('a.block[href^=\"/borsa-haber-akisi/\"]').length")
        log(f"Toplam haber (Öne çıkanlar): {item_count}")
        items = page.evaluate(JS_EXTRACTOR) or []
        log(f"KAP haberleri bulundu: {len(items)}")

        if not items:
            log("!! KAP haberi yok → debug-one-cikanlar.png kontrol et")
            browser.close()
            return

        posted_set = set(state.get("posted", []))
        newest_id = items[0]["id"]
        to_send = []
        last_id = state.get("last_id")

        for it in items:
            if last_id and it["id"] == last_id:
                break
            if it["id"] in posted_set:
                continue
            to_send.append(it)

        if not to_send:
            state["last_id"] = newest_id
            save_state(state)
            log("Yeni haber yok")
            browser.close()
            return

        sent = 0
        for it in to_send:
            if sent >= MAX_PER_RUN:
                break
            if not it.get("codes") or not it.get("content"):
                continue

            tweet = build_tweet(it["codes"], it["content"], it["id"])
            log(f"Tweeting: {tweet}")
            try:
                ok = send_tweet(tw, tweet)
                if ok:
                    posted_set.add(it["id"])
                    state["posted"] = sorted(list(posted_set))
                    state["count_today"] += 1
                    save_state(state)
                    sent += 1
                    if tw and sent < MAX_PER_RUN:
                        time.sleep(3)
            except RuntimeError as e:
                if str(e) == "RATE_LIMIT":
                    log("Rate limit → cooldown, başarısız tweet kaydedilmedi")
                    state["cooldown_until"] = (dt.now(timezone.utc) + timedelta(minutes=COOLDOWN_MIN)).isoformat()
                    save_state(state)
                    break

        if sent > 0:
            state["last_id"] = newest_id
            save_state(state)

        browser.close()
        log(f"Bitti. Gönderilen: {sent}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        log("!! FATAL !!")
        log(str(e))
        log(traceback.format_exc())
