"""crypto_desk.py - Binance öneri masası. SADECE ÖNERİ, EMİR YOK.

- Sadece Binance'in herkese açık piyasa verisini okur (API anahtarı gerekmez).
- Hiçbir emir/hesap fonksiyonu yoktur; işlemi sen elle açarsın.
- Roller: Scanner, News, Entry/Exit, Risk, Skeptic, Desk Lead (hepsi kural tabanlı, AI yok).
- Çıktı: ekran + desk_log.txt + signals.csv

Kurulum:  pip install requests
Çalıştır: python crypto_desk.py          (döngü, her EVERY_MIN dakikada bir)
          python crypto_desk.py --once   (tek tur)
"""
import os, sys, re, csv, json, math, time, datetime
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
import requests

BASE = os.path.dirname(os.path.abspath(__file__))

# ======================= AYARLAR =======================
ACCOUNT_USDT = float(os.environ.get("ACCOUNT_USDT", "1000"))  # Hesap büyüklüğün (USDT)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")  # telefona bildirim için ntfy kanal adı
RISK_PCT = 0.5            # Tek işlemde kaybedilebilecek en fazla % (SERT KURAL)
MAX_POS_PCT = 10.0        # Tek coine ayrılabilecek en fazla % (SERT KURAL)
EVERY_MIN = 30            # Kaç dakikada bir tur
MIN_QUOTE_VOL = 20_000_000  # 24s hacmi (USDT) bunun altındaki coinler elenir
TOP_N = 40                # Hacme göre ilk kaç coin taransın
MIN_VOL_RATIO = 2.0       # Son kapanmış 1s mum hacmi / önceki 24 saat ortalaması
MIN_CHG_24H = 2.0         # 24s değişim en az % (long için)
ATR_MULT = 1.5            # Stop = giriş - ATR_MULT x ATR
RR_TARGET = 2.0           # Hedef = risk x RR_TARGET
MAX_STOP_PCT = 6.0        # Stop girişten bu %'den uzaksa Risk reddeder
MAX_SIGNALS = 2           # Bir turda en fazla kaç öneri
COOLDOWN_H = 12           # Aynı coin için tekrar öneri bekleme süresi (saat)
REQUIRE_NEWS = False      # True yapınca haber (HARD/SOFT) olmadan hiç öneri çıkmaz
MIN_NOTIONAL = 5.0        # Bundan küçük pozisyonlar atlanır (Binance min ~5 USDT)

HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]
NEWS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
]
NAMES = {
    "BTC": ["bitcoin"], "ETH": ["ethereum", "ether "], "SOL": ["solana"],
    "XRP": ["ripple"], "DOGE": ["dogecoin"], "ADA": ["cardano"],
    "BNB": ["bnb chain", "binance coin"], "AVAX": ["avalanche"],
    "TRX": ["tron"], "DOT": ["polkadot"], "LINK": ["chainlink"],
    "SUI": ["sui network"], "TON": ["toncoin"], "SHIB": ["shiba"],
    "LTC": ["litecoin"], "NEAR": ["near protocol"], "APT": ["aptos"],
    "ARB": ["arbitrum"], "OP": ["optimism"], "PEPE": ["pepe coin"],
}
STABLES = {"SNDKB", "USDC", "FDUSD", "TUSD", "USDP", "DAI", "EUR", "TRY", "BUSD",
           "USD1", "USDE", "XUSD", "AEUR", "PAXG"}
# =======================================================

LOG = os.path.join(BASE, "desk_log.txt")
CSV = os.path.join(BASE, "signals.csv")
COOLDOWN_FILE = os.path.join(BASE, "cooldown.json")
_host = None


def post(name, text):
    """Ekrana yaz ve desk_log.txt dosyasına ekle."""
    msg = f"[{name}]\n{text}"
    print("\n" + msg)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"\n{datetime.datetime.now():%Y-%m-%d %H:%M} {msg}\n")


def notify(title, text):
    """Telefona bildirim (ntfy.sh). Kanal adı yoksa hiçbir şey yapmaz."""
    if not NTFY_TOPIC:
        return
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=text.encode("utf-8"),
                      headers={"Title": title, "Priority": "high"}, timeout=15)
    except requests.RequestException as e:
        print("ntfy hatası:", e)


