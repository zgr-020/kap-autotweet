# main.py — Stabil sürüm: satır tabanlı + anchor yedekli, sağlam bekleme ve dump
import os, re, json, time, random
from pathlib import Path
from playwright.sync_api import sync_playwright
import tweepy

# ============== X (Twitter) Secrets ==============
API_KEY = os.getenv("API_KEY"); API_KEY_SECRET = os.getenv("API_KEY_SECRET")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN"); ACCESS_TOKEN_SECRET = os.getenv("ACCESS_TOKEN_SECRET")

def twitter_client():
    if not all([API_KEY, API_KEY_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET]):
        print("!! Twitter secrets missing, tweeting disabled"); return None
    return tweepy.Client(
        consumer_key=API_KEY, consumer_secret=API_KEY_SECRET,
        access_token=ACCESS_TOKEN, access_token_secret=ACCESS_TOKEN_SECRET,
    )

# ============== State ==============
STATE_PATH = Path("state.json")
def load_state():
    if not STATE_PATH.exists(): return {"last_id": None, "posted": []}
    try:
        data = json.loads(STATE_PATH.read_text())
        if isinstance(data, list): return {"last_id": None, "posted": data}
        data.setdefault("last_id", None); data.setdefault("posted", [])
        return data
    except Exception:
        return {"last_id": None, "posted": []}
def save_state(state): STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2))
state = load_state(); posted = set(state.get("posted", [])); last_id = state.get("last_id")

# ============== Ayarlar ==============
AKIS_URL = "https://fintables.com/borsa-haber-akisi"
MAX_PER_RUN = 5
SLEEP_BETWEEN_TWEETS = 15
RELOAD_RETRIES = 4

UPPER_TR = "A-ZÇĞİÖŞÜ"
TICKER_RE = re.compile(rf"^[{UPPER_TR}]{{3,6}}[0-9]?$")  # BIST kodu
BANNED_TAGS = {"KAP","FINTABLES","FİNTABLES","GÜNLÜK","BÜLTEN","BULTEN","GUNLUK","HABER"}
NON_NEWS_PATTERNS = [r"\bGünlük Bülten\b", r"\bBülten\b", r"\bPiyasa temkini\b", r"\bPiyasa değerlendirmesi\b"]
STOP_PHRASES = [r"işbu açıklama.*?amaçla", r"yatırım tavsiyesi değildir", r"kamunun bilgisine arz olunur",
                r"saygılarımızla", r"özel durum açıklaması", r"yatırımcılarımızın bilgisine"]
TIME_PATTERNS = [r"\b\d{1,2}:\d{2}\b", r"\bDün\s+\d{1,2}:\d{2}\b", r"\bBugün\b", r"\bAz önce\b"]
REL_PREFIX = re.compile(r'^(?:dün|bugün|yesterday|today)\b[:\-–]?\s*', re.IGNORECASE)

def strip_relative_prefix(t:str)->str: return REL_PREFIX.sub('', t).lstrip('-–: ').strip()
def clean_text(t:str)->str:
    t = re.sub(r"\s+"," ",(t or "")).strip()
    for p in STOP_PHRASES: t = re.sub(p,"",t,flags=re.I)
    for p in TIME_PATTERNS: t = re.sub(p,"",t,flags=re.I)
    t = re.sub(r"\b(Fintables|KAP)\b\s*[·\.]?\s*","",t,flags=re.I)
    return t.strip(" -–—:|•·")
REWRITE_MAP=[(r"\bbildirdi\b","duyurdu"),(r"\bbildirimi\b","açıklaması"),(r"\bilgisine\b","paylaştı"),
             (r"\bgerçekleştirdi\b","tamamladı"),(r"\bbaşladı\b","başlattı"),(r"\bdevam ediyor\b","sürdürülüyor")]
def rewrite_tr_short(s:str)->str:
    s=clean_text(s); s=re.sub(r"[“”\"']","",s); s=re.sub(r"\(\s*\)","",s)
    for pat,rep in REWRITE_MAP: s=re.sub(pat,rep,s,flags=re.I)
    s=re.sub(r"^\s*[-–—•·]\s*","",s); return s.strip()
def summarize(t:str,limit:int)->str:
    t=clean_text(t); 
    if len(t)<=limit: return t
    parts=re.split(r"(?<=[.!?])\s+",t); out=""
    for s in parts:
        if not s: continue
        cand=(out+" "+s).strip()
        if len(cand)>limit: break
        out=cand
    return out or t[:limit]
