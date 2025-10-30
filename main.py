# main.py
import os, re, json, time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright
import tweepy

AKIS_URL = "https://fintables.com/borsa-haber-akisi"
STATE_PATH = Path("state.json")
MAX_PER_RUN = 5
COOLDOWN_MIN = 15

STOP_PHRASES = [
    "yatırım tavsiyesi değildir","yasal uyarı","kişisel veri","kvk","saygılarımızla","kamunun bilgisine"
]
REL_PREFIX = re.compile(r'^(?:dün|bugün|yesterday|today)\b[:\-–]?\s*', re.IGNORECASE)
NOT_TICKERS = {
    "VE","VEYA","İLE","ILE","DÜN","DUN","BUGÜN","BUGUN","EKİM","EKIM","YURT","YER","SAHİP","SAHIP",
    "TL","USD","EURO","DOLAR","KDV","ADET","PAY","BİRİM","BIRIM","HİSSE","HISSE","KAP",
    "FİNTABLES","FINTABLES","SEKTÖRLER","GÜNLÜK","BÜLTEN"
}

API_KEY = os.getenv("API_KEY")
API_KEY_SECRET = os.getenv("API_KEY_SECRET")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
ACCESS_TOKEN_SECRET = os.getenv("ACCESS_TOKEN_SECRET")

def log(msg): print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def twitter_client():
    if not all([API_KEY, API_KEY_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET]):
        log("!! Twitter anahtarları yok → simülasyon")
        return None
    return tweepy.Client(
        consumer_key=API_KEY, consumer_secret=API_KEY_SECRET,
        access_token=ACCESS_TOKEN, access_token_secret=ACCESS_TOKEN_SECRET
    )

def load_state():
    if not STATE_PATH.exists():
        return {"posted": [], "cooldown_until": None, "last_id": None}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list): data = {"posted": data, "cooldown_until": None, "last_id": None}
        for k,v in [("posted",[]),("cooldown_until",None),("last_id",None)]:
            data.setdefault(k, v)
        return data
    except Exception:
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
        state["cooldown_until"]=None; return False

def clean_content(text: str) -> str:
    t = re.sub(r"\s+", " ", text).strip()
    for p in STOP_PHRASES: t = re.sub(p, "", t, flags=re.I)
    t = REL_PREFIX.sub("", t).strip(" .-–—|•·")
    if t and t[-1] not in ".!?": t += "."
    return t

def build_tweet(codes, content):
    codes_str = " ".join(f"#{c}" for c in codes)
    body = clean_content(content)
    head = f"📰 {codes_str} | "
    if len(head + body) <= 279: return head + body
    max_len = 279 - len(head) - 3
    body = body[:max_len]
    if " " in body: body = body.rsplit(" ", 1)[0]
    return head + body + "..."

def valid_code(tok: str) -> bool:
    return bool(re.fullmatch(r"[A-ZÇĞİÖŞÜ]{3,5}", tok)) and tok not in NOT_TICKERS

# -------- robust highlights clicker --------
def click_highlights(page) -> bool:
    page.wait_for_load_state("domcontentloaded", timeout=30000)
    # sekme barı görünür olsun
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(300)

    texts = [
        "Öne çıkanlar","ÖNE ÇIKANLAR","Öne çıkanlar","One cikanlar",  # diakritik varyasyon
        "Öne Çıkanlar","ÖNE ÇIKANLAR"
    ]
    selectors = [
        "button:has-text('{}')", "a:has-text('{}')", "[role=tab]:has-text('{}')", "text={}"
    ]
    # 1) metin tabanlı denemeler
    for t in texts:
        for s in selectors:
            sel = s.format(t)
            try:
                loc = page.locator(sel)
                if loc.count() and loc.first.is_visible():
                    loc.first.click()
                    page.wait_for_timeout(300)
                    if is_highlights_active(page): return True
            except: pass

    # 2) XPath normalize-space
    try:
        loc = page.locator("xpath=//*[normalize-space(text())='Öne çıkanlar']")
        if loc.count(): loc.first.click(); page.wait_for_timeout(300)
        if is_highlights_active(page): return True
    except: pass

    # 3) header içinde arama (sağ üst küme)
    try:
        header = page.locator("main").locator("xpath=..").first
        btns = header.locator("xpath=.//a|.//button")
        n = btns.count()
        for i in range(min(n,30)):
            el = btns.nth(i)
            try:
                txt = el.inner_text(timeout=500).strip()
                if "Öne" in txt and "çıkan" in txt.lower():
                    el.click(); page.wait_for_timeout(300)
                    if is_highlights_active(page): return True
            except: pass
    except: pass

    # 4) doğrudan evaluate ile tıkla
    try:
        clicked = page.evaluate("""
        () => {
          const norm = s => (s||"").normalize('NFKD').replace(/[\\u0300-\\u036f]/g,'').toLowerCase();
          const want = norm('Öne çıkanlar');
          const all = [...document.querySelectorAll('a,button,div,span')];
          for (const el of all) {
            const t = norm(el.textContent);
            if (t.includes('one') && t.includes('cikanlar')) {
              el.click(); return true;
            }
          }
          return false;
        }
        """)
        if clicked:
            page.wait_for_timeout(300)
            if is_highlights_active(page): return True
    except: pass

    return False

