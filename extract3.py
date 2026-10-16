import json, re, glob, os

base = r"E:\Trade\TradingAgents\results\daily"
files = {
    "159740.SZ": "159740.SZ_20261014_141259_737780_52c32115817745efb02353ece9f5320f.json",
    "159158.SZ": "159158.SZ_20261014_141259_737780_52c32115817745efb02353ece9f5320f.json",
}

def grab(text, *keys):
    out = {}
    for k in keys:
        m = re.search(re.escape(k) + r"\**\s*:?\s*\*{0,2}\s*([^\n|]+)", text)
        if m:
            out[k] = m.group(1).strip().rstrip("*").strip()
    return out

for tk, fn in files.items():
    p = os.path.join(base, fn)
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
    r = d["result"]
    print("="*70)
    print("TICKER:", tk)
    print("decision:", r.get("decision"), "| final_rating:", r.get("final_rating"))
    plan = r.get("trader_investment_plan", "") or ""
    # premium from instrument_context
    ctx = r.get("instrument_context", "") or ""
    pm = re.findall(r"(\d{6}\.SZ[^\n]*?溢价\s*-?\d+\.\d+%[^\n]*)", ctx)
    if pm:
        for line in pm:
            print("PREMIUM:", line.strip())
    # price block
    for label in ["Action","Entry Price","Current Price","Stop Loss","Invalidation Price",
                  "Support Levels","Resistance Levels"]:
        mm = re.search(re.escape(label)+r"[^\n:]*:\s*\*{0,2}([^\n*]+)", plan)
        if mm:
            print(f"  {label}: {mm.group(1).strip()}")
    # scale out ladder table
    sm = re.search(r"Scale-out Ladder.*?(?:\n\n|\Z)", plan, re.S)
    if sm:
        print("  Scale-out Ladder:")
        for ln in sm.group(0).splitlines():
            if ln.strip().startswith("|") or "Rung" in ln:
                print("   ", ln.strip())
    # final decision tail
    fm = re.search(r"FINAL TRANSACTION PROPOSAL:\s*\**(\w+)\**", plan)
    if fm:
        print("  FINAL:", fm.group(1))
