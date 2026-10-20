"""A-share rotation model v2: evidence-gated, risk-controlled research signals.

No fictional quotes or money-flow. Keep JSON output backward-compatible with v1 UI.
"""
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import akshare as ak
import numpy as np
import pandas as pd

TZ = ZoneInfo('Asia/Shanghai')
OUT = Path('docs/data.json')
MAX_STOP_PCT = 8.0
MAX_MA20_DEVIATION_PCT = 10.0
UNIVERSE = {
    '300115': ('长盈精密', '机器人'),
    '688836': ('宇树科技-W', '机器人'),
    '603119': ('浙江荣泰', '机器人'),
    '605488': ('福莱新材', '机器人'),
    '002164': ('宁波东力', '机器人'),
    '688608': ('恒玄科技', '半导体'),
    '688689': ('银河微电', '半导体'),
    '688071': ('华依科技', '机器人'),
    '603068': ('博通集成', '半导体'),
    '603986': ('兆易创新', '半导体'),
    '688018': ('乐鑫科技', '半导体'),
    '688525': ('佰维存储', '半导体'),
}


def number(value, decimals=4):
    try:
        value = float(str(value).strip().replace(',', '').replace('%', ''))
        return round(value, decimals) if np.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def history(code):
    start = '20250101'
    end = datetime.now(TZ).strftime('%Y%m%d')
    try:
        df = ak.stock_zh_a_hist(symbol=code, period='daily', start_date=start, end_date=end, adjust='qfq')
        if df is None or df.empty:
            raise ValueError('东方财富行情为空')
        provider = 'eastmoney'
    except Exception as exc:
        print('EASTMONEY FAILED', code, str(exc)[:150], flush=True)
        prefix = 'sh' if code.startswith('6') else 'sz'
        df = ak.stock_zh_a_hist_tx(symbol=prefix + code, start_date=start, end_date=end, adjust='qfq')
        if df is None or df.empty:
            raise ValueError('腾讯行情为空')
        provider = 'tencent'
    df = df.rename(columns={'日期':'date','开盘':'open','最高':'high','最低':'low',
                            '收盘':'close','成交量':'volume','成交额':'amount'})
    # Tencent history historically labels traded-volume as 'amount'; never call it turnover.
    if 'volume' not in df.columns and 'amount' in df.columns:
        df['volume'] = df['amount']
        df = df.drop(columns=['amount'])
    required = ['date','open','high','low','close','volume']
    if any(k not in df.columns for k in required):
        raise ValueError('缺少OHLCV字段: ' + str(list(df.columns)))
    df['date'] = pd.to_datetime(df['date'], errors='coerce').dt.strftime('%Y-%m-%d')
    for key in required[1:]:
        df[key] = pd.to_numeric(df[key], errors='coerce')
    df = df.dropna(subset=required).sort_values('date').drop_duplicates('date').reset_index(drop=True)
    if len(df) < 40:
        raise ValueError('有效日线不足40根')
    print('SOURCE', provider, code, str(df.iloc[-1].date), flush=True)
    return df, provider


def money_flow(code):
    """Return source-provided net flow and source-provided net ratio, never synthesize."""
    try:
        market = 'sh' if code.startswith('6') else 'sz' if code.startswith(('0','3')) else 'bj'
        df = ak.stock_individual_fund_flow(stock=code, market=market)
        if df is None or df.empty or not {'日期','主力净流入-净额'}.issubset(df.columns):
            return []
        df = df.sort_values('日期').drop_duplicates('日期').tail(12)
        out = []
        for _, row in df.iterrows():
            dt = pd.to_datetime(row['日期'], errors='coerce')
            if pd.isna(dt):
                continue
            out.append({'date':dt.strftime('%Y-%m-%d'),
                        'net_yuan':number(row['主力净流入-净额'], 2),
                        'net_pct':number(row.get('主力净流入-净占比'))})
        return out
    except Exception as exc:
        print('FLOW FAILED', code, str(exc)[:100], flush=True)
        return []


