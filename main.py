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
    t = re.sub(r"\u00A0", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\b(Fintables|KAP)\b\s*[·\.]?\s*", "", t, flags=re.I)
    t = re.sub(r"\b(Dün\s+\d{1,2}:\d{2}|\d{1,2}:\d{2}|Bugün|Az önce)\b", "", t, flags=re.I)
    return t.strip(" -–—:|•·")

def build_tweet(code: str, detail: str) -> str:
    base = clean_text(detail)
    if len(base) > 240: base = base[:240].rstrip() + "…"
    return (f"📰 #{code} | {base}")[:279]

def infinite_scroll(page, steps=5, pause_ms=250):
    for _ in range(steps):
        page.mouse.wheel(0, 1800)
        page.wait_for_timeout(pause_ms)

# ---- JSON içinden ITEM bazlı güvenli çıkarım ----
TEXT_FIELDS = {"title","text","body","content","description","subtitle","summary","snippet","message","detail"}

def _collect_strings(x):
    """dict/list içindeki TÜM string alanları düz bir listede döndür (debug/regex için)."""
    out = []
    if isinstance(x, dict):
        for v in x.values(): out.extend(_collect_strings(v))
    elif isinstance(x, list):
        for v in x: out.extend(_collect_strings(v))
    elif isinstance(x, str):
        out.append(x)
    return out

def _best_text_from_item(d):
    """Item dict'inde anlamlı metni veren alan(lar)ı seç."""
    # 1) isimli alanlar
    for k, v in d.items():
        if k.lower() in TEXT_FIELDS and isinstance(v, str) and len(v) > 6:
            return v
    # 2) alt sözlüklerde aynı adlara bakalım
    for k, v in d.items():
        if isinstance(v, dict):
            for kk, vv in v.items():
                if kk.lower() in TEXT_FIELDS and isinstance(vv, str) and len(vv) > 6:
                    return vv
    # 3) fallback: toplanmış stringlerden en uzunu
    strings = [s for s in _collect_strings(d) if len(s) > 6]
    strings.sort(key=len, reverse=True)
    return strings[0] if strings else ""

def parse_item_if_kap(item):
    """
    Bir JSON 'item' nesnesi içinde aynı anda:
      - 'KAP' etiketine dair bir string
      - Geçerli TICKER
      - Anlamlı bir metin (detay)
    varsa {"code", "snippet"} döndürür.
    """
    strings = _collect_strings(item)
    big = " ".join(strings)

    if "KAP" not in big and "Kap" not in big and "kap" not in big:
        return None  # item KAP değil

    # Ticker adaylarını item içindeki stringlerden çıkar
    codes = []
    for s in strings:
        # "KOD", "KAP - KOD", "[KOD]" vb. yakalar
        m_all = re.findall(rf"\b([{UPPER_TR}]{{3,6}}[0-9]?)\b", s.upper())
        for c in m_all:
            if TICKER_RE.fullmatch(c) and c not in ("KAP", "BULTEN", "BÜLTEN"):
                codes.append(c)
    codes = [c for c in codes if c != "KAP"]
    if not codes:
        return None
    # Aynı item içinde en çok tekrar eden/ilk görüneni al
    code = codes[0]

    # Detay
    detail = _best_text_from_item(item)
    if not detail or NON_NEWS.search(detail):
        return None

    detail = clean_text(detail)
    if len(detail) < 8:
        return None

    rid = f"{code}-{hash(code+'|'+detail)}"
    return {"id": rid, "code": code, "snippet": detail}

def fetch_featured_via_network(page):
    """
    Sadece featured topic-feed çağrılarını dinler, her JSON item'ını
    AYRI AYRI parse eder (string “flatteing” yok → karışma yok).
    """
    collected = []

    def on_response(resp):
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

        # Item dizisi farklı anahtarlarla gelebilir; tüm dict/list içinde dolaş
        stack = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                # "items", "data", "entries", "results" vb.
                for k, v in cur.items():
                    if isinstance(v, list) and v and all(isinstance(x, (dict, list)) for x in v):
                        stack.append(v)
                # Tekil item gibi görünen dict'leri de deneyelim
                maybe = parse_item_if_kap(cur)
                if maybe:
                    collected.append(maybe)
            elif isinstance(cur, list):
                for v in cur:
                    if isinstance(v, (dict, list)):
                        stack.append(v)
                    else:
                        # basit tipleri atla
                        pass

    page.on("response", on_response)

    # sayfayı aç + network tetikle
    page.goto(AKIS_URL, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")
    infinite_scroll(page, 6, 250)

    # uniq yeni→eski
    uniq, seen = [], set()
    for it in collected:
        k = (it["code"], it["snippet"])
        if k in seen: continue
        seen.add(k); uniq.append(it)
    return uniq

# ===== MAIN =====
def main():
    print(">> start (featured network-only, item-safe)")
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

        # state filtresi
        new_items = [it for it in items if it["id"] not in posted]
        if not new_items:
            print(">> nothing new to post"); browser.close(); return

        # Eskiden → yeniye (timeline tutarlı)
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
