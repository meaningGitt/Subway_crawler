"""Monthly totals stay monthly; no fabricated daily, weekday, direction, or seat probability."""
import re
import pandas as pd

from ..source import SourceError
from .analysis import read_csv

# Fare-statistics route labels -> passenger-facing line. Keep source label in every natural key.
# Matching still requires a unique station within the line/operator; unknown routes fail closed.
ROUTES = {
    **{f'{n}호선': (f'{n}호선', None) for n in range(1, 10)},
    '9호선2~3단계': ('9호선', 'S9'),
    '경부선': ('1호선', 'KR'), '경인선': ('1호선', 'KR'),
    '경원선': (('1호선','경의중앙선'), 'KR'), '장항선': ('1호선', 'KR'),
    '일산선': ('3호선', 'KR'), '과천선': ('4호선', 'KR'), '안산선': ('4호선', 'KR'),
    '경강선': ('경강선', 'KR'), '경춘선': ('경춘선', 'KR'),
    '경의선': ('경의중앙선', 'KR'), '중앙선': ('경의중앙선', 'KR'),
    '분당선': ('수인분당선', 'KR'), '수인선': ('수인분당선', 'KR'),
    '서해선': ('서해선', None), '공항철도 1호선': ('공항철도', 'AR'),
    '우이신설선': ('우이신설선', 'UI'), '신림선': ('신림선', 'SL'),
}

# Explicit label differences visible in the KRIC reference, not general suffix/fuzzy matching.
NAME_ALIASES = {('5호선','하남검단산'):'하남검단산역', ('7호선','이수'):'총신대입구(이수)',
                ('서해선','김포공항'):'김포공항역', ('서해선','부천종합운동장'):'부천종합운동장역'}


def build_monthly(b, path, sid):
    prefix=f'calibration_{b.sources[sid]["collection_period"]}_' if b.sources[sid].get('collection_period') else ''
    d = read_csv(path).rename(columns={'사용월':'period','호선명':'source_line_name','지하철역':'station_name','작업일자':'provider_processed_date'})
    keys = ['period', 'source_line_name', 'station_name']
    bins = {c: re.fullmatch(r'(\d{2}시-\d{2}시) (승차|하차)인원', c) for c in d}
    bins = {c:m for c,m in bins.items() if m}
    if len(bins) != 48 or not set(keys).issubset(d):
        raise SourceError('monthly hourly schema changed: expected 24 hours x 2 flows')
    d['source_row'] = d.index+2
    raw_period = d.period.copy()
    expected = b.states[sid].get('metadata',{}).get('period')
    if expected and not raw_period.eq(expected).all():
        raise SourceError('CSV month does not match the requested published month')
    d['period'] = pd.to_datetime(raw_period,format='%Y%m',errors='coerce').dt.strftime('%Y-%m')
    if d.period.isna().any():
        raise SourceError('invalid monthly period')
    d,q = b.clean(d,keys,keys)
    mappings = []
    for row in d.to_dict('records'):
        route = ROUTES.get(row['source_line_name'])
        if route:
            line, operator = route
            match_name = NAME_ALIASES.get((row['source_line_name'],row['station_name']),row['station_name'])
            choices = line if isinstance(line,tuple) else (line,)
            matches = [(candidate,*b.mapper.match(match_name,candidate,operator)) for candidate in choices]
            resolved = [m for m in matches if m[2]=='matched']
            if len(resolved)==1:
                line,station,status = resolved[0]
            else:
                line,station,status = (choices[0] if len(choices)==1 else None),None,('ambiguous' if len(resolved)>1 or any(m[2]=='ambiguous' for m in matches) else 'unmatched')
        else:
            line,station,status = None,None,'unknown_line'
        mappings.append((line,station,status))
    d['line'],d['station_key'],d['mapping_status'] = zip(*mappings)
    d['direction'] = pd.NA; d['weekday'] = pd.NA; d['is_holiday'] = pd.NA
    caution = '월간 시간대별 개찰구 합계. 요일·공휴일·방향·열차 미제공. 일별 평균/중앙값/표본 일수 산출 불가. 기존 일별 자료와 합산 금지.'
    b.save(prefix+'monthly_ridership_wide',d,sid,'월·원본 노선·역·시간대별 승하차 열','집계 통계',['alighting','route'],caution,keys,csv=False)
    ids = [c for c in d if c not in bins]
    long = d.melt(id_vars=ids,value_vars=list(bins),var_name='source_time_column',value_name='passengers')
    long['time_bin'] = long.source_time_column.map(lambda c: bins[c][1])
    long['flow'] = long.source_time_column.map(lambda c: bins[c][2])
    long['passengers'] = pd.to_numeric(long.passengers,errors='coerce')
    long['quality_valid'] &= long.passengers.notna() & long.passengers.ge(0)
    long['raw_passengers']=long.passengers
    totals=long.groupby(keys).passengers.transform(lambda x:x.sum(min_count=48))
    zero=totals.eq(0)
    long['missing_reason']=pd.NA
    long.loc[zero,'missing_reason']='not_provided_zero_month_total'
    long.loc[zero,'passengers']=float('nan')
    long.loc[zero,'quality_valid']=False
    q['zero_month_groups']=long.loc[zero,keys].drop_duplicates().to_dict('records')
    q['zero_semantics']='원본 0은 raw_passengers와 wide 원본에 보존. 타선 합산은 공식 역별 근거 미확보로 단정하지 않음.'
    q['invalid_cells'] = int((~long.quality_valid).sum())
    q['unknown_source_lines'] = sorted(d.loc[d.mapping_status.eq('unknown_line'),'source_line_name'].unique())
    q['label_aliases'] = [{'source_line':line,'source_name':name,'reference_name':ref} for (line,name),ref in NAME_ALIASES.items()]
    b.quality[sid] = q
    natural = keys+['time_bin','flow']
    b.save(prefix+'monthly_ridership_hourly',long,sid,'월·원본 노선·역·시간대·승하차','집계 통계',['alighting','route'],caution,natural)
    eligible = long[long.mapping_status.eq('matched') & long.flow.eq('하차')].copy()
    # Denominator only complete 24-hour records; this share is temporal demand, never seat probability.
    grouped = eligible.groupby(keys,dropna=False)
    eligible['observed_hour_bins'] = grouped.passengers.transform('count')
    total = grouped.passengers.transform('sum')
    eligible['within_station_month_alighting_share'] = (eligible.passengers/total).where(eligible.observed_hour_bins.eq(24) & total.gt(0))
    b.save(prefix+'monthly_alighting_features',eligible,sid,'월·원본 노선·역·시간대 하차','집계 통계',['alighting'],
           caution+' 시간대 비중은 해당 역 월간 하차량 내 비중이며 좌석 확률이 아님.',natural)
    b.context[prefix+'monthly_scope'] = d.groupby('source_line_name').agg(station_rows=('station_name','size'),mapped_rows=('station_key','count')).reset_index().to_dict('records')
    b.context[prefix+'monthly_periods'] = sorted(d.period.unique())
