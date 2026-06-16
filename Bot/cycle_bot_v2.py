#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات چرخه سه‌سشنه + رفتارشناسی + نقدینگی + خبر (نسخه ۲)
═══════════════════════════════════════════════════════════
قابلیت‌ها:
  🌏🇬🇧🇺🇸 چرخه سه‌سشنه: آسیا (شکست 1H) → لندن (پولبک 15M) → US (حرکت 15M+5M)
  ⏰ هشدار تایم‌های US: Open / Lunch / Power Hour (به همه اعضا)
  📅 تقویم اقتصادی + خبر زنده بیت‌کوین (FOMC/CPI/NFP + اخبار)
  💧 رصد نقدینگی مستقل: ورود/خروج مکرر پول (از دیتابیس)
  🧮 تحلیل سناریو با numpy/pandas: احتمال بر اساس رفتار گذشته
  👥 مدیریت اعضا

پیش‌نیاز:  pip install requests numpy pandas
Env Vars:  TELEGRAM_TOKEN, ADMIN_CHAT, SCAN_INTERVAL (پیش‌فرض 300)
           NEWS_API (اختیاری: کلید cryptopanic برای خبر زنده)
"""

import os, time, sqlite3, threading
from datetime import datetime, timezone, timedelta

try:
    import requests
except ImportError:
    raise SystemExit("نصب کن:  pip install requests numpy pandas")

# numpy/pandas اختیاری — اگر نبود، تحلیل ساده‌تر می‌شود
try:
    import numpy as np
    import pandas as pd
    HAS_NP = True
except ImportError:
    HAS_NP = False
    print("[!] numpy/pandas نصب نیست — تحلیل آماری ساده می‌شود. برای کامل: pip install numpy pandas")

TOKEN      = os.environ.get("TELEGRAM_TOKEN", "")
ADMIN_CHAT = os.environ.get("ADMIN_CHAT", "")
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "300"))
NEWS_API   = os.environ.get("NEWS_API", "")   # کلید cryptopanic (اختیاری)
BASE = "https://fapi.binance.com"
SPOT_BASE = "https://api.binance.com"
TG   = f"https://api.telegram.org/bot{TOKEN}"
DB_FILE = "cycle_bot.db"
ALERT_COOLDOWN = 45 * 60
LIQ_COOLDOWN   = 60 * 60
BR_COOLDOWN    = 60 * 60

# لیست نمادها (برای دسترسی session_alert_loop به اسکن Pre-Market)
SCAN_SYMBOLS = []

DISCLAIMER = ("⚠️ <b>سلب مسئولیت:</b> ابزار آموزشی/اطلاعاتی، نه سیگنال مالی. "
              "تحلیل‌ها آماری و بر اساس رفتار گذشته‌اند. مسئولیت معاملات با خودت است.")

# ── تقویم اقتصادی ۲۰۲۶ (UTC) ──
ECON_EVENTS = [
    ("2026-06-11", "CPI (تورم می)"),
    ("2026-06-17", "FOMC + نرخ بهره"),
    ("2026-07-03", "NFP (اشتغال)"),
    ("2026-07-15", "CPI (تورم ژوئن)"),
    ("2026-07-29", "FOMC + نرخ بهره"),
    ("2026-08-07", "NFP (اشتغال)"),
    ("2026-08-12", "CPI (تورم ژوئیه)"),
    ("2026-09-04", "NFP (اشتغال)"),
    ("2026-09-11", "CPI (تورم اوت)"),
    ("2026-09-16", "FOMC + نرخ بهره"),
]


# ═══════════ دیتابیس ═══════════
def db():
    conn = sqlite3.connect(DB_FILE); conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db(); c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS members(
        chat_id TEXT PRIMARY KEY, username TEXT, joined TEXT, active INTEGER DEFAULT 1)""")
    c.execute("""CREATE TABLE IF NOT EXISTS cycle(
        symbol TEXT, day TEXT, asia_break TEXT, asia_method TEXT, asia_level REAL, asia_ts REAL,
        london_pullback INTEGER DEFAULT 0, london_quality TEXT, london_ts REAL,
        us_move INTEGER DEFAULT 0, us_ts REAL, PRIMARY KEY(symbol, day))""")
    c.execute("""CREATE TABLE IF NOT EXISTS history(
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, day TEXT,
        completed INTEGER, direction TEXT, ts REAL)""")
    # جدول جدید: جریان نقدینگی هر ارز در هر اسکن
    c.execute("""CREATE TABLE IF NOT EXISTS liquidity(
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, ts REAL,
        flow TEXT, cvd REAL, rvol REAL, chg REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS alerts(symbol TEXT PRIMARY KEY, last_ts REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS liq_alerts(symbol TEXT PRIMARY KEY, last_ts REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS br_alerts(symbol TEXT PRIMARY KEY, last_ts REAL)""")
    # داده روزانه هر ارز برای امتیازدهی Pre-Market
    c.execute("""CREATE TABLE IF NOT EXISTS daily_data(
        symbol TEXT, day TEXT,
        us_volume REAL DEFAULT 0,       -- حجم تجمعی در سشن US (روز قبل، برای فعال بودن)
        breakout_type TEXT,             -- آخرین شکست/ریجکت
        breakout_ts REAL,
        PRIMARY KEY(symbol, day))""")
    c.execute("""CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY, v TEXT)""")
    conn.commit(); conn.close()

def today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def get_state(k, default=None):
    conn=db();c=conn.cursor();c.execute("SELECT v FROM state WHERE k=?",(k,));r=c.fetchone();conn.close()
    return r["v"] if r else default
def set_state(k,v):
    conn=db();c=conn.cursor();c.execute("INSERT OR REPLACE INTO state(k,v) VALUES(?,?)",(k,str(v)));conn.commit();conn.close()

# اعضا
def add_member(cid,un):
    conn=db();c=conn.cursor();c.execute("INSERT OR REPLACE INTO members(chat_id,username,joined,active) VALUES(?,?,?,1)",(str(cid),un or "",datetime.now().isoformat()));conn.commit();conn.close()
def remove_member(cid):
    conn=db();c=conn.cursor();c.execute("UPDATE members SET active=0 WHERE chat_id=?",(str(cid),));conn.commit();conn.close()
def active_members():
    conn=db();c=conn.cursor();c.execute("SELECT chat_id FROM members WHERE active=1");r=[x["chat_id"] for x in c.fetchall()];conn.close();return r
def member_count():
    conn=db();c=conn.cursor();c.execute("SELECT COUNT(*) n FROM members WHERE active=1");n=c.fetchone()["n"];conn.close();return n

# چرخه
def get_cycle(symbol):
    conn=db();c=conn.cursor();c.execute("SELECT * FROM cycle WHERE symbol=? AND day=?",(symbol,today_str()));r=c.fetchone();conn.close();return r
def upsert_asia(symbol,d,method,level):
    conn=db();c=conn.cursor()
    c.execute("""INSERT INTO cycle(symbol,day,asia_break,asia_method,asia_level,asia_ts) VALUES(?,?,?,?,?,?)
        ON CONFLICT(symbol,day) DO UPDATE SET asia_break=?,asia_method=?,asia_level=?,asia_ts=?""",
        (symbol,today_str(),d,method,level,time.time(),d,method,level,time.time()))
    conn.commit();conn.close()
def upsert_london(symbol,q):
    conn=db();c=conn.cursor();c.execute("UPDATE cycle SET london_pullback=1,london_quality=?,london_ts=? WHERE symbol=? AND day=?",(q,time.time(),symbol,today_str()));conn.commit();conn.close()
def upsert_us(symbol):
    conn=db();c=conn.cursor();c.execute("UPDATE cycle SET us_move=1,us_ts=? WHERE symbol=? AND day=?",(time.time(),symbol,today_str()));conn.commit();conn.close()

# نقدینگی
def record_liquidity(symbol,flow,cvd,rvol,chg):
    conn=db();c=conn.cursor()
    c.execute("INSERT INTO liquidity(symbol,ts,flow,cvd,rvol,chg) VALUES(?,?,?,?,?,?)",(symbol,time.time(),flow,cvd,rvol,chg))
    c.execute("DELETE FROM liquidity WHERE ts < ?",(time.time()-7*86400,))
    conn.commit();conn.close()

def liquidity_history(symbol, hours=12):
    conn=db();c=conn.cursor()
    c.execute("SELECT * FROM liquidity WHERE symbol=? AND ts>? ORDER BY ts",(symbol,time.time()-hours*3600))
    rows=[dict(r) for r in c.fetchall()];conn.close();return rows

def can_alert(symbol,table="alerts",cooldown=ALERT_COOLDOWN):
    conn=db();c=conn.cursor();c.execute(f"SELECT last_ts FROM {table} WHERE symbol=?",(symbol,))
    row=c.fetchone();now=time.time()
    if row and now-row["last_ts"]<cooldown:conn.close();return False
    c.execute(f"INSERT OR REPLACE INTO {table}(symbol,last_ts) VALUES(?,?)",(symbol,now));conn.commit();conn.close();return True


