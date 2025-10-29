# main.py
import os, re, json, time, hashlib
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Set

from playwright.sync_api import sync_playwright
import tweepy

# ===================== CONFIG =====================
AKIS_URL = "https://fintables.com/borsa-haber-akisi"
MAX_PER_RUN = 5
REQUEST_TIMEOUT = 30000  # ms
COOLDOWN_MIN = 15        # 429 sonrası bekleme
STATE_PATH = Path("state.json")

# X (Twitter) secrets (repo secrets)
API_KEY = os.getenv("API_KEY")
API_KEY_SECRET = os.getenv("API_KEY_SECRET")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
ACCESS_TOKEN_SECRET = os.getenv("ACCESS_TOKEN_SECRET")

# ===================== LOG =====================
def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

# ===================== STATE =====================
def load_state() -> Dict:
    if not STATE_PATH.exists():
        return {"last_id": None, "posted": [], "cooldown_until": None}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):  # eski biçim
            data = {"last_id": None, "posted": data, "cooldown_until": None}
        data.setdefault("last_id", None)
        data.setdefault("posted", [])
        data.setdefault("cooldown_until", None)
        return data
    except Exception:
        return {"last_id": None, "posted": [], "cooldown_until": None}

def save_state(st: Dict):
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")

state = load_state()
posted: Set[str] = set(state.get("posted", []))
last_id: Optional[str] = state.get("last_id")

# ===================== TWITTER =====================
def twitter_client() -> Optional[tweepy.Client]:
    if not all([API_KEY, API_KEY_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET]):
        log("!! Twitter secrets missing → simulation mode (tweet atılmaz)")
        return None
    try:
        return tweepy.Client(
            consumer_key=API_KEY,
            consumer_secret=API_KEY_SECRET,
            access_token=ACCESS_TOKEN,
            access_token_secret=ACCESS_TOKEN_SECRET,
        )
    except Exception as e:
        log(f"!! Twitter client init failed: {e} → simulation mode")
        return None

# ===================== UTIL =====================
TR_UPPER = "A-ZÇĞİÖŞÜ"
CODE_RE = re.compile(rf"^[{TR_UPPER}]{{3,5}}[0-9]?$")
BAN_WORDS = {
    # kod değil, sık gelen kelimeler
    "KAP","FINTABLES","FINtables","BULTEN","GUNLUK","GÜNLÜK","BÜLTEN",
    "ADET","TL","MİLYON","MILYON","YUZDE","YÜZDE","PAY","HISSE","ŞIRKET","ŞİRKET",
    "YER","YURT","DUN","BUGUN","BUGÜN","YARIN","GUN","GÜN","SAAT","UYESI","ÜYESI","ÜYESİ",
    # ay/gün
    "OCAK","SUBAT","ŞUBAT","MART","NISAN","NİSAN","MAYIS","HAZIRAN","TEMMUZ",
    "AGUSTOS","AĞUSTOS","EYLUL","EYLÜL","EKIM","EKİM","KASIM","ARALIK",
}

def normalize_code(s: str) -> str:
    return (s or "").upper().replace("İ","I").replace("Ş","S").replace("Ğ","G").replace("Ç","C").replace("Ö","O").replace("Ü","U")

def build_tweet(codes: List[str], content: str) -> str:
    codes_str = " ".join([f"#{c}" for c in codes])
    t = re.sub(r"\s+", " ", content or "").strip()

    # gereksiz önek/ekleri temizle
    t = re.sub(r"^\s*(Şirket|Sirket)\s*", "", t, flags=re.I)
    # saat/tarih, 'Dün', 'Bugün' vb. temizle
    t = re.sub(r"\b(?:Dün|Bugün|Yesterday|Today)\b.*?$", "", t, flags=re.I)
    t = re.sub(r"\b\d{1,2}:\d{2}\b", "", t)

    # ilk cümleyi al, çok kısaysa genişlet
    parts = re.split(r"(?<=[\.\!\?])\s+", t)
    sentence = parts[0].strip() if parts and parts[0].strip() else t
    if len(sentence) < 40 and len(parts) > 1:
        sentence = (sentence + " " + parts[1]).strip()
    if not sentence.endswith((".", "!", "?")):
        sentence += "."

    # 279 sınırı
    base = f"📰 {codes_str} | {sentence}"
    if len(base) <= 279:
        return base
    cut = 279 - len(f"📰 {codes_str} | ...")
    sentence = sentence[:cut].rsplit(" ", 1)[0] + "..."
    return f"📰 {codes_str} | {sentence}"

