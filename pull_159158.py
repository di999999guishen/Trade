import urllib.request, json, statistics

code = "sz159158"
url = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={code}&scale=240&ma=no&datalen=90"
req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0","Referer":"https://finance.sina.com.cn"})
raw = urllib.request.urlopen(req, timeout=20).read().decode("gbk", errors="ignore")
arr = json.loads(raw)
data = []
for r in arr:
    data.append((r["day"], float(r["open"]), float(r["close"]), float(r["high"]), float(r["low"]), float(r["volume"])))
data.sort(key=lambda x:x[0])

closes=[x[2] for x in data]; dates=[x[0] for x in data]
def sma(a,n): return sum(a[-n:])/n if len(a)>=n else None
last_date,last_close=dates[-1],closes[-1]
i1=max(0,len(closes)-21); m1_date,m1_close=dates[i1],closes[i1]
m1_ret=(last_close/m1_close-1)*100
ma20=sma(closes,20); ma60=sma(closes,60)
win=closes[i1:]; hi1m,lo1m=max(win),min(win)
v5=sma([x[5] for x in data],5); v20=sma([x[5] for x in data],20)
mid=ma20; sd=statistics.pstdev(win[-20:]) if len(win)>=20 else None
up=mid+2*sd if sd else None; dn=mid-2*sd if sd else None
pos=(last_close-lo1m)/(hi1m-lo1m)*100 if hi1m>lo1m else 50
print(f"最后交易日: {last_date}  收盘: {last_close:.4f}")
print(f"一个月前({m1_date})收盘: {m1_close:.4f}  月内涨跌: {m1_ret:+.2f}%")
print(f"MA20: {ma20:.4f}  MA60: {ma60:.4f}")
print(f"价格 vs MA20: {(last_close/ma20-1)*100:+.2f}%   vs MA60: {(last_close/ma60-1)*100:+.2f}%")
print(f"月内最高: {hi1m:.4f}  最低: {lo1m:.4f}  当前在月内区间位置: {pos:.1f}%")
print(f"布林上轨: {up:.4f}  中轨: {mid:.4f}  下轨: {dn:.4f}")
print(f"近5日均量: {v5:,.0f}  近20日均量: {v20:,.0f}  量比: {v5/v20:.2f}")
print("\n近12交易日:")
for x in data[-12:]:
    print(f"  {x[0]}  O{x[1]:.4f} C{x[2]:.4f} H{x[3]:.4f} L{x[4]:.4f} V{x[5]:,.0f}")