# ═══════════ تلگرام ═══════════
def tg_send(cid,text,buttons=None,keyboard=None):
    payload={"chat_id":cid,"text":text,"parse_mode":"HTML","disable_web_page_preview":True}
    if buttons:
        payload["reply_markup"]={"inline_keyboard":buttons}
    elif keyboard:
        payload["reply_markup"]=keyboard
    try:requests.post(f"{TG}/sendMessage",json=payload,timeout=15)
    except Exception as e:print(f"[!] send {cid}: {e}")
def broadcast(text):
    for cid in active_members():
        tg_send(cid,text);time.sleep(0.4)

# ── پنل دکمه دائمی (Reply Keyboard) که همیشه پایین چت می‌ماند ──
def panel_keyboard(is_admin=False):
    rows=[
        ["📊 آمار","📅 رویداد اقتصادی"],
        ["📐 الگوی BTC","💧 نقدینگی BTC"],
        ["🟢 پیشنهاد اسپات","😱 احساسات بازار"],
        ["❓ راهنما","🔕 لغو عضویت"],
    ]
    if is_admin:
        rows.append(["👥 اعضا","🔧 آمار ادمین"])
    return {"keyboard":[[{"text":t} for t in row] for row in rows],
            "resize_keyboard":True,"persistent":True}

# ── منوی دکمه‌ای اصلی (inline، زیر پیام) ──
def main_menu(is_admin=False):
    kb=[
        [{"text":"📊 آمار","callback_data":"stats"},{"text":"📅 رویداد اقتصادی","callback_data":"econ"}],
        [{"text":"📐 الگوی BTC","callback_data":"pattern_BTC"},{"text":"💧 نقدینگی BTC","callback_data":"liq_BTC"}],
        [{"text":"❓ راهنما","callback_data":"help"},{"text":"🔕 لغو عضویت","callback_data":"stop"}],
    ]
    if is_admin:
        kb.append([{"text":"👥 اعضا","callback_data":"members"},{"text":"🔧 آمار ادمین","callback_data":"admin"}])
    return kb


# ═══════════ داده و سشن ═══════════
def http_get(url,timeout=15):
    try:r=requests.get(url,timeout=timeout,headers={"User-Agent":"bot"});return r.json() if r.ok else None
    except Exception:return None

def current_session():
    t=datetime.now(timezone.utc);m=t.hour*60+t.minute
    if m>=23*60 or m<7*60:return "asia"
    if 7*60<=m<13*60+30:return "london"
    if 13*60+30<=m<20*60:return "us"
    return "off"

def us_subsession():
    """زیربخش سشن US برای هشدار تایم‌ها (UTC)"""
    t=datetime.now(timezone.utc);m=t.hour*60+t.minute
    if 12*60<=m<13*60+30:return "premarket"   # پیش‌بازار (Pre-Market)
    if 13*60+30<=m<14*60+30:return "open"
    if 16*60<=m<16*60+30:return "lunch"
    if 19*60<=m<20*60:return "power"
    return None

def load_symbols():
    info=http_get(f"{BASE}/fapi/v1/exchangeInfo")
    if not info or "symbols" not in info:return []
    return [s["symbol"] for s in info["symbols"] if s.get("contractType")=="PERPETUAL" and s.get("quoteAsset")=="USDT" and s.get("status")=="TRADING"]


# ═══════════ تقویم اقتصادی + خبر ═══════════
def next_econ_event():
    today=datetime.now(timezone.utc).date()
    best=None;best_days=9999
    for ds,name in ECON_EVENTS:
        d=datetime.strptime(ds,"%Y-%m-%d").date()
        days=(d-today).days
        if 0<=days<best_days:best_days=days;best={"name":name,"days":days,"date":ds}
    return best

def econ_alert_text():
    e=next_econ_event()
    if not e:return None
    if e["days"]==0:return f"🔴 <b>امروز:</b> {e['name']} — تا بعد از انتشار (≈۲۱:۳۰ تهران برای FOMC) ترید نکن!"
    if e["days"]==1:return f"🟠 <b>فردا:</b> {e['name']} — نوسان شدید محتمل"
    return None

def fetch_btc_news():
    """خبر زنده بیت‌کوین از CryptoPanic (اگر کلید موجود باشد)"""
    if not NEWS_API:return None
    try:
        url=f"https://cryptopanic.com/api/v1/posts/?auth_token={NEWS_API}&currencies=BTC&filter=important&public=true"
        d=http_get(url)
        if not d or "results" not in d:return None
        items=d["results"][:3]
        if not items:return None
        out=["📰 <b>اخبار مهم بیت‌کوین:</b>"]
        for it in items:
            title=it.get("title","")
            out.append(f"• {title}")
        return "\n".join(out)
    except Exception:
        return None


def fetch_fear_greed():
    """شاخص ترس و طمع (Fear & Greed Index) — رایگان، بدون کلید"""
    try:
        d=http_get("https://api.alternative.me/fng/?limit=1")
        if not d or "data" not in d or not d["data"]:return None
        entry=d["data"][0]
        val=int(entry["value"])
        # دسته‌بندی فارسی + تفسیر
        if val<25:
            label="ترس شدید 😱";icon="😱";interp="تریدرها خیلی ترسیدن — معمولاً نقطه برگشت/فرصت خرید (دیدگاه معکوس)"
        elif val<45:
            label="ترس 😟";icon="😟";interp="فضای محتاطانه — بازار نگران است"
        elif val<55:
            label="خنثی 😐";icon="😐";interp="بازار بی‌جهت — احساسات متعادل"
        elif val<75:
            label="طمع 🤑";icon="🤑";interp="فضای مثبت — ولی مراقب اشباع باش"
        else:
            label="طمع شدید 🤯";icon="🤯";interp="تریدرها حریص شدن — احتیاط، احتمال اصلاح (دیدگاه معکوس)"
        return {"value":val,"label":label,"icon":icon,"interp":interp}
    except Exception:
        return None

def fear_greed_text():
    fg=fetch_fear_greed()
    if not fg:return None
    return (f"{fg['icon']} <b>احساسات بازار:</b> {fg['label']} ({fg['value']}/۱۰۰)\n"
            f"   {fg['interp']}")


# ═══════════ ابزار تحلیل ═══════════
def klines(symbol,interval,limit=50):
    return http_get(f"{BASE}/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}")
def to_candles(kl):
    return [{"o":float(k[1]),"h":float(k[2]),"l":float(k[3]),"c":float(k[4]),"vol":float(k[5]),"tb":float(k[9])} for k in kl]

def lin_reg(pts):
    n=len(pts)
    if n<3:return None
    sx=sum(range(n));sy=sum(pts);sxy=sum(i*pts[i] for i in range(n));sxx=sum(i*i for i in range(n))
    den=(n*sxx-sx*sx)
    if den==0:return None
    slope=(n*sxy-sx*sy)/den;intercept=(sy-slope*sx)/n
    my=sy/n;ss_tot=sum((p-my)**2 for p in pts);ss_res=sum((pts[i]-(slope*i+intercept))**2 for i in range(n))
    r2=1-ss_res/ss_tot if ss_tot>0 else 0
    return {"slope":slope,"intercept":intercept,"r2":r2,"pred":lambda i:slope*i+intercept}

def detect_asia_break(kl1h):
    if not kl1h or len(kl1h)<20:return None
    C=to_candles(kl1h);price=C[-1]["c"];recent=C[-12:]
    struct_high=max(c["h"] for c in recent[:-2]);struct_low=min(c["l"] for c in recent[:-2])
    bos=None
    if C[-1]["c"]>struct_high*1.001:bos="bull"
    elif C[-1]["c"]<struct_low*0.999:bos="bear"
    highs=[c["h"] for c in recent];lows=[c["l"] for c in recent]
    regH=lin_reg(highs);regL=lin_reg(lows);trend=None
    if regH and regL:
        n=len(recent);upper=regH["pred"](n-1);lower=regL["pred"](n-1)
        if price>upper*1.002 and regH["r2"]>0.4:trend="bull"
        elif price<lower*0.998 and regL["r2"]>0.4:trend="bear"
    direction=None;method=None;level=None
    if bos and trend and bos==trend:direction=bos;method="both";level=struct_high if bos=="bull" else struct_low
    elif bos:direction=bos;method="BOS";level=struct_high if bos=="bull" else struct_low
    elif trend:direction=trend;method="trendline";level=price
    if not direction:return None
    return {"direction":direction,"method":method,"level":level,"price":price}

