import os, re, json, time, hashlib
from pathlib import Path
from playwright.sync_api import sync_playwright
import tweepy

URL = "https://fintables.com/borsa-haber-akisi?tab=featured"
STATE_PATH = Path("state.json")
MAX_TWEET_LEN = 279

# X (Twitter) secrets
API_KEY = os.environ["API_KEY"]
API_SECRET = os.environ["API_KEY_SECRET"]
ACCESS_TOKEN = os.environ["ACCESS_TOKEN"]
ACCESS_SECRET = os.environ["ACCESS_TOKEN_SECRET"]

# ---------------- state ----------------
def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"hashes": []}

def save_state(s):
    s["hashes"] = s["hashes"][-5000:]
    STATE_PATH.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")

def sha(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

# ---------------- helpers ----------------
TIME_PATTS = [
    re.compile(r"\s*(?:Dün|Bugün|Az önce|Saat)?\s*\d{1,2}[:.]\d{2}\s*$", re.I),
    re.compile(r"\s*\d{1,2}\s+[A-Za-zÇĞİÖŞÜçğıöşü]+\s+\d{1,2}[:.]\d{2}\s*$", re.I),
]
def strip_time(s: str) -> str:
    s = s.strip()
    for p in TIME_PATTS: s = p.sub("", s)
    return s.strip()

def format_tweet(code: str, text: str) -> str:
    prefix = f"📰 #{code} | "
    body = re.sub(r"\s+", " ", text).strip()
    room = MAX_TWEET_LEN - len(prefix)
    if len(body) > room:
        body = body[:room-1].rstrip() + "…"
    return prefix + body

def dismiss_banners(page):
    for sel in ["button:has-text('Kabul et')","button:has-text('Kabul')",
                "button:has-text('Accept')","button:has-text('Accept all')",
                "text=Kabul et"]:
        try:
            el = page.locator(sel).first
            if el and el.is_visible():
                el.click(timeout=800)
                page.wait_for_timeout(200)
                break
        except Exception:
            pass

# ---------------- core: robust DOM scrape in-page JS ----------------
JS_SCRAPE = r"""
(() => {
  // normalize helper
  const norm = (s) => (s||"")
    .replace(/\u00A0/g, " ")   // nbsp
    .replace(/\s+/g, " ")
    .trim();

  // tüm satır adayları: li, article, div (metin içerenler)
  const nodes = Array.from(document.querySelectorAll('li, article, div'))
    .filter(n => n.innerText && /KAP\s*[-–]/i.test(n.innerText));

  const out = [];
  for (const n of nodes) {
    const raw = norm(n.innerText);

    // Fintables özel/Günlük Bülten ele
    if (/Fintables|G[üu]nl[üu]k B[üu]lten|Bültenler/i.test(raw)) continue;
    if (!/KAP\s*[-–]/i.test(raw)) continue;

    // hisse kodu: önce mavi linklerden
    let code = null;
    const links = n.querySelectorAll("a[href*='/borsa/hisse/']");
    for (const a of links) {
      const t = norm(a.innerText).toUpperCase();
      if (/^[A-Z]{3,5}$/.test(t)) { code = t; break; }
    }
    // yedek: "KAP - KOD ..." paterninden
    if (!code) {
      const m = raw.match(/KAP\s*[-–]\s*([A-Z]{3,5})\b/i);
      if (m) code = m[1].toUpperCase();
    }
    if (!code) continue;

    // detay: "KAP - KOD" sonrası her şey
    let detail = raw.replace(new RegExp(`^[\\s\\S]*?KAP\\s*[-–]\\s*${code}\\s*`,`i`), "");
    detail = norm(detail).replace(/^[\-\|–—\s]+/,"");
    if (!detail) continue;

    out.push({ code, detail });
  }

  // uniq
  const seen = new Set();
  return out.filter(it => {
    const k = it.code + "|" + it.detail;
    if (seen.has(k)) return false;
    seen.add(k); return true;
  });
})()
"""

def scrape_once(page):
    # lazy load için bir miktar scroll
    for _ in range(6):
        page.mouse.wheel(0, 2200)
        page.wait_for_timeout(300)

    # görünür metinde 'KAP -' oluşana kadar bekle (maks 10 sn)
    try:
        page.wait_for_function(
            "document.body && document.body.innerText && /KAP\\s*[-–]/i.test(document.body.innerText)",
            timeout=10000
        )
    except Exception:
        pass

    items = page.evaluate(JS_SCRAPE)
    # eskiden yeniye
    items = list(dict.fromkeys([(i["code"], i["detail"]) for i in items]))
    return [{"code": c, "detail": d} for (c, d) in items][::-1]

def scrape():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            locale="tr-TR",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127 Safari/537.36"),
            viewport={"width": 1440, "height": 900},
        )
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page = ctx.new_page()

        results = []
        for attempt in range(3):  # sağlam retry
            page.goto(URL, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle")
            dismiss_banners(page)

            items = scrape_once(page)
            if items:
                results = items
                break
            # küçük gecikme ve tekrar
            page.wait_for_timeout(1200)

        browser.close()
        # saat/tarih temizliği ve son filtre
        cleaned = []
        for it in results:
            detail = strip_time(it["detail"])
            if not detail: continue
            cleaned.append({"code": it["code"], "detail": detail})
        return cleaned

# ---------------- twitter ----------------
def post_to_twitter(text: str):
    auth = tweepy.OAuth1UserHandler(API_KEY, API_SECRET, ACCESS_TOKEN, ACCESS_SECRET)
    api = tweepy.API(auth)
    api.update_status(status=text)

# ---------------- main ----------------
def main():
    state = load_state()
    items = scrape()

    to_post = []
    for it in items:
        h = sha(f"{it['code']}|{it['detail']}")
        if h not in state["hashes"]:
            to_post.append((h, it))

    posted = 0
    for h, it in to_post:
        tweet = format_tweet(it["code"], it["detail"])
        if len(tweet) < 10: continue
        post_to_twitter(tweet)
        state["hashes"].append(h)
        posted += 1
        time.sleep(2)

    save_state(state)
    print(f"Scanned {len(items)}, posted {posted}")

if __name__ == "__main__":
    main()