def calculate(code, name, sector, df, provider, flow_rows):
    df = df.copy()
    for n in (5,10,20):
        df[f'ma{n}'] = df.close.rolling(n).mean()
    ema12, ema26 = df.close.ewm(span=12, adjust=False).mean(), df.close.ewm(span=26, adjust=False).mean()
    df['dif'] = ema12 - ema26
    df['dea'] = df.dif.ewm(span=9, adjust=False).mean()
    df['macd'] = 2*(df.dif-df.dea)
    last, prev = df.iloc[-1], df.iloc[-2]
    asof = str(last.date)
    close, ma20 = float(last.close), float(last.ma20)
    dev = 100*(close/ma20-1)
    ma20_slope = 100*(ma20/float(df.iloc[-6].ma20)-1)
    vol5 = float(df.volume.tail(5).mean())
    volume_ratio = float(last.volume/vol5) if vol5 > 0 else 0

    # Require flows for exactly the most recent three daily candles, not an arbitrary old 3-day set.
    expected_dates = df.date.tail(3).tolist()
    flows_by_day = {f['date']:f for f in flow_rows}
    aligned = [flows_by_day.get(day, {'date':day,'net_yuan':None,'net_pct':None}) for day in expected_dates]
    flow_complete = all(x.get('net_yuan') is not None and x.get('net_pct') is not None for x in aligned)
    flow_amount_complete = all(x.get('net_yuan') is not None for x in aligned)
    positives = sum(1 for x in aligned if x.get('net_yuan') is not None and x['net_yuan'] > 0)
    flow_total = sum(x['net_yuan'] for x in aligned) if flow_amount_complete else None
    flow_mean_pct = (sum(x['net_pct'] for x in aligned)/3) if flow_complete else None
    flow_strength = bool(flow_complete and positives >= 2 and flow_total > 0 and flow_mean_pct >= 0.5)

    price_above_ma = close > ma20
    ma_trending = ma20_slope > 0
    ma_stacked = bool(float(last.ma5) > float(last.ma10) > ma20)
    macd_strong = bool(last.dif > last.dea and last.dif > prev.dif)
    volume_support = volume_ratio >= 1.10 and close > float(prev.close)

    # 100-point transparent score; missing fund data earns zero fund points.
    trend_points = (15 if price_above_ma else 0) + (12 if ma_trending else 0) + (8 if ma_stacked else 0)
    macd_points = (15 if macd_strong else 0) + (5 if last.macd > prev.macd else 0)
    volume_points = (10 if volume_support else 0)
    flow_points = 0
    if flow_complete:
        flow_points += 10 if positives == 3 else (6 if positives == 2 else 0)
        flow_points += 10 if flow_total > 0 and flow_mean_pct >= 1 else (6 if flow_total > 0 and flow_mean_pct >= 0.5 else 0)
        flow_points += 5 if aligned[-1]['net_yuan'] > 0 and aligned[-1]['net_pct'] > 0 else 0
    momentum_points = 10 if close > float(df.iloc[-6].close) else 0
    score = min(100, int(trend_points+macd_points+volume_points+flow_points+momentum_points))

    # One actionable breakout trigger and one structural stop, with strict stop-distance gate.
    breakout = max(float(df.high.iloc[-11:-1].max()), close)
    entry = round(breakout*1.002, 2)
    # Stop chosen closer to entry: recent five-day support or MA20 protection.
    raw_stop = max(float(df.low.tail(5).min())*0.995, ma20*0.975)
    stop = round(raw_stop, 2) if raw_stop < entry else None
    risk_pct = 100*(entry-stop)/entry if stop is not None else None
    risk_ok = bool(risk_pct is not None and 0 < risk_pct <= MAX_STOP_PCT)
    dev_ok = bool(0 < dev <= MAX_MA20_DEVIATION_PCT)

    blockers=[]
    if not price_above_ma: blockers.append('收盘价未站上日线MA20')
    if not ma_trending: blockers.append('日线MA20尚未向上')
    if not flow_complete: blockers.append('最近三交易日主力净流入金额或净占比未齐')
    elif not flow_strength: blockers.append('三日资金连续性/强度不足')
    if not dev_ok: blockers.append('MA20偏离率未落在0%至10%区间')
    if not risk_ok: blockers.append('止损距离超过8%或结构止损无效')
    if not macd_strong: blockers.append('MACD未确认')

    eligible_first = (score >= 70 and ma_trending and price_above_ma and macd_strong
                      and flow_strength and risk_ok and dev_ok)
    tier = ('第一梯队：重点盯盘' if eligible_first else
            '第二梯队：观察回踩机会' if score >= 45 and price_above_ma else
            '第三梯队：条件未成熟')
    candles=[]
    for _, r in df.tail(90).iterrows():
        candles.append({'date':str(r.date), **{k:number(r[k]) for k in
                 ['open','high','low','close','volume','ma5','ma10','ma20','dif','dea','macd']}})
    return {'code':code,'name':name,'sector':sector,'asof':asof,'provider':provider,
            'close':number(close),'ma20':number(ma20),'ma20_deviation_pct':number(dev),
            'ma20_slope_5d_pct':number(ma20_slope),'volume_ratio_5d':number(volume_ratio),
            'score':score,'tier':tier,'buy_trigger':entry,'stop_reference':stop,
            'stop_distance_pct':number(risk_pct),'risk_ok':risk_ok,'trade_eligible':eligible_first,
            'flow_positive_days':positives,'flow_net_sum_3d_yuan':number(flow_total,2),
            'flow_avg_net_pct_3d':number(flow_mean_pct),'flow_strong':flow_strength,
            'fund_flow_3d':aligned,'fund_flow_status':('available' if flow_complete else
                             'amount_only' if flow_amount_complete else 'unavailable'),
            'signal_blockers':blockers,'candles':candles}


