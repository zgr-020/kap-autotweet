import os, re, json, time, logging, base64
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from playwright.sync_api import sync_playwright
import tweepy
from tweepy import TooManyRequests

AKIS_URL = "https://fintables.com/borsa-haber-akisi"
STATE_PATH = Path("state.json")
MAX_PER_RUN = 5
COOLDOWN_MIN = 15

API_KEY            = os.getenv("API_KEY")
API_KEY_SECRET     = os.getenv("API_KEY_SECRET")
ACCESS_TOKEN       = os.getenv("ACCESS_TOKEN")
ACCESS_TOKEN_SECRET= os.getenv("ACCESS_TOKEN_SECRET")

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s', datefmt='%H:%M:%S')
log = logging.info

def load_state():
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            data.setdefault("posted", [])
            data.setdefault("cooldown_until", None)
            return data
        except:
            pass
    return {"posted": [], "cooldown_until": None}

def save_state(s): STATE_PATH.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
state = load_state()

def in_cooldown() -> bool:
    cd = state.get("cooldown_until")
    if not cd: return False
    try:
        until = datetime.fromisoformat(cd.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) < until
    except:
        return False

def twitter_client() -> Optional[tweepy.Client]:
    if not all([API_KEY, API_KEY_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET]):
        log("Twitter anahtarları yok → simülasyon mod")
        return None
    try:
        return tweepy.Client(
            consumer_key=API_KEY,
            consumer_secret=API_KEY_SECRET,
            access_token=ACCESS_TOKEN,
            access_token_secret=ACCESS_TOKEN_SECRET,
        )
    except Exception as e:
        log(f"Twitter init hata: {e}")
        return None

def tweet(client: Optional[tweepy.Client], text: str) -> bool:
    if not client:
        log(f"SIM TWEET: {text}")
        return True
    try:
        client.create_tweet(text=text)
        log("Tweet gönderildi")
        return True
    except TooManyRequests:
        log("429: Limit doldu → cooldown")
        state["cooldown_until"] = (datetime.now(timezone.utc) + timedelta(minutes=COOLDOWN_MIN)).isoformat()
        save_state(state)
        return False
    except Exception as e:
        log(f"Tweet hata: {e}")
        return False

UPPER = "A-ZÇĞİÖŞÜ"
KAP_HEADER_RE = re.compile(
    rf"^KAP\s*[•·\-\.:]?\s*([{UPPER}]{{3,6}})(?:\s*/\s*([{UPPER}]{{3,6}}))?",
    re.UNICODE
)
SPAM_PAT = re.compile(r"(Fintables|Bülten|Piyasa|Analiz|Rapor|KVK|Kişisel Veri)", re.I)
REL_TIME = re.compile(r"\b(Dün|Bugün)\b", re.I)
CLOCK = re.compile(r"\b\d{1,2}:\d{2}\b")

def clean_detail(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "")).strip()
    t = REL_TIME.sub("", t)
    t = CLOCK.sub("", t)
    t = re.sub(r"\b(Fintables|KAP)\b\s*[•·\-\.:]?\s*", "", t, flags=re.I)
    return t.strip(" -–—:|•·")

def build_id(codes: List[str], detail: str) -> str:
    h = base64.urlsafe_b64encode(detail.encode("utf-8")).decode("ascii")[:10]
    return f"kap-{'-'.join(codes)}-{h}"

def build_tweet(codes: List[str], detail: str) -> str:
    codes_part = " ".join(f"#{c}" for c in codes[:2])
    txt = f"📰 {codes_part} | {detail}"
    return txt[:279]

JS_EXTRACTOR = r"""
() => {
  const cards = Array.from(document.querySelectorAll("main div, main li, main article"));
  const blocks = [];
  for (const el of cards) {
    const raw = (el.innerText || "").trim();
    if (!raw) continue;
    const lines = raw.split(/\n+/).map(s => s.trim()).filter(Boolean);
    if (lines.length < 2) continue;
    const header = lines[0];
    if (!/^KAP\b/.test(header)) continue;
    let detail = lines.slice(1).join(" ").replace(/\s+/g, " ").trim();
    if (!detail || detail.length < 30) continue;
    blocks.push({header, detail});
  }
  return blocks;
}
"""

def extract_items(page) -> List[dict]:
    blocks = page.evaluate(JS_EXTRACTOR)
    items = []
    for blk in blocks:
        header = blk["header"]
        detail = blk["detail"]
        if SPAM_PAT.search(header) or SPAM_PAT.search(detail):
            continue
        m = KAP_HEADER_RE.search(header.upper())
        if not m: 
            continue
        codes = [m.group(1)]
        if m.group(2): codes.append(m.group(2))
        codes = [c for c in codes if re.fullmatch(rf"[{UPPER}]{{3,6}}", c)]
        if not codes: continue
        detail_clean = clean_detail(detail)
        if len(detail_clean) < 30: continue
        item_id = build_id(codes, detail_clean)
        items.append({"id": item_id, "codes": codes, "content": detail_clean})
    return items

def main():
    log("Başladı")
    if in_cooldown():
        log("Cooldown aktif, çıkılıyor")
        return
    tw = twitter_client()

    pw = sync_playwright().start()
    br = pw.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
    ctx = br.new_context(
        locale="tr-TR",
        timezone_id="Europe/Istanbul",
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/118 Safari/537.36")
    )
    pg = ctx.new_page()
    pg.set_default_timeout(30000)

    # sayfayı aç
    for attempt in range(3):
        try:
            log(f"Sayfa yükleme deneme {attempt+1}/3")
            pg.goto(AKIS_URL, wait_until="networkidle")
            break
        except Exception as e:
            log(f"Yükleme hatası: {e}")
            if attempt == 2:
                ctx.close(); br.close(); pw.stop()
                return
            time.sleep(3)

    # "Öne çıkanlar" sekmesi
    try:
        pg.locator("text=Öne çıkanlar").first.click(timeout=4000)
        pg.wait_for_timeout(800)
    except:
        pass

    try:
        pg.evaluate("window.scrollTo(0, 600)")
        pg.wait_for_timeout(600)
    except:
        pass

    items = extract_items(pg)
    log(f"Bulunan KAP haberleri: {len(items)}")
    if not items:
        ctx.close(); br.close(); pw.stop()
        return

    new_items = [i for i in reversed(items) if i["id"] not in state["posted"]]
    if not new_items:
        log("Yeni haber yok")
        ctx.close(); br.close(); pw.stop()
        return

    sent = 0
    for it in new_items:
        if sent >= MAX_PER_RUN: break
        t = build_tweet(it["codes"], it["content"])
        log(f"TWEET: {t}")
        ok = tweet(tw, t)
        if not ok: break
        state["posted"].append(it["id"])
        save_state(state)
        sent += 1
        time.sleep(2)

    log(f"Bitti. Gönderilen: {sent}")
    ctx.close(); br.close(); pw.stop()

if __name__ == "__main__":
    main()
