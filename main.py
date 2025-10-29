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
DEBUG = os.getenv("DEBUG", "1") == "1"

TEXT_FIELDS = {"title","text","body","content","description","subtitle","summary","snippet","message","detail","headline"}
KAP_FIELDS  = {"source","label","labels","tag","tags","topics","category","categories","section","sections"}
SYM_FIELDS  = {"symbol","symbolCode","symbol_code","ticker","code","abbr","shortCode","short_code"}

def clean_text(t: str) -> str:
    if not t: return ""
    t = re.sub(r"\u00A0", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\b(Fintables|KAP)\b\s*[·\.]?\s*", "", t, flags=re.I)
    t = re.sub(r"\b(Dün\s+\d{1,2}:\d{2}|\d{1,2}:\d{2}|Bugün|Az önce)\b", "", t, flags=re.I)
    return t.strip(" -–—:|•·")

def build_tweet(code: str, detail: str) -> str:
    base = clean_text(detail)
    if len(base) > 240: base = base[:240].rstrip() + "…"
    return (f"📰 #{code} | {base}")[:279]

def infinite_scroll(page, steps=6, pause_ms=250):
    for _ in range(steps):
        page.mouse.wheel(0, 1800)
        page.wait_for_timeout(pause_ms)

# ----- JSON yardımcıları -----
def _iter_scalars(x):
    if isinstance(x, dict):
        for v in x.values(): yield from _iter_scalars(v)
    elif isinstance(x, list):
        for v in x: yield from _iter_scalars(v)
    else:
        yield x

def _strings_in(x):
    return [s for s in _iter_scalars(x) if isinstance(s, str)]

def _any_kap(x) -> bool:
    # Önce semantik alanlarda bak
    if isinstance(x, dict):
        for k, v in x.items():
            lk = k.lower()
            if lk in KAP_FIELDS:
                if isinstance(v, str) and "kap" in v.lower(): return True
                if isinstance(v, list) and any(isinstance(i,str) and "kap" in i.lower() for i in v): return True
        # yoksa stringlerde ara
    return any("kap" in s.lower() for s in _strings_in(x))

def _best_symbol(x):
    # Önce semantik alanlarda
    if isinstance(x, dict):
        for k, v in x.items():
            lk = k.lower()
            if lk in SYM_FIELDS:
                if isinstance(v, str) and TICKER_RE.fullmatch(v.upper()): return v.upper()
        for k, v in x.items():
            if isinstance(v, dict):
                s = _best_symbol(v)
                if s: return s
            if isinstance(v, list):
                for i in v:
                    s = _best_symbol(i)
                    if s: return s
    # Fallback: tüm stringlerde ara
    for s in _strings_in(x):
        m = re.findall(rf"\b([{UPPER_TR}]{{3,6}}[0-9]?)\b", s.upper())
        for c in m:
            if TICKER_RE.fullmatch(c) and c not in {"KAP","BULTEN","BÜLTEN"}:
                return c
    return None

def _best_detail(x):
    # Anlamsal alanlardan biri
    if isinstance(x, dict):
        for k, v in x.items():
            if k.lower() in TEXT_FIELDS and isinstance(v, str) and len(v) > 6:
                return v
        for k, v in x.items():
            if isinstance(v, dict):
                d = _best_detail(v)
                if d: return d
            if isinstance(v, list):
                for i in v:
                    d = _best_detail(i)
                    if d: return d
    # Fallback: en uzun anlamlı string
    cand = [s for s in _strings_in(x) if len(s) > 12]
    cand.sort(key=len, reverse=True)
    return cand[0] if cand else ""

def parse_item(item):
    if not _any_kap(item): 
        return None
    code = _best_symbol(item)
    if not code: 
        return None
    detail = _best_detail(item)
    if not detail or NON_NEWS.search(detail): 
        return None
    detail = clean_text(detail)
    if len(detail) < 8: 
        return None
    return {"id": f"{code}-{hash(code+'|'+detail)}", "code": code, "snippet": detail}

def walk_items(x, sink):
    if isinstance(x, dict):
        # dene: tekil item olabilir
        it = parse_item(x)
        if it: sink.append(it)
        for v in x.values(): walk_items(v, sink)
    elif isinstance(x, list):
        for v in x: walk_items(v, sink)

# ----- Featured feed dinleyici + debug dump -----
def fetch_featured_via_network(page):
    collected = []
    dump_count = 0

    def on_response(resp):
        nonlocal dump_count
        url = (resp.url or "").lower()
        if "topic-feed" not in url:
            return
        if "featured" not in url and "topic_tab=featured" not in url and "tab=featured" not in url:
            return
        try:
            ctype = (resp.headers or {}).get("content-type","").lower()
            if "json" not in ctype:
                return
            data = resp.json()
        except Exception:
            return

        # DEBUG: ham cevabı kaydet
        if DEBUG and dump_count < 3:
            Path(f"debug_featured_{dump_count+1}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
            dump_count += 1
            print(f"[debug] saved debug_featured_{dump_count}.json from {resp.url}")

        tmp = []
        walk_items(data, tmp)
        collected.extend(tmp)

    page.on("response", on_response)
    page.goto(AKIS_URL, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")
    infinite_scroll(page, 6, 250)

    # uniq yeni→eski
    uniq, seen = [], set()
    for it in collected:
        k = (it["code"], it["snippet"])
        if k in seen: continue
        seen.add(k); uniq.append(it)
    print(f"[debug] featured parsed items: {len(uniq)}")
    return uniq

# ===== MAIN =====
def main():
    print(">> start (featured network – field-based + debug)")
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

        items = fetch_featured_via_network(page)
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