def is_highlights_active(page) -> bool:
    # KAP etiketli satırlar görünür mü?
    try:
        page.wait_for_selector("main :text('KAP')", timeout=1500)
        # aynı hatta sağ üst segmentte 'Öne çıkanlar' aria-selected true olabilir
        try:
            active = page.locator("[aria-selected='true']").filter(has_text="Öne")
            if active.count(): return True
        except: pass
        return True
    except: 
        return False

# -------- extraction (mavi anchorlardan kod) --------
JS_EXTRACT = """
() => {
  const items = [];
  const root = document.querySelector('main') || document.body;
  const rows = root.querySelectorAll('div, li, article, section');
  const isAllUpper = s => /^[A-ZÇĞİÖŞÜ]{3,5}$/.test(s||"");
  const NOTS = new Set(%s);

  for (const el of rows) {
    try{
      const txt = (el.innerText||"").replace(/\\s+/g," ").trim();
      if (!txt || !/\\bKAP\\b/.test(txt)) continue;
      if (/^Fintables\\b/i.test(txt) || /^Günlük Bülten\\b/i.test(txt)) continue;

      const anchors = Array.from(el.querySelectorAll('a'));
      let codes = anchors.map(a => (a.textContent||"").trim().toUpperCase())
                         .filter(s => isAllUpper(s) && !NOTS.has(s));
      codes = Array.from(new Set(codes));
      if (codes.length===0) continue;

      let content = txt.replace(/^\\s*KAP\\s*[•·\\-\\.]?\\s*.+?\\s+/, "");
      if (content.length < 25) content = txt;

      const h = Array.from((codes.join("-")+"|"+txt).slice(0,120))
                     .reduce((a,c)=>((a*31 + c.charCodeAt(0))>>>0),0);
      items.push({id:`kap_${h}`, codes, content, raw:txt});
    }catch(e){}
  }
  return items;
}
""" % (json.dumps(list(NOT_TICKERS), ensure_ascii=False))

def extract_items(page):
    page.wait_for_selector("main", timeout=20000)
    page.evaluate("window.scrollTo(0, 240)")
    page.wait_for_timeout(400)
    return page.evaluate(JS_EXTRACT)

def send_tweet(client, text):
    if not client:
        log(f"(SIM) {text}")
        return True
    try:
        client.create_tweet(text=text); return True
    except Exception as e:
        if "429" in str(e) or "Too Many Requests" in str(e):
            raise RuntimeError("RATE_LIMIT")
        log(f"Tweet hatası: {e}"); return False

def main():
    log("Başladı")
    if in_cooldown():
        log("Cooldown aktif, çıkılıyor"); return
    tw = twitter_client()

    with sync_playwright() as pw:
        br = pw.chromium.launch(headless=True, args=[
            "--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage","--disable-gpu"
        ])
        ctx = br.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118 Safari/537.36",
            locale="tr-TR", timezone_id="Europe/Istanbul",
            viewport={"width":1280,"height":1024}
        )
        page = ctx.new_page(); page.set_default_timeout(30000)

        # sayfayı aç
        for i in range(3):
            try:
                log(f"Sayfa yükleme deneme {i+1}/3")
                page.goto(AKIS_URL, wait_until="networkidle"); break
            except Exception as e:
                log(f"Yükleme hatası: {e}")
                if i==2: br.close(); return
                time.sleep(2)

        # Öne çıkanlar
        if click_highlights(page):
            log(">> Öne çıkanlar sekmesi aktif")
        else:
            log("!! Öne çıkanlar butonu bulunamadı; iş sonlandırıldı")
            br.close(); return

        items = extract_items(page)
        log(f"Bulunan KAP haberleri: {len(items)}")
        if not items: br.close(); return

        last_id = state.get("last_id")
        to_post = []
        for it in items:
            if last_id and it["id"] == last_id: break
            if it["id"] in posted: continue
            to_post.append(it)

        if not to_post:
            state["last_id"] = items[0]["id"]; save_state(state)
            br.close(); log("Yeni haber yok"); return

        to_post = list(reversed(to_post))[:MAX_PER_RUN]
        sent = 0
        for it in to_post:
            codes = [c for c in it["codes"] if valid_code(c)]
            if not codes: continue
            tweet = build_tweet(codes, it["content"])
            log(f"Tweet: {tweet}")
            try:
                if send_tweet(tw, tweet):
                    posted.add(it["id"]); sent += 1
                    state["posted"] = sorted(list(posted))
                    state["last_id"] = items[0]["id"]
                    save_state(state)
                    if tw and sent < MAX_PER_RUN: time.sleep(2)
            except RuntimeError:
                log(">> 429 alındı; cooldown başlatıldı")
                state["cooldown_until"] = (datetime.now(timezone.utc)+timedelta(minutes=COOLDOWN_MIN)).isoformat()
                save_state(state); break

        br.close(); log(f"Bitti. Gönderilen tweet: {sent}")

if __name__ == "__main__":
    main()