def build_tweet(code:str, snippet:str)->str:
    base=rewrite_tr_short(snippet); base=summarize(base,240); base=strip_relative_prefix(base)
    return (f"📰 #{code} | "+base)[:279]

def close_banners(page):
    for sel in ["button:has-text('Kabul')","button:has-text('Anladım')","button:has-text('Kapat')"]:
        try:
            btn=page.locator(sel)
            if btn.count(): btn.first.click(timeout=1200); page.wait_for_timeout(200)
        except: pass

def click_highlights(page):
    # 'Öne çıkanlar' a mutlaka geç
    for sel in ["button:has-text('Öne çıkanlar')","[role='tab']:has-text('Öne çıkanlar')",
                "a:has-text('Öne çıkanlar')","text=Öne çıkanlar"]:
        try:
            loc=page.locator(sel)
            if loc.count(): loc.first.click(timeout=1800); page.wait_for_timeout(600); print(">> highlights ON"); return
        except: pass
    print(">> highlights NOT found (continue on default tab)")

def is_vis(loc):
    try:
        box = loc.bounding_box()
        return box and box["width"]>0 and box["height"]>0
    except: return True
def safe_text(loc,timeout=900):
    try: return (loc.inner_text(timeout=timeout) or "").strip()
    except: return ""

# ——— 1) ESKİ/ÇALIŞAN YÖNTEM: satır tarayıp etiketten kod bul ———
ROW_SELECTORS = [
    "main li:visible", "main [role='listitem']:visible", "main article:visible",
    "main section:visible", "main div:visible"
]
def best_ticker_in_row(row)->str:
    # Önce /hisse/ anchor’ına bak
    try:
        a = row.locator("a[href^='/hisse/']")
        if a.count():
            href = a.first.get_attribute("href") or ""
            m = re.search(r"/hisse/([A-Za-zÇĞİÖŞÜçğıöşü0-9]{3,6})", href)
            if m:
                code = m.group(1).upper()
                if TICKER_RE.fullmatch(code) and code not in BANNED_TAGS:
                    return code
    except: pass
    # Sonra metin parçalarından bul
    anchors = row.locator("a, span, div, strong, b")
    cnt = 0
    try: cnt = min(60, max(0, anchors.count()))
    except: pass
    for j in range(cnt):
        tt = safe_text(anchors.nth(j)).upper()
        if not tt or tt in BANNED_TAGS: continue
        if TICKER_RE.fullmatch(tt): return tt
    return ""

def extract_rows_by_rowsel(page, max_scan=200):
    for sel in ROW_SELECTORS:
        loc = page.locator(sel)
        try: count = loc.count()
        except: count = 0
        if count == 0: continue
        count = min(max_scan, count)
        items=[]
        for i in range(count):  # en yeni → eski olma ihtimali yüksek
            row=loc.nth(i)
            if not is_vis(row): continue
            code=best_ticker_in_row(row)
            if not code: continue
            text = safe_text(row); 
            if not text: continue
            text_norm = re.sub(r"\s+"," ",text)
            if any(re.search(p,text_norm,flags=re.I) for p in NON_NEWS_PATTERNS): continue
            pos=text_norm.upper().find(code)
            snippet = text_norm[pos+len(code):].strip() if pos>=0 else text_norm
            snippet = clean_text(snippet)
            if len(snippet)<15: continue
            rid=f"{code}-{hash(text_norm)}"
            items.append({"id":rid,"code":code,"snippet":snippet})
        if items:
            print(f">> rows via '{sel}': {len(items)} eligible")
            return items
    print(">> row-based extraction yielded 0")
    return []

