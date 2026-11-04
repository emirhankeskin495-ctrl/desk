"""crypto_desk.py - Binance öneri masası. SADECE ÖNERİ, EMİR YOK.

- Sadece Binance'in herkese açık piyasa verisini okur (API anahtarı gerekmez).
- Hiçbir emir/hesap fonksiyonu yoktur; işlemi sen elle açarsın.
- Roller: Scanner, News, Entry/Exit, Risk, Skeptic, Desk Lead (hepsi kural tabanlı, AI yok).
- Çıktı: ekran + desk_log.txt + history.json + site/data.json (web paneli için)

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
WATCH_N = 12             # Teknik analiz tablosunda kaç coin izlensin (hacme göre)
ADX_TREND = 25            # ADX bunun üstündeyse piyasa "trend", altındaysa "yatay" sayılır
FWD = 6                   # Geçmiş sinyal istatistiği: sinyalden kaç saat sonrasına bakılsın
CHART_N = 96              # Grafikte gösterilecek mum sayısı (1s)
MAX_TOTAL_RISK_PCT = 1.5  # Açık önerilerin toplam riski hesabın en fazla bu %'si olabilir
DAILY_LOSS_R = 3.0        # Son 24 saatte toplam kayıp bu kadar R'yi geçerse yeni öneri durur
FEE_PCT = 0.1             # Backtest: her işlem yönü için komisyon (%)
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
HISTORY = os.path.join(BASE, "history.json")
SITE = os.path.join(BASE, "site")
FINAL = {"HEDEF", "STOP", "DOLMADI", "SÜRE DOLDU"}
COOLDOWN_FILE = os.path.join(BASE, "cooldown.json")
WATCHLIST_FILE = os.path.join(BASE, "watchlist.txt")
ALERTS_FILE = os.path.join(BASE, "alerts.txt")
_host = None


def post(name, text):
    """Ekrana yaz ve desk_log.txt dosyasına ekle."""
    msg = f"[{name}]\n{text}"
    print("\n" + msg)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"\n{datetime.datetime.now():%Y-%m-%d %H:%M} {msg}\n")


def already_sent(title, text):
    """ntfy'nin kendi son 12 saatlik kaydına bakar: aynı bildirim gitmişse tekrar göndermez."""
    try:
        r = requests.get(f"https://ntfy.sh/{NTFY_TOPIC}/json",
                         params={"poll": "1", "since": "12h"}, timeout=10)
        for line in r.text.splitlines():
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("event") == "message" and m.get("title") == title and m.get("message") == text:
                return True
    except requests.RequestException:
        pass
    return False


