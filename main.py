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
            'fund_flow_3d':aligned,'fund_flow_status':('available' if flow_complete else
                             'amount_only' if flow_amount_complete else 'unavailable'),
            'signal_blockers':blockers,'candles':candles}


def main():
    now = datetime.now(TZ)
    data = {'updated_at_beijing':now.isoformat(), 'status':'ok',
            'model_version':'v2.2-close-confirmed','provider':'AKShare Eastmoney / Tencent / Sina (unadjusted); timestamped Tencent quote confirmation',
            'universe':'固定12只候选股票，非全市场动态轮动',
            'strategy_rules':{'max_stop_distance_pct':MAX_STOP_PCT,
                              'max_ma20_deviation_pct':MAX_MA20_DEVIATION_PCT,
                              'fund_flow':'必须与最近3根日K线日期一致，至少2日流入、3日净额为正且日均净占比>=0.5%',
                              'note':'收盘后日线须核对完整OHLCV及15:00后快照；临时行情、过期数据及缺失资金禁止第一梯队'},
            'disclaimer':'研究信号与条件参考价，不构成投资建议；不得按本工具自动下单。',
            'stocks':[], 'errors':[]}
    for code, (name, sector) in UNIVERSE.items():
        try:
            df,provider,provisional,quote_time,bar_status,reason = history(code)
            data['stocks'].append(calculate(code,name,sector,df,provider,money_flow(code),
                                            provisional,quote_time,bar_status,reason))
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
