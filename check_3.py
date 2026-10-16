import requests
S = requests.Session()
S.trust_env = False
codes = ["159792", "159740", "159158"]
r = S.get("https://qt.gtimg.cn/q=" + ",".join(f"sz{c}" for c in codes), timeout=20)
r.encoding = "gbk"
for line in r.text.strip().split(";"):
    if "=" not in line:
        continue
    v = line.partition("=")[2].strip().strip('"').split("~")
    if len(v) > 45:
        print(f"{v[2]} | {v[1]} | 最新价={v[3]} | 涨跌幅={v[32]}% | 最高={v[33]} | 最低={v[34]} | 成交额(万)={float(v[37]) if v[37] else 0:.0f} | 换手={v[38]}%", flush=True)
