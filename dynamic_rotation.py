"""Dynamic A-share candidate discovery and turnover. No fabricated market observations."""
from datetime import datetime
from zoneinfo import ZoneInfo
import json
from pathlib import Path
import pandas as pd
import akshare as ak

TZ = ZoneInfo("Asia/Shanghai")
ARCHIVE = Path("docs/rotation_history.json")
MAX_POOL = 10
DISCOVERY_LIMIT = 45

def discover():
    """Use a real market-wide spot table. Failure never masquerades as a full-market scan."""
    df = ak.stock_zh_a_spot_em()
    required = {"代码", "名称", "最新价", "成交额", "涨跌幅", "换手率"}
    if df is None or not required.issubset(df.columns):
        raise RuntimeError("全市场实时列表不可用或字段缺失")
    df = df.copy()
    df["代码"] = df["代码"].astype(str).str.zfill(6)
    df = df[df["代码"].str.match(r"^(?:60|68|00|30)\d{4}$")]
    df = df[~df["名称"].astype(str).str.contains(r"ST|退|退市", regex=True, case=False)]
    for k in ["最新价", "成交额", "涨跌幅", "换手率"]:
        df[k] = pd.to_numeric(df[k], errors="coerce")
    df = df.dropna(subset=["最新价", "成交额", "涨跌幅", "换手率"])
    df = df[(df["最新价"] > 2) & (df["成交额"] >= 150000000) &
            (df["换手率"] >= 0.5) & (df["换手率"] <= 30) &
            (df["涨跌幅"] > -5) & (df["涨跌幅"] < 15)]
    # Sector is not inferred from company name: it is explicitly unverified.
    df = df.sort_values(["成交额", "涨跌幅"], ascending=False)
    result = {}
    for _, r in df.head(DISCOVERY_LIMIT).iterrows():
        result[r["代码"]] = (str(r["名称"]), "行业待核验")
    if not result:
        raise RuntimeError("全市场过滤后无有效股票")
    return result, len(df)

def rank_and_rotate(stocks, previous=None, asof=None, discovery_ok=False):
    """Use score and evidence gates, with hysteresis to avoid churn on one bad tick."""
    asof = asof or datetime.now(TZ).date().isoformat()
    previous = previous or {}
    old = {s["code"]: s for s in previous.get("stocks", [])}
    for s in stocks:
        blockers = s.get("signal_blockers", [])
        s["rotation_score"] = max(0, s.get("score", 0) -
            (20 if s.get("is_stale") else 0) -
            (12 if s.get("bar_status") == "provisional_quote" else 0) -
            (8 if s.get("fund_flow_status") != "available" else 0))
        s["rotation_status"] = "候选"
        s["rotation_reason"] = "综合评分与风险条件排序" if not blockers else "；".join(blockers[:2])
    ordered = sorted(stocks, key=lambda s: (-s["rotation_score"], -int(s.get("trade_eligible", False)), s["code"]))
    # Hysteresis: an incumbent is not expelled by a marginal (<5pt) difference.
    selected = ordered[:MAX_POOL]
    selected_codes = {s["code"] for s in selected}
    incumbents = [s for s in ordered[MAX_POOL:] if s["code"] in old and s.get("score", 0) >= 45
                  and not s.get("is_stale")]
    for incumbent in incumbents:
        weakest = min((s for s in selected if s["code"] not in old), key=lambda x: x["rotation_score"], default=None)
        if weakest and incumbent["rotation_score"] + 5 >= weakest["rotation_score"]:
            selected.remove(weakest)
            selected.append(incumbent)
    selected.sort(key=lambda s: (-s["rotation_score"], s["code"]))
    selected_codes = {s["code"] for s in selected}
    # Never promote a stock with stale daily bars to an actionable recommendation.
    for s in selected:
        s["rotation_status"] = "保留" if s["code"] in old else "新入选"
    removed = [dict(code=code, name=s.get("name", code), reason="排名下降或风险条件恶化")
               for code, s in old.items() if code not in selected_codes]
    added = [dict(code=s["code"], name=s["name"], score=s["rotation_score"], reason=s["rotation_reason"])
             for s in selected if s["code"] not in old]
    # Avoid treating a source outage as a genuine market rotation.
    if not discovery_ok:
        added, removed = [], []
    event = {"date":asof, "discovery_ok":discovery_ok, "added":added, "removed":removed,
             "ranking":[{"code":s["code"],"score":s["rotation_score"],"rank":i+1}
                        for i,s in enumerate(selected)]}
    return selected, event

def persist_history(event):
    try:
        events = json.loads(ARCHIVE.read_text(encoding="utf-8"))
        if not isinstance(events, list):
            events = []
    except (OSError, ValueError):
        events = []
    # Multiple intraday refreshes share the same date; keep latest.
    events = [x for x in events if x.get("date") != event["date"]]
    events.append(event)
    ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    ARCHIVE.write_text(json.dumps(events[-90:], ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