def notify(title, text):
    """Telefona bildirim (ntfy.sh). Kanal adı yoksa hiçbir şey yapmaz."""
    if not NTFY_TOPIC:
        return
    if already_sent(title, text):
        print("ntfy: aynı bildirim zaten gönderilmiş, atlandı.")
        return
    try:
        hdr = {"Title": title, "Priority": "high"}
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        if "/" in repo:
            owner, name = repo.split("/", 1)
            hdr["Click"] = f"https://{owner.lower()}.github.io/{name}/"
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=text.encode("utf-8"),
                      headers=hdr, timeout=15)
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
    out = [dict(t=int(k[0]), o=float(k[1]), h=float(k[2]), l=float(k[3]), c=float(k[4]),
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


# ---------------- TEKNİK ANALİZ ----------------
def r6(x):
    return float(f"{x:.6g}")


def ema_series(v, n):
    k = 2 / (n + 1)
    out = [v[0]]
    for x in v[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def rsi_series(c, n=14):
    out = [50.0] * len(c)
    if len(c) < n + 2:
        return out
    ag = al = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]
        ag += max(d, 0)
        al += max(-d, 0)
    ag, al = ag / n, al / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def bb_series(c, n=20, m=2.0):
    mid, up, lo = [], [], []
    for i in range(len(c)):
        w = c[max(0, i - n + 1):i + 1]
        mu = sum(w) / len(w)
        sd = (sum((x - mu) ** 2 for x in w) / len(w)) ** 0.5
        mid.append(mu)
        up.append(mu + m * sd)
        lo.append(mu - m * sd)
    return mid, up, lo


def adx_series(k, n=14):
    L = len(k)
    adx = [0.0] * L
    if L < 2 * n + 2:
        return adx
    tr, pdm, mdm = [0.0] * L, [0.0] * L, [0.0] * L
    for i in range(1, L):
        up = k[i]["h"] - k[i - 1]["h"]
        dn = k[i - 1]["l"] - k[i]["l"]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i] = max(k[i]["h"] - k[i]["l"], abs(k[i]["h"] - k[i - 1]["c"]),
                    abs(k[i]["l"] - k[i - 1]["c"]))
    a_tr, a_p, a_m = sum(tr[1:n + 1]), sum(pdm[1:n + 1]), sum(mdm[1:n + 1])
    dx = [0.0] * L
    for i in range(n, L):
        if i > n:
            a_tr = a_tr - a_tr / n + tr[i]
            a_p = a_p - a_p / n + pdm[i]
            a_m = a_m - a_m / n + mdm[i]
        pdi = 100 * a_p / a_tr if a_tr else 0
        mdi = 100 * a_m / a_tr if a_tr else 0
        dx[i] = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0
    a = sum(dx[n:2 * n]) / n
    adx[2 * n - 1] = a
    for i in range(2 * n, L):
        a = (a * (n - 1) + dx[i]) / n
        adx[i] = a
    return adx


def build_series(k):
    c = [x["c"] for x in k]
    macd = [a - b for a, b in zip(ema_series(c, 12), ema_series(c, 26))]
    mh = [a - b for a, b in zip(macd, ema_series(macd, 9))]
    _, bu, bl = bb_series(c)
    obv = [0.0]
    for i in range(1, len(k)):
        obv.append(obv[-1] + (k[i]["v"] if c[i] > c[i - 1] else -k[i]["v"] if c[i] < c[i - 1] else 0))
    return dict(c=c, h=[x["h"] for x in k], l=[x["l"] for x in k],
                e20=ema_series(c, 20), e50=ema_series(c, 50), rsi=rsi_series(c),
                mh=mh, bu=bu, bl=bl, adx=adx_series(k), obv=obv, obvE=ema_series(obv, 20))


def score_at(j, S):
    """Rejime duyarlı bileşik skor: ADX yüksekse trend göstergeleri, düşükse dönüş göstergeleri oy verir."""
    c = S["c"][j]
    ema_v = 1 if (S["e20"][j] > S["e50"][j] and c > S["e50"][j]) else \
        (-1 if (S["e20"][j] < S["e50"][j] and c < S["e50"][j]) else 0)
    mh, mhp = S["mh"][j], S["mh"][j - 1]
    macd_v = 1 if (mh > 0 and mh >= mhp) else (-1 if (mh < 0 and mh <= mhp) else 0)
    hh, ll = max(S["h"][j - 20:j]), min(S["l"][j - 20:j])
    don_v = 1 if c > hh else (-1 if c < ll else 0)
    obv_v = 1 if S["obv"][j] > S["obvE"][j] else -1
    r = S["rsi"][j]
    rsi_v = 1 if r < 30 else (-1 if r > 70 else 0)
    bb_v = 1 if c < S["bl"][j] else (-1 if c > S["bu"][j] else 0)
    if S["adx"][j] >= ADX_TREND:
        regime = "TREND"
        v = {"EMA 20/50": ema_v, "MACD": macd_v, "Donchian": don_v, "OBV": obv_v}
        sc = sum(v.values())
        sig = "AL" if sc >= 3 else ("SAT" if sc <= -3 else
                                    ("ZAYIF" if (sc == 2 and ema_v == 1) else "BEKLE"))
    else:
        regime = "YATAY"
        v = {"RSI": rsi_v, "Bollinger": bb_v, "OBV": obv_v}
        sc = sum(v.values())
        if sc >= 2 and (rsi_v == 1 or bb_v == 1):
            sig = "AL"
        elif sc <= -2 and (rsi_v == -1 or bb_v == -1):
            sig = "SAT"
        elif r < 40 and obv_v == 1:
            sig = "ZAYIF"
        else:
            sig = "BEKLE"
    return sc, sig, regime, v


def _stat(flips, kind, c):
    xs = [c[j + FWD] / c[j] - 1 for j, t in flips if t == kind and j + FWD < len(c)]
    if not xs:
        return {"n": 0, "hit": 0, "avg": 0}
    good = [x for x in xs if (x > 0 if kind == "AL" else x < 0)]
    return {"n": len(xs), "hit": round(100 * len(good) / len(xs)),
            "avg": round(100 * sum(xs) / len(xs), 2)}


def backtest(k, S, res):
    """Stratejiyi geçmişte oynatır: AL'a dönüşte sonraki mumun açılışından girer, stop/hedef/SAT/48 saat ile çıkar."""
    L = len(k)
    fee = FEE_PCT / 100
    eq, ev, trades, busy = 1.0, {}, [], -1
    for j in range(61, L - 1):
        if j <= busy or res[j][1] != "AL" or res[j - 1][1] == "AL":
            continue
        a = atr(k[max(0, j - 60):j + 1])
        entry = k[j + 1]["o"]
        stop = entry - ATR_MULT * a
        if stop <= 0 or entry <= stop:
            continue
        target = entry + RR_TARGET * (entry - stop)
        xp = xi = None
        for m in range(j + 1, min(L, j + 49)):
            if k[m]["l"] <= stop:
                xp, xi = stop, m
                break
            if k[m]["h"] >= target:
                xp, xi = target, m
                break
            if res[m] and res[m][1] == "SAT":
                xp, xi = k[m]["c"], m
                break
        if xp is None:
            xi = min(L - 1, j + 48)
            xp = k[xi]["c"]
        net = xp / entry - 1 - 2 * fee
        frac = min(RISK_PCT / 100 / ((entry - stop) / entry), MAX_POS_PCT / 100)
        eq *= 1 + frac * net
        trades.append(frac * net)
        ev[xi] = eq
        busy = xi
    cur, curve = 1.0, []
    for q in range(60, L):
        cur = ev.get(q, cur)
        curve.append(cur)
    c0 = S["c"][60]
    hold = [1 + MAX_POS_PCT / 100 * (S["c"][q] / c0 - 1) for q in range(60, L)]
    peak, dd = 1.0, 0.0
    for x in curve:
        peak = max(peak, x)
        dd = max(dd, 1 - x / peak)
    gp = sum(x for x in trades if x > 0)
    gl = -sum(x for x in trades if x < 0)
    step = max(1, len(curve) // 80)
    idx = list(range(0, len(curve), step))
    if idx[-1] != len(curve) - 1:
        idx.append(len(curve) - 1)
    return dict(n=len(trades), win=round(100 * sum(1 for x in trades if x > 0) / len(trades)) if trades else 0,
                ret=round(100 * (eq - 1), 2), bh=round(100 * (hold[-1] - 1), 2), dd=round(100 * dd, 2),
                pf=round(gp / gl, 2) if gl else None, days=round((L - 60) / 24),
                curve=[round(100 * (curve[q] - 1), 2) for q in idx],
                bhc=[round(100 * (hold[q] - 1), 2) for q in idx])


def analyze(sym, btc):
    k = klines(sym, "1h", 1000)
    if len(k) < 120:
        return None
    S = build_series(k)
    L = len(k)
    res = [None] * L
    for j in range(60, L):
        res[j] = score_at(j, S)
    i = L - 1
    sc, raw, regime, votes = res[i]
    flips = [(j, res[j][1]) for j in range(61, L)
             if res[j][1] != res[j - 1][1] and res[j][1] in ("AL", "SAT")]
    k4 = klines(sym, "4h", 100)
    c4 = [x["c"] for x in k4]
    e20, e50 = ema_series(c4, 20), ema_series(c4, 50)
    trend4 = "YUKARI" if (c4[-1] > e50[-1] and e20[-1] > e50[-1]) else \
        ("AŞAĞI" if (c4[-1] < e50[-1] and e20[-1] < e50[-1]) else "YATAY")
    final, note = raw, ""
    if raw in ("AL", "ZAYIF") and trend4 == "AŞAĞI":
        final, note = "BEKLE", f"4 saatlik trend aşağı, {raw} iptal"
    elif raw in ("AL", "ZAYIF") and btc["weak"]:
        final, note = "BEKLE", f"BTC zayıf, {raw} iptal"
    price = S["c"][i]
    a = atr(k)
    stop = price - ATR_MULT * a
    s0 = L - CHART_N
    rows = lambda arr: [r6(x) for x in arr[s0:]]
    return dict(
        sym=sym, price=r6(price), signal=final, raw=raw, score=sc, regime=regime,
        votes=votes, trend4=trend4, rsi=round(S["rsi"][i]), adx=round(S["adx"][i]),
        note=note, stop=r6(stop), target=r6(price + RR_TARGET * (price - stop)),
        bt=backtest(k, S, res),
        stats={"al": _stat(flips, "AL", S["c"]), "sat": _stat(flips, "SAT", S["c"]),
               "days": round((L - 60) / 24)},
        chart=dict(t0=k[s0]["t"], t1=k[-1]["t"],
                   o=rows([x["o"] for x in k]), h=rows(S["h"]), l=rows(S["l"]), c=rows(S["c"]),
                   e20=rows(S["e20"]), e50=rows(S["e50"]), bu=rows(S["bu"]), bl=rows(S["bl"]),
                   rsi=[round(x, 1) for x in S["rsi"][s0:]], mh=rows(S["mh"]),
                   mk=[{"i": j - s0, "t": t} for j, t in flips if j >= s0]))


# ---------------- piyasa duyarlılığı ----------------
def sentiment():
    """Korku/Açgözlülük endeksi, BTC fonlama oranı, BTC dominansı. Erişilemeyenler None kalır."""
    out = {"fng": None, "fng_label": "", "funding": None, "dominance": None}
    try:
        d = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10).json()["data"][0]
        out["fng"], out["fng_label"] = int(d["value"]), d["value_classification"]
    except Exception:
        pass
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex",
                         params={"symbol": "BTCUSDT"}, timeout=10)
        if r.ok:
            out["funding"] = round(float(r.json()["lastFundingRate"]) * 100, 4)
    except Exception:
        pass
    try:
        d = requests.get("https://api.coingecko.com/api/v3/global", timeout=10).json()["data"]
        out["dominance"] = round(d["market_cap_percentage"]["btc"], 1)
    except Exception:
        pass
    return out


