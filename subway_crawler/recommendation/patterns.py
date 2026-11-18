"""Daily holiday policies and reproducible mean/median diagnostics."""
import pandas as pd


def daily_patterns(b, long, sid, prefix):
    valid=long[long.quality_valid & long.mapping_status.eq('matched')].copy()
    group=['station_key','station_name','line','weekday','time_bin','flow']
    results={};default_agg=None
    for policy, frame in [('exclude_holidays',valid[valid.is_holiday.eq(False)]),('include_holidays',valid)]:
        agg=frame.groupby(group,dropna=False).agg(mean_passengers=('passengers','mean'),median_passengers=('passengers','median'),
            p90_passengers=('passengers',lambda s:s.quantile(.9)),sample_days=('service_date','nunique'),
            observation_start=('service_date','min'),observation_end=('service_date','max')).reset_index()
        agg['direction']=pd.NA;agg['is_holiday']=False if policy=='exclude_holidays' else pd.NA
        agg['holiday_policy']=policy
        agg['sample_check']=agg.sample_days.map(lambda n:'under_8_days' if n<8 else 'at_least_8_days_not_validation')
        suffix='' if policy=='exclude_holidays' else '_including_holidays'
        keys=group+['observation_start','observation_end','holiday_policy']
        b.save(prefix+'station_weekday_patterns'+suffix,agg,sid,'역·요일·시간대·공휴일 정책','집계 통계',['alighting','route'],
            '일별 관측 기반. 일반 토/일과 특정 공휴일을 구분. 평균·중앙값은 실제 관측일에서 계산.',keys)
        b.save(prefix+'alighting_features'+suffix,agg[agg.flow.eq('하차')],sid,'역·요일·시간대·공휴일 정책','집계 통계',['alighting'],
            '개찰구 하차 수요. 열차 하차율·좌석 확률 아님. direction 미상.',keys)
        if policy=='exclude_holidays':default_agg=agg
        sample=agg[agg.weekday.eq(0)&agg.time_bin.isin(['08-09시간대','08시간대','08시-09시'])&agg.flow.eq('하차')&agg.median_passengers.gt(0)]
        results[policy]={'statistic':'median of station mean/median ratios, Monday 08-09 alighting','station_samples':len(sample),
                        'ratio':float((sample.mean_passengers/sample.median_passengers).median()) if len(sample) else None}
    b.quality[sid]['holiday_comparison']=results
    b.quality[sid]['holiday_exclusion_target_met']=results['exclude_holidays']['ratio'] is not None and results['exclude_holidays']['ratio']>=.95
    if sid=='metro_ridership_types':
        b.context['top_alighting']=default_agg[default_agg.flow.eq('하차')].sort_values('mean_passengers',ascending=False).head(15).to_dict('records')
        b.context['hourly']=valid[valid.is_holiday.eq(False)].groupby(['time_bin','flow']).passengers.mean().unstack().reset_index().to_dict('records')
