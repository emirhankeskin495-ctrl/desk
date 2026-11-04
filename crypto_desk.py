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
FWD_H = 6                 # Geçmiş sinyal istatistiği: sinyalden kaç saat sonrasına bakılsın
ADAPTIVE = True           # True: ağırlık profili her coin için geçmişten seçilir; False: hep statik "dengeli" profil
SLIP_PCT = 0.05           # Backtest: her işlem yönü için kayma (%)
VOL_HIGH = 1.8            # ATR% son 100 mum medyanının bu katını aşarsa "VOLATİL" rejim
VOL_RISK_MULT = 0.5       # VOLATİL rejimde pozisyon riski çarpanı
HOLDOUT = 240             # Son bu kadar mum HİÇBİR seçimde kullanılmaz; sadece bir kez final test olarak ölçülür
MIN_GAIN = 0.002          # Varsayılan profilden vazgeçmek için işlem başı en az %0,2 üstünlük şartı
MIN_TR_FIT = 8            # Profil seçiminde bir adayın değerlendirilmesi için gereken en az işlem sayısı
WF_TRAIN = 360            # Walk-forward: ağırlık seçimi için kullanılan geçmiş mum sayısı
WF_TEST = 120             # Walk-forward: seçilen ağırlıkların denendiği görülmemiş mum sayısı
TIMEFRAME = os.environ.get("TIMEFRAME", "1h")  # Sinyal zaman dilimi: "1h" (sakin) ya da "15m" (sık sinyal, daha çok gürültü ve komisyon)
CHART_N = 96              # Grafikte gösterilecek mum sayısı (1s)
MAX_TOTAL_RISK_PCT = 1.5  # Açık önerilerin toplam riski hesabın en fazla bu %'si olabilir
DAILY_LOSS_R = 3.0        # Son 24 saatte toplam kayıp bu kadar R'yi geçerse yeni öneri durur
FEE_PCT = 0.1             # Backtest: her işlem yönü için komisyon (%)
WATCH_COOLDOWN_H = 0      # Takip listesi: aynı coin için aynı yönde tekrar bildirim bekleme süresi (saat)
MIN_NOTIONAL = 5.0        # Bundan küçük pozisyonlar atlanır (Binance min ~5 USDT)

_TF = {"1h": (60, "4h", "1 saatlik", "4 saatlik", 240), "15m": (15, "1h", "15 dakikalık", "1 saatlik", 60)}
if TIMEFRAME not in _TF:
    TIMEFRAME = "1h"
TF_MIN, HTF, TF_LABEL, HTF_LABEL, HTF_MIN = _TF[TIMEFRAME]   # ana mum süresi, üst zaman dilimi (trend filtresi)

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
    """Kapanmış mumlar (son açık mum atılır). limit>1000 ise geriye doğru sayfalar."""
    raw, end, need = [], None, limit
    while need > 0:
        n = min(1000, need)
        params = dict(symbol=sym, interval=interval, limit=n)
        if end is not None:
            params["endTime"] = end
        page = api("/api/v3/klines", **params)
        if not page:
            break
        raw = page + raw
        need -= len(page)
        if len(page) < n:
            break
        end = int(page[0][0]) - 1
    out = [dict(t=int(k[0]), o=float(k[1]), h=float(k[2]), l=float(k[3]), c=float(k[4]),
                v=float(k[5])) for k in raw]
    return out[:-1]


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


def atr_series(k, n=14):
    L = len(k)
    out = [0.0] * L
    if L < n + 2:
        return out
    trs = [0.0] + [max(k[i]["h"] - k[i]["l"], abs(k[i]["h"] - k[i - 1]["c"]),
                       abs(k[i]["l"] - k[i - 1]["c"])) for i in range(1, L)]
    a = sum(trs[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, L):
        a = (a * (n - 1) + trs[i]) / n
        out[i] = a
    return out


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
                mh=mh, bu=bu, bl=bl, adx=adx_series(k), obv=obv, obvE=ema_series(obv, 20),
                atr=atr_series(k), v=[x["v"] for x in k])


# ======================================================================
#  SİNYAL ÜRETİMİ: saf fonksiyonlar. Bir mum için sadece o muma kadar olan veriyi kullanır.
# ======================================================================
FSTART = 120   # göstergelerin ve volatilite tabanının oturduğu ilk mum