def detect_london_pullback(kl15,asia_dir,asia_level):
    if not kl15 or len(kl15)<20 or not asia_level:return None
    C=to_candles(kl15);price=C[-1]["c"];recent=C[-12:]
    vols=[c["vol"] for c in C];avg_v=sum(vols[-21:-1])/20 if len(vols)>=21 else sum(vols)/len(vols)
    rvol=vols[-1]/avg_v if avg_v>0 else 1
    deltas=[c["tb"]-(c["vol"]-c["tb"]) for c in C];cvd=sum(deltas[-8:])
    if asia_dir=="bull":
        low_after=min(c["l"] for c in recent);near=abs(low_after-asia_level)/asia_level<0.012
        if near or (price<max(c["h"] for c in recent) and price>asia_level):
            return {"quality":"healthy" if (rvol<1.5 or cvd>0) else "weak","rvol":round(rvol,2),"cvd":round(cvd,1)}
    elif asia_dir=="bear":
        high_after=max(c["h"] for c in recent);near=abs(high_after-asia_level)/asia_level<0.012
        if near or (price>min(c["l"] for c in recent) and price<asia_level):
            return {"quality":"healthy" if (rvol<1.5 or cvd<0) else "weak","rvol":round(rvol,2),"cvd":round(cvd,1)}
    return None

def detect_us_move(kl15,kl5,asia_dir):
    if not kl15 or len(kl15)<20:return None
    C=to_candles(kl15);price=C[-1]["c"]
    vols=[c["vol"] for c in C];avg_v=sum(vols[-21:-1])/20 if len(vols)>=21 else sum(vols)/len(vols)
    rvol=vols[-1]/avg_v if avg_v>0 else 1
    deltas=[c["tb"]-(c["vol"]-c["tb"]) for c in C];cvd=sum(deltas[-8:])
    recent=C[-6:];move=(recent[-1]["c"]-recent[0]["o"])/recent[0]["o"]*100
    c5_ok=False
    if kl5 and len(kl5)>=10:
        C5=to_candles(kl5);d5=[c["tb"]-(c["vol"]-c["tb"]) for c in C5];cvd5=sum(d5[-6:])
        c5_ok=(cvd5>0) if asia_dir=="bull" else (cvd5<0)
    aligned=False;strength=0
    if asia_dir=="bull" and move>0.8 and cvd>0:aligned=True;strength=move
    elif asia_dir=="bear" and move<-0.8 and cvd<0:aligned=True;strength=abs(move)
    if not aligned:return None
    return {"rvol":round(rvol,2),"cvd":round(cvd,1),"move":round(move,2),"c5_confirm":c5_ok,"strength":round(strength,2),"price":price}

# ── تحلیل نقدینگی (مستقل از چرخه) ──
def analyze_liquidity(kl15):
    if not kl15 or len(kl15)<25:return None
    C=to_candles(kl15);price=C[-1]["c"]
    vols=[c["vol"] for c in C];avg_v=sum(vols[-21:-1])/20
    rvol=vols[-1]/avg_v if avg_v>0 else 1
    deltas=[c["tb"]-(c["vol"]-c["tb"]) for c in C];cvd=sum(deltas[-8:])
    chg=(C[-1]["c"]-C[-6]["c"])/C[-6]["c"]*100
    flow="none"
    if rvol>=1.5 and cvd>0:flow="in"
    elif rvol>=1.5 and cvd<0:flow="out"
    return {"flow":flow,"cvd":round(cvd,1),"rvol":round(rvol,2),"chg":round(chg,2),"price":price}

# ── تشخیص شکست محدوده / ریجکت روی ۱ ساعته با تأیید نقدینگی ──
def detect_breakout_rejection(kl1h):
    """
    شکست محدوده (Breakout): قیمت از سقف/کف رنج اخیر زد بیرون + پول هم‌جهت
    ریجکت (Rejection): به سطح خورد، سایه بلند زد و برگشت + پول مخالف
    فقط وقتی پول تغذیه شده باشد (RVOL + CVD) برمی‌گرداند.
    """
    if not kl1h or len(kl1h)<30:return None
    C=to_candles(kl1h);price=C[-1]["c"];last=C[-1]
    # محدوده اخیر (۲۰ کندل قبل، به‌جز ۲ تای آخر)
    rng=C[-22:-2]
    hi=max(c["h"] for c in rng);lo=min(c["l"] for c in rng)
    # نقدینگی
    vols=[c["vol"] for c in C];avg_v=sum(vols[-21:-1])/20 if len(vols)>=21 else sum(vols)/len(vols)
    rvol=vols[-1]/avg_v if avg_v>0 else 1
    deltas=[c["tb"]-(c["vol"]-c["tb"]) for c in C];cvd=sum(deltas[-5:])
    # مشخصات کندل آخر
    body=abs(last["c"]-last["o"]);rng_c=last["h"]-last["l"]
    up_wick=last["h"]-max(last["c"],last["o"])
    dn_wick=min(last["c"],last["o"])-last["l"]

    # ── شکست صعودی: close بالای سقف رنج + حجم + CVD مثبت ──
    if last["c"]>hi and rvol>=1.6 and cvd>0 and (body/rng_c>0.5 if rng_c>0 else False):
        return {"type":"breakout_up","level":round(hi,4),"price":price,
                "rvol":round(rvol,2),"cvd":round(cvd,1),
                "desc":"شکست صعودی محدوده + ورود پول"}
    # ── شکست نزولی: close زیر کف رنج + حجم + CVD منفی ──
    if last["c"]<lo and rvol>=1.6 and cvd<0 and (body/rng_c>0.5 if rng_c>0 else False):
        return {"type":"breakout_down","level":round(lo,4),"price":price,
                "rvol":round(rvol,2),"cvd":round(cvd,1),
                "desc":"شکست نزولی محدوده + خروج پول"}
    # ── ریجکت از سقف: به سقف خورد، سایه بالا بلند، برگشت + CVD منفی ──
    if last["h"]>=hi*0.999 and up_wick>body*1.5 and up_wick>rng_c*0.4 and rvol>=1.5 and cvd<0:
        return {"type":"reject_top","level":round(hi,4),"price":price,
                "rvol":round(rvol,2),"cvd":round(cvd,1),
                "desc":"ریجکت از سقف محدوده + خروج پول"}
    # ── ریجکت از کف: به کف خورد، سایه پایین بلند، برگشت + CVD مثبت ──
    if last["l"]<=lo*1.001 and dn_wick>body*1.5 and dn_wick>rng_c*0.4 and rvol>=1.5 and cvd>0:
        return {"type":"reject_bottom","level":round(lo,4),"price":price,
                "rvol":round(rvol,2),"cvd":round(cvd,1),
                "desc":"ریجکت از کف محدوده + ورود پول"}
    return None

