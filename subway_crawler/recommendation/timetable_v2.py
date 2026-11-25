"""One published release per line, explicit exclusions, and time-continuous trip facts."""
import json
from pathlib import Path
import pandas as pd
from ..pipeline import now
from .analysis import clock_seconds, name_key

CONFIG=Path(__file__).resolve().parents[2]/'config'

def settings():return json.loads((CONFIG/'seat_evidence_v2.json').read_text())

def clock_text(seconds):
    if seconds is None or pd.isna(seconds):return None
    seconds=int(seconds);return f'{seconds//3600:02}:{seconds%3600//60:02}:{seconds%60:02}'

def continuous_times(rows):
    """Only a >12h reversal advances the service-day clock; small reversals remain observable."""
    last=None;offset=0;result=[]
    for n,r in enumerate(rows):
        times=[]
        for field in ('arrival_time','departure_time'):
            raw=clock_seconds(r.get(field))
            if raw is None:times.append(None);continue
            if raw==0 and ((n==0 and field=='arrival_time') or (n==len(rows)-1 and field=='departure_time')):
                times.append(None);continue
            value=raw+(offset if raw<86400 else 0)
            if last is not None and value-last < -43200:
                offset+=86400;value+=86400
            times.append(value);last=value
        result.append(times)
    return result

def infer_trip_direction(g):
    line=g.iloc[0]['line'];pairs=[]
    mapped=g[g.station_key.notna()]
    for _,subset in mapped.groupby(mapped.station_key.str.split(':').str[:2].str.join(':')):
        codes=pd.to_numeric(subset.station_key.str.split(':').str[-1],errors='coerce').dropna()
        for a,b in zip(codes,codes.iloc[1:]):
            diff=b-a
            if line=='2호선' and 201<=a<=243 and 201<=b<=243:
                if diff>21:diff-=43
                if diff<-21:diff+=43
            if diff:pairs.append(1 if diff>0 else -1)
    if not pairs:return None
    sign=pd.Series(pairs).median()
    if sign==0:return None
    return ('내선' if sign>0 else '외선') if line=='2호선' else ('하선' if sign>0 else '상선')

