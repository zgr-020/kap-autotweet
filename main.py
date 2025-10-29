import os, re, json, time
from pathlib import Path
from playwright.sync_api import sync_playwright
import tweepy

# -------- X (Twitter) secrets --------
API_KEY = os.getenv("API_KEY")
API_KEY_SECRET = os.getenv("API_KEY_SECRET")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
ACCESS_TOKEN_SECRET = os.getenv("ACCESS_TOKEN_SECRET")

def twitter_client():
    if not all([API_KEY, API_KEY_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET]):
        print("!! Twitter secrets missing; tweets will be skipped")
        return None
    return tweepy.Client(
        consumer_key=API_KEY,
        consumer_secret=API_KEY_SECRET,
        access_token=ACCESS_TOKEN,
        access_token_secret=ACCESS_TOKEN_SECRET,
    )

# -------- State (avoid duplicates) --------
STATE_FILE = Path("state.json")
posted = set(json.loads(STATE_FILE.read_text())) if STATE_FILE.exists() else set()
def save_state():
    STATE_FILE.write_text(json.dumps(sorted(list(posted))[-5000:], ensure_ascii=False))

# -------- Source & helpers --------
FOREKS_URL = "https://www.foreks.com/analizler/piyasa-analizleri/sirket"
UPPER_TR = "A-ZÇĞİÖŞÜ"
TICKER_RE = re.compile(rf"^[{UPPER_TR}]{{3,6}}$")  # 3–6 harfli BIST kodu

def clean_text(t: str) -> str:
    if not t: return ""
    t = re.sub(r"\u00A0", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t.strip(" -–—:|•·")

def build_tweet(code: str, detail: str) -> str:
    base = clean_text(detail)
    if len(base) > 240: base = base[:240].rstrip() + "…"
    return (f"📰 #{code} | {base}")[:279]

def infinite_scroll(page, steps=4, pause_ms=250):
    for _ in range(steps):
        page.mouse.wheel(0, 1400)
        page.wait_for_timeout(pause_ms)

def dismiss_popups(page):
    # Cookie / bildirim / reklam kapat
    selectors = [
        "button:has-text('Kabul')",
        "button:has-text('Kabul Et')",
        "button:has-text('Tamam')",
        "button:has-text('Kapat')",
        "text=Anladım"
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.count():
                el.click(timeout=800)
                page.wait_for_timeout(200)
        except Exception:
            pass

# ---- DOM extraction: same row → rightmost ticker + headline ----
DOM_JS = r"""
(() => {
  const norm = s => (s||"").replace(/\u00A0/g,' ').replace(/\s+/g,' ').trim();
  const isTicker = s => /^[A-ZÇĞİÖŞÜ]{3,6}$/.test(s);

  // Satırları topla (liste öğeleri / kartlar)
  const rows = new Set();
  for (const a of document.querySelectorAll('a[href*="/analizler/piyasa-analizleri/"]')) {
    const row = a.closest("li") || a.closest("article") || a.closest("div");
    if (row) rows.add(row);
  }

  const items = [];
  for (const row of rows) {
    // Başlık
    let titleEl = row.querySelector('a[href*="/analizler/piyasa-analizleri/"]');
    let detail = titleEl ? norm(titleEl.textContent) : "";
    if (!detail || detail.length < 6) {
      const candidate = Array.from(row.querySelectorAll("h1,h2,h3,div,span"))
        .map(el => norm(el.textContent)).find(t => t && t.length > 6);
      if (candidate) detail = candidate;
    }

    // Kod: satırdaki en SAĞDA görünen kısa büyük harf etiketi
    const cands = [];
    for (const el of row.querySelectorAll("a,span,div")) {
      const txt = norm(el.textContent).toUpperCase();
      if (!txt || txt.length > 8) continue;
      if (!isTicker(txt)) continue;
      const r = el.getBoundingClientRect();
      cands.push({ txt, x: r.right });
    }
    if (!cands.length) continue;
    cands.sort((a,b) => b.x - a.x);
    const code = cands[0].txt;

    if (!detail || detail.length < 6) continue;
    items.push({ code, detail });
  }

  // benzersiz
  const seen = new Set(), out = [];
  for (const it of items) {
    const k = it.code + "|" + it.detail;
    if (seen.has(k)) continue;
    seen.add(k); out.push(it);
  }
  return out; // sayfadaki görünüm sırası (yeni→eski)
})()
"""

def fetch_foreks_rows(page):
    page.goto(FOREKS_URL, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")
    dismiss_popups(page)
    infinite_scroll(page, 5, 250)
    try:
        raw = page.evaluate(DOM_JS)
    except Exception:
        raw = []
    items = []
    for it in raw:
        code = it["code"].upper()
        if not TICKER_RE.fullmatch(code): 
            continue
        detail = clean_text(it["detail"])
        if len(detail) < 8:
            continue
        rid = f"{code}-{hash(code+'|'+detail)}"
        items.append({"id": rid, "code": code, "snippet": detail})
    return items

# -------- Main --------
def main():
    print(">> start (Foreks BIST Şirketleri)")
    tw = twitter_client()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-gpu","--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
            locale="tr-TR", timezone_id="Europe/Istanbul",
        )
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page = ctx.new_page(); page.set_default_timeout(30000)

        items = fetch_foreks_rows(page)
        if not items:
            print(">> no eligible rows"); browser.close(); return

        # yeni olanları seç
        new_items = [it for it in items if it["id"] not in posted]
        if not new_items:
            print(">> nothing new to post"); browser.close(); return

        # Eskiden → yeniye gönder
        new_items.reverse()

        sent = 0
        for it in new_items:
            tweet = build_tweet(it["code"], it["snippet"])
            print(">> TWEET:", tweet)
            if tw:
                try:
                    tw.create_tweet(text=tweet)
                    print(">> tweet sent ✓")
                except Exception as e:
                    print("!! tweet error:", e); continue
            posted.add(it["id"]); save_state()
            sent += 1
            time.sleep(2)

        browser.close()
        print(f">> done (posted: {sent})")

if __name__ == "__main__":
    main()