# ---------------- 0. piyasa rejimi ----------------
def btc_regime():
    k = klines("BTCUSDT", "1h", 30)
    c4 = (k[-1]["c"] / k[-5]["c"] - 1) * 100
    return {"c4": c4, "weak": c4 < -2.0}


# ---------------- 1. SCANNER ----------------
def universe():
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
    return cands


def scanner(cands):
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
def risk_check(p, btc, cooldown, ctx=None):
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
    if ctx:
        if ctx["day_r"] <= -DAILY_LOSS_R:
            return False, f"günlük zarar limiti doldu ({ctx['day_r']:.1f}R)"
        nr = size(pl["entry"], pl["stop"]) * (pl["entry"] - pl["stop"])
        if ctx["open_risk"] + nr > ctx["limit"]:
            return False, "toplam açık risk limiti dolu"
    return True, f"stop %{pl['stop_pct']:.1f}, ödül/risk {pl['rr']:.1f}"


# ---------------- 5. SKEPTIC ----------------
def skeptic(p, btc, tech=None):
    k = p["k"]
    closes = [x["c"] for x in k]
    why = []
    if tech:
        if tech["raw"] == "SAT":
            why.append("teknik skor SAT (trend/momentum aşağı)")
        elif tech["trend4"] == "AŞAĞI":
            why.append("4 saatlik trend aşağı")
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
    if btc.get("fng") is not None and btc["fng"] >= 80:
        why.append(f"piyasa aşırı açgözlü (Korku/Açgözlülük {btc['fng']})")
    if btc.get("funding") is not None and btc["funding"] > 0.05:
        why.append(f"BTC fonlama oranı yüksek (%{btc['funding']}), kalabalık alım")
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


