"""恐惧贪婪指数 + 山寨季指数"""
import requests

def get_fear_greed():
    """alternative.me 免费 API"""
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        d = r.json()["data"][0]
        val = int(d["value"])
        label = d["value_classification"]
        # 反向映射：极度恐惧=3分，恐惧=2，中性=2，贪婪=1，极度贪婪=0
        if val <= 25: score, level = 3, "极度恐惧 → 买入信号"
        elif val <= 45: score, level = 2, "恐惧 → 偏多"
        elif val <= 55: score, level = 2, "中性"
        elif val <= 75: score, level = 1, "贪婪 → 偏空"
        else: score, level = 0, "极度贪婪 → 卖出信号"
        return {"value": val, "label": label, "score": score, "level": level, "source": "alternative.me"}
    except:
        return {"value": None, "label": "N/A", "score": 1, "level": "无法获取", "source": "error"}

def get_altcoin_season():
    """山寨季指数 - Blockchain Center 免费"""
    try:
        r = requests.get("https://www.blockchaincenter.net/api/altcoin-season/", timeout=10)
        d = r.json()
        val = int(d.get("altcoinSeasonIndex", 50))
        if val >= 75: score, label = 3, f"山寨季({val})"
        elif val >= 50: score, label = 2, f"过渡({val})"
        elif val >= 25: score, label = 1, f"比特币季({val})"
        else: score, label = 0, f"深度比特币季({val})"
        return {"value": val, "score": score, "label": label, "source": "blockchaincenter.net"}
    except:
        return {"value": None, "score": 2, "label": "N/A", "source": "error"}

if __name__ == "__main__":
    fg = get_fear_greed()
    alt = get_altcoin_season()
    print(f"恐惧贪婪: {fg}")
    print(f"山寨季: {alt}")
