"""
NSE 200 scanner (bearish setup) - 1 hour candles, data from Yahoo Finance.

A stock is flagged when, TODAY:
  First candle (9:15 candle):
    1. Open  is below the upper Bollinger band (20,2)
    2. Close is below SMA 20
    3. High  is above the upper Bollinger band (20,2)
    4. Low   is below the upper Bollinger band (20,2)
  AND a later 1h candle today drops below the first candle's Low.

Runs every 5 minutes on GitHub Actions between 9:15 and 15:15 IST.
Each stock is alerted only once per day.
"""
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests

IST = ZoneInfo("Asia/Kolkata")
STATE_FILE = "alerted.json"
BB_PERIOD = 20
BB_MULT = 2.0
CHUNK = 50

# Nifty 200 list from NSE (niftyindices). Update twice a year when NSE rebalances.
SYMBOLS = """
360ONE ABB APLAPOLLO AUBANK ADANIENSOL ADANIENT ADANIGREEN ADANIPORTS ADANIPOWER ATGL
ABCAPITAL AMBUJACEM APARINDS APOLLOHOSP ASHOKLEY ASIANPAINT ASTRAL AUROPHARMA DMART AXISBANK
BSE BAJAJ-AUTO BAJFINANCE BAJAJFINSV BAJAJHLDNG BANKBARODA BANKINDIA MAHABANK BDL BEL
BHARATFORG BHEL BPCL BHARTIARTL GROWW BIOCON BLUESTARCO BOSCHLTD BRITANNIA CGPOWER
CANBK CHOLAFIN CIPLA COALINDIA COCHINSHIP COFORGE COLPAL CONCOR CUMMINSIND DLF
DABUR DIVISLAB DIXON DRREDDY EICHERMOT ETERNAL EXIDEIND NYKAA FEDERALBNK FORTIS
GAIL GVT&D GMRAIRPORT GLENMARK GODFRYPHLP GODREJCP GODREJPROP GRASIM HCLTECH HDFCAMC
HDFCBANK HDFCLIFE HAVELLS HEROMOTOCO HINDALCO HAL HINDCOPPER HINDPETRO HINDUNILVR HINDZINC
POWERINDIA HYUNDAI ICICIBANK ICICIGI ICICIAMC IDFCFIRSTB ITC INDIANB INDHOTEL IOC
IRCTC IRFC IREDA INDUSTOWER INDUSINDBK NAUKRI INFY INDIGO JSWENERGY JSWSTEEL
JINDALSTEL JIOFIN JUBLFOOD KEI KALYANKJIL KOTAKBANK LTF LGEINDIA LICHSGFIN LTM
LT LAURUSLABS LENSKART LICI LODHA LUPIN MRF M&MFIN M&M MANKIND MARICO
MARUTI MFSL MAXHEALTH MAZDOCK MEESHO MOTILALOFS MPHASIS MCX MUTHOOTFIN NHPC
NLCINDIA NMDC NTPC NATIONALUM NESTLEIND OBEROIRLTY ONGC OIL PAYTM OFSS
POLICYBZR PIIND PAGEIND PATANJALI PERSISTENT PHOENIXLTD PIDILITIND POLYCAB PFC POWERGRID
PREMIERENE PRESTIGE PNB RECLTD RADICO RVNL RELIANCE SBICARD SBILIFE SRF
MOTHERSON SHRIRAMFIN ENRIN SIEMENS SOLARINDS SBIN SAIL SUNPHARMA SUPREMEIND SUZLON
SWIGGY TVSMOTOR TATACAP TATACOMM TCS TATACONSUM TMCV TMPV TATAPOWER TATASTEEL
TECHM TITAN TORNTPHARM TRENT TIINDIA UPL ULTRACEMCO UNIONBANK UNITDSPR VBL
VAML VEDL VMM IDEA VOLTAS WAAREEENER WIPRO YESBANK ZYDUSLIFE
""".split()


def notify(text):
    print(text)
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": text},
            timeout=30,
        )
    except Exception as e:
        print("Telegram error:", e)