def api(path, **params):
    """Binance herkese açık veri. Bir adres çalışmazsa diğerini dener."""
    global _host
    order = ([_host] if _host else []) + [h for h in HOSTS if h != _host]
    err = "?"
    for h in order:
        try:
            r = requests.get(h + path, params=params, timeout=15)
            if r.ok:
                _host = h
                return r.json()
            err = f"{h}: {r.status_code} {r.text[:120]}"
        except requests.RequestException as e:
            err = f"{h}: {e}"
    raise RuntimeError("Binance'e ulaşılamadı -> " + err +
                       " (bölgende erişim engeli olabilir; VPN gerekebilir)")


def klines(sym, interval="1h", limit=60):
    raw = api("/api/v3/klines", symbol=sym, interval=interval, limit=limit)
    out = [dict(o=float(k[1]), h=float(k[2]), l=float(k[3]), c=float(k[4]),
                v=float(k[5])) for k in raw]
    return out[:-1]  # son mum henüz kapanmadı, atılır


# ---------------- göstergeler ----------------
def ema(vals, n):
    k = 2 / (n + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def rsi(c, n=14):
    if len(c) < n + 2:
        return 50.0
    g = [max(c[i] - c[i - 1], 0) for i in range(1, len(c))]
    l = [max(c[i - 1] - c[i], 0) for i in range(1, len(c))]
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + l[i]) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def atr(k, n=14):
    trs = [max(k[i]["h"] - k[i]["l"], abs(k[i]["h"] - k[i - 1]["c"]),
               abs(k[i]["l"] - k[i - 1]["c"])) for i in range(1, len(k))]
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def fmt(x):
    return f"{x:.6g}"


# ---------------- 0. piyasa rejimi ----------------
def btc_regime():
    k = klines("BTCUSDT", "1h", 30)
    c4 = (k[-1]["c"] / k[-5]["c"] - 1) * 100
    return {"c4": c4, "weak": c4 < -2.0}


# ---------------- 1. SCANNER ----------------
def scanner():
    tk = api("/api/v3/ticker/24hr")
    cands = []
    for t in tk:
        s = t["symbol"]
        if not s.endswith("USDT") or s[:-4] in STABLES:
            continue
        qv = float(t["quoteVolume"])
        if qv >= MIN_QUOTE_VOL:
            cands.append((qv, s, float(t["priceChangePercent"]), float(t["lastPrice"])))
    cands.sort(reverse=True)
    picks = []
    for qv, s, chg, price in cands[:TOP_N]:
        try:
            k = klines(s, "1h", 60)
        except RuntimeError:
            continue
        if len(k) < 30:
            continue
        avg = sum(x["v"] for x in k[-25:-1]) / 24
        ratio = k[-1]["v"] / avg if avg else 0
        if ratio >= MIN_VOL_RATIO and chg >= MIN_CHG_24H and k[-1]["c"] > k[-1]["o"]:
            picks.append(dict(sym=s, base=s[:-4], price=price, chg=chg,
                              ratio=ratio, k=k, qv=qv))
    picks.sort(key=lambda p: p["ratio"], reverse=True)
    return picks[:3]


# ---------------- 2. NEWS ----------------
NEG = re.compile(r"hack|exploit|delist|lawsuit|sues|\bban\b|outage|stolen|rug pull|"
                 r"investigat|fraud|collaps", re.I)
HARD = re.compile(r"binance (will )?list|lists?\b.*\bbinance|mainnet|hard fork|upgrade|"
                  r"\betf\b|approv|partnership|integrat|launch|buyback|burn|"
                  r"acquir|treasury", re.I)
SOFT = re.compile(r"analyst|rally|surge|soar|jump|climb|breakout|adoption|outlook|"
                  r"price target|inflow", re.I)
RUMOR = re.compile(r"rumou?r|reportedly|sources say|speculat|whale|could|may |might",
                   re.I)
RANK = {"NEGATIVE": 4, "HARD": 3, "SOFT": 2, "RUMOR": 1, "MENTION": 0}


def classify(title):
    for lab, pat in (("NEGATIVE", NEG), ("HARD", HARD), ("SOFT", SOFT), ("RUMOR", RUMOR)):
        if pat.search(title):
            return lab
    return "MENTION"


def recent(pub):
    try:
        d = parsedate_to_datetime(pub)
        if d.tzinfo is None:
            d = d.replace(tzinfo=datetime.timezone.utc)
        return (datetime.datetime.now(datetime.timezone.utc) - d).total_seconds() < 24 * 3600
    except Exception:
        return True