# ===================== JS EXTRACTOR =====================
JS = """
() => {
  // 'Öne çıkanlar' tabındaki satırlardan KAP haberlerini döndür.
  const isVisible = el => !!(el && el.offsetParent !== null);
  const rows = Array.from(document.querySelectorAll('main li, main [role="listitem"], main article, main div'))
    .filter(isVisible)
    .slice(0, 400);

  const items = [];
  for (const row of rows) {
    const text = (row.innerText || '').replace(/\\s+/g, ' ').trim();
    if (!text) continue;

    // Fintables analizi vs. hariç
    if (/^\\s*Fintables\\b/i.test(text)) continue;

    // 'KAP' içermeyenler hariç
    if (!/\\bKAP\\b/i.test(text)) continue;

    // satırdaki muhtemel "rozet" / tag alanlarından kod topla
    const tags = Array.from(row.querySelectorAll('a, span, div, button'))
      .map(el => (el.innerText || '').trim())
      .filter(Boolean);

    let codes = [];
    for (const tag of tags) {
      const t = tag.toUpperCase().replace('İ','I').replace('Ş','S').replace('Ğ','G').replace('Ç','C').replace('Ö','O').replace('Ü','U');
      // TERA/BVSAN gibi ikili yazımlar
      const parts = t.split(/[\\/\\s]+/).filter(Boolean);
      for (const p of parts) {
        if (/^[A-Z]{3,5}[0-9]?$/.test(p)) codes.push(p);
      }
    }
    // benzersiz sırayla
    const uniq = [];
    for (const c of codes) if (!uniq.includes(c)) uniq.push(c);

    // içerik: satır metni, ama baştaki KAP ve kod rozetleri atılacak
    let content = text;
    content = content.replace(/^\\s*KAP\\b\\s*[•·\\-\\.]?\\s*/i, '');
    for (const c of uniq) {
      const r = new RegExp('^\\s*' + c + '\\b\\s*', 'i');
      content = content.replace(r, '');
    }
    content = content.trim();

    // sağdaki saat/tarih ID için kullanılabilir
    let timeMatch = text.match(/\\b(?:Dün|Bugün)\\s*\\d{1,2}:\\d{2}\\b|\\b\\d{1,2}:\\d{2}\\b/);
    const timeStr = timeMatch ? timeMatch[0] : '';

    // benzersiz id
    const h = (s) => {
      let x = 0; for (let i=0;i<s.length;i++) x = (x*31 + s.charCodeAt(i)) >>> 0; return x.toString(16);
    };
    const id = (uniq[0] || 'KAP') + '-' + h(text + timeStr);

    if (content && uniq.length > 0) {
      items.push({ id, codes: uniq.slice(0,2), content });
    }
  }
  return items;
}
"""

# ===================== SCRAPE & TWEET =====================
def scrape_items() -> List[Dict]:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage","--disable-gpu"]
        )
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/118.0.0.0 Safari/537.36"),
            locale="tr-TR",
            timezone_id="Europe/Istanbul",
            viewport={"width": 1440, "height": 900}
        )
        page = ctx.new_page()
        page.set_default_timeout(REQUEST_TIMEOUT)

        log("goto…")
        page.goto(AKIS_URL, wait_until="domcontentloaded")
        page.wait_for_selector("main", timeout=REQUEST_TIMEOUT)
        # 'Öne çıkanlar'
        for sel in [
            "button:has-text('Öne çıkanlar')",
            "[role='tab']:has-text('Öne çıkanlar')",
            "a:has-text('Öne çıkanlar')",
            "text=Öne çıkanlar",
        ]:
            try:
                if page.locator(sel).first.is_visible():
                    page.click(sel)
                    page.wait_for_timeout(800)
                    log("highlights ON")
                    break
            except:
                pass

        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(800)

        try:
            items = page.evaluate(JS)
        except Exception as e:
            log(f"!! JS eval failed: {e}")
            items = []

        browser.close()
        return items

def filter_codes(codes: List[str]) -> List[str]:
    out = []
    for c in codes:
        cc = normalize_code(c)
        if cc in BAN_WORDS: 
            continue
        if CODE_RE.fullmatch(cc):
            out.append(cc)
    # benzersiz ve en fazla 2
    seen = set()
    res = []
    for c in out:
        if c not in seen:
            seen.add(c); res.append(c)
    return res[:2]

def main():
    log("start")

    # cooldown kontrol
    if state.get("cooldown_until"):
        try:
            cd = datetime.fromisoformat(state["cooldown_until"].replace("Z", "+00:00"))
            if datetime.now(timezone.utc) < cd:
                log(f"cooldown active → exits until {cd.isoformat()}")
                return
        except Exception:
            state["cooldown_until"] = None

    items = scrape_items()
    log(f"extracted: {len(items)}")

    if not items:
        log("no items")
        return

    # en yeni ilk olsun
    newest_id = items[0]["id"]

    # son çalışma sonrası yeni gelenleri sırala
    to_tweet = []
    for it in items:
        if last_id and it["id"] == last_id:
            break
        to_tweet.append(it)

    if not to_tweet:
        state["last_id"] = newest_id
        save_state(state)
        log("no new items")
        return

    # en eskiden yeniye
    to_tweet = to_tweet[:MAX_PER_RUN]
    to_tweet.reverse()

    tw = twitter_client()
    sent = 0

    for it in to_tweet:
        if it["id"] in posted:
            continue

        codes = filter_codes(it.get("codes", []))
        if not codes:
            log("skip: no valid codes")
            continue

        tweet = build_tweet(codes, it.get("content",""))
        log(f"TWEET → {tweet}")

        try:
            if tw:
                tw.create_tweet(text=tweet)
            posted.add(it["id"]); sent += 1
            state["posted"] = sorted(list(posted))
            state["last_id"] = newest_id
            save_state(state)
            time.sleep(3)  # güvenli aralık
        except Exception as e:
            msg = str(e)
            log(f"tweet error: {msg}")
            if "429" in msg or "Too Many Requests" in msg:
                cd = (datetime.now(timezone.utc) + timedelta(minutes=COOLDOWN_MIN)).isoformat()
                state["cooldown_until"] = cd
                save_state(state)
                log(f"cooldown set for {COOLDOWN_MIN} minutes")
                break

    # son görülen id’yi güncelle
    state["last_id"] = newest_id
    save_state(state)
    log(f"done (sent: {sent})")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        log("!! FATAL ERROR")
        log(traceback.format_exc())
        raise