def features(j, S):
    """j. mumun kapanışında bilinen her şey: gösterge oyları, hacim teyidi, volatilite ve rejim."""
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
    vols = S["v"][j - 20:j]
    vol_ok = S["v"][j] >= sum(vols) / len(vols)
    base = sorted(S["atr"][q] / S["c"][q] for q in range(j - 100, j))[50]
    vr = (S["atr"][j] / c) / base if base else 1.0
    adx = S["adx"][j]
    regime = "VOLATİL" if vr >= VOL_HIGH else ("TREND" if adx >= ADX_TREND else "YATAY")
    return dict(ema=ema_v, macd=macd_v, don=don_v, obv=obv_v, rsi=rsi_v, bb=bb_v, vol_ok=vol_ok,
                vr=vr, adx=adx, regime=regime, rsi_val=r)


# Ağırlık profilleri: sırayla (ema, macd, donchian, obv) ve (rsi, bollinger, obv)
TREND_PRE = {"dengeli": (1, 1, 1, 1), "ema_odak": (2, 1, 1, 1),
             "macd_odak": (1, 2, 1, 1), "kirilim_odak": (1, 1, 2, 1)}
RANGE_PRE = {"dengeli": (1, 1, 1), "rsi_odak": (2, 1, 1), "bb_odak": (1, 2, 1)}
TREND_NAMES = ("EMA 20/50", "MACD", "Donchian", "OBV")
RANGE_NAMES = ("RSI", "Bollinger", "OBV")
THR = {"TREND": 0.7, "YATAY": 0.6, "VOLATİL": 0.8}   # AL/SAT eşiği (VOLATİL rejimde daha sıkı)
THR_WEAK = 0.4
COMBOS = [(tp, rp) for tp in TREND_PRE for rp in RANGE_PRE]
DEFAULT = ("dengeli", "dengeli")


def sig_from(f, tp, rp):
    """Ağırlıklı skor ve sinyal. Yatay rejimde RSI/Bollinger, trend rejiminde EMA/MACD/Donchian öne çıkar;
    trend gücü (ADX) trend skorunu büyütür; volatil rejimde eşik yükselir."""
    reg = f["regime"]
    if reg == "YATAY":
        w = RANGE_PRE[rp]
        s = sum(a * b for a, b in zip(w, (f["rsi"], f["bb"], f["obv"]))) / sum(w)
        if s >= THR[reg] and (f["rsi"] == 1 or f["bb"] == 1):
            return s, "AL"
        if s <= -THR[reg] and (f["rsi"] == -1 or f["bb"] == -1):
            return s, "SAT"
        if f["rsi_val"] < 40 and f["obv"] == 1:
            return s, "ZAYIF"
        return s, "BEKLE"
    w = TREND_PRE[tp]
    s = sum(a * b for a, b in zip(w, (f["ema"], f["macd"], f["don"], f["obv"]))) / sum(w)
    if reg == "TREND":
        s *= min(1.3, f["adx"] / ADX_TREND)
    if s >= THR[reg]:
        return s, "AL"
    if s <= -THR[reg]:
        return s, "SAT"
    if reg == "TREND" and s >= THR_WEAK and f["ema"] == 1:
        return s, "ZAYIF"
    return s, "BEKLE"


def trend_of(c):
    e20, e50 = ema_series(c, 20)[-1], ema_series(c, 50)[-1]
    return "YUKARI" if (c[-1] > e50 and e20 > e50) else ("AŞAĞI" if (c[-1] < e50 and e20 < e50) else "YATAY")


def htf_trends(hk):
    c = [x["c"] for x in hk]
    e20, e50 = ema_series(c, 20), ema_series(c, 50)
    return ["YUKARI" if (c[i] > e50[i] and e20[i] > e50[i]) else
            ("AŞAĞI" if (c[i] < e50[i] and e20[i] < e50[i]) else "YATAY") for i in range(len(c))]


def align_htf(k, hk, htr):
    """Her mum için, o mum KAPANDIĞI anda tamamlanmış olan son üst zaman dilimi mumunun trendi (geleceğe bakma yok)."""
    out, h = [], -1
    for j in range(len(k)):
        close_t = k[j]["t"] + TF_MIN * 60000
        while h + 1 < len(hk) and hk[h + 1]["t"] + HTF_MIN * 60000 <= close_t:
            h += 1
        out.append(htr[h] if h >= 0 else "YATAY")
    return out


# ======================================================================
#  DEĞERLENDİRME: sinyal üretiminden ayrı. Komisyon + kayma dahil, pencere dışına bakmaz.
# ======================================================================
def _stat(flips, kind, c):
    fwd = FWD_H * 60 // TF_MIN
    xs = [c[j + fwd] / c[j] - 1 for j, t in flips if t == kind and j + fwd < len(c)]
    if not xs:
        return {"n": 0, "hit": 0, "avg": 0}
    good = [x for x in xs if (x > 0 if kind == "AL" else x < 0)]
    return {"n": len(xs), "hit": round(100 * len(good) / len(xs)),
            "avg": round(100 * sum(xs) / len(xs), 2)}