def fetch_headlines():
    items = []
    for url in NEWS_FEEDS:
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            src = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
            for it in ET.fromstring(r.content).iter("item"):
                title = (it.findtext("title") or "").strip()
                if title and recent(it.findtext("pubDate") or ""):
                    items.append((title, src))
        except Exception:
            pass
    return items


def news_for(base, headlines):
    pat = re.compile(r"\b" + re.escape(base) + r"\b")
    names = NAMES.get(base, [])
    best = None
    for title, src in headlines:
        if pat.search(title) or any(n in title.lower() for n in names):
            lab = classify(title)
            if best is None or RANK[lab] > RANK[best[0]]:
                best = (lab, title, src)
    return best  # None = haber bulunamadı


# ---------------- 3. ENTRY / EXIT ----------------
def plan(p):
    a = atr(p["k"])
    entry = p["price"] * 0.999          # limit emir: anlık fiyatın biraz altı
    stop = entry - ATR_MULT * a
    target = entry + RR_TARGET * (entry - stop)
    return dict(entry=entry, stop=stop, target=target, atr=a,
                stop_pct=(entry - stop) / entry * 100,
                rr=(target - entry) / (entry - stop))


# ---------------- 4. RISK ----------------
def risk_check(p, btc, cooldown):
    pl, nw = p["plan"], p["news"]
    if pl["stop_pct"] > MAX_STOP_PCT:
        return False, f"stop %{pl['stop_pct']:.1f} uzak (limit %{MAX_STOP_PCT})"
    if pl["rr"] < RR_TARGET - 1e-9:
        return False, f"ödül/risk {pl['rr']:.1f} < {RR_TARGET}"
    if nw and nw[0] == "NEGATIVE":
        return False, "negatif haber başlığı var"
    if REQUIRE_NEWS and not (nw and nw[0] in ("HARD", "SOFT")):
        return False, "doğrulanmış haber yok (RUMOR/NO CATALYST)"
    last = cooldown.get(p["sym"])
    if last and time.time() - last < COOLDOWN_H * 3600:
        return False, f"son {COOLDOWN_H} saatte zaten önerildi"
    return True, f"stop %{pl['stop_pct']:.1f}, ödül/risk {pl['rr']:.1f}"


# ---------------- 5. SKEPTIC ----------------
def skeptic(p, btc):
    k = p["k"]
    closes = [x["c"] for x in k]
    why = []
    if p["chg"] > 15:
        why.append(f"24s +%{p['chg']:.0f}: hareketin büyük kısmı fiyatlanmış olabilir")
    r = rsi(closes)
    if r > 75:
        why.append(f"RSI {r:.0f}: aşırı alım")
    e20 = ema(closes[-40:], 20)
    if closes[-1] > e20 * 1.06:
        why.append("fiyat 20 mumluk ortalamanın %6+ üstünde (uzamış)")
    if k[-1]["v"] < k[-2]["v"] * 0.6:
        why.append("hacim bir önceki muma göre sönüyor")
    rng = k[-1]["h"] - k[-1]["l"]
    if rng > 0 and (k[-1]["h"] - k[-1]["c"]) / rng > 0.5:
        why.append("son mumda uzun üst fitil (satış baskısı)")
    if btc["weak"]:
        why.append(f"BTC zayıf (4s %{btc['c4']:.1f}), altlar genelde onunla düşer")
    n = len(why)
    vote = "BUY" if n <= 1 else ("WAIT" if n == 2 else "SKIP")
    return vote, why


# ---------------- hard money rule ----------------
def size(entry, stop):
    """Sert para kuralı: düz kod, hiçbir şey bunu aşamaz."""
    rps = entry - stop
    if rps <= 0:
        return 0.0
    by_risk = (ACCOUNT_USDT * RISK_PCT / 100) / rps
    by_cap = (ACCOUNT_USDT * MAX_POS_PCT / 100) / entry
    return math.floor(min(by_risk, by_cap) * 1e6) / 1e6