def fmt(x):
    return f"{x:,.2f}"


def evaluate(df, today):
    """df: 1h candles (index in IST, columns Open/High/Low/Close).
    Returns a dict if the stock matches today, else None."""
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    if len(df) < BB_PERIOD + 2:
        return None
    close = df["Close"]
    sma = close.rolling(BB_PERIOD).mean()
    std = close.rolling(BB_PERIOD).std(ddof=0)
    upper = sma + BB_MULT * std

    day_rows = df[df.index.date == today]
    if day_rows.empty:
        return None
    first_ts = day_rows.index[0]
    if not (first_ts.hour == 9 and first_ts.minute == 15):
        return None  # today's first candle is missing
    first = df.loc[first_ts]
    up, ma = upper.loc[first_ts], sma.loc[first_ts]
    if pd.isna(up) or pd.isna(ma):
        return None

    setup = (
        first["Open"] < up
        and first["Close"] < ma
        and first["High"] > up
        and first["Low"] < up
    )
    if not setup:
        return None

    later = day_rows.iloc[1:]  # candles after the first one
    broke = later[later["Low"] < first["Low"]]
    if broke.empty:
        return None
    return {
        "first_low": float(first["Low"]),
        "first_high": float(first["High"]),
        "upper_bb": float(up),
        "sma20": float(ma),
        "price": float(later["Close"].iloc[-1]),
        "low_now": float(later["Low"].min()),
        "since": broke.index[0],
    }


def download(symbols):
    import yfinance as yf  # imported here so tests can run without it

    out, skipped = {}, []
    for i in range(0, len(symbols), CHUNK):
        chunk = symbols[i:i + CHUNK]
        tickers = [s + ".NS" for s in chunk]
        try:
            data = yf.download(
                tickers, period="1mo", interval="1h", group_by="ticker",
                auto_adjust=False, threads=True, progress=False,
            )
        except Exception as e:
            print("Download error:", e)
            skipped += chunk
            continue
        for s, t in zip(chunk, tickers):
            try:
                d = data[t].dropna(how="all")
                if d.empty:
                    skipped.append(s)
                    continue
                if d.index.tz is None:
                    d.index = d.index.tz_localize("UTC")
                d.index = d.index.tz_convert(IST)
                out[s] = d
            except Exception:
                skipped.append(s)
    return out, skipped


def load_state(today_str):
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                st = json.load(f)
            if st.get("date") == today_str:
                return st
        except Exception:
            pass
    return {"date": today_str, "symbols": []}


def main():
    now = datetime.now(IST)
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    minutes = now.hour * 60 + now.minute
    in_window = now.weekday() < 5 and 9 * 60 + 15 <= minutes <= 15 * 60 + 15
    if not in_window and not manual:
        print("Outside 9:15-15:15 IST (Mon-Fri). Nothing to do.")
        return

    today = now.date()
    state = load_state(today.isoformat())
    data, skipped = download(SYMBOLS)
    print(f"Loaded {len(data)} stocks, skipped {len(skipped)}: {skipped}")

    hits = []
    for s, df in data.items():
        if s in state["symbols"]:
            continue
        try:
            r = evaluate(df, today)
        except Exception as e:
            print(s, "error:", e)
            continue
        if r:
            hits.append((s, r))

    if hits:
        lines = [f"📉 NSE scanner: {len(hits)} new signal(s) ({now.strftime('%I:%M %p')} IST)"]
        for s, r in hits:
            lines.append(
                f"\n{s}\n"
                f"Price {fmt(r['price'])} broke below first candle low {fmt(r['first_low'])}\n"
                f"First candle high {fmt(r['first_high'])} vs upper BB {fmt(r['upper_bb'])}, SMA20 {fmt(r['sma20'])}"
            )
            state["symbols"].append(s)
        notify("\n".join(lines))
    elif manual:
        notify(
            f"✅ NSE scanner test OK\n{len(data)} stocks scanned, "
            f"{len(skipped)} skipped (no Yahoo data), 0 signals right now."
        )

    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


if __name__ == "__main__":
    main()
  