def simulate(k, S, F, fs, a, b):
    """fs: nihai sinyal listesi. Sadece [a, b) penceresinde işlem açar ve pencere dışındaki mumlara bakmaz.
    Giriş: sinyalin ertesi mumunun açılışı (+kayma). Çıkış: stop (kayma ile), hedef, SAT sinyali ya da 48 mum."""
    fee, slip = FEE_PCT / 100, SLIP_PCT / 100
    trades, busy = [], a - 1
    for j in range(max(a, FSTART + 1), b - 1):
        if j <= busy or fs[j] != "AL" or fs[j - 1] == "AL":
            continue
        entry = k[j + 1]["o"] * (1 + slip)
        stop = entry - ATR_MULT * S["atr"][j]
        if stop <= 0 or entry <= stop:
            continue
        target = entry + RR_TARGET * (entry - stop)
        last = min(b - 1, j + 48)
        xp = xi = None
        for m in range(j + 1, last + 1):
            if k[m]["l"] <= stop:
                xp, xi = stop * (1 - slip), m
                break
            if k[m]["h"] >= target:
                xp, xi = target, m
                break
            if fs[m] == "SAT":
                xp, xi = k[m]["c"] * (1 - slip), m
                break
        if xp is None:
            xi = last
            xp = k[last]["c"] * (1 - slip)
        net = xp / entry - 1 - 2 * fee
        frac = min(RISK_PCT / 100 / ((entry - stop) / entry), MAX_POS_PCT / 100)
        if F[j]["regime"] == "VOLATİL":
            frac *= VOL_RISK_MULT
        trades.append(dict(j=j, ei=j + 1, ep=entry, frac=frac, xi=xi, net=net, pe=frac * net,
                           reg=F[j]["regime"]))
        busy = xi
    return trades


def metrics(trades):
    n = len(trades)
    if not n:
        return dict(n=0, win=0, avg=0.0, ret=0.0, pf=None)
    eq = 1.0
    for t in trades:
        eq *= 1 + t["pe"]
    gp = sum(t["pe"] for t in trades if t["pe"] > 0)
    gl = -sum(t["pe"] for t in trades if t["pe"] < 0)
    return dict(n=n, win=round(100 * sum(1 for t in trades if t["net"] > 0) / n),
                avg=round(100 * sum(t["net"] for t in trades) / n, 2),
                ret=round(100 * (eq - 1), 2), pf=round(gp / gl, 2) if gl else None)


def by_regime(trades):
    out = {}
    for r in ("TREND", "YATAY", "VOLATİL"):
        xs = [t for t in trades if t["reg"] == r]
        out[r] = dict(n=len(xs), avg=round(100 * sum(t["net"] for t in xs) / len(xs), 2) if xs else 0.0)
    return out


def mtm(trades, k, a, b):
    """Mum bazında özsermaye (açık pozisyon dahil, kapanışa göre) ve en kötü ARA-MUM düşüşü (mumun dibine göre)."""
    fee2 = 2 * FEE_PCT / 100
    eq, ptr, peak, dd, curve = 1.0, 0, 1.0, 0.0, []
    for q in range(a, b):
        while ptr < len(trades) and trades[ptr]["xi"] < q:
            eq *= 1 + trades[ptr]["pe"]
            ptr += 1
        val = low = eq
        if ptr < len(trades) and trades[ptr]["ei"] <= q <= trades[ptr]["xi"]:
            t = trades[ptr]
            low = eq * (1 + t["frac"] * (k[q]["l"] / t["ep"] - 1 - fee2))
            val = eq * (1 + t["pe"]) if q == t["xi"] else eq * (1 + t["frac"] * (k[q]["c"] / t["ep"] - 1 - fee2))
        peak = max(peak, val)
        dd = max(dd, 1 - min(val, low) / peak)
        curve.append(val)
    return curve, dd


