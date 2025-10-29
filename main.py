import os, re, json, time
from pathlib import Path
from playwright.sync_api import sync_playwright
import tweepy

# ===== X (Twitter) =====
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

# ===== STATE =====
STATE_FILE = Path("state.json")
posted = set(json.loads(STATE_FILE.read_text())) if STATE_FILE.exists() else set()
def save_state():
    keep = sorted(list(posted))[-5000:]
    STATE_FILE.write_text(json.dumps(keep, ensure_ascii=False))

# ===== HELPERS =====
AKIS_URL = "https://fintables.com/borsa-haber-akisi?tab=featured"
UPPER_TR = "A-ZÇĞİÖŞÜ"
TICKER_RE = re.compile(rf"^[{UPPER_TR}]{{3,6}}[0-9]?$")
NON_NEWS = re.compile(r"(Fintables|G[üu]nl[üu]k\s*B[üu]lten|Bültenler?)", re.I)

def clean_text(t: str) -> str:
    if not t: return ""
    t = re.sub(r"\u00A0", " ", t)  # nbsp
    t = re.sub(r"\s+", " ", t).strip()
    # kaynak ve zaman kırp
    t = re.sub(r"\b(Fintables|KAP)\b\s*[·\.]?\s*", "", t, flags=re.I)
    t = re.sub(r"\b(Dün\s+\d{1,2}:\d{2}|\d{1,2}:\d{2}|Bugün|Az önce)\b", "", t, flags=re.I)
    return t.strip(" -–—:|•·")

def build_tweet(code: str, detail: str) -> str:
    base = clean_text(detail)
    if len(base) > 240: base = base[:240].rstrip() + "…"
    return (f"📰 #{code} | {base}")[:279]

def infinite_scroll(page, steps=6, pause_ms=250):
    for _ in range(steps):
        page.mouse.wheel(0, 1600)
        page.wait_for_timeout(pause_ms)

def ensure_featured(page):
    page.goto(AKIS_URL, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")
    # tıkla (aktif değilse de aktifleşsin)
    for sel in [
        "button:has-text('Öne çıkanlar')",
        "role=button[name='Öne çıkanlar']",
        "xpath=//button[contains(normalize-space(.),'Öne çıkanlar')]",
    ]:
        try:
            loc = page.locator(sel).first
            if loc and loc.count() > 0:
                loc.click(timeout=1500)
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(200)
                break
        except Exception:
            pass
    # içerik gelene kadar KAP etiketini bekle
    try:
        page.wait_for_selector("div.text-utility-02.text-fg-03", timeout=15000)
    except Exception:
        pass

# ---------- DOM: Aynı blokta KAP + Kod + Detay (yukarı doğru arama) ----------
DOM_JS = r"""
(() => {
  const norm = s => (s||"").replace(/\u00A0/g,' ').replace(/\s+/g,' ').trim();

  // detaydan sadece text node'ları topla
  const textOnly = el =>
    Array.from(el.childNodes).filter(n => n.nodeType === Node.TEXT_NODE)
      .map(n => n.textContent).join(' ');

  // KAP etiketinden yukarı doğru çıkıp aynı blokta kod+detay arayan yardımcı
  const findContainer = (el) => {
    let node = el;
    for (let i=0; i<6 && node; i++) {        // en fazla 6 seviye yukarı
      const codeEl = node.querySelector("span.text-shared-brand-01");
      const detailEl = node.querySelector("div.font-medium.text-body-sm");
      if (codeEl && detailEl) return { node, codeEl, detailEl };
      node = node.parentElement;
    }
    return null;
  };

  const kapTags = Array
    .from(document.querySelectorAll("div.text-utility-02.text-fg-03"))
    .filter(el => norm(el.textContent) === "KAP");

  const out = [];
  for (const kap of kapTags) {
    const pack = findContainer(kap);
    if (!pack) continue;

    const code = norm(pack.codeEl.textContent).toUpperCase();
    if (!/^[A-ZÇĞİÖŞÜ]{3,6}[0-9]?$/.test(code)) continue;

    const rawDetail = textOnly(pack.detailEl);
    const detail = norm(rawDetail);
    if (!detail || /Fintables|G[üu]nl[üu]k\s*B[üu]lten|Bültenler?/i.test(detail)) continue;

    out.push({ code, detail });
  }

  // uniq ve görünüm sırasını koru
  const seen = new Set();
  return out.filter(it => {
    const k = it.code + "|" + it.detail;
    if (seen.has(k)) return false;
    seen.add(k); return true;
  });
})()
"""

def fetch_featured_dom(page):
    ensure_featured(page)
    infinite_scroll(page, 6, 250)
    try:
        raw = page.evaluate(DOM_JS)
    except Exception:
        raw = []
    items = []
    for it in raw:
        code = it["code"].upper()
        if not TICKER_RE.fullmatch(code): continue
        detail = clean_text(it["detail"])
        if len(detail) < 8: continue
        rid = f"{code}-{hash(code+'|'+detail)}"
        items.append({"id": rid, "code": code, "snippet": detail})
    return items  # ekrandaki yeni→eski sıra

# ===== MAIN =====
def main():
    print(">> start (featured DOM robust)")
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

        items = fetch_featured_dom(page)
        if not items:
            print(">> no eligible rows"); browser.close(); return

        new_items = [it for it in items if it["id"] not in posted]
        if not new_items:
            print(">> nothing new to post"); browser.close(); return

        # Eskiden → yeniye
        new_items.reverse()

        sent = 0
        for it in new_items:
            text = build_tweet(it["code"], it["snippet"])
            print(">> TWEET:", text)
            if tw:
                try:
                    tw.create_tweet(text=text)
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