def build_breakout_message(symbol,br):
    coin=symbol.replace("USDT","")
    icons={"breakout_up":"🔼","breakout_down":"🔽","reject_top":"⛔","reject_bottom":"✅"}
    titles={"breakout_up":"شکست صعودی","breakout_down":"شکست نزولی",
            "reject_top":"ریجکت از سقف","reject_bottom":"ریجکت از کف"}
    ic=icons.get(br["type"],"•")
    L=[f"{ic} <b>{coin}</b> — {titles.get(br['type'],'')} (۱ ساعته)"]
    L.append(f"💵 ${br['price']} · سطح: ${br['level']}")
    L.append(f"📊 {br['desc']}")
    L.append(f"   RVOL {br['rvol']} · CVD {'+' if br['cvd']>0 else ''}{br['cvd']:.0f}")
    L.append(f"\n🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
    return "\n".join(L)


# ── EMA ساده برای ربات ──
def ema_series(values, period):
    if len(values)<period:return None
    k=2/(period+1);e=values[0]
    out=[e]
    for v in values[1:]:
        e=v*k+e*(1-k);out.append(e)
    return out


# ═══════════ تشخیص الگو با ZigZag (حرفه‌ای) ═══════════
def lin_reg_xy(xs,ys):
    """رگرسیون خطی + R² (کیفیت برازش)"""
    n=len(xs)
    if n<2:return None
    sx=sum(xs);sy=sum(ys);sxy=sum(xs[i]*ys[i] for i in range(n));sxx=sum(x*x for x in xs)
    den=n*sxx-sx*sx
    if den==0:return None
    slope=(n*sxy-sx*sy)/den;intercept=(sy-slope*sx)/n
    # R²
    mean_y=sy/n
    ss_tot=sum((y-mean_y)**2 for y in ys)
    ss_res=sum((ys[i]-(slope*xs[i]+intercept))**2 for i in range(n))
    r2=1-(ss_res/ss_tot) if ss_tot>0 else 0
    return {"slope":slope,"intercept":intercept,"r2":r2}


def zigzag_pivots(C, pct=None):
    """
    نقاط چرخش مهم (ZigZag) — فقط چرخش‌های معنادار.
    اگر pct داده نشود، از نوسان واقعی بازار (ATR) استفاده می‌کند.
    خروجی: لیست متناوب [(index, price, 'H'|'L'), ...]
    """
    if len(C)<10:return []
    # آستانه تطبیقی بر اساس ATR (نوسان طبیعی همان ارز)
    if pct is None:
        trs=[]
        for i in range(1,len(C)):
            tr=max(C[i]["h"]-C[i]["l"],abs(C[i]["h"]-C[i-1]["c"]),abs(C[i]["l"]-C[i-1]["c"]))
            trs.append(tr)
        atr=sum(trs[-14:])/min(14,len(trs))
        avg_price=sum(c["c"] for c in C)/len(C)
        pct=max((atr/avg_price)*1.5, 0.01)  # حداقل ۱٪

    pivots=[]
    direction=0
    cur_ext_idx=0;cur_ext_price=C[0]["c"]

    for i in range(1,len(C)):
        h=C[i]["h"];l=C[i]["l"]
        if direction>=0:
            if h>cur_ext_price:
                cur_ext_price=h;cur_ext_idx=i
            if l<cur_ext_price*(1-pct):
                pivots.append((cur_ext_idx,cur_ext_price,"H"))
                direction=-1;cur_ext_price=l;cur_ext_idx=i
        else:
            if l<cur_ext_price:
                cur_ext_price=l;cur_ext_idx=i
            if h>cur_ext_price*(1+pct):
                pivots.append((cur_ext_idx,cur_ext_price,"L"))
                direction=1;cur_ext_price=h;cur_ext_idx=i
    return pivots


def line_touches(pivots_xy, slope, intercept, tol):
    """چند pivot واقعاً روی این خط هستند (±tol)"""
    cnt=0
    for x,y in pivots_xy:
        expected=slope*x+intercept
        if abs(y-expected)/expected<tol:
            cnt+=1
    return cnt


def detect_triangle_wedge(kl1h):
    """
    الگوی مثلث/کنج با ZigZag + تأیید ۳ لمس + کیفیت خط (R²).
    فقط الگوهای معتبر با شکست حجم‌دار را برمی‌گرداند.
    """
    if not kl1h or len(kl1h)<50:return None
    C=to_candles(kl1h);price=C[-1]["c"]
    piv=zigzag_pivots(C)
    if len(piv)<5:return None  # حداقل ۵ چرخش برای یک الگوی معتبر

    highs=[(i,p) for i,p,t in piv if t=="H"]
    lows=[(i,p) for i,p,t in piv if t=="L"]
    if len(highs)<3 or len(lows)<3:return None  # حداقل ۳ سقف و ۳ کف

    # فقط ۴ چرخش اخیر هر کدام (الگوی فعلی، نه قدیمی)
    highs=highs[-4:];lows=lows[-4:]
    hx=[i for i,_ in highs];hy=[v for _,v in highs]
    lx=[i for i,_ in lows];ly=[v for _,v in lows]
    rh=lin_reg_xy(hx,hy);rl=lin_reg_xy(lx,ly)
    if not rh or not rl:return None

    # کیفیت خط: R² باید بالا باشد (نقاط واقعاً روی خط)
    if rh["r2"]<0.6 or rl["r2"]<0.6:return None

    # تأیید ۳ لمس: حداقل ۳ سقف روی خط بالا، ۳ کف روی خط پایین
    if line_touches(list(zip(hx,hy)),rh["slope"],rh["intercept"],0.012)<3:return None
    if line_touches(list(zip(lx,ly)),rl["slope"],rl["intercept"],0.012)<3:return None

    n=len(C)
    upper_now=rh["slope"]*(n-1)+rh["intercept"]
    lower_now=rl["slope"]*(n-1)+rl["intercept"]
    # عرض ابتدا و انتهای الگو
    start_x=min(hx[0],lx[0])
    upper_start=rh["slope"]*start_x+rh["intercept"]
    lower_start=rl["slope"]*start_x+rl["intercept"]
    width_start=upper_start-lower_start
    width_now=upper_now-lower_now
    if width_start<=0 or width_now<=0:return None

    sh=rh["slope"]/price;sl=rl["slope"]/price  # شیب نرمال‌شده
    FLAT=0.0002

    # ── تشخیص نوع الگو ──
    converging=width_now<width_start*0.75
    if abs(sh)<FLAT and sl>FLAT:
        ptype="asc_triangle";pname="مثلث صعودی";bias="bull"
    elif abs(sl)<FLAT and sh<-FLAT:
        ptype="desc_triangle";pname="مثلث نزولی";bias="bear"
    elif sh<-FLAT and sl>FLAT and converging:
        ptype="sym_triangle";pname="مثلث متقارن";bias="neutral"
    elif sh>FLAT and sl>FLAT and converging:
        ptype="rising_wedge";pname="کنج صعودی";bias="bear"  # کنج صعودی معمولاً نزولی می‌شکند
    elif sh<-FLAT and sl<-FLAT and converging:
        ptype="falling_wedge";pname="کنج نزولی";bias="bull"  # کنج نزولی معمولاً صعودی می‌شکند
    else:
        return None  # الگوی واضحی نیست

    # ── نقدینگی برای تأیید شکست ──
    vols=[c["vol"] for c in C];avg_v=sum(vols[-21:-1])/20 if len(vols)>=21 else sum(vols)/len(vols)
    rvol=vols[-1]/avg_v if avg_v>0 else 1
    deltas=[c["tb"]-(c["vol"]-c["tb"]) for c in C];cvd=sum(deltas[-5:])

    # ── شکست با حجم ──
    broke=None
    if price>upper_now*1.001 and rvol>=1.6 and cvd>0:
        broke="up"
    elif price<lower_now*0.999 and rvol>=1.6 and cvd<0:
        broke="down"
    if not broke:return None

    return {"pattern":ptype,"pname":pname,"break_dir":broke,"bias":bias,
            "upper":round(upper_now,4),"lower":round(lower_now,4),
            "price":price,"rvol":round(rvol,2),"cvd":round(cvd,1),
            "touches_h":line_touches(list(zip(hx,hy)),rh["slope"],rh["intercept"],0.012),
            "touches_l":line_touches(list(zip(lx,ly)),rl["slope"],rl["intercept"],0.012),
            "r2":round(min(rh["r2"],rl["r2"]),2)}


# ── تشخیص سر و شونه (Head & Shoulders) ──
def detect_head_shoulders(kl1h):
    """
    سر و شونه: ۳ قله که وسطی (سر) بلندتر از دو کناری (شونه‌ها)
    + نسخه معکوس (کف). شکست خط گردن با حجم → سیگنال.
    """
    if not kl1h or len(kl1h)<50:return None
    C=to_candles(kl1h);price=C[-1]["c"]
    piv=zigzag_pivots(C)
    if len(piv)<5:return None

    highs=[(i,p) for i,p,t in piv if t=="H"]
    lows=[(i,p) for i,p,t in piv if t=="L"]

    vols=[c["vol"] for c in C];avg_v=sum(vols[-21:-1])/20 if len(vols)>=21 else sum(vols)/len(vols)
    rvol=vols[-1]/avg_v if avg_v>0 else 1
    deltas=[c["tb"]-(c["vol"]-c["tb"]) for c in C];cvd=sum(deltas[-5:])

    # ── سر و شونه سقف (نزولی) ──
    if len(highs)>=3:
        ls,head,rs=highs[-3],highs[-2],highs[-1]  # شونه چپ، سر، شونه راست
        # سر باید بلندتر از هر دو شونه باشد
        if head[1]>ls[1] and head[1]>rs[1]:
            # دو شونه تقریباً هم‌ارتفاع (±3%)
            if abs(ls[1]-rs[1])/ls[1]<0.03:
                # خط گردن = کف بین قله‌ها
                neck_lows=[p for i,p in lows if ls[0]<i<rs[0]]
                if neck_lows:
                    neckline=sum(neck_lows)/len(neck_lows)
                    # شکست زیر خط گردن با حجم
                    if price<neckline*0.999 and rvol>=1.6 and cvd<0:
                        return {"pattern":"head_shoulders","pname":"سر و شونه سقف",
                                "break_dir":"down","bias":"bear","neckline":round(neckline,4),
                                "price":price,"rvol":round(rvol,2),"cvd":round(cvd,1)}

    # ── سر و شونه معکوس (کف، صعودی) ──
    if len(lows)>=3:
        ls,head,rs=lows[-3],lows[-2],lows[-1]
        # سر باید پایین‌تر از هر دو شونه باشد
        if head[1]<ls[1] and head[1]<rs[1]:
            if abs(ls[1]-rs[1])/ls[1]<0.03:
                neck_highs=[p for i,p in highs if ls[0]<i<rs[0]]
                if neck_highs:
                    neckline=sum(neck_highs)/len(neck_highs)
                    if price>neckline*1.001 and rvol>=1.6 and cvd>0:
                        return {"pattern":"inv_head_shoulders","pname":"سر و شونه معکوس",
                                "break_dir":"up","bias":"bull","neckline":round(neckline,4),
                                "price":price,"rvol":round(rvol,2),"cvd":round(cvd,1)}
    return None


def detect_pattern(kl1h):
    """تشخیص ترکیبی: اول سر و شونه، بعد مثلث/کنج"""
    hs=detect_head_shoulders(kl1h)
    if hs:return hs
    return detect_triangle_wedge(kl1h)


def build_pattern_message(symbol,pt):
    coin=symbol.replace("USDT","")
    ic="🔼" if pt["break_dir"]=="up" else "🔽"
    dir_fa="صعودی" if pt["break_dir"]=="up" else "نزولی"
    L=[f"{ic} <b>{coin}</b> — شکست از {pt['pname']}! (۱ ساعته)"]
    L.append(f"💵 ${pt['price']}")
    L.append(f"📐 الگو: {pt['pname']}")
    L.append(f"{ic} جهت شکست: {dir_fa}")
    # جزئیات کیفیت (اگر مثلث/کنج بود)
    if pt.get("touches_h"):
        L.append(f"✔️ کیفیت: {pt['touches_h']} لمس سقف · {pt['touches_l']} لمس کف · R²={pt['r2']}")
    if pt.get("neckline"):
        L.append(f"〰️ خط گردن: ${pt['neckline']}")
    L.append(f"📊 حجم: ✅ تأیید (RVOL {pt['rvol']} · CVD {'+' if pt['cvd']>0 else ''}{pt['cvd']:.0f})")
    L.append(f"\n⚠️ آماری، نه سیگنال. مدیریت ریسک کن.")
    L.append(f"🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
    return "\n".join(L)


# ── پولبک به سطح مهم (فیبو + OB + EMA + هم‌پوشانی) برای امتیاز Pre-Market ──
def detect_pullback_to_level(kl1h):
    """
    آیا قیمت به یه سطح مهم پولبک زده و واکنش نشون می‌ده؟
    سطح مهم = فیبوناچی / Order Block / EMA — هرچه هم‌پوشانی بیشتر، قوی‌تر.
    خروجی: امتیاز ۰ تا ۳ + جزئیات
    """
    if not kl1h or len(kl1h)<60:return None
    C=to_candles(kl1h);price=C[-1]["c"]
    closes=[c["c"] for c in C]

    # EMA 50 و 200
    e50=ema_series(closes,50);e200=ema_series(closes,200)
    ema50=e50[-1] if e50 else None
    ema200=e200[-1] if e200 else None

    # swing برای فیبو
    seg=C[-60:]
    hi=max(c["h"] for c in seg);lo=min(c["l"] for c in seg)
    hi_idx=max(range(len(seg)),key=lambda i:seg[i]["h"])
    lo_idx=min(range(len(seg)),key=lambda i:seg[i]["l"])
    swing_up=hi_idx>lo_idx
    diff=hi-lo
    fib_levels={}
    for name,r in [("0.5",0.5),("0.618",0.618),("0.705",0.705)]:
        fib_levels[name]=(hi-diff*r) if swing_up else (lo+diff*r)

    # Order Block ساده (FVG اخیر)
    obs=[]
    for i in range(len(C)-3,max(0,len(C)-30),-1):
        nxt=C[i+1]
        if abs(nxt["c"]-nxt["o"])/nxt["o"]>0.006:
            mid=(C[i]["l"]+C[i]["h"])/2
            obs.append(mid)

    # چک کن قیمت نزدیک کدوم سطوحه (±0.6%)
    near=[]
    def close_to(level):
        return level and abs(price-level)/price<0.006
    if close_to(ema50):near.append("EMA50")
    if close_to(ema200):near.append("EMA200")
    for fn,fl in fib_levels.items():
        if close_to(fl):near.append(f"فیبو {fn}")
    for ob in obs:
        if close_to(ob):near.append("OB");break

    if not near:return None

    # امتیاز: هرچه هم‌پوشانی بیشتر
    score=min(len(near),3)
    # تأیید واکنش: کندل اخیر برگشتی باشه (سایه)
    last=C[-1]
    body=abs(last["c"]-last["o"]);rng=last["h"]-last["l"]
    dn_wick=min(last["c"],last["o"])-last["l"]
    up_wick=last["h"]-max(last["c"],last["o"])
    reaction=(dn_wick>body or up_wick>body) if rng>0 else False

    return {"score":score,"levels":near,"price":price,"reaction":reaction,
            "overlap":len(near)>=2}


# ── تحلیل سناریو با numpy/pandas ──
def scenario_analysis(symbol):
    """بر اساس تاریخچه نقدینگی، احتمال سناریو را تخمین می‌زند"""
    hist=liquidity_history(symbol,hours=12)
    if len(hist)<4:return None
    in_count=sum(1 for h in hist if h["flow"]=="in")
    out_count=sum(1 for h in hist if h["flow"]=="out")
    total=in_count+out_count
    if total<3:return None

    result={"in":in_count,"out":out_count,"total":total}

    if HAS_NP:
        df=pd.DataFrame(hist)
        # روند CVD تجمعی
        df["cvd_cum"]=df["cvd"].cumsum()
        cvd_trend=np.polyfit(range(len(df)),df["cvd_cum"],1)[0] if len(df)>2 else 0
        # همبستگی نقدینگی با تغییر قیمت
        if len(df)>3 and df["chg"].std()>0 and df["cvd"].std()>0:
            corr=np.corrcoef(df["cvd"],df["chg"])[0,1]
        else:
            corr=0
        result["cvd_trend"]=round(float(cvd_trend),1)
        result["corr"]=round(float(corr),2)
        # میانگین حرکت بعد از ورود پول
        result["avg_chg"]=round(float(df["chg"].mean()),2)
    else:
        result["cvd_trend"]=0;result["corr"]=0
        result["avg_chg"]=round(sum(h["chg"] for h in hist)/len(hist),2)

    # تعیین سناریو محتمل
    in_ratio=in_count/total
    if in_ratio>=0.65 and result.get("avg_chg",0)>0:
        result["scenario"]="accumulation"   # تجمع → احتمال رشد
        result["prob"]=round(in_ratio*100)
    elif out_count/total>=0.65 and result.get("avg_chg",0)<0:
        result["scenario"]="distribution"    # توزیع → احتمال ریزش
        result["prob"]=round(out_count/total*100)
    elif in_ratio>0.55:
        result["scenario"]="mild_accum"
        result["prob"]=round(in_ratio*100)
    elif out_count/total>0.55:
        result["scenario"]="mild_dist"
        result["prob"]=round(out_count/total*100)
    else:
        result["scenario"]="mixed";result["prob"]=50
    return result


# ═══════════ پیام‌ها ═══════════
def build_stage3_message(symbol,cyc,us):
    coin=symbol.replace("USDT","");d=cyc["asia_break"];dfa="صعودی 📈" if d=="bull" else "نزولی 📉"
    L=[f"🚀 <b>{coin}</b> — حرکت اصلی سشن آمریکا!"]
    L.append(f"💵 ${us['price']} · جهت چرخه: {dfa}");L.append("")
    L.append("🔄 <b>چرخه سه‌سشنه کامل شد:</b>")
    m={"BOS":"شکست ساختار","trendline":"شکست خط روند","both":"شکست ساختار+خط روند"}.get(cyc["asia_method"],cyc["asia_method"])
    L.append(f"   🌏 آسیا: {m} ({dfa})")
    lq=cyc["london_quality"];lqf="سالم ✅" if lq=="healthy" else "ضعیف ⚠️" if lq=="weak" else "—"
    L.append(f"   🇬🇧 لندن: پولبک {lqf}")
    L.append(f"   🇺🇸 آمریکا: حرکت {'+' if us['move']>0 else ''}{us['move']}% · RVOL {us['rvol']} · CVD {'+' if us['cvd']>0 else ''}{us['cvd']:.0f}")
    if us["c5_confirm"]:L.append("   ✅ تأیید 5M: جریان هم‌جهت")
    # سناریو
    sc=scenario_analysis(symbol)
    if sc:L.append("");L.append(scenario_text(sc))
    L.append("")
    if cyc["london_quality"]=="healthy" and us["c5_confirm"]:
        L.append("🥇 <b>چرخه باکیفیت</b> — کاندیدای قوی حرکت اصلی!")
    else:L.append("⚖️ چرخه شکل گرفت، کیفیت کامل نیست — احتیاط.")
    L.append(f"\n🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
    return "\n".join(L)

def scenario_text(sc):
    names={"accumulation":"تجمع قوی (ورود مکرر پول) → احتمال رشد","distribution":"توزیع قوی (خروج مکرر پول) → احتمال ریزش",
           "mild_accum":"تجمع ملایم","mild_dist":"توزیع ملایم","mixed":"مختلط/بی‌جهت"}
    icon={"accumulation":"🟢","distribution":"🔴","mild_accum":"🟡","mild_dist":"🟠","mixed":"⚪"}
    t=f"🧮 <b>سناریو محتمل:</b> {icon.get(sc['scenario'],'')} {names.get(sc['scenario'],'')} (~{sc['prob']}%)"
    t+=f"\n   ورود {sc['in']} بار / خروج {sc['out']} بار در ۱۲س اخیر"
    if HAS_NP and "corr" in sc:
        t+=f"\n   همبستگی نقدینگی-قیمت: {sc['corr']}"
    return t

def build_liquidity_message(symbol,liq,sc):
    coin=symbol.replace("USDT","")
    fl="📥 ورود مکرر پول" if liq["flow"]=="in" else "📤 خروج مکرر پول"
    L=[f"💧 <b>{coin}</b> — {fl}"]
    L.append(f"💵 ${liq['price']} · تغییر {'+' if liq['chg']>0 else ''}{liq['chg']}% · RVOL {liq['rvol']}")
    if sc:L.append("");L.append(scenario_text(sc))
    L.append(f"\n🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
    return "\n".join(L)


# ═══════════ بخش اسپات (Spot) — نوسان کوتاه ۲-۳ روزه ═══════════
def spot_get(path):
    """درخواست به API اسپات بایننس"""
    return http_get(f"{SPOT_BASE}{path}")

def spot_klines(symbol, interval, limit=200):
    return spot_get(f"/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}")

_spot_symbols_cache={"ts":0,"list":[]}
def load_spot_symbols(top=50):
    """۵۰ ارز اول اسپات بر اساس حجم دلاری ۲۴ ساعته (USDT)"""
    now=time.time()
    if _spot_symbols_cache["list"] and now-_spot_symbols_cache["ts"]<6*3600:
        return _spot_symbols_cache["list"]
    data=spot_get("/api/v3/ticker/24hr")
    if not data:return _spot_symbols_cache["list"] or []
    # فقط جفت‌های USDT، حذف اهرم‌دارها و استیبل‌ها
    skip=("UPUSDT","DOWNUSDT","BULLUSDT","BEARUSDT")
    stables=("USDCUSDT","FDUSDUSDT","TUSDUSDT","BUSDUSDT","DAIUSDT","EURUSDT")
    rows=[]
    for d in data:
        s=d.get("symbol","")
        if not s.endswith("USDT"):continue
        if s in stables or any(x in s for x in skip):continue
        try:qv=float(d.get("quoteVolume",0))
        except:qv=0
        rows.append((s,qv))
    rows.sort(key=lambda x:-x[1])
    syms=[s for s,_ in rows[:top]]
    _spot_symbols_cache["ts"]=now;_spot_symbols_cache["list"]=syms
    return syms

def rsi_calc(closes, period=14):
    """RSI ساده"""
    if len(closes)<period+1:return 50
    gains=[];losses=[]
    for i in range(1,len(closes)):
        ch=closes[i]-closes[i-1]
        gains.append(max(ch,0));losses.append(max(-ch,0))
    ag=sum(gains[-period:])/period;al=sum(losses[-period:])/period
    if al==0:return 100
    rs=ag/al
    return 100-(100/(1+rs))

def score_spot(symbol):
    """
    امتیاز ۰-۱۰ برای ورود اسپات (نوسان ۲-۳ روزه).
    منطق: روند میان‌مدت صعودی + پولبک به سطح خوب + RSI سالم + واکنش.
    """
    # ۴ ساعته برای روند و سطوح، روزانه برای روند بزرگ
    kl4=spot_klines(symbol,"4h",200)
    kld=spot_klines(symbol,"1d",60)
    if not kl4 or len(kl4)<60 or not kld or len(kld)<30:return None
    C4=to_candles(kl4);Cd=to_candles(kld)
    price=C4[-1]["c"]
    closes4=[c["c"] for c in C4];closesd=[c["c"] for c in Cd]

    score=0;reasons=[]

    # ── ۱. روند میان‌مدت صعودی (۴ساعته بالای EMA50) → ۲ ──
    e50=ema_series(closes4,50)
    ema50=e50[-1] if e50 else None
    if ema50 and price>ema50:
        score+=2;reasons.append("روند ۴ساعته صعودی (بالای EMA50)")
    elif ema50 and price>ema50*0.98:
        score+=1;reasons.append("نزدیک EMA50")

    # ── ۲. روند بزرگ سالم (روزانه بالای EMA20 روزانه) → ۱ ──
    ed20=ema_series(closesd,20)
    if ed20 and price>ed20[-1]:
        score+=1;reasons.append("روند روزانه مثبت")

    # ── ۳. پولبک به سطح خوب (تخفیف، نه قله) → ۳ ──
    # فاصله از سقف ۲۰ کندل اخیر ۴ساعته
    recent_high=max(c["h"] for c in C4[-20:])
    pullback=(recent_high-price)/recent_high
    if 0.03<=pullback<=0.12:
        score+=3;reasons.append(f"پولبک سالم ({round(pullback*100,1)}٪ از سقف)")
    elif 0.01<=pullback<0.03:
        score+=1;reasons.append("پولبک کوچک")
    elif pullback>0.12:
        reasons.append("افت زیاد (احتیاط)")

    # ── ۴. RSI سالم (نه اشباع خرید) → ۲ ──
    r=rsi_calc(closes4)
    if 40<=r<=60:
        score+=2;reasons.append(f"RSI متعادل ({round(r)})")
    elif 35<=r<65:
        score+=1;reasons.append(f"RSI قابل‌قبول ({round(r)})")
    elif r>70:
        reasons.append(f"RSI اشباع خرید ({round(r)}) ⚠️")

    # ── ۵. واکنش مثبت (کندل برگشتی) → ۲ ──
    last=C4[-1]
    body=abs(last["c"]-last["o"]);rng=last["h"]-last["l"]
    dn_wick=min(last["c"],last["o"])-last["l"]
    if rng>0 and dn_wick>body and last["c"]>last["o"]:
        score+=2;reasons.append("کندل برگشتی صعودی")
    elif last["c"]>last["o"]:
        score+=1

    # محاسبه حد سود و ضرر (برای ۲-۳ روز)
    # ATR ساده روی ۴ساعته
    trs=[]
    for i in range(1,len(C4)):
        trs.append(max(C4[i]["h"]-C4[i]["l"],abs(C4[i]["h"]-C4[i-1]["c"]),abs(C4[i]["l"]-C4[i-1]["c"])))
    atr=sum(trs[-14:])/14 if len(trs)>=14 else (sum(trs)/len(trs) if trs else price*0.02)

    entry=price
    stop=round(price-atr*1.5,4)        # حد ضرر: ۱.۵ برابر ATR پایین
    target=round(price+atr*3,4)        # حد سود: ۳ برابر ATR بالا (R/R=۲)
    rr=round((target-entry)/(entry-stop),1) if entry>stop else 0

    return {"symbol":symbol,"score":min(score,10),"reasons":reasons,
            "price":round(price,4),"entry":round(entry,4),"stop":stop,
            "target":target,"rr":rr,"rsi":round(r)}

def build_spot_report(candidates):
    """گزارش پیشنهاد اسپات از بهترین‌ها (امتیاز ۶+)"""
    qualified=[c for c in candidates if c and c["score"]>=6]
    qualified.sort(key=lambda x:-x["score"])
    qualified=qualified[:5]  # حداکثر ۵ تا
    fg=fear_greed_text()
    header=["🟢 <b>پیشنهاد اسپات (نوسان ۲-۳ روزه)</b>"]
    if fg:header.append(fg+"\n")
    if not qualified:
        return "\n".join(header)+"\n\nالان ارز با شرایط خوب (امتیاز ۶+) برای اسپات پیدا نشد. صبر بهتر از ورود بد است."
    medals=["🥇","🥈","🥉","▫️","▫️"]
    L=header+[f"🎯 {len(qualified)} ارز با بهترین شرایط:\n"]
    for i,c in enumerate(qualified):
        coin=c["symbol"].replace("USDT","")
        L.append(f"{medals[i]} <b>{coin}</b> — امتیاز {c['score']}/۱۰")
        L.append(f"   💵 ورود: ${c['entry']}")
        L.append(f"   🎯 حد سود: ${c['target']}")
        L.append(f"   🛑 حد ضرر: ${c['stop']}")
        L.append(f"   ⚖️ R/R: {c['rr']} · RSI: {c['rsi']}")
        for rs in c["reasons"][:3]:
            L.append(f"   • {rs}")
        L.append("")
    L.append("⚠️ آماری برای نوسان کوتاه، نه سیگنال قطعی. حد ضرر را رعایت کن.")
    return "\n".join(L)

def run_spot_scan():
    """اسکن اسپات و ساخت گزارش"""
    syms=load_spot_symbols(50)
    if not syms:return "❌ دریافت لیست ارزهای اسپات ناموفق بود (API اسپات در دسترس نیست)."
    cands=[]
    for s in syms:
        try:
            sc=score_spot(s)
            if sc:cands.append(sc)
        except Exception:pass
        time.sleep(0.05)
    return build_spot_report(cands)


# ═══════════ دستورات ═══════════
# نگاشت متن دکمه‌های پنل دائمی به دستورها
PANEL_MAP={
    "📊 آمار":"/stats","📅 رویداد اقتصادی":"/econ",
    "📐 الگوی BTC":"/pattern BTC","💧 نقدینگی BTC":"/liq BTC",
    "🟢 پیشنهاد اسپات":"/spot",
    "😱 احساسات بازار":"/sentiment",
    "❓ راهنما":"/help","🔕 لغو عضویت":"/stop",
    "👥 اعضا":"/members","🔧 آمار ادمین":"/admin",
}

def handle_command(cid,un,text):
    text=(text or "").strip();is_admin=str(cid)==str(ADMIN_CHAT)
    # اگر متن یکی از دکمه‌های پنل بود، به دستورش تبدیل کن
    if text in PANEL_MAP:
        text=PANEL_MAP[text]
    if text.startswith("/start"):
        add_member(cid,un)
        tg_send(cid,f"✅ خوش آمدی! عضو شدی.\n\nاین ربات رصد می‌کند:\n🔄 چرخه سه‌سشنه (آسیا→لندن→US)\n💧 ورود/خروج مکرر نقدینگی\n🔼 شکست محدوده / 🔽 ریجکت (با تأیید پول)\n⏰ تایم‌های مهم US\n📅 رویدادهای اقتصادی\n🎯 ستاپ سایت تحلیل\n\n{DISCLAIMER}\n\n👇 از پنل دکمه‌های پایین استفاده کن:",
                keyboard=panel_keyboard(is_admin))
    elif text.startswith("/stop"):
        remove_member(cid);tg_send(cid,"🔕 لغو شد. /start برای بازگشت.")
    elif text.startswith("/help") or text.startswith("/menu"):
        h="📋 <b>منوی ربات</b>\n\nاز دکمه‌های زیر استفاده کن، یا دستورها را تایپ کن:\n/pattern ETH — الگوی مثلث/کنج یک ارز\n/liq ETH — نقدینگی یک ارز\n/spot — پیشنهاد اسپات (نوسان ۲-۳ روزه)\n/sentiment — احساسات بازار"
        tg_send(cid,h,buttons=main_menu(is_admin))
    elif text.startswith("/stats"):
        tg_send(cid,f"👥 اعضا: {member_count()}\n🕐 سشن: {current_session()}")
    elif text.startswith("/econ"):
        e=next_econ_event()
        if e:tg_send(cid,f"📅 رویداد بعدی: <b>{e['name']}</b>\nتاریخ: {e['date']} ({e['days']} روز دیگر)")
        else:tg_send(cid,"رویدادی ثبت نشده.")
    elif text.startswith("/sentiment"):
        fg=fear_greed_text()
        if fg:tg_send(cid,fg)
        else:tg_send(cid,"شاخص احساسات در دسترس نیست (بعداً امتحان کن).")
    elif text.startswith("/spot"):
        tg_send(cid,"🔍 در حال اسکن اسپات (۵۰ ارز اول)... چند لحظه صبر کن.")
        try:
            report=run_spot_scan()
            tg_send(cid,report)
        except Exception as e:
            tg_send(cid,f"خطا در اسکن اسپات. بعداً امتحان کن.")
            print(f"[!] spot scan: {e}")
    elif text.startswith("/pattern"):
        parts=text.split()
        if len(parts)>=2:
            sym=parts[1].upper();sym=sym if sym.endswith("USDT") else sym+"USDT"
            kl1h=klines(sym,"1h",60)
            pt=detect_pattern(kl1h)
            coin=sym.replace("USDT","")
            if pt:
                dir_fa="صعودی 🔼" if pt["break_dir"]=="up" else "نزولی 🔽"
                tg_send(cid,f"📐 {coin}:\nالگو: {pt['pname']}\nشکست: {dir_fa}\nRVOL {pt['rvol']} · CVD {pt['cvd']:.0f}")
            else:
                tg_send(cid,f"📐 {coin}: الگوی مثلث/کنج فعال (با شکست) یافت نشد.\n(یا الگو هست ولی هنوز نشکسته، یا الگویی نیست)")
        else:tg_send(cid,"استفاده: /pattern ETH")
    elif text.startswith("/liq"):
        parts=text.split()
        if len(parts)>=2:
            sym=parts[1].upper();sym=sym if sym.endswith("USDT") else sym+"USDT"
            sc=scenario_analysis(sym)
            if sc:tg_send(cid,f"💧 نقدینگی {sym.replace('USDT','')}:\n{scenario_text(sc)}")
            else:tg_send(cid,f"داده کافی برای {sym.replace('USDT','')} نیست (هنوز رصد نشده).")
        else:tg_send(cid,"استفاده: /liq ETH")
    elif text.startswith("/members") and is_admin:
        tg_send(cid,f"👥 اعضا: {member_count()}")
    elif text.startswith("/admin") and is_admin:
        conn=db();c=conn.cursor();c.execute("SELECT COUNT(*) n FROM cycle WHERE day=?",(today_str(),));cn=c.fetchone()["n"]
        c.execute("SELECT COUNT(*) n FROM liquidity");ln=c.fetchone()["n"];conn.close()
        tg_send(cid,f"🔧 آمار:\n👥 اعضا: {member_count()}\n🔄 چرخه امروز: {cn}\n💧 رکورد نقدینگی: {ln}\n🕐 سشن: {current_session()}\n🧮 numpy: {'✅' if HAS_NP else '❌'}")
    elif text.startswith("/broadcast") and is_admin:
        msg=text[len("/broadcast"):].strip()
        if msg:broadcast(f"📢 {msg}");tg_send(cid,f"✅ به {member_count()} نفر ارسال شد.")
        else:tg_send(cid,"استفاده: /broadcast متن")

def answer_callback(callback_id):
    """به تلگرام بگو کلیک دریافت شد (تا لودینگ دکمه قطع شود)"""
    try:requests.post(f"{TG}/answerCallbackQuery",json={"callback_query_id":callback_id},timeout=10)
    except Exception:pass

def handle_callback(cid,un,data,callback_id):
    """مدیریت کلیک روی دکمه‌ها"""
    answer_callback(callback_id)
    is_admin=str(cid)==str(ADMIN_CHAT)
    # دکمه‌ها را به دستورهای متنی نگاشت کن
    if data=="stats":handle_command(cid,un,"/stats")
    elif data=="econ":handle_command(cid,un,"/econ")
    elif data=="help":handle_command(cid,un,"/help")
    elif data=="stop":handle_command(cid,un,"/stop")
    elif data=="members" and is_admin:handle_command(cid,un,"/members")
    elif data=="admin" and is_admin:handle_command(cid,un,"/admin")
    elif data=="pattern_BTC":handle_command(cid,un,"/pattern BTC")
    elif data=="liq_BTC":handle_command(cid,un,"/liq BTC")

def poll_updates():
    offset=None
    while True:
        try:
            params={"timeout":30}
            if offset:params["offset"]=offset
            r=requests.get(f"{TG}/getUpdates",params=params,timeout=35);data=r.json()
            for upd in data.get("result",[]):
                offset=upd["update_id"]+1
                # پیام متنی
                msg=upd.get("message")
                if msg:
                    handle_command(msg["chat"]["id"],msg["chat"].get("username",""),msg.get("text",""))
                    continue
                # کلیک دکمه
                cb=upd.get("callback_query")
                if cb:
                    handle_callback(cb["from"]["id"],cb["from"].get("username",""),
                                    cb.get("data",""),cb["id"])
        except Exception as e:
            print(f"[!] poll: {e}");time.sleep(5)


# ═══════════ امتیازدهی و گزارش Pre-Market ═══════════
# ارزهای فعال در سشن US بر اساس حجم بالا تشخیص داده می‌شوند (پویا، نه لیست ثابت)
def is_us_active(symbol):
    """آیا این ارز در سشن US (روز اخیر) حجم بالایی داشته؟"""
    conn=db();c=conn.cursor()
    c.execute("SELECT us_volume FROM daily_data WHERE symbol=? ORDER BY day DESC LIMIT 1",(symbol,))
    r=c.fetchone();conn.close()
    return (r["us_volume"] if r else 0)

def score_candidate(symbol):
    """
    امتیاز ۰ تا ۱۰ برای ارزش ترید یک ارز در Pre-Market.
    """
    score=0;reasons=[];direction=None

    # ── پولبک به سطح مهم (فیبو/OB/EMA/هم‌پوشانی) → ۳ ──
    kl1h=klines(symbol,"1h",200)
    pb=detect_pullback_to_level(kl1h)
    if pb:
        if pb["overlap"] and pb["reaction"]:
            score+=3;reasons.append("پولبک به سطح مهم (هم‌پوشانی: "+"+".join(pb["levels"][:3])+")")
        elif pb["overlap"]:
            score+=2;reasons.append("پولبک به هم‌پوشانی سطوح ("+"+".join(pb["levels"][:2])+")")
        else:
            score+=1;reasons.append("پولبک به سطح "+pb["levels"][0])

    # ── نقدینگی (تجمع/توزیع قوی) → ۲ ──
    sc=scenario_analysis(symbol)
    if sc:
        if sc["scenario"]=="accumulation" and sc["prob"]>=70:
            score+=2;reasons.append(f"تجمع پول ({sc['prob']}٪)")
            if not direction:direction="bull"
        elif sc["scenario"]=="distribution" and sc["prob"]>=70:
            score+=2;reasons.append(f"توزیع پول ({sc['prob']}٪)")
            if not direction:direction="bear"
        elif sc["scenario"] in ("mild_accum","mild_dist"):
            score+=1;reasons.append("نقدینگی ملایم")

    # ── شکست مثلث/کنج معتبر امروز → ۲ ──
    conn=db();c=conn.cursor()
    c.execute("SELECT breakout_type,breakout_ts FROM daily_data WHERE symbol=? AND day=?",(symbol,today_str()))
    r=c.fetchone();conn.close()
    if r and r["breakout_type"] and r["breakout_ts"] and time.time()-r["breakout_ts"]<8*3600:
        bt=r["breakout_type"]
        score+=2;reasons.append("شکست الگو ("+bt+")")
        if not direction:
            direction="bull" if "up" in bt else "bear"

    # ── سناریو واضح → ۲ ──
    if sc and sc.get("prob",0)>=75:
        score+=2;reasons.append("سناریو واضح")
    elif sc and sc.get("prob",0)>=65:
        score+=1

    # ── فعال در سشن US (حجم بالا) → ۱ ──
    if is_us_active(symbol)>0:
        score+=1;reasons.append("فعال در سشن US")

    return {"symbol":symbol,"score":min(score,10),"reasons":reasons,"direction":direction}

def build_premarket_report(candidates):
    """گزارش Pre-Market از بهترین ارزها (امتیاز ۵+)"""
    # شاخص احساسات بازار (در ابتدای گزارش)
    fg=fear_greed_text()
    header=["⏰ <b>گزارش Pre-Market US</b>"]
    if fg:header.append(fg+"\n")

    qualified=[c for c in candidates if c["score"]>=5]
    qualified.sort(key=lambda x:-x["score"])
    if not qualified:
        return "\n".join(header)+"\n\nامروز ارز با امتیاز کافی (۵+) پیدا نشد. بازار شرایط واضحی ندارد — احتیاط."
    medals=["🥇","🥈","🥉"]+["▫️"]*20
    L=header+[f"🎯 {len(qualified)} ارز ارزشمند امروز:\n"]
    for i,c in enumerate(qualified):
        coin=c["symbol"].replace("USDT","")
        dir_fa="صعودی 📈" if c["direction"]=="bull" else "نزولی 📉" if c["direction"]=="bear" else "نامشخص"
        L.append(f"{medals[i]} <b>{coin}</b> — امتیاز {c['score']}/۱۰")
        L.append(f"   جهت محتمل: {dir_fa}")
        for rs in c["reasons"][:4]:
            L.append(f"   • {rs}")
        L.append("")
    L.append("⚠️ آماری بر اساس تحلیل تکنیکال، نه سیگنال قطعی. مدیریت ریسک کن.")
    return "\n".join(L)

def run_premarket_scan(symbols):
    """همه ارزها را امتیاز می‌دهد و گزارش را به همه می‌فرستد"""
    cands=[]
    for sym in symbols:
        try:
            sc=score_candidate(sym)
            if sc["score"]>=5:cands.append(sc)
        except Exception:pass
    report=build_premarket_report(cands)
    broadcast(report)
    print(f"[+] گزارش Pre-Market ارسال شد ({len([c for c in cands if c['score']>=5])} ارز).")


# ═══════════ هشدارهای زمان‌بندی‌شده (تایم US + اقتصاد) ═══════════
def session_alert_loop():
    """هشدار تایم‌های US و رویدادهای اقتصادی"""
    while True:
        try:
            sub=us_subsession()
            last_sub=get_state("last_us_sub","")
            if sub and sub!=last_sub:
                set_state("last_us_sub",sub)
                msgs={"premarket":"⏰ <b>Pre-Market US</b> — یک ساعت تا باز شدن بازار آمریکا.",
                      "open":"🔔 <b>US Open</b> شروع شد (Killzone) — بهترین زمان setup. مراقب باش.",
                      "lunch":"🍽 <b>US Lunch</b> — معمولاً کم‌نوسان، احتیاط در ورود.",
                      "power":"⚡ <b>Power Hour</b> شروع شد (Killzone) — حرکت‌های قوی پایان روز محتمل."}
                broadcast(msgs[sub])
                # خبر و اقتصاد همراه Pre-Market و US Open
                if sub in ("premarket","open"):
                    ea=econ_alert_text()
                    if ea:broadcast(ea)
                    news=fetch_btc_news()
                    if news:broadcast(news)
                # گزارش معرفی ارزها فقط در Pre-Market (یک‌بار در روز)
                if sub=="premarket" and get_state("premarket_day","")!=today_str():
                    set_state("premarket_day",today_str())
                    if SCAN_SYMBOLS:
                        run_premarket_scan(SCAN_SYMBOLS)
                    # گزارش اسپات روزانه (یک‌بار، همراه Pre-Market)
                    try:
                        spot_report=run_spot_scan()
                        broadcast(spot_report)
                    except Exception as e:
                        print(f"[!] spot daily: {e}")
            elif not sub and last_sub:
                set_state("last_us_sub","")

            # هشدار روزانه رویداد اقتصادی (یک‌بار در روز، صبح UTC)
            today=today_str()
            if get_state("econ_daily_day","")!=today and datetime.now(timezone.utc).hour==6:
                set_state("econ_daily_day",today)
                ea=econ_alert_text()
                if ea:broadcast(ea)
        except Exception as e:
            print(f"[!] session_alert: {e}")
        time.sleep(60)


# ═══════════ حلقه اسکن اصلی ═══════════
def scan_loop():
    global SCAN_SYMBOLS
    symbols=load_symbols();SCAN_SYMBOLS=symbols;print(f"[+] {len(symbols)} نماد.")
    sym_reload=time.time()
    while True:
        try:
            if time.time()-sym_reload>6*3600:
                symbols=load_symbols();SCAN_SYMBOLS=symbols;sym_reload=time.time()
            sess=current_session();t0=time.time();pattern_alerts=[]
            for i,sym in enumerate(symbols):
                try:
                    kl15=klines(sym,"15m",30)
                    # ── جمع‌آوری نقدینگی (بی‌صدا) ──
                    liq=analyze_liquidity(kl15)
                    if liq and liq["flow"]!="none":
                        record_liquidity(sym,liq["flow"],liq["cvd"],liq["rvol"],liq["chg"])

                    # ── الگوی مثلث/کنج روی ۱ ساعته + شکست با حجم (هشدار لحظه‌ای) ──
                    kl1h=klines(sym,"1h",60)
                    pt=detect_pattern(kl1h)
                    if pt and can_alert(sym,"br_alerts",BR_COOLDOWN):
                        # ثبت برای امتیاز Pre-Market
                        conn=db();c=conn.cursor()
                        btype=f"{pt['pattern']}_{pt['break_dir']}"
                        c.execute("""INSERT INTO daily_data(symbol,day,breakout_type,breakout_ts)
                            VALUES(?,?,?,?) ON CONFLICT(symbol,day) DO UPDATE SET breakout_type=?,breakout_ts=?""",
                            (sym,today_str(),btype,time.time(),btype,time.time()))
                        conn.commit();conn.close()
                        pattern_alerts.append((sym,pt))

                    # ── ثبت حجم US برای «فعال در سشن US» ──
                    if sess=="us" and liq:
                        conn=db();c=conn.cursor()
                        c.execute("""INSERT INTO daily_data(symbol,day,us_volume) VALUES(?,?,?)
                            ON CONFLICT(symbol,day) DO UPDATE SET us_volume=us_volume+?""",
                            (sym,today_str(),liq["rvol"],liq["rvol"]))
                        conn.commit();conn.close()
                except Exception:
                    pass
                if i%8==0:time.sleep(0.4)
            # ── ارسال هشدار شکست الگو (حداکثر ۸ تا که spam نشه) ──
            for sym,pt in pattern_alerts[:8]:
                broadcast(build_pattern_message(sym,pt));time.sleep(0.5)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] سشن={sess} اسکن={int(time.time()-t0)}s شکست‌الگو={len(pattern_alerts)}")
        except Exception as e:
            print(f"[!] scan: {e}")
        time.sleep(SCAN_INTERVAL)


def main():
    print("="*50);print("ربات چرخه + نقدینگی + خبر (نسخه ۲)");print("="*50)
    if not TOKEN or not ADMIN_CHAT:print("[!] TOKEN/ADMIN_CHAT تنظیم نشده!")
    print(f"[*] numpy/pandas: {'فعال' if HAS_NP else 'غیرفعال'}")
    print(f"[*] خبر زنده: {'فعال' if NEWS_API else 'غیرفعال (NEWS_API نذاشتی)'}")
    init_db()
    if ADMIN_CHAT:tg_send(ADMIN_CHAT,"✅ ربات نسخه ۲ روشن شد (چرخه + نقدینگی + خبر + تایم US).")
    threading.Thread(target=poll_updates,daemon=True).start()
    threading.Thread(target=session_alert_loop,daemon=True).start()
    scan_loop()

if __name__=="__main__":
    main()