def main():
    now = datetime.now(TZ)
    data = {'updated_at_beijing':now.isoformat(), 'status':'ok',
            'model_version':'v2.0-risk-gated','provider':'AKShare Eastmoney / Tencent historical K lines',
            'universe':'固定12只候选股票，非全市场动态轮动',
            'strategy_rules':{'max_stop_distance_pct':MAX_STOP_PCT,
                              'max_ma20_deviation_pct':MAX_MA20_DEVIATION_PCT,
                              'fund_flow':'必须与最近3根日K线日期一致，至少2日流入、3日净额为正且日均净占比>=0.5%',
                              'note':'数据时间差、缺失资金将阻止进入第一梯队'},
            'disclaimer':'研究信号与条件参考价，不构成投资建议；不得按本工具自动下单。',
            'stocks':[], 'errors':[]}
    for code, (name, sector) in UNIVERSE.items():
        try:
            df, provider = history(code)
            data['stocks'].append(calculate(code,name,sector,df,provider,money_flow(code)))
            print('OK', code, flush=True)
        except Exception as exc:
            data['errors'].append({'code':code,'error':str(exc)[:180]})
            print('ERROR',code,str(exc)[:180],flush=True)
        time.sleep(0.6)
    if not data['stocks']:
        raise RuntimeError('所有行情源失败，保留上次已发布的有效数据')

    # Demote stocks whose daily bars lag behind the freshest bar in this refresh batch.
    freshest = max(x['asof'] for x in data['stocks'])
    # Intraday on a weekday: yesterday's last candle is stale, even if all providers lag.
    # Holidays may cause conservative false negatives; we never substitute an invented candle.
    current_session_expected = (now.weekday() < 5 and now.hour >= 10)
    expected_date = max(freshest, now.strftime('%Y-%m-%d')) if current_session_expected else freshest
    for s in data['stocks']:
        s['latest_universe_date'] = freshest
        s['expected_session_date'] = expected_date
        s['is_stale'] = s['asof'] < expected_date
        if s['is_stale']:
            s['signal_blockers'].insert(0, f'日线滞后：{s["asof"]}，参考交易日：{expected_date}')
            if s['tier'].startswith('第一'):
                s['tier']='第二梯队：观察回踩机会'
                s['trade_eligible']=False
    data['latest_universe_date']=freshest
    data['expected_session_date']=expected_date
    data['stocks'].sort(key=lambda s: (
        {'第一':0,'第二':1,'第三':2}[s['tier'][0:2]], -s['score'], s['code']))
    if data['errors']:
        data['status']='partial'
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data,ensure_ascii=False,separators=(',',':')), encoding='utf-8')
    print('Wrote', OUT, 'stocks',len(data['stocks']), 'latest',freshest)


if __name__ == '__main__':
    main()