def _idx(n, m=80):
    step = max(1, n // m)
    idx = list(range(0, n, step))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    return idx


def bt_from(trades, k, S, a, L):
    """Tüm dönem backtest (doğrulama değil, iyimser): eğitim ve test ayrımı yok."""
    curve, dd = mtm(trades, k, a, L)
    c0 = S["c"][a]
    hold = [1 + MAX_POS_PCT / 100 * (S["c"][q] / c0 - 1) for q in range(a, L)]
    m = metrics(trades)
    idx = _idx(len(curve))
    return dict(n=m["n"], win=m["win"], ret=m["ret"], bh=round(100 * (hold[-1] - 1), 2),
                dd=round(100 * dd, 2), pf=m["pf"], days=round((L - a) * TF_MIN / 1440),
                hold_h=round(48 * TF_MIN / 60),
                curve=[round(100 * (curve[q] - 1), 2) for q in idx],
                bhc=[round(100 * (hold[q] - 1), 2) for q in idx])


def fit(k, S, F, fin, a, b):
    """[a, b) penceresinde ağırlık profili seçer. Varsayılan (dengeli) profilden ancak, aday en az MIN_TR_FIT işlem
    yaptıysa ve işlem başı net getiriyi en az MIN_GAIN kadar artırıyorsa vazgeçilir (gürültüye uymayı azaltır).
    Dönüş: (profil, seçim anındaki işlem başı getiri ya da None, varsayılandan farklı mı)."""
    base = simulate(k, S, F, fin[DEFAULT], a, b)
    base_sc = sum(t["net"] for t in base) / len(base) if len(base) >= 4 else None
    best, bs = DEFAULT, None
    for cb in COMBOS:
        if cb == DEFAULT:
            continue
        tr = simulate(k, S, F, fin[cb], a, b)
        if len(tr) < MIN_TR_FIT:
            continue
        sc = sum(t["net"] for t in tr) / len(tr)
        if sc >= (base_sc or 0.0) + MIN_GAIN and (bs is None or sc > bs):
            best, bs = cb, sc
    return best, (bs if bs is not None else base_sc), best != DEFAULT


def walk_forward(k, S, F, fin, L):
    """1) Walk-forward: her pencerede profil SADECE geçmiş WF_TRAIN mumdan seçilir, sonraki WF_TEST mumda denenir.
    2) Final test: son HOLDOUT mum seçimde/pencerelerde hiç kullanılmaz; profil bu bölgeden hemen önceki
    WF_TRAIN mumdan seçilip bir kez denenir. Her ikisi statik (dengeli) profille kıyaslanır.
    Dönüş: (özet, adaptif görülmemiş işlemler)."""
    E = L - 1 - HOLDOUT
    s0, ad, st, iss, nf, sw, first, last = FSTART + 1, [], [], [], 0, 0, None, None
    while s0 + WF_TRAIN + WF_TEST <= E:
        tr_b = s0 + WF_TRAIN
        cb, sc, switched = fit(k, S, F, fin, s0, tr_b)
        ad += simulate(k, S, F, fin[cb], tr_b, tr_b + WF_TEST)
        st += simulate(k, S, F, fin[DEFAULT], tr_b, tr_b + WF_TEST)
        if sc is not None:
            iss.append(sc)
        nf += 1
        sw += 1 if switched else 0
        first = tr_b if first is None else first
        last = tr_b + WF_TEST
        s0 += WF_TEST
    if nf < 2:
        return None, []
    ca, dda = mtm(ad, k, first, last)
    cs, dds = mtm(st, k, first, last)
    idx = _idx(len(ca))
    ho = None
    if E - WF_TRAIN >= FSTART + 1:
        cbh = fit(k, S, F, fin, E - WF_TRAIN, E)[0]
        had = simulate(k, S, F, fin[cbh], E, L - 1)
        hst = simulate(k, S, F, fin[DEFAULT], E, L - 1)
        ho = dict(days=round(HOLDOUT * TF_MIN / 1440, 1), ad=metrics(had), st=metrics(hst),
                  dd_ad=round(100 * mtm(had, k, E, L - 1)[1], 2), dd_st=round(100 * mtm(hst, k, E, L - 1)[1], 2))
    return dict(folds=nf, train=WF_TRAIN, test=WF_TEST, days=round((last - first) * TF_MIN / 1440, 1),
                profiles=len(COMBOS), switched=sw,
                ad=metrics(ad), st=metrics(st), reg_ad=by_regime(ad), reg_st=by_regime(st),
                dd_ad=round(100 * dda, 2), dd_st=round(100 * dds, 2),
                is_avg=round(100 * sum(iss) / len(iss), 2) if iss else 0.0,
                curve=[round(100 * (ca[q] - 1), 2) for q in idx],
                stc=[round(100 * (cs[q] - 1), 2) for q in idx], ho=ho), ad


# ======================================================================
#  İLERİ TEST: canlı, gerçek zamanlı, bağımsız doğrulama (adaptif ve statik yan yana)
# ======================================================================
def fwd_resolve(e):
    """Giriş: sinyal mumunu izleyen ilk 15 dk mumunun açılışı (+kayma). Stop önce sayılır. Net R hesaplanır."""
    slip = SLIP_PCT / 100
    try:
        raw = api("/api/v3/klines", symbol=e["sym"], interval="15m", startTime=int(e["sig_t"]), limit=400)
    except RuntimeError:
        return
    raw = [x for x in raw if int(x[0]) >= e["sig_t"]]
    if not raw:
        return
    if "entry" not in e:
        e["entry"] = float(raw[0][1]) * (1 + slip)
        e["stop"] = e["entry"] - ATR_MULT * e["atr"]
        e["target"] = e["entry"] + RR_TARGET * (e["entry"] - e["stop"])
        if e["stop"] <= 0:
            e["status"] = "GEÇERSİZ"
            return
    hold_ms = 48 * TF_MIN * 60000
    for x in raw:
        hi, lo, cl = float(x[2]), float(x[3]), float(x[4])
        if lo <= e["stop"]:
            e["status"], e["r"] = "STOP", _rnet(e["entry"], e["stop"], e["stop"] * (1 - slip))
            return
        if hi >= e["target"]:
            e["status"], e["r"] = "HEDEF", _rnet(e["entry"], e["stop"], e["target"])
            return
        if int(x[0]) + 900000 - e["sig_t"] >= hold_ms:
            e["status"], e["r"] = "SÜRE DOLDU", _rnet(e["entry"], e["stop"], cl * (1 - slip))
            return
    e["status"] = "AÇIK"


def fwd_open(cd, cands):
    """Her analiz edilen coin için, nihai sinyal AL'a DÖNDÜĞÜNDE (adaptif ve statik ayrı) bir ileri test kaydı açar."""
    st, log, now = cd.setdefault("_fstate", {}), cd.setdefault("_tlog", []), time.time()
    for c in cands:
        for var, sig in (("adaptif", c["sig_ad"]), ("statik", c["sig_st"])):
            key = f"{c['sym']}:{var}"
            prev = st.get(key)
            fresh = prev is not None and now - prev[1] < 3 * 3600
            dup = any(e["sym"] == c["sym"] and e["variant"] == var and
                      (e["status"] == "AÇIK" or e["sig_t"] == c["sig_t"]) for e in log)
            if sig == "AL" and fresh and prev[0] != "AL" and not dup:
                log.append(dict(sym=c["sym"], variant=var, sig_t=c["sig_t"], atr=c["atr"],
                                conf=c["conf"] if var == "adaptif" else None,
                                regime=c["regime"], status="AÇIK"))
            st[key] = [sig, now]
    for e in log:
        if e["status"] == "AÇIK":
            fwd_resolve(e)
    if len(log) > 400:
        done = [e for e in log if e["status"] != "AÇIK"]
        drop = set(id(e) for e in done[:len(log) - 400])
        cd["_tlog"] = [e for e in log if id(e) not in drop]


def fwd_stats(log):
    fin_st = ("HEDEF", "STOP", "SÜRE DOLDU")
    out = {"variants": {}, "calib": [], "brier": None,
           "pending": sum(1 for e in log if e["status"] == "AÇIK")}
    for var in ("adaptif", "statik"):
        rs = [e for e in log if e["variant"] == var and e["status"] in fin_st]
        n = len(rs)
        out["variants"][var] = dict(n=n, win=round(100 * sum(1 for e in rs if e["r"] > 0) / n) if n else 0,
                                    avg_r=round(sum(e["r"] for e in rs) / n, 2) if n else 0.0,
                                    tot_r=round(sum(e["r"] for e in rs), 2))
    ad = [e for e in log if e["variant"] == "adaptif" and e["status"] in fin_st and e.get("conf") is not None]
    for lo, hi in ((0, 50), (50, 65), (65, 80), (80, 101)):
        xs = [e for e in ad if lo <= e["conf"] < hi]
        out["calib"].append(dict(lo=lo, hi=min(hi, 100), n=len(xs),
                                 obs=round(100 * sum(1 for e in xs if e["r"] > 0) / len(xs)) if xs else None))
    if ad:
        y = [1 if e["r"] > 0 else 0 for e in ad]
        base = sum(y) / len(y)
        out["brier"] = dict(score=round(sum((e["conf"] / 100 - v) ** 2 for e, v in zip(ad, y)) / len(ad), 3),
                            base=round(base * (1 - base), 3), n=len(ad))
    return out


# ======================================================================
#  GÜVEN PUANI VE AÇIKLAMA
# ======================================================================
def confidence(f, sig, s, align, rel):
    """Kural tabanlı güven puanı (kalibre edilmiş olasılık DEĞİL)."""
    if sig not in ("AL", "ZAYIF", "SAT"):
        return None, []
    c = 40 + 40 * min(1.0, abs(s))
    parts = [f"skor gücü {round(c)}"]
    if not f["vol_ok"]:
        c -= 15
        parts.append("hacim teyidi yok -15")
    want, opp = ("AŞAĞI", "YUKARI") if sig == "SAT" else ("YUKARI", "AŞAĞI")
    ag = sum(1 for v in align.values() if v == want)
    op = sum(1 for v in align.values() if v == opp)
    d = (ag - op) * 4
    c += d
    parts.append(f"zaman dilimi uyumu {ag}/4 {d:+d}")
    d = round((rel - 0.5) * 30)
    c += d
    parts.append(f"benzer geçmiş sinyaller {d:+d}")
    if f["regime"] == "VOLATİL":
        c -= 10
        parts.append("yüksek volatilite -10")
    return int(max(5, min(95, round(c)))), parts


def explain(f, s, cb, align):
    reg = f["regime"]
    out = [f"Rejim: {reg} (ADX {f['adx']:.0f}, volatilite normalin {f['vr']:.1f} katı)"]
    if reg == "YATAY":
        out.append(f"RSI {f['rsi_val']:.0f}: " + ("aşırı satım bölgesi (+)" if f["rsi"] == 1 else
                   "aşırı alım bölgesi (−)" if f["rsi"] == -1 else "nötr bölge (0)"))
        out.append("Bollinger: " + ("fiyat alt bandın altında (+)" if f["bb"] == 1 else
                   "fiyat üst bandın üstünde (−)" if f["bb"] == -1 else "fiyat bant içinde (0)"))
    else:
        out.append("EMA20/50: " + ("EMA20 > EMA50 ve fiyat EMA50 üstünde (+)" if f["ema"] == 1 else
                   "EMA20 < EMA50 ve fiyat EMA50 altında (−)" if f["ema"] == -1 else "EMA'lar karışık (0)"))
        out.append("MACD histogramı: " + ("pozitif ve güçleniyor (+)" if f["macd"] == 1 else
                   "negatif ve zayıflıyor (−)" if f["macd"] == -1 else "kararsız (0)"))
        out.append("Donchian: " + ("20 mumluk tepe kırıldı (+)" if f["don"] == 1 else
                   "20 mumluk dip kırıldı (−)" if f["don"] == -1 else "kırılım yok (0)"))
    out.append("OBV: " + ("ortalamasının üstünde, hacim girişi (+)" if f["obv"] == 1 else
               "ortalamasının altında, hacim çıkışı (−)"))
    out.append("Hacim teyidi: " + ("var" if f["vol_ok"] else "yok, güven puanı düşürüldü"))
    out.append(f"Ağırlık profili: trend={cb[0]}, yatay={cb[1]}; ağırlıklı skor {s:+.2f}")
    out.append("Zaman dilimleri: " + ", ".join(f"{a} {b.lower()}" for a, b in align.items()))
    return out


def analyze(sym, btc):
    k = klines(sym, TIMEFRAME, 2000)
    if len(k) < 200:
        return None
    hk = klines(sym, HTF, 600)
    S = build_series(k)
    L = len(k)
    F = [None] * L
    for j in range(FSTART, L):
        F[j] = features(j, S)
    htf = align_htf(k, hk, htf_trends(hk))
    raw_sg, fin = {}, {}
    for cb in COMBOS:
        sg = [sig_from(f, *cb) if f else (0.0, "BEKLE") for f in F]
        raw_sg[cb] = sg
        fin[cb] = ["BEKLE" if (g[1] in ("AL", "ZAYIF") and h == "AŞAĞI") else g[1]
                   for g, h in zip(sg, htf)]
    i = L - 1
    cb_live = fit(k, S, F, fin, max(FSTART + 1, L - 1 - WF_TRAIN), L - 1)[0] if ADAPTIVE else DEFAULT
    s_live, raw = raw_sg[cb_live][i]
    signal, note = fin[cb_live][i], ""
    sig_st = fin[DEFAULT][i]
    if raw in ("AL", "ZAYIF") and signal == "BEKLE":
        note = f"{HTF_LABEL} trend aşağı, {raw} iptal"
    elif raw in ("AL", "ZAYIF") and btc["weak"]:
        signal, note = "BEKLE", f"BTC zayıf, {raw} iptal"
    if sig_st in ("AL", "ZAYIF") and btc["weak"]:
        sig_st = "BEKLE"
    f = F[i]
    hs = htf[i]
    tfc = {TIMEFRAME: trend_of(S["c"]), HTF: hs}
    for tf in ("15m", "1h", "4h", "1d"):
        if tf not in tfc:
            try:
                tfc[tf] = trend_of([x["c"] for x in klines(sym, tf, 100)])
            except Exception:
                tfc[tf] = "?"
    align = {tf: tfc[tf] for tf in ("15m", "1h", "4h", "1d")}
    fs_live = fin[cb_live]
    can_wf = L >= FSTART + 1 + WF_TRAIN + 2 * WF_TEST + HOLDOUT + 2
    wf, ad_oos = walk_forward(k, S, F, fin, L) if can_wf else (None, [])
    same = [t for t in ad_oos if t["reg"] == f["regime"]]
    rel = (sum(1 for t in same if t["net"] > 0) + 2) / (len(same) + 4)
    conf, parts = confidence(f, signal if signal != "BEKLE" else raw, s_live, align, rel)
    why = explain(f, s_live, cb_live, align)
    why.append(f"Benzer sinyaller ({f['regime']} rejimi, görülmemiş walk-forward işlemlerinden): "
               f"{len(same)} işlem, %{round(100 * rel)} başarı (az işlemde 50'ye çekilmiş tahmin)")
    flips = [(j, fs_live[j]) for j in range(FSTART + 1, L)
             if fs_live[j] != fs_live[j - 1] and fs_live[j] in ("AL", "SAT")]
    price = S["c"][i]
    stop = price - ATR_MULT * S["atr"][i]
    s0 = L - CHART_N
    rows = lambda arr: [r6(x) for x in arr[s0:]]
    if f["regime"] == "YATAY":
        names, vals, wts = RANGE_NAMES, (f["rsi"], f["bb"], f["obv"]), RANGE_PRE[cb_live[1]]
    else:
        names, vals, wts = TREND_NAMES, (f["ema"], f["macd"], f["don"], f["obv"]), TREND_PRE[cb_live[0]]
    return dict(
        sym=sym, price=r6(price), signal=signal, raw=raw, score=round(s_live, 2), regime=f["regime"],
        votes=dict(zip(names, vals)), wts=dict(zip(names, wts)), trend4=hs,
        rsi=round(S["rsi"][i]), adx=round(f["adx"]), vr=round(f["vr"], 1), vol_ok=f["vol_ok"],
        note=note, stop=r6(stop), target=r6(price + RR_TARGET * (price - stop)),
        conf=conf, conf_parts=parts, explain=why, mtf=align,
        profile={"trend": cb_live[0], "range": cb_live[1]},
        risk_mult=VOL_RISK_MULT if f["regime"] == "VOLATİL" else 1.0,
        sig_t=k[-1]["t"] + TF_MIN * 60000, atr=S["atr"][i], sig_static=sig_st,
        bt=bt_from(simulate(k, S, F, fin[DEFAULT], FSTART + 1, L), k, S, FSTART + 1, L),
        wf=wf,
        stats={"al": _stat(flips, "AL", S["c"]), "sat": _stat(flips, "SAT", S["c"]),
               "days": round((L - FSTART) * TF_MIN / 1440), "fwd_h": FWD_H},
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
            why.append(f"{HTF_LABEL} trend aşağı")
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
    fwd_open(cd, snap.pop("_fw", []))
    snap["forward"] = fwd_stats(cd.get("_tlog", []))
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
        wkly = [h for h in hist if h["status"] in ("HEDEF", "STOP", "SÜRE DOLDU")
                and time.time() - h.get("closed_ts", 0) < 7 * 86400]
        w = sum(1 for h in wkly if h["status"] == "HEDEF")
        l = len(wkly) - w
        notify("Haftalik ozet", f"Bu hafta {len(wkly)} oneri kapandi: {w} hedef, {l} stop, "
                                f"net {sum(hr(h) for h in wkly):+.2f}R (maliyet dahil). Acik oneri: {n_open}.")
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


def _rnet(entry, stop, xp):
    """Net R: komisyon (iki yön) dahil, riskin katı olarak."""
    return round((xp / entry - 1 - 2 * FEE_PCT / 100) / ((entry - stop) / entry), 3)


def hr(h):
    """Bir önerinin net R değeri (eski kayıtlarda brüt: hedef=+RR, stop=-1)."""
    if "r" in h:
        return h["r"]
    return RR_TARGET if h["status"] == "HEDEF" else (-1.0 if h["status"] == "STOP" else 0.0)


def evaluate(h):
    """Limit emir takibi. Kurallar: sinyalden ÖNCE açılmış mumlar kullanılmaz; limit, fiyat limitin ALTINA
    inince dolmuş sayılır (sadece dokunmak yetmez); dolum mumunda yalnızca stop sayılır (hedef bir sonraki
    mumdan itibaren); stop ve hedef aynı mumdaysa stop. Dönüş: (durum, net_R ya da None)."""
    slip = SLIP_PCT / 100
    try:
        raw = api("/api/v3/klines", symbol=h["symbol"], interval="15m",
                  startTime=int(h["ts"] * 1000), limit=200)
    except RuntimeError:
        return h.get("status", "BEKLİYOR"), h.get("r")
    filled, lastc = False, None
    for k in raw:
        if int(k[0]) < h["ts"] * 1000:
            continue
        hi, lo, lastc = float(k[2]), float(k[3]), float(k[4])
        if not filled:
            if lo < h["entry"]:
                filled = True
                if lo <= h["stop"]:
                    return "STOP", _rnet(h["entry"], h["stop"], h["stop"] * (1 - slip))
            continue
        if lo <= h["stop"]:
            return "STOP", _rnet(h["entry"], h["stop"], h["stop"] * (1 - slip))
        if hi >= h["target"]:
            return "HEDEF", _rnet(h["entry"], h["stop"], h["target"])
    age = time.time() - h["ts"]
    if filled:
        if age < 48 * 3600:
            return "AÇIK", None
        return "SÜRE DOLDU", _rnet(h["entry"], h["stop"], (lastc or h["entry"]) * (1 - slip))
    return ("BEKLİYOR", None) if age < 24 * 3600 else ("DOLMADI", None)


def write_site(snap, hist):
    os.makedirs(SITE, exist_ok=True)
    done = [h for h in hist if h["status"] in ("HEDEF", "STOP")]
    wins = sum(1 for h in done if h["status"] == "HEDEF")
    losses = len(done) - wins
    snap["stats"] = {"wins": wins, "losses": losses,
                     "r": round(sum(hr(h) for h in hist
                                    if h["status"] in ("HEDEF", "STOP", "SÜRE DOLDU")), 2),
                     "open": sum(1 for h in hist if h["status"] in ("BEKLİYOR", "AÇIK"))}
    snap["history"] = hist[-30:][::-1]
    snap["updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    save_json(os.path.join(SITE, "data.json"), snap)


def run_round():
    hist = load_json(HISTORY, [])
    for h in hist:
        if h.get("status") not in FINAL:
            new, r = evaluate(h)
            if new in FINAL:
                h["closed_ts"] = time.time()
                if r is not None:
                    h["r"] = r
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
    snap["tf"] = {"tf": TIMEFRAME, "label": TF_LABEL, "hlabel": HTF_LABEL}
    now_t = time.time()
    active = [h for h in hist if h["status"] in ("BEKLİYOR", "AÇIK")]
    ctx = {"open_risk": sum(h["qty"] * (h["entry"] - h["stop"]) for h in active),
           "limit": ACCOUNT_USDT * MAX_TOTAL_RISK_PCT / 100,
           "day_r": round(sum(hr(h) for h in hist
                              if h["status"] in ("HEDEF", "STOP", "SÜRE DOLDU")
                              and now_t - h.get("closed_ts", 0) < 86400), 2)}
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
    snap["_fw"] = [dict(sym=t["sym"], sig_ad=t["signal"], sig_st=t["sig_static"], sig_t=t["sig_t"],
                        atr=t["atr"], conf=t["conf"], regime=t["regime"]) for t in tech.values() if t]
    cd0 = load_cd()
    wst = cd0.get("_watch", {})
    for s_ in pinned:
        t = tech.get(s_)
        if not t:
            continue
        prev = wst.get(s_)
        wl = cd0.setdefault("_wlast", {})
        wkey = f"{s_}:{t['signal']}"
        if (prev is not None and prev != t["signal"] and t["signal"] in ("AL", "SAT")
                and time.time() - wl.get(wkey, 0) >= WATCH_COOLDOWN_H * 3600):
            extra = (f" Stop {fmt(t['stop'])}, hedef {fmt(t['target'])}." if t["signal"] == "AL"
                     else " Elinde varsa cikisi dusun, yoksa alma.")
            notify(f"Takip: {s_[:-4]} {t['signal']}",
                   f"{s_}: teknik sinyal {prev} -> {t['signal']} (rejim {t['regime']}, "
                   f"skor {t['score']}, fiyat {fmt(t['price'])}).{extra}"
                   + (f" Güven {t['conf']}/100." if t.get("conf") is not None else "")
                   + (" Yüksek volatilite: pozisyonu yarıya indir." if t.get("risk_mult", 1) < 1 else ""))
            wl[wkey] = time.time()
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
            rm = (get_tech(p["sym"]) or {}).get("risk_mult", 1.0)
            qty = math.floor(size(p["plan"]["entry"], p["plan"]["stop"]) * rm * 1e6) / 1e6
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
                f"  gerekçe: hacim {p['ratio']:.1f}x, 24s {p['chg']:+.1f}%, {nw}"
                + (", yüksek volatilite: risk yarıya indirildi" if rm < 1 else ""))
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
