"""板块轮动 + ETH/BTC比率 + DXY"""
import requests

def get_sector_rotation():
    """CoinGecko 各类别24h涨跌幅 → 判断资金流向"""
    try:
        r = requests.get("https://api.coingecko.com/api/v3/coins/categories", timeout=15)
        cats = r.json()[:15]
        # 取前5热门赛道
        top = []
        for c in cats[:5]:
            name = c["name"]
            # 跳过太泛的类别
            if any(x in name.lower() for x in ["layer 1", "smart contract", " cryptocurrency", "stablecoin"]):
                continue
            chg = c.get("market_cap_change_24h", 0) or 0
            top.append({"name": name, "change_24h": round(chg, 1)})
        return {"top_sectors": top[:3], "score": 2 if top and top[0]["change_24h"] > 1 else 1, "source": "coingecko"}
    except:
        return {"top_sectors": [], "score": 2, "source": "error"}

def get_eth_btc_ratio():
    """ETH/BTC 比率 → 山寨季信号"""
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=ethereum,bitcoin&vs_currencies=usd", timeout=10)
        eth = r.json()["ethereum"]["usd"]
        btc = r.json()["bitcoin"]["usd"]
        ratio = eth / btc
        # >0.06=山寨季, 0.04-0.06=中性, <0.04=比特币主导
        if ratio >= 0.06: score, level = 2, f"山寨季({ratio:.4f})"
        elif ratio >= 0.04: score, level = 1, f"中性({ratio:.4f})"
        else: score, level = 0, f"比特币主导({ratio:.4f})"
        return {"ratio": round(ratio, 4), "score": score, "level": level, "eth": eth, "btc": btc, "source": "coingecko"}
    except:
        return {"ratio": None, "score": 1, "level": "N/A", "source": "error"}

def get_dxy():
    """美元指数 DXY"""
    try:
        import yfinance as yf
        dxy = yf.Ticker("DX-Y.NYB")
        hist = dxy.history(period="1mo")
        if not hist.empty:
            current = hist["Close"].iloc[-1]
            prev = hist["Close"].iloc[0]
            chg = (current - prev) / prev * 100
            # DXY上涨=利空, 下跌=利好
            if chg < -1: score, level = 3, f"↓{abs(chg):.1f}% 利好"
            elif chg < 0: score, level = 2, f"↓{abs(chg):.1f}%"
            elif chg < 1: score, level = 1, f"↑{chg:.1f}%"
            else: score, level = 0, f"↑{chg:.1f}% 利空"
            return {"value": round(current, 2), "change_pct": round(chg, 1), "score": score, "level": level}
    except:
        pass
    return {"value": None, "score": 2, "level": "N/A", "source": "error"}

if __name__ == "__main__":
    r = get_sector_rotation()
    e = get_eth_btc_ratio()
    d = get_dxy()
    print(f"板块: {r}")
    print(f"ETH/BTC: {e}")
    print(f"DXY: {d}")
