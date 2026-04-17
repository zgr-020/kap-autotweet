import os
import re
import json
import time
import logging
from logging.handlers import RotatingFileHandler
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
MAX_TODAY = 50
COOLDOWN_MIN = 15
SCORE_MIN = 3

# ================== SECRETS ==================
API_KEY = os.getenv("API_KEY")
API_KEY_SECRET = os.getenv("API_KEY_SECRET")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
ACCESS_TOKEN_SECRET = os.getenv("ACCESS_TOKEN_SECRET")

# ================== LOG AYARLARI ==================
log_handler = RotatingFileHandler(
    "bot.log", 
    maxBytes=2*1024*1024,
    backupCount=1,
    encoding="utf-8"
)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[log_handler, logging.StreamHandler()]
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
        log(f"⚠️ TWITTER API HATASI: {e}")
        
        if "duplicate content" in err_msg:
            log("Twitter: Duplicate content → zaten atılmış.")
            return True
        if "429" in err_msg or "too many requests" in err_msg:
            log("⛔️ Rate limit (429) algılandı!")
            raise RuntimeError("RATE_LIMIT")
            
        return False

# ================== EXTRACTOR (HER ŞEYİ TARA MODU) ==================
JS_EXTRACTOR = r"""
() => {
  const out = [];
  const banList = ["KAP", "DUN", "BUGUN", "YARIN", "SAAT", "DÜN", "BUGÜN", "TL", "LOT", "USD", "EURO", "BIST", "VIOP"];

  // Sayfadaki potansiyel haber satırlarını bul
  const potentialNodes = document.querySelectorAll('div, li, p, span');
  const seenTexts = new Set(); 

  for (const node of potentialNodes) {
    if (!node.innerText) continue;
    
    let rawText = node.innerText.replace(/\s+/g, " ").trim();
    if (rawText.length < 15 || rawText.length > 500) continue;

    // KAP SİNYALİ ARA
    let splitIndex = rawText.search(/KAP\s*[:•·\-]/i);
    if (splitIndex === -1) continue;
    
    if (seenTexts.has(rawText)) continue;
    seenTexts.add(rawText);

    let afterKap = rawText.substring(splitIndex).replace(/^KAP\s*[:•·\-]/i, "").trim();
    let tokens = afterKap.split(" ");
    let codes = [];
    let contentStartIndex = 0;

    for (let i = 0; i < tokens.length; i++) {
        let t = tokens[i];
        let upperT = t.toUpperCase().replace(/[^A-ZÇĞİÖŞÜ0-9]/g, ""); 

        const isAllLetters = /^[A-ZÇĞİÖŞÜ]+$/.test(upperT);
        const isLengthOk = upperT.length >= 3 && upperT.length <= 6;
        const notBanned = !banList.includes(upperT);
        const isOriginalUpper = (t === t.toUpperCase());

        if (isAllLetters && isLengthOk && notBanned && isOriginalUpper) {
            codes.push(upperT);
        } else {
            contentStartIndex = i;
            break; 
        }
    }

    if (codes.length === 0) continue;

    let content = tokens.slice(contentStartIndex).join(" ");
    let oldContent = "";
    while (content !== oldContent) {
        oldContent = content;
        content = content
            .replace(/^\s*(?:Dün|Bugün|Yarın|Pazartesi|Salı|Çarşamba|Perşembe|Cuma|Cumartesi|Pazar)\b/i, "")
            .replace(/^\s*\d{1,2}[:\.]\d{2}\b/, "")
            .replace(/^\s*\d{1,2}\s+(?:Ocak|Şubat|Mart|Nisan|Mayıs|Haziran|Temmuz|Ağustos|Eylül|Ekim|Kasım|Aralık)(?:\s+\d{4})?\b/i, "")
            .replace(/^\s*(?:ün|ugün|arın)\b/i, "")
            .replace(/^[^\wÇĞİÖŞÜçğıöşü\d]+/, "")
            .trim();
    }
    
    if (content.length < 5) continue;

    let hash = 0;
    const base = codes.join('') + content; 
    for (let i = 0; i < base.length; i++) {
      hash = ((hash << 5) - hash + base.charCodeAt(i)) | 0;
    }

    out.push({
      id: `kap-${codes[0]}-${Math.abs(hash)}`,
      codes: codes, 
      content: content,
      raw: rawText
    });
  }
  return out;
}
"""

TWEET_EMOJI = "📣"
ADD_UNIQ = False

# ================== ÖNEM SKORU ==================
_HIGH = [
    "temettü", "kar payı", "kâr payı",
    "sermaye artırım", "bedelsiz", "rüçhan",
    "birleşme", "satın alma", "devralma", "ortaklık",
    "sözleşme", "ihale", "anlaşma",
    "finansal sonuç", "bilanço", "kâr açıkladı", "zarar açıkladı",
    "halka arz", "geri alım", "pay geri",
    "yönetim kurulu", "genel müdür", "ceo",
]
_LOW = [
    "faaliyet raporu", "iç kontrol", "bağımsız denetim",
    "genel kurul toplantı", "bildirim yükümlülüğ",
    "özel durum açıklaması güncellem",
]

def score_item(content: str) -> int:
    text = content.lower()
    score = 1
    for kw in _HIGH:
        if kw in text:
            score += 2
    for kw in _LOW:
        if kw in text:
            score -= 1
    return max(score, 0)

