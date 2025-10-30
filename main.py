# main.py
import os, re, json, time, hashlib
from pathlib import Path
from datetime import datetime, timezone, timedelta

from playwright.sync_api import sync_playwright
import tweepy

# ===================== CONFIG =====================
AKIS_URL = "https://fintables.com/borsa-haber-akisi"
STATE_PATH = Path("state.json")
MAX_PER_RUN = 5
COOLDOWN_MIN = 15

# Tweet şablonu/filtreler
STOP_PHRASES = [
    "yatırım tavsiyesi değildir", "yasal uyarı", "kişisel veri", "kvk", "saygılarımızla",
    "kamunun bilgisine", "bilgilendirme"
]
REL_PREFIX = re.compile(r'^(?:dün|bugün|yesterday|today)\b[:\-–]?\s*', re.IGNORECASE)
# Gövde metninde yanlışlıkla KOD sanılmasın diye kara liste (tam UPPER kelimeler)
NOT_TICKERS = {
    "VE","VEYA","İLE","ILE","DÜN","DUN","BUGÜN","BUGUN","EKİM","EKIM","YURT","YER","SAHİP","SAHIP",
    "TL","USD","EURO","DOLAR","KDV","ADET","PAY","BİRİM","BIRIM","HİSSE","HISSE","KAP","FİNTABLES","FINTABLES"
}

API_KEY = os.getenv("API_KEY")
API_KEY_SECRET = os.getenv("API_KEY_SECRET")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
ACCESS_TOKEN_SECRET = os.getenv("ACCESS_TOKEN_SECRET")

def log(msg): print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def twitter_client():
    if not all([API_KEY, API_KEY_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET]):
        log("!! Twitter anahtarları yok → simülasyon modunda çalışacak")
        return None
    return tweepy.Client(
        consumer_key=API_KEY,
        consumer_secret=API_KEY_SECRET,
        access_token=ACCESS_TOKEN,
        access_token_secret=ACCESS_TOKEN_SECRET,
    )

# ===================== STATE =====================
def load_state():
    if not STATE_PATH.exists():
        return {"posted": [], "cooldown_until": None, "last_id": None}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            data = {"posted": data, "cooldown_until": None, "last_id": None}
        data.setdefault("posted", [])
        data.setdefault("cooldown_until", None)
        data.setdefault("last_id", None)
        return data
    except Exception as e:
        log(f"!! state.json okunamadı, sıfırlandı: {e}")
        return {"posted": [], "cooldown_until": None, "last_id": None}

def save_state(state):
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

state = load_state()
posted = set(state.get("posted", []))

def in_cooldown():
    cu = state.get("cooldown_until")
    if not cu: return False
    try:
        return datetime.now(timezone.utc) < datetime.fromisoformat(cu.replace("Z","+00:00"))
    except: 
        state["cooldown_until"] = None
        return False

# ===================== HELPERS =====================
def clean_content(text: str) -> str:
    t = re.sub(r"\s+", " ", text).strip()
    for p in STOP_PHRASES:
        t = re.sub(p, "", t, flags=re.I)
    t = REL_PREFIX.sub("", t)
    # baştaki gereksiz ayıraçlar
    t = t.strip(" .-–—|•·")
    # cümle sonuna nokta
    if t and t[-1] not in ".!?":
        t += "."
    return t

def build_tweet(codes, content):
    codes_str = " ".join(f"#{c}" for c in codes)
    body = clean_content(content)
    base = f"📰 {codes_str} | {body}"
    if len(base) <= 279: 
        return base
    # 279 sınırı içinde kırp
    head = f"📰 {codes_str} | "
    max_len = 279 - len(head) - 3
    clipped = body[:max_len]
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    return head + clipped + "..."

def is_company_news(item_text: str) -> bool:
    t = item_text.strip()
    # Fintables/Günlük Bülten vb atla
    if t.startswith("Fintables") or t.startswith("Günlük Bülten"):
        return False
    return True

def valid_code(tok: str) -> bool:
    if not re.fullmatch(r"[A-ZÇĞİÖŞÜ]{3,5}", tok): 
        return False
    return tok not in NOT_TICKERS

# ===================== BROWSER/EXTRACT =====================
JS_EXTRACT = """
() => {
  // Öne çıkanlar sekmesinin gerçekten aktif olduğundan emin değiliz;
  // bu kod sadece listeden KAP + mavi anchor içeren satırları toplar.
  const items = [];
  const main = document.querySelector('main') || document.body;
  const nodes = main.querySelectorAll('div, li, article, section');
  const isAllUpper = s => /^[A-ZÇĞİÖŞÜ]{3,5}$/.test(s || "");
  const NOTS = new Set(["VE","VEYA","İLE","ILE","DÜN","DUN","BUGÜN","BUGUN","EKİM","EKIM","YURT","YER","SAHİP","SAHIP","TL","USD","EURO","DOLAR","KDV","ADET","PAY","BİRİM","BIRIM","HİSSE","HISSE","KAP","FİNTABLES","FINTABLES"]);

  for (const el of nodes) {
    try {
      const txt = (el.innerText || "").replace(/\\s+/g," ").trim();
      if (!txt) continue;
      if (!/\\bKAP\\b/.test(txt)) continue;
      if (/^Fintables\\b/i.test(txt) || /^Günlük Bülten\\b/i.test(txt)) continue;

      // Yalnızca mavi link (anchor) içinde yazan kodlar
      const anchors = Array.from(el.querySelectorAll('a'));
      let codes = anchors.map(a => (a.textContent || "").trim().toUpperCase())
                         .filter(s => isAllUpper(s) && !NOTS.has(s));
      codes = Array.from(new Set(codes));
      if (codes.length === 0) continue;

      // Satırın tamamından anlamlı içerik üret (baş kısımdaki "KAP • CODE ..." şapkasını kırp)
      let raw = txt;
      // header'ı ilk nokta/iki nokta/pipe sonrası gövde olarak almayı dene
      let content = raw.replace(/^\\s*KAP\\s*[•·\\-\\.]?\\s*.+?\\s+/, "");
      // Çok kısaysa tüm metni kullan
      if (content.length < 25) content = raw;

      // benzersiz id: codes + ilk 120 karakter hash
      const h = Array.from((codes.join("-") + "|" + raw).slice(0,120))
                      .reduce((a,c)=>((a*31 + c.charCodeAt(0))>>>0),0);
      items.push({ id: `kap_${h}`, codes, content, raw });
    } catch {}
  }
  // En üste en yeni geliyor; öyle bırak
  return items;
}
"""

