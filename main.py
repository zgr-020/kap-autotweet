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
MAX_PER_RUN = 5     # Her çalışmada atılacak maksimum tweet
MAX_TODAY = 150     # Günlük toplam limit
COOLDOWN_MIN = 15   # Rate limit yerse beklenecek dakika

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
        err_str = str(e).lower()
        log(f"⚠️ TWITTER API HATASI: {e}") 
        
        if "duplicate content" in err_str:
            log("Twitter: Duplicate content → zaten atılmış, işlem tamam sayılıyor.")
            return True
        
        if "429" in err_str or "too many requests" in err_str:
            log("⛔️ Rate limit (429) algılandı!")
            raise RuntimeError("RATE_LIMIT")
            
        return False

# ================== EXTRACTOR (YEPYENİ TOKENIZER MANTIĞI) ==================
JS_EXTRACTOR = r"""
() => {
  const out = [];
  // Sayfadaki haberleri seç
  const nodes = Array.from(document.querySelectorAll('a.block[href^="/borsa-haber-akisi/"]')).slice(0, 50);
  
  // Yasaklı kelimeler (Kod sanılabilecek zaman ifadeleri)
  const banList = ["OCA", "ŞUB", "MAR", "NIS", "MAY", "HAZ", "TEM", "AĞU", "EYL", "EKI", "KAS", "ARA", "DÜN", "BUGÜN", "YARIN", "SAAT"];

  for (const a of nodes) {
    let text = (a.textContent || "").trim();
    // Satırın tamamında "Dün" kelimesi bağımsız olarak geçiyorsa bu haberi direkt atla (Eski haber koruması)
    if (/\bDün\b/.test(text)) continue;

    // KAP ibaresini bul
    const kapIndex = text.search(/KAP\s*[:•·\-]/i);
    if (kapIndex === -1) continue;

    // Metni KAP işaretinden sonrasını alacak şekilde kes
    // Örnek text: "18:30 KAP • ODINE TCELL +2 Lorem ipsum..." -> " ODINE TCELL +2 Lorem ipsum..."
    let rawContent = text.substring(kapIndex).replace(/^KAP\s*[:•·\-]/i, "").trim();

    // Şimdi kelime kelime (token) inceleyeceğiz
    const tokens = rawContent.split(/\s+/);
    const codes = [];
    let contentStartIndex = 0;

    for (let i = 0; i < tokens.length; i++) {
        let t = tokens[i].replace(/[^a-zA-Z0-9]/g, ""); // Noktalama temizle
        let upperT = t.toUpperCase();

        // Eğer kelime "+2" gibi bir sayı ise atla
        if (tokens[i].startsWith("+") && !isNaN(parseInt(tokens[i]))) {
            continue;
        }

        // Kelime 3-10 karakter arası, tamamen BÜYÜK HARF ve yasaklı listede değilse KOD'dur.
        // Örn: ODINE, TCELL, MGMT
        if (t.length >= 3 && t.length <= 10 && t === upperT && !banList.includes(upperT) && !/^\d/.test(t)) {
            codes.push(upperT);
        } else {
            // Kod olmayan ilk kelimeye geldik, demek ki içerik buradan başlıyor.
            contentStartIndex = i;
            break;
        }
    }

    if (codes.length === 0) continue;

    // İçeriği birleştir (Token'ların geri kalanı)
    // tokens arrayindeki contentStartIndex'ten sonrasını alıp birleştiriyoruz.
    let content = tokens.slice(contentStartIndex).join(" ");

    // --- TEMİZLİK ---
    // İçeriğin başındaki saat, tarih, "ün", "ugün" gibi kalıntıları temizle
    // Döngüyle temizliyoruz ki iç içe geçmişse de silsin.
    let clean = content;
    for(let k=0; k<3; k++) {
        clean = clean
            .replace(/^(?:Bugün|Yarın|Pazartesi|Salı|Çarşamba|Perşembe|Cuma|Cumartesi|Pazar)/i, "")
            .replace(/^\d{1,2}:\d{2}/, "")  // 18:31 gibi saatleri sil
            .replace(/^(?:ün|ugün|arın)/i, "") // Kesik kelimeleri sil
            .replace(/^[^\wÇĞİÖŞÜçğıöşü]+/, "") // Baştaki noktalama işaretlerini sil (- . ,)
            .trim();
    }
    
    if (clean.length < 10) continue;

    // ID OLUŞTURMA (Zaman bağımsız, sadece Kod + İçerik)
    let hash = 0;
    const base = codes.join('') + clean; 
    for (let i = 0; i < base.length; i++) {
      hash = ((hash << 5) - hash + base.charCodeAt(i)) | 0;
    }

    out.push({
      id: `kap-${codes[0]}-${Math.abs(hash)}`,
      codes: codes, 
      content: clean,
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
    
    # Python tarafında son güvenlik temizliği
    text = content.strip()
    
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
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_selector('a.block[href^="/borsa-haber-akisi/"]', timeout=20000)
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
                return True
        except Exception as e:
            pass # Sessizce diğer selector'a geç
    log(">> 'ÖNE ÇIKANLAR' butonu BULUNAMADI (Sorun olmayabilir)")
    return False

def scroll_warmup(page):
    log(">> Scroll warmup başlıyor")
    page.evaluate("window.scrollTo(0,1000)")
    page.wait_for_timeout(1000)
    page.evaluate("window.scrollTo(0,0)")

# ================== ANA AKIŞ ==================
def main():
    log("Bot başladı")
    state = load_state()
    today = dt.now().strftime("%Y-%m-%d")
    
    # Gün değişmişse sayacı sıfırla
    if state.get("day") != today:
        state["count_today"] = 0
        state["day"] = today

    # Cooldown kontrolü
    if state.get("cooldown_until"):
        try:
            cd = dt.fromisoformat(state["cooldown_until"])
            cd = cd.replace(tzinfo=timezone.utc) if cd.tzinfo is None else cd
            if dt.now(timezone.utc) < cd:
                log("Cooldown (Ceza) süresi dolmadı. Bekleniyor...")
                return
            state["cooldown_until"] = None
        except:
            state["cooldown_until"] = None

    if state["count_today"] >= MAX_TODAY:
        log(f"Günlük tweet limiti ({MAX_TODAY}) doldu.")
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

        items = page.evaluate(JS_EXTRACTOR) or []
        log(f"Bulunan KAP haberi: {len(items)}")

        if not items:
            log("Haber bulunamadı.")
            browser.close()
            return

        posted_set = set(state.get("posted", []))
        to_send = []
        last_id = state.get("last_id")

        # 1. Filtreleme: Gönderilenleri ve last_id öncesini ele
        # items listesi EN YENİDEN -> EN ESKİYE doğru gelir.
        for it in items:
            if last_id and it["id"] == last_id:
                break # En son attığımız habere geldik, daha eskiye gitmeye gerek yok.
            if it["id"] in posted_set:
                continue
            to_send.append(it)

        if not to_send:
            # Yeni haber yok ama last_id'yi en güncel habere çekelim ki
            # state dosyası güncel kalsın.
            state["last_id"] = items[0]["id"]
            save_state(state)
            log("Yeni haber yok.")
            browser.close()
            return

        # 2. SIRALAMA DÜZELTMESİ (KRİTİK HAMLE)
        # items[0] en yeni haberdir. to_send şu an [YENİ, DAHA YENİ, DAHA YENİ...] diye gidiyor.
        # Twitter'a atarken zaman akışına uymak için ESKİDEN -> YENİYE doğru atmalıyız.
        # Ayrıca bu sayede yarıda kesilirse (limit) eski haberler atılmış olur, sonraki turda yeniler atılır.
        to_send.reverse() 
        
        log(f"Kuyrukta bekleyen tweet sayısı: {len(to_send)}")

        sent_count = 0
        for it in to_send:
            if sent_count >= MAX_PER_RUN:
                log(f"Bu çalışma için limit ({MAX_PER_RUN}) doldu. Kalanlar sonraki tura.")
                break
                
            tweet = build_tweet(it["codes"], it["content"], it["id"])
            log(f"Sıradaki Tweet: {tweet}")
            
            try:
                ok = send_tweet(tw, tweet)
                if ok:
                    posted_set.add(it["id"])
                    state["posted"] = sorted(list(posted_set))[-5000:] # Liste çok şişmesin
                    state["count_today"] += 1
                    
                    # KRİTİK: Her başarılı tweette last_id'yi güncelle.
                    # Böylece script şimdi patlasa bile bu haber "atıldı" sayılacak ve next run'da bunun üstündekileri alacak.
                    state["last_id"] = it["id"] 
                    save_state(state)
                    
                    sent_count += 1
                    if tw and sent_count < MAX_PER_RUN:
                        time.sleep(5) # İki tweet arası biraz nefes al
                        
            except RuntimeError as e:
                if str(e) == "RATE_LIMIT":
                    state["cooldown_until"] = (dt.now(timezone.utc) + timedelta(minutes=COOLDOWN_MIN)).isoformat()
                    save_state(state)
                    log("Limit nedeniyle durduruldu. State kaydedildi.")
                    break

        browser.close()
        log(f"Tamamlandı. Gönderilen: {sent_count}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        log("!! FATAL ERROR !!")
        log(str(e))
        log(traceback.format_exc())
