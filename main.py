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
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\b(Fintables|KAP)\b\s*[·\.]?\s*", "", t, flags=re.I)
    t = re.sub(r"\b(Dün\s+\d{1,2}:\d{2}|\d{1,2}:\d{2}|Bugün|Az önce)\b", "", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip(" -–—:|•·")
    return t

def build_tweet(code: str, detail: str) -> str:
    base = clean_text(detail)
    if len(base) > 240: base = base[:240].rstrip() + "…"
    return (f"📰 #{code} | {base}")[:279]

def infinite_scroll(page, steps=6, pause_ms=300):
    for _ in range(steps):
        page.mouse.wheel(0, 1800)
        page.wait_for_timeout(pause_ms)

# ---------- 1) Network üzerinden yakala ----------
def extract_from_string(s: str):
    if NON_NEWS.search(s): return None
    m = re.search(r"\bKAP\s*[-–]\s*([A-ZÇĞİÖŞÜ]{3,6}[0-9]?)\b", s)
    if not m: return None
    code = m.group(1).upper()
    if not TICKER_RE.fullmatch(code): return None
    # 'KAP - KOD' sonrası
    after = re.split(rf"KAP\s*[-–]\s*{re.escape(code)}\s*", s, flags=re.I, maxsplit=1)
    detail = clean_text(after[1] if len(after) == 2 else s)
    if len(detail) < 8: return None
    return {"id": f"{code}-{hash(s)}", "code": code, "snippet": detail}

def walk_json(x, bag):
    if isinstance(x, dict):
        for v in x.values(): walk_json(v, bag)
    elif isinstance(x, list):
        for v in x: walk_json(v, bag)
    elif isinstance(x, str):
        if "KAP" in x: bag.append(re.sub(r"\s+", " ", x).strip())

def fetch_via_network(page):
    captured = []
    def on_response(resp):
        url = (resp.url or "").lower()
        if "topic-feed" not in url: return
        try:
            ctype = resp.headers.get("content-type","").lower()
            if "json" in ctype:
                data = resp.json(); tmp = []; walk_json(data, tmp); captured.extend(tmp)
            else:
                txt = resp.text(); 
                if txt: captured.extend(txt.splitlines())
        except Exception:
            pass
    page.on("response", on_response)

    page.goto(AKIS_URL, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")
    infinite_scroll(page, 6, 300)

    items = []
    for s in captured:
        it = extract_from_string(s)
        if it: items.append(it)

    # uniq yeni→eski
    uniq, seen = [], set()
    for it in items:
        k = (it["code"], it["snippet"])
        if k in seen: continue
        seen.add(k); uniq.append(it)
    return uniq

# ---------- 2) DOM üzerinden (senin sınıflarla) ----------
DOM_JS = r"""
(() => {
  const norm = s => (s||"").replace(/\u00A0/g,' ').replace(/\s+/g,' ').trim();
  const rows = Array.from(document.querySelectorAll("li, div"));

  const out = [];
  for (const r of rows) {
    const kap = r.querySelector("div.text-utility-02.text-fg-03");
    const codeEl = r.querySelector("span.text-shared-brand-01");
    if (!kap || !codeEl) continue;
    if (norm(kap.textContent) !== "KAP") continue;

    const code = norm(codeEl.textContent).toUpperCase();
    if (!/^[A-ZÇĞİÖŞÜ]{3,6}[0-9]?$/.test(code)) continue;

    const d = r.querySelector("div.font-medium.text-body-sm");
    if (!d) continue;
    // sadece text node'ları al (button/svg hariç)
    const detail = norm(Array.from(d.childNodes)
      .filter(n => n.nodeType === Node.TEXT_NODE)
      .map(n => n.textContent).join(" "));
    if (!detail || /Fintables|G[üu]nl[üu]k\s*B[üu]lten|Bültenler?/i.test(detail)) continue;

    out.push({ code, detail });
  }

  // uniq yeni→eski
  const seen = new Set();
  return out.filter(it => {
    const k = it.code + "|" + it.detail;
    if (seen.has(k)) return false;
    seen.add(k); return true;
  });
})()
"""

def fetch_via_dom(page):
    page.goto(AKIS_URL, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")
    infinite_scroll(page, 6, 300)
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
        items.append({"id": f"{code}-{hash(code+'|'+detail)}", "code": code, "snippet": detail})
    return items

# ===== MAIN =====
def main():
    print(">> start (hybrid featured)")
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

        items = fetch_via_network(page)
        if not items:
            items = fetch_via_dom(page)

        if not items:
            print(">> no eligible rows"); browser.close(); return

        new_items = [it for it in items if it["id"] not in posted]
        if not new_items:
            print(">> nothing new to post"); browser.close(); return

        new_items.reverse()  # eskiden → yeniye

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