# ——— 2) YEDEK YÖNTEM: sayfa geneli anchor taraması ———
def extract_rows_by_anchor(page, max_scan=200):
    anchors = page.locator("a[href^='/hisse/']")
    try: cnt = anchors.count()
    except: cnt = 0
    cnt = min(max_scan, max(0,cnt))
    print(f">> hisse anchors found: {cnt}")
    seen=set(); items=[]
    for i in range(cnt):
        a=anchors.nth(i)
        try: href=a.get_attribute("href") or ""
        except: href=""
        m=re.search(r"/hisse/([A-Za-zÇĞİÖŞÜçğıöşü0-9]{3,6})", href)
        if not m: continue
        code=m.group(1).upper()
        if not TICKER_RE.fullmatch(code) or code in BANNED_TAGS: continue
        # Yakın kapsayıcıdan metin
        try:
            h = a.element_handle()
            container = h.evaluate_handle("""
              el => el.closest('article, li, [role=listitem], section, .card, .group, .feed-item, .flex, .grid') || el.parentElement
            """)
            txt = container.evaluate("(n)=> (n && n.innerText) ? n.innerText.trim() : ''")
        except:
            txt = safe_text(a)
        if not txt: continue
        text_norm=re.sub(r"\s+"," ",txt)
        if any(re.search(p,text_norm,flags=re.I) for p in NON_NEWS_PATTERNS): continue
        pos=text_norm.upper().find(code)
        snippet= text_norm[pos+len(code):].strip() if pos>=0 else text_norm
        snippet=clean_text(snippet)
        if len(snippet)<15: continue
        rid=f"{code}-{hash(text_norm)}"
        if rid in seen: continue
        seen.add(rid)
        items.append({"id":rid,"code":code,"snippet":snippet})
    print(f">> eligible via anchors: {len(items)}")
    return items

def dump_debug(page, prefix="debug"):
    try:
        page.screenshot(path=f"{prefix}.png", full_page=True)
        html = page.content()
        Path(f"{prefix}.html").write_text(html, encoding="utf-8")
        print(f">> dump saved: {prefix}.png / {prefix}.html")
    except Exception as e:
        print(">> dump failed:", e)

# ============== MAIN ==============
def main():
    print(">> entry", flush=True)
    tw = twitter_client()
    ua_pool = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
    ]
    ua=random.choice(ua_pool)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu",
                  "--disable-blink-features=AutomationControlled",
                  "--disable-features=site-per-process,IsolateOrigins"],
        )
        ctx = browser.new_context(
            user_agent=ua, locale="tr-TR", timezone_id="Europe/Istanbul",
            viewport={"width":1366,"height":900}, java_script_enabled=True,
            ignore_https_errors=True,
            extra_http_headers={"Accept-Language":"tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7"},
        )
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")

        page = ctx.new_page()
        page.set_default_timeout(30000); page.set_default_navigation_timeout(90000)

        items=[]
        for attempt in range(1, RELOAD_RETRIES+1):
            try:
                print(f">> load attempt {attempt}")
                page.goto(AKIS_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(800)
                close_banners(page)
                click_highlights(page)

                # 1) satır-temelli
                items = extract_rows_by_rowsel(page)
                if not items:
                    # 2) anchor yedeği
                    items = extract_rows_by_anchor(page)

                if items:
                    break
                # hala yoksa küçük scroll + tekrar
                page.mouse.wheel(0,800); page.wait_for_timeout(400)
                page.mouse.wheel(0,-800); page.wait_for_timeout(400)
            except Exception as e:
                print(f"!! page load error (attempt {attempt}): {e}")
                page.wait_for_timeout(1200)
                continue

        if not items:
            dump_debug(page, prefix="debug_noitems")
            print(">> done (no items)"); browser.close(); return

        newest_seen_id = items[0]["id"]

        # last_id’ye kadar olan “yeni”ler
        to_tweet=[]
        for it in items:
            if last_id and it["id"] == last_id: break
            to_tweet.append(it)

        if not to_tweet:
            print(">> no new items since last run")
            state["last_id"]=newest_seen_id; save_state(state)
            browser.close(); print(">> done"); return

        to_tweet = to_tweet[:MAX_PER_RUN]; to_tweet.reverse()  # eski→yeni sırayla at

        sent=0
        for it in to_tweet:
            if it["id"] in posted:
                print(">> already posted, skip"); continue
            tweet = build_tweet(it["code"], it["snippet"])
            print(">> TWEET:", tweet)
            try:
                if tw: tw.create_tweet(text=tweet)
                posted.add(it["id"]); sent += 1; print(">> tweet sent ✓")
                # anında state yaz
                state["posted"]=sorted(list(posted)); state["last_id"]=newest_seen_id; save_state(state)
                time.sleep(SLEEP_BETWEEN_TWEETS)
            except Exception as e:
                print("!! tweet error:", e); continue

        state["posted"]=sorted(list(posted)); state["last_id"]=newest_seen_id; save_state(state)
        browser.close(); print(f">> done (sent: {sent})")

if __name__=="__main__":
    try:
        main(); print(">> main() finished", flush=True)
    except Exception as e:
        import traceback; tb=traceback.format_exc()
        print("!! UNCAUGHT ERROR !!"); print(tb)
        try: Path("debug.log").write_text(tb+"\n", encoding="utf-8")
        except: pass
        raise
