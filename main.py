"""A-share rotation model v2: evidence-gated, risk-controlled research signals.

No fictional quotes or money-flow. Keep JSON output backward-compatible with v1 UI.
"""
import json
import re
import time
import requests
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
