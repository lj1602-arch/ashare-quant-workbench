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
