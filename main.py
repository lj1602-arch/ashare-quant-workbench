"""A-share daily rotation research; no simulated market data."""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import akshare as ak
import numpy as np
import pandas as pd

TZ = ZoneInfo('Asia/Shanghai')
OUT = Path('docs/data.json')
# Curated liquid-sector universe. Expand only after provider reliability is verified.
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

def number(x):
    try:
        f = float(x)
        return round(f, 4) if np.isfinite(f) else None
    except (ValueError, TypeError):
        return None


def history(code):
    start = '20250101'
    end = datetime.now(TZ).strftime('%Y%m%d')

    try:
        # 第一数据源：东方财富
        df = ak.stock_zh_a_hist(
            symbol=code,
            period='daily',
            start_date=start,
            end_date=end,
            adjust='qfq'
        )
        if df is None or df.empty:
            raise ValueError('东方财富返回空数据')
        print('SOURCE eastmoney', code, flush=True)

    except Exception as exc:
        print('EASTMONEY FAILED', code, str(exc), flush=True)

        # 第二数据源：腾讯证券
        prefix = 'sh' if code.startswith('6') else 'sz'
        df = ak.stock_zh_a_hist_tx(
            symbol=prefix + code,
            start_date=start,
            end_date=end,
            adjust='qfq'
        )
        if df is None or df.empty:
            raise ValueError('腾讯证券返回空数据')
        print('SOURCE tencent', code, flush=True)

    mapping = {
        '日期': 'date',
        '开盘': 'open',
        '收盘': 'close',
        '最高': 'high',
        '最低': 'low',
        '成交量': 'volume',
        '成交额': 'amount'
    }
    df = df.rename(columns=mapping)

    # 腾讯接口部分版本将成交量放在 amount 列
    # 只将其用于同源相对量能比较，不视为成交额
    if 'volume' not in df.columns and 'amount' in df.columns:
        df['volume'] = df['amount']
        df = df.drop(columns=['amount'])

    needed = ['date', 'open', 'high', 'low', 'close', 'volume']
    if not all(k in df.columns for k in needed):
        raise ValueError('行情缺少必要字段: ' + str(list(df.columns)))

    df = df.sort_values('date').drop_duplicates('date').reset_index(drop=True)

    for k in needed[1:]:
        df[k] = pd.to_numeric(df[k], errors='coerce')

    df = df.dropna(subset=needed[1:])

    if len(df) < 35:
        raise ValueError('有效日K线不足35根')

    return df


def money_flow(code):
    # AKShare provider may change schema or fail; never estimate missing flows.
    try:
        market = 'sh' if code.startswith('6') else 'sz' if code.startswith(('0','3')) else 'bj'
        d = ak.stock_individual_fund_flow(stock=code, market=market)
        if '日期' not in d.columns or '主力净流入-净额' not in d.columns: return []
        d = d.sort_values('日期').tail(3)
        return [{'date':str(row['日期']), 'net_yuan':number(row['主力净流入-净额'])}
                for _,row in d.iterrows()]
    except Exception:
        return []

def analyze(code, name, sector):
    d = history(code)
    for n in (5,10,20): d[f'ma{n}'] = d.close.rolling(n).mean()
    ema12 = d.close.ewm(span=12,adjust=False).mean()
    ema26 = d.close.ewm(span=26,adjust=False).mean()
    d['dif'] = ema12-ema26
    d['dea'] = d.dif.ewm(span=9,adjust=False).mean()
    d['macd'] = 2*(d.dif-d.dea)
    last = d.iloc[-1]
    prev = d.iloc[-2]
    close = float(last.close)
    ma20 = float(last.ma20)
    dev = (close/ma20-1)*100
    v5 = float(d.volume.tail(5).mean())
    vr = float(last.volume/v5) if v5 else 0
    flows = money_flow(code)
    pos_flow = sum(1 for x in flows if x['net_yuan'] is not None and x['net_yuan']>0)
    # Missing flow contributes no positive points; keep distinct from negative evidence.
    score = (30 if close>ma20 else 0) + (15 if last.ma5>last.ma10>last.ma20 else 0)
    score += (20 if last.dif>last.dea and last.dif>prev.dif else 0)
    score += (10 if vr>1.15 and close>prev.close else 0)
    score += (5*pos_flow if len(flows)==3 else 0)
    score += (10 if float(last.ma20)>float(d.iloc[-6].ma20) else 0)
    tier = '第一梯队：重点盯盘' if score>=65 else '第二梯队：观察回踩机会' if score>=40 else '第三梯队：条件未成熟'
    # Entry is a conditional trigger, not an executable guaranteed fill.
    recent_high = float(d.high.tail(10).max())
    recent_low = float(d.low.tail(10).min())
    entry = round(max(recent_high,close)*1.002,2) if tier.startswith('第一') else round(ma20*1.005,2)
    stop = round(min(float(d.low.tail(5).min()),ma20)*0.995,2)
    candles = []
    for _,r in d.tail(90).iterrows():
        candles.append({'date':str(r.date)[:10],**{k:number(r[k]) for k in ['open','high','low','close','volume','ma5','ma10','ma20','dif','dea','macd']}})
    return {'code':code,'name':name,'sector':sector,'asof':str(last.date)[:10],
            'close':number(close),'ma20':number(ma20),'ma20_deviation_pct':number(dev),
            'volume_ratio_5d':number(vr),'score':score,'tier':tier,
            'buy_trigger':entry,'stop_reference':stop,'fund_flow_3d':flows,
            'fund_flow_status':'available' if len(flows)==3 else 'unavailable',
            'candles':candles}

def main():
    now = datetime.now(TZ)
    data = {'updated_at_beijing':now.isoformat(),'status':'ok','provider':'AKShare public upstream (may be delayed)',
            'disclaimer':'Research signals only. Entries and stops are conditional reference prices; no automatic trading.',
            'stocks':[],'errors':[]}
    for code,(name,sector) in UNIVERSE.items():
        try:
            data['stocks'].append(analyze(code,name,sector))
            print('OK',code,flush=True)
        except Exception as exc:
            data['errors'].append({'code':code,'error':str(exc)[:180]})
            print('ERROR',code,str(exc)[:180],flush=True)
        time.sleep(0.6)
    data['stocks'].sort(key=lambda x:x['score'],reverse=True)
    if not data['stocks']:
        raise RuntimeError('All providers failed; do not overwrite last known good data')
    if data['errors']: data['status']='partial'
    OUT.parent.mkdir(parents=True,exist_ok=True)
    OUT.write_text(json.dumps(data,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    print('Wrote',OUT,'stocks',len(data['stocks']))

if __name__=='__main__': main()