def canonical_timetable(b,d,sid):
    cfg=settings();policy=cfg['canonical_timetable'];d=d.copy()
    aliases=b.mapper.alias_config['aliases']
    for col in ('station_name','origin_name','destination_name'):
        d['source_'+col]=d[col]
        d[col]=[aliases.get(line,{}).get(str(v),v) for line,v in zip(d.line,d[col])]
    d['version_id']=d.line+'|'+d.reference_date.fillna('unknown')
    # A source line code is not sufficient proof of geography: the KRIC I4101 sheet contains Daegyeong trips.
    nonmetro_trips=set(d.loc[d.station_name.isin(policy['excluded_nonmetro_stations']),'trip_key'])
    priority=d.source_line_code.map(policy['source_priority']).fillna(1)
    d['_priority']=priority
    chosen={};chosen_priority={}
    for line,g in d.groupby('line'):
        chosen_priority[line]=g._priority.min()
        candidate=g[g._priority.eq(chosen_priority[line])]
        chosen[line]=candidate.reference_date.dropna().max() if candidate.reference_date.notna().any() else None
    d['superseded_by_version']=pd.NA;d['exclusion_reason']=pd.NA
    obsolete=d.reference_date.fillna('unknown').ne(d.line.map(chosen).fillna('unknown')) | d._priority.ne(d.line.map(chosen_priority))
    d.loc[obsolete,'superseded_by_version']=d.loc[obsolete,'line']+'|'+d.loc[obsolete,'line'].map(chosen)
    d.loc[obsolete,'exclusion_reason']='superseded_by_version'
    d.loc[d.trip_key.isin(nonmetro_trips),'exclusion_reason']='non_metropolitan_trip'
    d=d.drop(columns='_priority')
    excluded=d[d.exclusion_reason.notna()].copy()
    b.save('timetable_excluded',excluded,sid,'공표 정차행·원본 버전','공표 계획',[],
           '추천 제외 버전/비수도권 운행. 원본 및 버전/사유 보존.', ['trip_key','stop_sequence'],csv=False)
    d=d[d.exclusion_reason.isna()].copy()
    d=b.mapper.attach(d,code='absent_code')
    d['mapping_reason']=d.mapping_status.map({'matched':None,'unmatched':'no_exact_scoped_reference_or_unreviewed_abbreviation','ambiguous':'multiple_operator_or_station_candidates'})
    d['effective_date']=pd.NA;d['effective_date_source']='unknown'
    asof=getattr(b,'as_of',now()[:10]);d['stale']=(pd.Timestamp(asof)-pd.to_datetime(d.reference_date,errors='coerce')).dt.days.ge(policy['stale_days'])
    d['stale_as_of']=asof
    parts=[];edges=[]
    for trip,g in d.groupby('trip_key',sort=False):
        g=g.sort_values('stop_sequence').copy();records=g.to_dict('records');normalized=continuous_times(records)
        arrivals=[v[0] for v in normalized];departures=[v[1] for v in normalized]
        g['arrival_time']=[clock_text(v) for v in arrivals];g['departure_time']=[clock_text(v) for v in departures]
        g['arrival_seconds']=arrivals;g['departure_seconds']=departures
        origin=g.iloc[0].origin_name;dest=g.iloc[0].destination_name
        g['is_origin_stop']=[i==0 and name_key(r['station_name'])==name_key(origin) for i,r in enumerate(records)]
        terminals=policy['normal_terminals'].get(g.iloc[0].line)
        short=name_key(dest) not in {name_key(t) for t in terminals} if terminals else pd.NA
        g['is_short_turn']=short
        start=departures[0] if bool(g.iloc[0].is_origin_stop) else None
        g['minutes_from_origin']=[(v-start)/60 if start is not None and v is not None and v>=start else None for v in departures]
        g['direction']=infer_trip_direction(g);g['direction_path']=origin+' → '+dest
        parts.append(g)
        records=g.to_dict('records')
        for a,z in zip(records,records[1:]):
            dep=a['departure_seconds'];arr=z['arrival_seconds']
            diff=arr-dep if pd.notna(dep) and pd.notna(arr) else None
            duration=diff if diff is not None and 0<=diff<=7200 else None
            reason='station_mapping_missing' if pd.isna(a['station_key']) or pd.isna(z['station_key']) else ('time_missing' if diff is None else 'negative_travel_time' if diff<0 else 'travel_over_2h' if diff>7200 else None)
            edge={k:a[k] for k in ['trip_key','train_no','line','source_line_code','day_type','service_type','origin_name','destination_name','direction','direction_path','version_id','reference_date','effective_date','effective_date_source','stale','stale_as_of','stop_sequence','station_key','station_name','departure_time','departure_seconds','is_origin_stop','is_short_turn','minutes_from_origin']}
            edges.append(edge|{'next_station_key':z['station_key'],'next_station_name':z['station_name'],'arrival_time':z['arrival_time'],'arrival_seconds':arr,'raw_travel_seconds':diff,
                'travel_seconds':duration,'quality_valid':reason is None,'invalid_reason':reason,'source_row':a['source_row'],'next_source_row':z['source_row']})
    d=pd.concat(parts,ignore_index=True);e=pd.DataFrame(edges)
    b.save('timetable_stops',d,sid,'노선별 canonical 발행본·운행·정차순서','공표 계획',['route'],
        'reference_date는 시행일 아님. 명시적 별칭; HH:MM:SS, 큰 자정 역전만 익일. stale은 기준일 365일 경과.', ['trip_key','stop_sequence'])
    b.save('route_features',e,sid,'canonical 운행의 인접 정차역 구간','공표 계획에서 계산',['route'],
        'is_origin_stop은 운행 시발 표시이며 빈 좌석 증거 아님. is_short_turn은 설정 종착역 외 종착 계획이며 동일 차량 회차 확정 아님.', ['trip_key','stop_sequence'])
    b.quality[sid].update(canonical_dates=chosen,excluded_rows=len(excluded),non_metropolitan_rows=int(excluded.exclusion_reason.eq('non_metropolitan_trip').sum()),
        unusable_edges=int((~e.quality_valid).sum()),invalid_reasons=e.invalid_reason.value_counts().to_dict(),stale_stops=int(d.stale.sum()),effective_date_unknown=int(d.effective_date.isna().sum()))
    l1=e[e.line.eq('1호선')&e.raw_travel_seconds.notna()]
    b.quality[sid]['line1_negative_duration_ratio']=float(l1.raw_travel_seconds.lt(0).mean()) if len(l1) else None
    b.quality[sid]['line4_weekday_hyehwa_stops']=int((d.line.eq('4호선')&d.day_type.eq('평일')&d.station_name.eq('혜화')).sum())
    wanted=e[e.line.eq('2호선')&e.day_type.eq('평일')&e.station_name.eq('신도림')&e.next_station_name.eq('대림')&e.departure_seconds.ge(7*3600)&e.departure_seconds.le(9*3600)]
    b.quality[sid]['line2_sindorim_daerim_07_09']={'departures':len(wanted),'origin_departures':int(wanted.is_origin_stop.sum()),'interval':'07:00 <= departure <= 09:00','half_open_departures':int(wanted.departure_seconds.lt(9*3600).sum())}
    key=['line','day_type','station_key','departure_time','direction']
    b.quality[sid]['cross_version_duplicate_combinations']=int(d[d.station_key.notna()&d.departure_time.notna()].groupby(key,dropna=False).version_id.nunique().gt(1).sum())
