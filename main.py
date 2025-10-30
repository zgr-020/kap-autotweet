import os, re, json, time, logging, base64
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from playwright.sync_api import sync_playwright
import tweepy
from tweepy import TooManyRequests

# ================== Sabitler ==================
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

# ================== State ==================
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

# ================== Twitter ==================
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

def send_tweet(client: Optional[tweepy.Client], text: str) -> bool:
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

# ================== Metin İşleme ==================
UPPER = "A-ZÇĞİÖŞÜ"
# Başlık satırı: KAP · KOD(/KOD2) varyasyonları
KAP_HEADER_RE = re.compile(
    rf"^\s*KAP\s*[•·\-\.:]?\s*([{UPPER}]{{3,6}})(?:\s*/\s*([{UPPER}]{{3,6}}))?\b",
    re.UNICODE | re.IGNORECASE
)
# spam/rapor/bülten ele
SPAM_PAT = re.compile(r"(Fintables|Bülten|Piyasa|Analiz|Rapor|KVK|Kişisel Veri|Politika)", re.I)
REL_TIME = re.compile(r"\b(Dün|Bugün|Yesterday|Today)\b", re.I)
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
    # İki koda kadar etiketle
    codes_part = " ".join(f"#{c}" for c in codes[:2])
    txt = f"📰 {codes_part} | {detail}".strip()
    return txt[:279]

# ================== Playwright ==================
def goto_with_retry(page, url: str, tries: int = 3) -> bool:
    for i in range(tries):
        try:
            log(f"Sayfa yükleme deneme {i+1}/{tries}")
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_load_state("networkidle", timeout=30000)
            # İçerik akışı görünene kadar küçük bekleme
            page.wait_for_timeout(1200)
            return True
        except Exception as e:
            log(f"Yükleme hatası: {e}")
            if i < tries - 1:
                time.sleep(3)
    return False

def click_highlights(page):
    # “Öne çıkanlar” için farklı varyasyonları dene (büyük/küçük)
    selectors = [
        "button:has-text('Öne çıkanlar')",
        "button:has-text('Öne Çıkanlar')",
        "[role='tab']:has-text('Öne çıkanlar')",
        "[role='tab']:has-text('Öne Çıkanlar')",
        "a:has-text('Öne çıkanlar')",
        "a:has-text('Öne Çıkanlar')",
        "text=Öne çıkanlar",
        "text=Öne Çıkanlar",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible():
                loc.first.click()
                page.wait_for_load_state("networkidle", timeout=15000)
                page.wait_for_timeout(800)
                log(">> Öne çıkanlar ON")
                return True
        except:
            continue
    log(">> Öne çıkanlar butonu bulunamadı (Tümü'nde kalındı)")
    return False

def scroll_feed(page, steps: int = 6, dy: int = 1200, pause_ms: int = 600):
    # Lazy-load için kademeli kaydır
    for _ in range(steps):
        page.evaluate(f"window.scrollBy(0, {dy});")
        page.wait_for_timeout(pause_ms)

def extract_items_from_dom(page) -> List[dict]:
    """
    Her bir KAP kartı / satırı şu şablonda:
    Satır 1: 'KAP · KOD' veya 'KAP · KOD/KOD2'
    Alt satırlar: haber detayı (nokta+cümleler)
    Bu fonksiyon satır bazlı ayrıştırır.
    """
    rows_sel = "main li:visible, main article:visible, main div[role='listitem']:visible, main div:visible"
    rows = page.locator(rows_sel)
    total = min(rows.count(), 400)
    log(f">> Raw rows: {total}")

    items = []
    for i in range(total):
        try:
            row = rows.nth(i)
            text = (row.inner_text() or "").strip()
            if not text or len(text) < 25:
                continue

            # Satırları parçala
            lines = [ln.strip() for ln in re.split(r"\n+", text) if ln.strip()]
            if not lines: 
                continue

            # Başlığı bul: KAP ... ile başlayan ilk satır
            header_idx = None
            for idx, ln in enumerate(lines):
                if KAP_HEADER_RE.match(ln):
                    header_idx = idx
                    break
            if header_idx is None:
                continue

            header = lines[header_idx]
            # Detay: başlık satırından SONRA gelen kısımların birleşimi
            detail = " ".join(lines[header_idx+1:]).strip()
            if not detail or len(detail) < 30:
                continue

            if SPAM_PAT.search(header) or SPAM_PAT.search(detail):
                continue

            # Kodları çıkar
            m = KAP_HEADER_RE.match(header.upper())
            if not m:
                continue
            codes = [m.group(1)]
            if m.group(2): 
                codes.append(m.group(2))
            # Kod doğrulama
            codes = [c for c in codes if re.fullmatch(rf"[{UPPER}]{{3,6}}", c)]
            if not codes:
                continue

            detail_clean = clean_detail(detail)
            if len(detail_clean) < 30:
                continue

            item_id = build_id(codes, detail_clean)
            items.append({"id": item_id, "codes": codes, "content": detail_clean})
        except Exception:
            continue

    return items

# ================== Ana Akış ==================
def main():
    log("Başladı")
    if in_cooldown():
        log("Cooldown aktif, çıkılıyor")
        return

    tw = twitter_client()

    pw = sync_playwright().start()
    br = pw.chromium.launch(headless=True, args=[
        "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"
    ])
    ctx = br.new_context(
        locale="tr-TR",
        timezone_id="Europe/Istanbul",
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/118 Safari/537.36"),
        viewport={"width": 1440, "height": 900}
    )
    pg = ctx.new_page()
    pg.set_default_timeout(30000)

    if not goto_with_retry(pg, AKIS_URL, tries=3):
        ctx.close(); br.close(); pw.stop()
        return

    click_highlights(pg)
    # Ekranı biraz canlandır
    scroll_feed(pg, steps=2, dy=900, pause_ms=500)

    # 3 tur dene: her tur kaydır + çıkar
    collected = []
    for _ in range(3):
        items = extract_items_from_dom(pg)
        collected.extend(items)
        scroll_feed(pg, steps=2, dy=1200, pause_ms=600)

    # benzersizle
    uniq = {i["id"]: i for i in collected}
    items = list(uniq.values())
    log(f"Bulunan KAP haberleri: {len(items)}")

    if not items:
        ctx.close(); br.close(); pw.stop()
        return

    # En yeniden eskiye diye geldi varsayıp, tersine çevirip en eskiden yeniye gönderelim:
    items.sort(key=lambda x: x["id"])  # id hash içeriyor; tutarlı sıralama için
    new_items = [i for i in items if i["id"] not in state["posted"]]

    if not new_items:
        log("Yeni haber yok")
        ctx.close(); br.close(); pw.stop()
        return

    sent = 0
    for it in new_items:
        if sent >= MAX_PER_RUN: 
            break
        t = build_tweet(it["codes"], it["content"])
        log(f"TWEET: {t}")
        ok = send_tweet(tw, t)
        if not ok:
            break
        state["posted"].append(it["id"])
        save_state(state)
        sent += 1
        time.sleep(2)

    log(f"Bitti. Gönderilen: {sent}")
    ctx.close(); br.close(); pw.stop()

if __name__ == "__main__":
    main()
