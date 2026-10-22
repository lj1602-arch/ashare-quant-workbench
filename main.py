"""A-share rotation model v2: evidence-gated, risk-controlled research signals.

No fictional quotes or money-flow. Keep JSON output backward-compatible with v1 UI.
"""
import json
import re
import time
import requests
from datetime import datetime, time as clock_time
from pathlib import Path
from zoneinfo import ZoneInfo

import akshare as ak
from dynamic_rotation import discover, rank_and_rotate, persist_history
import numpy as np
import pandas as pd

TZ = ZoneInfo('Asia/Shanghai')
OUT = Path('docs/data.json')
MAX_STOP_PCT = 8.0
MAX_MA20_DEVIATION_PCT = 10.0
# Wait for the upstream's delayed official daily-bar publication, not merely the 15:00 bell.
CLOSE_CONFIRM_AFTER = clock_time(15, 10)
QUOTE_CLOSE_REL_TOL = 0.001  # 0.1%, with minimum ¥0.01 absolute

UNIVERSE = {
    '300115': ('长盈精密', '机器人'),
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


def normalize_history(df, provider):
    """Normalize unadjusted historical OHLCV; never mix qfq with spot raw quotes."""
    if df is None or df.empty:
        raise ValueError('历史行情为空')
    df = df.rename(columns={'日期':'date','开盘':'open','最高':'high','最低':'low',
                            '收盘':'close','成交量':'volume','成交额':'amount'}).copy()
    # Tencent's API may call share volume 'amount'; check known column meanings.
    if 'volume' not in df.columns and 'amount' in df.columns and provider == 'tencent':
        df['volume'] = df['amount']
        df = df.drop(columns=['amount'])
    required = ['date','open','high','low','close','volume']
    if any(k not in df.columns for k in required):
        raise ValueError('OHLCV字段缺失：' + str(list(df.columns)))
    df['date'] = pd.to_datetime(df['date'], errors='coerce').dt.strftime('%Y-%m-%d')
    for key in required[1:]:
        df[key] = pd.to_numeric(df[key], errors='coerce')
    df = df.dropna(subset=required).sort_values('date').drop_duplicates('date').reset_index(drop=True)
    if len(df) < 26:
        raise ValueError(f'有效日线只有{len(df)}根，至少需要26根')
    return df


def quote_tencent(code, now):
    """Optional latest timestamped quote; no quote means no synthesized new bar.

    Tencent quote positions: last=3, open=5, stamp=30, high=33, low=34.
    This feed is not a certified exchange closing-data feed.
    """
    prefix = 'sh' if code.startswith('6') else 'sz'
    res = requests.get('https://qt.gtimg.cn/q=' + prefix + code,
                       headers={'User-Agent':'Mozilla/5.0','Referer':'https://gu.qq.com/'},
                       timeout=7)
    res.raise_for_status()
    text = res.content.decode('gbk', errors='replace')
    match = re.search(r'="([^"]+)"', text)
    if not match:
        raise ValueError('腾讯快照结构不符')
    v = match.group(1).split('~')
    if len(v) < 35 or v[2] != code or not re.fullmatch(r'\d{14}', v[30]):
        raise ValueError('腾讯快照股票或时间字段无效')
    stamp = datetime.strptime(v[30], '%Y%m%d%H%M%S').replace(tzinfo=TZ)
    seconds_old = (now-stamp).total_seconds()
    if stamp > now or (seconds_old > 1200 and not (15 <= now.hour <= 20 and stamp.hour == 15)):
        raise ValueError('快照时间过旧/无效：' + stamp.isoformat())
    if stamp.date() != now.date():
        raise ValueError('快照不是今日行情：' + stamp.isoformat())
    px, op, hi, lo = (float(v[i]) for i in (3,5,33,34))
    if not (0 < lo <= min(op,px) <= max(op,px) <= hi and hi < 100000):
        raise ValueError('快照OHLC逻辑校验失败')
    return {'date':stamp.strftime('%Y-%m-%d'), 'open':op,'high':hi,'low':lo,
            'close':px, 'stamp':stamp.isoformat()}


def validate_historical_bar(df, now, code, quote=None):
    """Return (confirmed, reason). A daily row dated today is not enough at 15:00.

    A public source's complete end-of-day OHLCV and a post-close quote with a
    matching close are required for today's 'confirmed' display status.
    Historical rows from past dates are source-published historical candles.
    This is NOT an exchange-certified or broker-settled confirmation.
    """
    row = df.iloc[-1]
    last_date = str(row.date)
    if last_date != now.strftime('%Y-%m-%d'):
        return True, '历史交易日日线：公开数据源提供的完整OHLCV'
    if now.time() < CLOSE_CONFIRM_AFTER:
        return False, '15:10前不得把当日历史K线视为已确认收盘数据'
    values = [number(row[k]) for k in ('open','high','low','close','volume')]
    if any(x is None for x in values) or values[-1] <= 0:
        return False, '当日收盘日线OHLCV不完整'
    op,hi,lo,cl,vol = values
    if not (0 < lo <= min(op,cl) <= max(op,cl) <= hi):
        return False, '当日日K行情逻辑校验失败'
    if not quote or quote['date'] != last_date:
        return False, '等待15:00后有效行情快照交叉核对收盘价'
    stamp = datetime.fromisoformat(quote['stamp'])
    if stamp.time() < clock_time(15,0):
        return False, '快照未覆盖收盘时段'
    tolerance = max(0.01, cl*QUOTE_CLOSE_REL_TOL)
    if abs(quote['close'] - cl) > tolerance:
        return False, '日K收盘价与收盘后快照不一致'
    return True, '公开历史日K（含成交量）与收盘后行情快照核对通过'


def choose_daily_candle(candidates, now, quote):
    """Confirm today's bar only from complete daily OHLCV and independent evidence.

    If the official vendor API is unavailable, two independent public historical
    feeds agreeing on the same closing price suffice for a *cross-checked public*
    close. Never call this exchange-certified data.
    """
    latest = max(candidates, key=lambda x: (x[0], x[1]))
    last_date, _, provider, df = latest
    diagnostics = []
    for date, length, source, frame in candidates:
        row = frame.iloc[-1]
        diagnostics.append({'source': source, 'last_date': date,
                            'bars': length, 'last_close': number(row.close),
                            'last_volume': number(row.volume), 'status': 'ok'})
    if last_date != now.strftime('%Y-%m-%d'):
        return df, provider, False, '历史数据最新日为'+last_date, diagnostics
    valid, reason = validate_historical_bar(df, now, '', quote)
    if valid:
        return df, provider, True, reason, diagnostics
    if now.time() < CLOSE_CONFIRM_AFTER:
        return df, provider, False, reason, diagnostics
    matching = []
    base_close = float(df.iloc[-1].close)
    tol = max(0.01, base_close*QUOTE_CLOSE_REL_TOL)
    for date, _, src, frame in candidates:
        row = frame.iloc[-1]
        if (date == last_date and pd.notna(row.volume) and float(row.volume)>0
                and abs(float(row.close)-base_close)<=tol):
            matching.append(src)
    if len(set(matching)) >= 2:
        return df, provider, True, '两家独立公开日线源收盘价与完整成交量核对一致：'+','.join(matching), diagnostics
    return df, provider, False, reason+'；交叉确认不足', diagnostics


def history(code):
    start = '20250101'
    now = datetime.now(TZ)
    end = now.strftime('%Y%m%d')
    prefix = 'sh' if code.startswith('6') else 'sz'
    upstream = [
        ('eastmoney', lambda: ak.stock_zh_a_hist(symbol=code,period='daily',start_date=start,end_date=end,adjust='')),
        ('tencent', lambda: ak.stock_zh_a_hist_tx(symbol=prefix+code,start_date=start,end_date=end,adjust='')),
        ('sina', lambda: ak.stock_zh_a_daily(symbol=prefix+code,start_date=start,end_date=end,adjust='')),
    ]
    candidates, diagnostics = [], []
    for provider, get in upstream:
        try:
            df = normalize_history(get(), provider)
            date = str(df.iloc[-1].date)
            candidates.append((date,len(df),provider,df))
            print('HISTORY',code,provider,date,'bars',len(df),flush=True)
        except Exception as exc:
            message = str(exc)[:170]
            diagnostics.append({'source':provider,'status':'failed','error':message})
            print('HISTORY FAIL',code,provider,message,flush=True)
    if not candidates:
        raise RuntimeError('所有历史日线来源失败，不能生成收盘确认')
    q, quote_time, quote_error = None, None, None
    if now.weekday()<5 and clock_time(9,30)<=now.time()<=clock_time(23,59):
        try:
            q=quote_tencent(code,now)
            quote_time=q['stamp']
        except Exception as exc:
            quote_error=str(exc)[:170]
            print('QUOTE FAIL',code,quote_error,flush=True)
    df, provider, confirmed, reason, successes = choose_daily_candle(candidates,now,q)
    diagnostics += successes
    historical_last=str(df.iloc[-1].date)
    provisional = False
    if q is not None and q['date'] > historical_last:
        df=pd.concat([df,pd.DataFrame([{k:q[k] for k in ('date','open','high','low','close')}])],ignore_index=True)
        df['volume']=pd.to_numeric(df['volume'],errors='coerce')
        provisional=True
        reason='临时行情：当日历史日K未发布，成交量未知'
    elif historical_last == now.strftime('%Y-%m-%d'):
        provisional=not confirmed
    # Older historical bar is not the current session's close; downstream stale filter still applies.
    diag={'historical_sources':diagnostics,'quote_status': 'ok' if q else 'unavailable',
          'quote_error':quote_error,'close_check':reason,
          'selected_historical_date':historical_last}
    return df,provider,provisional,quote_time,('provisional_quote' if provisional else 'historical_daily'),reason,diag


def money_flow(code):
    """Return source-provided net flow and source-provided net ratio, never synthesize."""
    try:
        market = 'sh' if code.startswith('6') else 'sz' if code.startswith(('0','3')) else 'bj'
        df = ak.stock_individual_fund_flow(stock=code, market=market)
        if df is None or df.empty or not {'日期','主力净流入-净额'}.issubset(df.columns):
            return []
        df = df.sort_values('日期').drop_duplicates('日期').tail(20)
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


def calculate(code, name, sector, df, provider, flow_rows, provisional=False, quote_time=None, bar_status="historical_daily", confirmation_reason="", diagnostics=None):
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
    volume_ratio = float(last.volume/vol5) if vol5 > 0 and pd.notna(last.volume) else None

    # Require flows for exactly the most recent three daily candles, not an arbitrary old 3-day set.
    expected_dates = df.date.tail(3).tolist()
    flows_by_day = {f['date']:f for f in flow_rows}
    aligned = [dict(flows_by_day.get(day, {'date':day,'net_yuan':None,'net_pct':None}),
                    status='available' if flows_by_day.get(day,{}).get('net_yuan') is not None else 'pending_or_unavailable')
               for day in expected_dates]
    # Independently show the three latest published flow dates (even if D0 is missing).
    published = [dict(x,status='available') for x in sorted(flow_rows,key=lambda z:z['date'])
                 if x.get('net_yuan') is not None][-3:]
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
    volume_support = volume_ratio is not None and volume_ratio >= 1.10 and close > float(prev.close)

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
    if provisional: blockers.append('未确认正式收盘日K：禁止进入第一梯队（'+confirmation_reason+'）')

    eligible_first = (score >= 70 and ma_trending and price_above_ma and macd_strong
                      and flow_strength and risk_ok and dev_ok and not provisional)
    tier = ('第一梯队：重点盯盘' if eligible_first else
            '第二梯队：观察回踩机会' if score >= 45 and price_above_ma else
            '第三梯队：条件未成熟')
    candles=[]
    for _, r in df.tail(90).iterrows():
        candles.append({'date':str(r.date), **{k:number(r[k]) for k in
                 ['open','high','low','close','volume','ma5','ma10','ma20','dif','dea','macd']}})
    return {'code':code,'name':name,'sector':sector,'asof':asof,'provider':provider,
            'bar_status':bar_status,
            'bar_status_label':('临时行情' if provisional else '正式收盘数据（公开日线）'),
            'close_confirmation':confirmation_reason,
            'quote_time_beijing':quote_time,
            'close':number(close),'ma20':number(ma20),'ma20_deviation_pct':number(dev),
            'ma20_slope_5d_pct':number(ma20_slope),'volume_ratio_5d':number(volume_ratio),
            'score':score,'tier':tier,'buy_trigger':entry,'stop_reference':stop,
            'stop_distance_pct':number(risk_pct),'risk_ok':risk_ok,'trade_eligible':eligible_first,
            'flow_positive_days':positives,'flow_net_sum_3d_yuan':number(flow_total,2),
            'flow_avg_net_pct_3d':number(flow_mean_pct),'flow_strong':flow_strength,
            'fund_flow_3d':aligned,'fund_flow_published_recent':published,
            'fund_flow_missing_dates':[x['date'] for x in aligned if x['status']!='available'],
            'data_diagnostics':diagnostics or {},'fund_flow_status':('available' if flow_complete else
                             'amount_only' if flow_amount_complete else 'unavailable'),
            'signal_blockers':blockers,'candles':candles}


def main():
    now = datetime.now(TZ)
    data = {'updated_at_beijing':now.isoformat(), 'status':'ok',
            'model_version':'v2.4-dynamic-rotation','provider':'AKShare Eastmoney / Tencent / Sina (unadjusted); timestamped Tencent quote confirmation',
            'universe':'固定12只候选股票，非全市场动态轮动',
            'strategy_rules':{'max_stop_distance_pct':MAX_STOP_PCT,
                              'max_ma20_deviation_pct':MAX_MA20_DEVIATION_PCT,
                              'fund_flow':'必须与最近3根日K线日期一致，至少2日流入、3日净额为正且日均净占比>=0.5%',
                              'note':'收盘后日线须核对完整OHLCV及15:00后快照；临时行情、过期数据及缺失资金禁止第一梯队'},
            'disclaimer':'研究信号与条件参考价，不构成投资建议；不得按本工具自动下单。',
            'stocks':[], 'errors':[]}
    previous = {}
    if OUT.exists():
        try:
            previous = json.loads(OUT.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            pass
    discovery_ok = False
    universe = dict(UNIVERSE)
    try:
        discovered, screened_count = discover()
        universe = discovered
        discovery_ok = True
        data['universe'] = '全A股实时列表筛选后的候选股票（非全市场完整逐只技术评分）'
        data['discovery_screened_count'] = screened_count
    except Exception as exc:
        data['discovery_error'] = str(exc)[:180]
        data['universe'] = '全市场发现失败：使用固定观察池降级，禁止声称全市场动态选股'
    for code, (name, sector) in universe.items():
        try:
            df,provider,provisional,quote_time,bar_status,reason,diag = history(code)
            data['stocks'].append(calculate(code,name,sector,df,provider,money_flow(code),
                                            provisional,quote_time,bar_status,reason,diag))
            print('OK', code, flush=True)
        except Exception as exc:
            data['errors'].append({'code':code,'error':str(exc)[:180]})
            print('ERROR',code,str(exc)[:180],flush=True)
        time.sleep(0.2)
    if not data['stocks']:
        raise RuntimeError('所有行情源失败，保留上次已发布的有效数据')
    # Too few verified candidates is an incomplete scan, not evidence of rotation.
    if discovery_ok and len(data['stocks']) < 8:
        raise RuntimeError('动态扫描有效股票不足8只，停止发布，保留旧数据')

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
    selected, event = rank_and_rotate(data['stocks'], previous, now.date().isoformat(), discovery_ok)
    if not discovery_ok:
        data['rotation_warning'] = '动态发现失败：未发生有效的新增/淘汰判定'
    data['stocks'] = selected
    data['rotation'] = event
    data['rotation_method'] = '全市场成交额初筛最多45只；日线评分前10；5分缓冲避免频繁换股'
    if data['errors'] or not discovery_ok:
        data['status']='partial'
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data,ensure_ascii=False,separators=(',',':')), encoding='utf-8')
    if discovery_ok:
        persist_history(event)
    print('Wrote', OUT, 'stocks',len(data['stocks']), 'latest',freshest)


if __name__ == '__main__':
    main()