def click_highlights(page):
    # "Öne çıkanlar" kesin tıklansın
    sel_variants = [
        "button:has-text('Öne çıkanlar')",
        "a:has-text('Öne çıkanlar')",
        "[role=tab]:has-text('Öne çıkanlar')",
        "text=Öne çıkanlar"
    ]
    # buton görünür olana kadar bekle
    page.wait_for_load_state("domcontentloaded", timeout=30000)
    for _ in range(30):
        for s in sel_variants:
            try:
                loc = page.locator(s)
                if loc.count() and loc.first.is_visible():
                    loc.first.click()
                    # KAP etiketli satırlar görününceye kadar bekle
                    try:
                        page.wait_for_selector("main :text('KAP')", timeout=5000)
                    except:
                        pass
                    return True
            except:
                continue
        time.sleep(0.3)
    return False

def extract_items(page):
    page.wait_for_selector("main", timeout=20000)
    # bir miktar aşağı kaydır ki liste render olsun
    page.evaluate("window.scrollTo(0, 300)")
    page.wait_for_timeout(500)
    return page.evaluate(JS_EXTRACT)

# ===================== SEND TWEET =====================
def send_tweet(client, text):
    if not client:
        log(f"(SIM) {text}")
        return True
    try:
        client.create_tweet(text=text)
        return True
    except Exception as e:
        if "429" in str(e) or "Too Many Requests" in str(e):
            raise RuntimeError("RATE_LIMIT")
        log(f"Tweet hatası: {e}")
        return False

# ===================== MAIN =====================
def main():
    log("Başladı")
    if in_cooldown():
        log("Cooldown aktif, çıkılıyor")
        return

    tw = twitter_client()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=[
            "--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage","--disable-gpu"
        ])
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118 Safari/537.36",
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            viewport={"width":1280, "height":1024}
        )
        page = ctx.new_page()
        page.set_default_timeout(30000)

        # sayfayı yükle
        for i in range(3):
            try:
                log(f"Sayfa yükleme deneme {i+1}/3")
                page.goto(AKIS_URL, wait_until="networkidle")
                break
            except Exception as e:
                log(f"Yükleme hatası: {e}")
                if i==2: 
                    browser.close(); 
                    return
                time.sleep(2)

        # öne çıkanları tıkla (zorunlu)
        if click_highlights(page):
            log(">> Öne çıkanlar sekmesi açıldı")
        else:
            log("!! Öne çıkanlar butonu bulunamadı (Tümü'nden veri çekilmeyecek).")
            browser.close()
            return

        # öğeleri topla
        items = extract_items(page)
        log(f"Bulunan KAP haberleri: {len(items)}")

        if not items:
            browser.close(); 
            return

        # en yeni yukarıda; last_id varsa ona kadar al
        last_id = state.get("last_id")
        to_post = []
        for it in items:
            if last_id and it["id"] == last_id:
                break
            if not is_company_news(it["raw"]): 
                continue
            to_post.append(it)

        if not to_post:
            # yeni yoksa son id güncelle
            state["last_id"] = items[0]["id"]
            save_state(state)
            browser.close()
            log("Yeni haber yok")
            return

        # eskiden → yeniye sırala
        to_post = list(reversed(to_post))[:MAX_PER_RUN]
        sent = 0

        for it in to_post:
            if it["id"] in posted: 
                continue
            codes = [c for c in it["codes"] if valid_code(c)]
            if not codes: 
                continue

            tweet = build_tweet(codes, it["content"])
            log(f"Tweet: {tweet}")

            try:
                if send_tweet(tw, tweet):
                    posted.add(it["id"])
                    sent += 1
                    state["posted"] = sorted(list(posted))
                    state["last_id"] = items[0]["id"]  # en üstteki en yeni id
                    save_state(state)
                    if tw and sent < MAX_PER_RUN:
                        time.sleep(2)
            except RuntimeError as r:
                if str(r) == "RATE_LIMIT":
                    log(">> 429: Cooldown başlatıldı")
                    state["cooldown_until"] = (datetime.now(timezone.utc)+timedelta(minutes=COOLDOWN_MIN)).isoformat()
                    save_state(state)
                    break

        browser.close()
        log(f"Bitti. Gönderilen tweet: {sent}")

if __name__ == "__main__":
    main()