def load_cd():
    try:
        with open(COOLDOWN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cd(cd):
    with open(COOLDOWN_FILE, "w", encoding="utf-8") as f:
        json.dump(cd, f)


def run_round():
    now = datetime.datetime.now()
    btc = btc_regime()
    post("MARKET", f"{now:%Y-%m-%d %H:%M} | BTC 4s: {btc['c4']:+.1f}% "
                   f"{'(ZAYIF)' if btc['weak'] else ''} | hesap: {ACCOUNT_USDT:g} USDT")

    picks = scanner()
    if not picks:
        post("SCANNER", "NOTHING UNUSUAL")
        post("DESK LEAD", "Bu turda öneri yok. (Çoğu tur böyle olmalı.)")
        return
    post("SCANNER", "\n".join(
        f"{p['sym']} | {fmt(p['price'])} | 24s {p['chg']:+.1f}% | hacim {p['ratio']:.1f}x"
        for p in picks))

    heads = fetch_headlines()
    lines = []
    for p in picks:
        p["news"] = news_for(p["base"], heads)
        n = p["news"]
        lines.append(f"{p['sym']}: {n[0]} - {n[1][:110]} ({n[2]})" if n
                     else f"{p['sym']}: NO CLEAR CATALYST")
    post("NEWS", "\n".join(lines))

    for p in picks:
        p["plan"] = plan(p)
    post("ENTRY/EXIT", "\n".join(
        f"{p['sym']}: giriş {fmt(p['plan']['entry'])}, stop {fmt(p['plan']['stop'])}, "
        f"hedef {fmt(p['plan']['target'])}, ödül/risk {p['plan']['rr']:.1f}" for p in picks))

    cd = load_cd()
    for p in picks:
        p["risk"] = risk_check(p, btc, cd)
    post("RISK", "\n".join(f"{p['sym']}: {'PASS' if p['risk'][0] else 'FAIL'} - {p['risk'][1]}"
                           for p in picks))

    sk = []
    for p in picks:
        if p["risk"][0]:
            p["vote"], why = skeptic(p, btc)
            sk.append(f"{p['sym']}: " + ("; ".join(why) if why else "güçlü itiraz yok")
                      + f"\n  Oy: {p['vote']}")
    post("SKEPTIC", "\n".join(sk) if sk else "Risk'ten geçen plan yok.")

    out, approved = [], []
    for p in picks:
        ok = p["risk"][0] and p.get("vote") == "BUY"
        if ok and len(approved) < MAX_SIGNALS:
            qty = size(p["plan"]["entry"], p["plan"]["stop"])
            notional = qty * p["plan"]["entry"]
            if notional < MIN_NOTIONAL:
                out.append(f"{p['sym']}: SKIP (pozisyon çok küçük: {notional:.1f} USDT)")
                continue
            approved.append(p)
            pl = p["plan"]
            nw = f"haber: {p['news'][0]}" if p["news"] else "haber yok"
            out.append(
                f"AL (spot, limit) {p['sym']}\n"
                f"  giriş {fmt(pl['entry'])} | stop {fmt(pl['stop'])} | hedef {fmt(pl['target'])}\n"
                f"  miktar {qty:g} {p['base']} (~{notional:.0f} USDT, "
                f"maks. kayıp ~{qty * (pl['entry'] - pl['stop']):.2f} USDT)\n"
                f"  gerekçe: hacim {p['ratio']:.1f}x, 24s {p['chg']:+.1f}%, {nw}")
        else:
            why = p["risk"][1] if not p["risk"][0] else f"Skeptic oyu {p.get('vote')}"
            out.append(f"{p['sym']}: {'WAIT' if p.get('vote') == 'WAIT' else 'SKIP'} ({why})")
    post("DESK LEAD", "\n".join(out) + ("\n\nNot: Sadece öneri. Emri sen elle gir; "
                                        "stopu mutlaka koy." if approved else ""))

    if approved:
        notify("Kripto onerisi", "\n\n".join(o for o in out if o.startswith("AL")))
        new_file = not os.path.exists(CSV)
        with open(CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(["time", "symbol", "entry", "stop", "target", "qty", "news"])
            for p in approved:
                pl = p["plan"]
                w.writerow([f"{now:%Y-%m-%d %H:%M}", p["sym"], fmt(pl["entry"]),
                            fmt(pl["stop"]), fmt(pl["target"]),
                            size(pl["entry"], pl["stop"]),
                            p["news"][0] if p["news"] else "NONE"])
                cd[p["sym"]] = time.time()
        save_cd(cd)


if __name__ == "__main__":
    once = "--once" in sys.argv
    print("Crypto desk başladı (sadece öneri, emir yok). Durdurmak için Ctrl + C.")
    while True:
        try:
            run_round()
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print("Hata:", e)
        if once:
            break
        try:
            time.sleep(EVERY_MIN * 60)
        except KeyboardInterrupt:
            print("\nDurduruldu.")
            break