def build_tweet(codes, content, tweet_id=""):
    codes_str = " ".join(f"#{c}" for c in codes)

    text = content.strip()
    text = re.sub(r'^(?:Dün|Bugün|Yarın)\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^\d{1,2}[:\.]\d{2}\s*', '', text)
    text = re.sub(r'^\d{1,2}\s+(?:Ocak|Şubat|Mart|Nisan|Mayıs|Haziran|Temmuz|Ağustos|Eylül|Ekim|Kasım|Aralık)(?:\s+\d{4})?\s*', '', text, flags=re.IGNORECASE)

    prefix = f"{TWEET_EMOJI} {codes_str} | "
    suffix = ""
    if ADD_UNIQ and tweet_id:
        uniq = tweet_id[-4:]
        suffix = f" [K{uniq}]"

    max_len = 279 - len(prefix) - len(suffix)
    if len(text) > max_len:
        return None

    return prefix + text + suffix

# ================== SAYFA İŞLEMLERİ ==================
def goto_with_retry(page, url, retries=3) -> bool:
    for i in range(retries):
        try:
            log(f"Sayfa yükleme deneme {i+1}/{retries}")
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
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
    page.wait_for_timeout(3000)
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible(timeout=5000):
                loc.first.click()
                log(">> 'ÖNE ÇIKANLAR' butonuna tıklandı.")
                page.wait_for_timeout(3000)
                log(">> 'ÖNE ÇIKANLAR' sekmesi aktif!")
                return True
        except Exception as e:
            pass 
    
    # EĞER BURAYA GELDİYSE BUTONU BULAMADI DEMEKTİR
    log(">> 'ÖNE ÇIKANLAR' butonu BULUNAMADI! İşlem iptal ediliyor.")
    return False

def scroll_warmup(page):
    log(">> Scroll warmup başlıyor")
    page.evaluate("window.scrollTo(0,1000)")
    page.wait_for_timeout(2000) 
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
                log("Cooldown aktif. Bekleniyor...")
                return
            state["cooldown_until"] = None
        except:
            state["cooldown_until"] = None

    if state["count_today"] >= MAX_TODAY:
        log(f"Günlük limit ({MAX_TODAY}) doldu.")
        return

    tw = twitter_client()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True, 
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="tr-TR", timezone_id="Europe/Istanbul", viewport={"width": 1920, "height": 1080}
        )
        page = ctx.new_page()
        page.set_default_timeout(45000)

        if not goto_with_retry(page, AKIS_URL):
            browser.close()
            return

        # EĞER BUTON BULUNAMAZSA FALSE DÖNECEK VE BURADA ÇIKACAĞIZ
        if not click_highlights(page):
            log("🛑 Önemli haber filtresi açılamadı. Hatalı işlem yapmamak için durduruluyor.")
            browser.close()
            return

        scroll_warmup(page)

        try:
            log(">> Haberlerin ekrana düşmesi bekleniyor...")
            page.wait_for_timeout(5000) 
        except Exception:
            pass

        items = page.evaluate(JS_EXTRACTOR) or []
        log(f"Bulunan KAP haberi: {len(items)}")

        if not items:
            log("Haber bulunamadı.")
            page.screenshot(path="debug-not-found.png")
            browser.close()
            return

        posted_set = set(state.get("posted", []))
        to_send = []
        last_id = state.get("last_id")

        for it in items:
            if last_id and it["id"] == last_id:
                break 
            
            if it["id"] in posted_set:
                continue
            
            to_send.append(it)

        if not to_send:
            if items:
                state["last_id"] = items[0]["id"]
                save_state(state)
            log("Yeni haber yok")
            browser.close()
            return

        log(f"Kuyrukta bekleyen tweet sayısı: {len(to_send)}")

        sent = 0
        for it in to_send:
            if sent >= MAX_PER_RUN:
                log(f"Limit ({MAX_PER_RUN}) doldu.")
                break

            score = score_item(it["content"])
            if score < SCORE_MIN:
                log(f"Düşük skor ({score}), atlanıyor: {it['id']}")
                posted_set.add(it["id"])
                state["posted"] = sorted(list(posted_set))[-5000:]
                save_state(state)
                continue

            tweet = build_tweet(it["codes"], it["content"], it["id"])
            if tweet is None:
                log(f"Haber tweete sığmıyor, atlanıyor: {it['id']}")
                posted_set.add(it["id"])
                state["posted"] = sorted(list(posted_set))[-5000:]
                save_state(state)
                continue
            log(f"Skor: {score} | Tweet: {tweet}")
            
            try:
                ok = send_tweet(tw, tweet)
                if ok:
                    posted_set.add(it["id"])
                    state["posted"] = sorted(list(posted_set))[-5000:]
                    state["count_today"] += 1
                    
                    state["last_id"] = it["id"]
                    save_state(state)
                    
                    sent += 1
                    if tw and sent < MAX_PER_RUN:
                        time.sleep(5)
            except RuntimeError as e:
                if str(e) == "RATE_LIMIT":
                    log("Rate limit → cooldown, durduruluyor.")
                    state["cooldown_until"] = (dt.now(timezone.utc) + timedelta(minutes=COOLDOWN_MIN)).isoformat()
                    save_state(state)
                    break

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