def load_watchlist():
    """watchlist.txt dosyasındaki coinleri okur (PAXG yazman yeter, USDT eklenir)."""
    try:
        with open(WATCHLIST_FILE, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        raw = "PAXG"
    out = []
    for line in raw.splitlines():
        if line.strip().startswith("#"):
            continue
        for tok in re.split(r"[,\s]+", line):
            tok = tok.strip().upper()
            if not tok:
                continue
            if not tok.endswith("USDT"):
                tok += "USDT"
            if tok not in out:
                out.append(tok)
    return out


def load_alerts():
    """alerts.txt: her satır 'PAXG > 4100' ya da 'SOL < 120'."""
    out = []
    try:
        with open(ALERTS_FILE, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for line in lines:
        m = re.match(r"^\s*([A-Za-z0-9]+)\s*([<>])\s*([0-9]*\.?[0-9]+)", line)
        if not m:
            continue
        sym = m.group(1).upper()
        sym = sym if sym.endswith("USDT") else sym + "USDT"
        out.append((sym, m.group(2), float(m.group(3))))
    return out


def check_alerts(cd):
    st, res = cd.get("_alerts", {}), []
    for sym, op, val in load_alerts():
        key = f"{sym}{op}{val}"
        try:
            price = float(api("/api/v3/ticker/price", symbol=sym)["price"])
        except Exception:
            continue
        hit = price > val if op == ">" else price < val
        if hit and not st.get(key):
            notify(f"Alarm: {sym[:-4]}", f"{sym} {op} {fmt(val)} oldu (fiyat {fmt(price)}).")
        st[key] = hit
        res.append(dict(sym=sym, op=op, val=val, price=r6(price), hit=hit))
    cd["_alerts"] = st
    return res


def extras(snap, hist):
    """Fiyat alarmları, günlük kontrol bildirimi ve haftalık özet."""
    cd = load_cd()
    now = datetime.datetime.now(datetime.timezone.utc)
    snap["alerts"] = check_alerts(cd)
    n_open = sum(1 for h in hist if h["status"] in ("BEKLİYOR", "AÇIK"))
    hb = cd.get("_hb", {"rounds": 0, "signals": 0, "sent": ""})
    hb["rounds"] += 1
    hb["signals"] += snap.get("approved", 0)
    today = f"{now:%Y-%m-%d}"
    if now.hour >= 6 and hb.get("sent") != today:
        notify("Gunluk kontrol", f"Sistem calisiyor. Son 24 saatte {hb['rounds']} tur, "
                                 f"{hb['signals']} oneri. Acik oneri: {n_open}.")
        hb = {"rounds": 0, "signals": 0, "sent": today}
    cd["_hb"] = hb
    wk = now.strftime("%G-W%V")
    if now.weekday() == 6 and now.hour >= 17 and cd.get("_wk") != wk:
        wkly = [h for h in hist if h["status"] in ("HEDEF", "STOP")
                and time.time() - h.get("closed_ts", 0) < 7 * 86400]
        w = sum(1 for h in wkly if h["status"] == "HEDEF")
        l = len(wkly) - w
        notify("Haftalik ozet", f"Bu hafta {len(wkly)} oneri kapandi: {w} hedef, {l} stop, "
                                f"net {w * RR_TARGET - l:+g}R. Acik oneri: {n_open}.")
        cd["_wk"] = wk
    save_cd(cd)


def load_cd():
    try:
        with open(COOLDOWN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cd(cd):
    with open(COOLDOWN_FILE, "w", encoding="utf-8") as f:
        json.dump(cd, f)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def evaluate(h):
    """Öneriden sonraki 15 dk mumlara bakıp durumu bulur (stop ve hedef aynı mumdaysa stop sayılır)."""
    try:
        raw = api("/api/v3/klines", symbol=h["symbol"], interval="15m",
                  startTime=int(h["ts"] * 1000), limit=200)
    except RuntimeError:
        return h.get("status", "BEKLİYOR")
    filled = False
    for k in raw:
        hi, lo = float(k[2]), float(k[3])
        if not filled:
            if lo <= h["entry"]:
                filled = True
            else:
                continue
        if lo <= h["stop"]:
            return "STOP"
        if hi >= h["target"]:
            return "HEDEF"
    age = time.time() - h["ts"]
    if filled:
        return "AÇIK" if age < 48 * 3600 else "SÜRE DOLDU"
    return "BEKLİYOR" if age < 24 * 3600 else "DOLMADI"


def write_site(snap, hist):
    os.makedirs(SITE, exist_ok=True)
    done = [h for h in hist if h["status"] in ("HEDEF", "STOP")]
    wins = sum(1 for h in done if h["status"] == "HEDEF")
    losses = len(done) - wins
    snap["stats"] = {"wins": wins, "losses": losses,
                     "r": round(wins * RR_TARGET - losses, 2),
                     "open": sum(1 for h in hist if h["status"] in ("BEKLİYOR", "AÇIK"))}
    snap["history"] = hist[-30:][::-1]
    snap["updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    save_json(os.path.join(SITE, "data.json"), snap)


def run_round():
    hist = load_json(HISTORY, [])
    for h in hist:
        if h.get("status") not in FINAL:
            new = evaluate(h)
            if new in FINAL:
                h["closed_ts"] = time.time()
            h["status"] = new
    save_json(HISTORY, hist)
    snap = {"btc": None, "picks": [], "lead": "", "error": "", "approved": 0}
    err = None
    try:
        _round(snap, hist)
    except Exception as e:
        err = e
        snap["error"] = str(e)[:200]
    try:
        extras(snap, hist)
    except Exception as e:
        print("ekstra hata:", e)
    write_site(snap, hist)
    if err:
        raise err


def _round(snap, hist):
    now = datetime.datetime.now()
    btc = btc_regime()
    sent = sentiment()
    btc.update(sent)
    snap["sentiment"] = sent
    snap["btc"] = {"c4": round(btc["c4"], 2), "weak": btc["weak"]}
    now_t = time.time()
    active = [h for h in hist if h["status"] in ("BEKLİYOR", "AÇIK")]
    ctx = {"open_risk": sum(h["qty"] * (h["entry"] - h["stop"]) for h in active),
           "limit": ACCOUNT_USDT * MAX_TOTAL_RISK_PCT / 100,
           "day_r": sum((RR_TARGET if h["status"] == "HEDEF" else -1) for h in hist
                        if h["status"] in ("HEDEF", "STOP") and now_t - h.get("closed_ts", 0) < 86400)}
    snap["risk_ctx"] = {"open_pct": round(100 * ctx["open_risk"] / ACCOUNT_USDT, 2),
                        "limit_pct": MAX_TOTAL_RISK_PCT, "day_r": ctx["day_r"]}
    post("MARKET", f"{now:%Y-%m-%d %H:%M} | BTC 4s: {btc['c4']:+.1f}% "
                   f"{'(ZAYIF)' if btc['weak'] else ''}")

    cands = universe()
    open_syms = [h["symbol"] for h in hist if h["status"] in ("BEKLİYOR", "AÇIK")]
    tech = {}

    def get_tech(sym):
        if sym not in tech:
            try:
                tech[sym] = analyze(sym, btc)
            except Exception as e:
                print("analiz hatası", sym, e)
                tech[sym] = None
        return tech[sym]

    pinned = load_watchlist()
    for sym in pinned + [c[1] for c in cands[:WATCH_N]] + open_syms:
        get_tech(sym)
    snap["missing"] = [s_ for s_ in pinned if not tech.get(s_)]
    for s_ in pinned:
        if tech.get(s_):
            tech[s_]["pin"] = True

    def publish_watch():
        order = {"AL": 0, "ZAYIF": 1, "SAT": 2, "BEKLE": 3}
        pins = [tech[s_] for s_ in pinned if tech.get(s_)]
        ws = [t for t in tech.values() if t and not t.get("pin")]
        ws.sort(key=lambda t: (order[t["signal"]], -abs(t["score"])))
        snap["watch"] = pins + ws
        snap["now"] = {s: tech[s]["raw"] for s in open_syms if tech.get(s)}

    publish_watch()
    cd0 = load_cd()
    wst = cd0.get("_watch", {})
    for s_ in pinned:
        t = tech.get(s_)
        if not t:
            continue
        prev = wst.get(s_)
        if prev is not None and prev != t["signal"] and t["signal"] in ("AL", "SAT"):
            extra = (f" Stop {fmt(t['stop'])}, hedef {fmt(t['target'])}." if t["signal"] == "AL"
                     else " Elinde varsa cikisi dusun, yoksa alma.")
            notify(f"Takip: {s_[:-4]} {t['signal']}",
                   f"{s_}: teknik sinyal {prev} -> {t['signal']} (rejim {t['regime']}, "
                   f"skor {t['score']}, fiyat {fmt(t['price'])}).{extra}")
        wst[s_] = t["signal"]
    cd0["_watch"] = wst
    save_cd(cd0)
    for h in hist:
        t = tech.get(h["symbol"])
        if h["status"] == "AÇIK" and t and t["raw"] == "SAT" and not h.get("exit_warned"):
            notify("SAT uyarisi", f"{h['symbol']}: teknik skor SAT verdi. "
                                  f"Bu coini aldiysan cikisi dusun (stop {fmt(h['stop'])}).")
            h["exit_warned"] = True
            save_json(HISTORY, hist)

    picks = scanner(cands)
    if not picks:
        post("SCANNER", "NOTHING UNUSUAL")
        post("DESK LEAD", "Bu turda öneri yok. (Çoğu tur böyle olmalı.)")
        snap["lead"] = "Bu turda olağandışı hareket yok."
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
        p["risk"] = risk_check(p, btc, cd, ctx)
    post("RISK", "\n".join(f"{p['sym']}: {'PASS' if p['risk'][0] else 'FAIL'} - {p['risk'][1]}"
                           for p in picks))

    sk = []
    for p in picks:
        p["why"] = []
        if p["risk"][0]:
            p["vote"], p["why"] = skeptic(p, btc, get_tech(p["sym"]))
            sk.append(f"{p['sym']}: " + ("; ".join(p["why"]) if p["why"] else "güçlü itiraz yok")
                      + f"\n  Oy: {p['vote']}")
    post("SKEPTIC", "\n".join(sk) if sk else "Risk'ten geçen plan yok.")

    out, approved = [], []
    for p in picks:
        ok = p["risk"][0] and p.get("vote") == "BUY"
        p["decision"] = "ATLA"
        if ok and len(approved) < MAX_SIGNALS:
            qty = size(p["plan"]["entry"], p["plan"]["stop"])
            notional = qty * p["plan"]["entry"]
            if notional < MIN_NOTIONAL:
                p["note"] = f"pozisyon çok küçük ({notional:.1f} USDT)"
                out.append(f"{p['sym']}: SKIP ({p['note']})")
                continue
            new_r = qty * (p["plan"]["entry"] - p["plan"]["stop"])
            if ctx["open_risk"] + new_r > ctx["limit"]:
                p["note"] = "toplam açık risk limiti dolu"
                out.append(f"{p['sym']}: SKIP ({p['note']})")
                continue
            ctx["open_risk"] += new_r
            approved.append(p)
            p["decision"], p["qty"], p["notional"] = "AL", qty, notional
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
            p["decision"] = "BEKLE" if p.get("vote") == "WAIT" else "ATLA"
            p["note"] = why
            out.append(f"{p['sym']}: {p['decision']} ({why})")
    lead = "\n".join(out) + ("\n\nNot: Sadece öneri. Emri sen elle gir; "
                             "stopu mutlaka koy." if approved else "")
    post("DESK LEAD", lead)

    snap["lead"] = lead
    publish_watch()
    for p in picks:
        pl, n = p["plan"], p["news"]
        snap["picks"].append(dict(
            sym=p["sym"], price=p["price"], chg=round(p["chg"], 1), ratio=round(p["ratio"], 1),
            news=n[0] if n else "YOK", headline=n[1] if n else "",
            entry=pl["entry"], stop=pl["stop"], target=pl["target"],
            risk_ok=p["risk"][0], risk_note=p["risk"][1], vote=p.get("vote", ""),
            why=p.get("why", []), decision=p["decision"], note=p.get("note", "")))

    snap["approved"] = len(approved)
    if approved:
        notify("Kripto onerisi", "\n\n".join(o for o in out if o.startswith("AL")))
        for p in approved:
            pl = p["plan"]
            hist.append(dict(symbol=p["sym"], ts=time.time(),
                             time=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                             entry=pl["entry"], stop=pl["stop"], target=pl["target"],
                             qty=p["qty"], notional=round(p["notional"], 2),
                             news=p["news"][0] if p["news"] else "YOK", status="BEKLİYOR"))
            cd[p["sym"]] = time.time()
        save_json(HISTORY, hist)
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
