"""Matched-month weekday-share ratios. Cross-line transfer remains explicitly estimated."""
import pandas as pd


def build_calibration(b):
    if 'ridership_hourly' not in b.datasets:return
    p=b.out/'datasets'
    months=[name for name in b.datasets if name.startswith('calibration_') and name.endswith('_monthly_alighting_features')]
    if not months:
        b.quality['monthly_calibration']={'status':'unavailable','reason':'no matching-period monthly data'};return
    daily=pd.read_parquet(p/'ridership_hourly.parquet')
    d=daily[daily.quality_valid & daily.mapping_status.eq('matched') & daily.flow.eq('하차') & daily.weekday.lt(5)&daily.is_holiday.eq(False)].copy()
    d['period']=d.service_date.str[:7]
    d['hour']=d.time_bin.str.extract(r'^(\d{2})-\d{2}시간대$')[0]
    totals=d.groupby(['period','station_key']).passengers.transform('sum')
    d['weekday_total']=totals
    dg=d.groupby(['period','station_key','hour'],dropna=False).agg(weekday_hour_passengers=('passengers','sum'),weekday_total=('weekday_total','first'),sample_days=('service_date','nunique')).reset_index()
    dg=dg[dg.hour.notna()];dg['time_bin']=dg.hour+':00';dg['weekday_share']=dg.weekday_hour_passengers/dg.weekday_total.where(dg.weekday_total.gt(0))
    m=pd.concat([pd.read_parquet(p/(n+'.parquet')) for n in months],ignore_index=True)
    m['time_bin']=m.time_bin.str[:2]+':00'
    keys=['period','station_key','time_bin'];m=m[m.quality_valid & m.within_station_month_alighting_share.gt(0)]
    calibration=dg.merge(m[keys+['within_station_month_alighting_share','raw_sha256']],on=keys,validate='one_to_one')
    if calibration.empty:raise ValueError('no matched station/month/hour pairs; inspect source time-bin schema')
    calibration=calibration.rename(columns={'raw_sha256':'monthly_raw_sha256','within_station_month_alighting_share':'monthly_share'})
    calibration['weekday_correction_factor']=calibration.weekday_share/calibration.monthly_share
    calibration['holiday_policy']='exclude_holidays';calibration['estimate_kind']='matched_period_weekday_share_ratio'
    b.save('monthly_weekday_calibration',calibration,'metro_ridership_types','동일 역·월·시간대의 평일 하차 비중 / 월간 하차 비중','집계 통계에서 계산',['alighting_prior'],
        '일별 자료가 있는 역에서 동일 월만 비교. 06~23시 개별 시간대만 계수 산출. 과거 계수를 타 노선에 적용한 값은 추정이며 요일 통계가 아님.',keys)
    summary=calibration.groupby('time_bin').agg(median_factor=('weekday_correction_factor','median'),station_month_samples=('station_key','size'),station_count=('station_key','nunique')).reset_index()
    b.save('weekday_correction_factors',summary,'metro_ridership_types','시간대별 역·월 보정계수 중앙값','집계 통계에서 계산',['alighting_prior'],
        '표본 기관의 중앙 계수; 미관측 노선의 실제 평일 통계나 검증된 예측 아님. 확장 노선 적용은 별도 estimated 데이터셋에만 보존.', ['time_bin'])
    morning=calibration[calibration.time_bin.eq('08:00')]
    b.quality['monthly_calibration']={'status':'matched_period_computed','station_count':calibration.station_key.nunique(),'station_month_rows':len(calibration),
        'periods':sorted(calibration.period.unique()),'08_factor_median':float(morning.weekday_correction_factor.median()),
        '08_share_pearson':float(morning.weekday_share.corr(morning.monthly_share)) if len(morning)>1 and morning.weekday_share.std()>0 and morning.monthly_share.std()>0 else None,'extension_application':'estimated_weights_only; no actual weekday counts generated'}

    if 'monthly_alighting_features' in b.datasets:
        extension=pd.read_parquet(p/'monthly_alighting_features.parquet')
        observed=set(daily.loc[daily.mapping_status.eq('matched'),'station_key'].dropna())
        extension=extension[~extension.station_key.isin(observed)].copy()
        extension['calibration_time_bin']=extension.time_bin.str[:2]+':00'
        extension=extension.merge(summary.rename(columns={'time_bin':'calibration_time_bin'}),on='calibration_time_bin',how='left',validate='many_to_one')
        extension['estimated_weekday_share_weight']=extension.within_station_month_alighting_share*extension.median_factor
        extension['estimate_kind']='cross_line_transfer_estimate_not_weekday_observation'
        extension['calibration_periods']='|'.join(sorted(calibration.period.unique()))
        extension['calibration_daily_raw_sha256']=b.states['metro_ridership_types']['receipt']['sha256']
        extension['calibration_monthly_raw_sha256']='|'.join(sorted(calibration.monthly_raw_sha256.unique()))
        b.save('monthly_weekday_adjusted_estimates',extension,'seoul_monthly_hourly','확장노선 역·월·시간대 보정 가중치','보정 추정',['alighting_prior'],
            '원래 passengers/share 보존. 타 노선에서 전이한 계수, 검증된 요일 통계 아님. 06~23시 외 계수 미제공. 가중치 합계는 확률분포로 정규화하지 않음.', ['period','source_line_name','station_name','time_bin','flow'])
