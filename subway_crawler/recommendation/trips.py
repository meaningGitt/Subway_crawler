"""Time-local congestion joins and boarding-station queries, never seat probabilities."""
import pandas as pd
from .timetable_v2 import settings, clock_seconds


def join_trip_stops(timetable,segment):
    t=timetable.sort_values(['trip_key','stop_sequence']).copy()
    t['next_station_key']=t.groupby('trip_key').station_key.shift(-1)
    t['time_bin']=t.departure_seconds.map(lambda x:f'{int(x//3600)%24:02}:{int(x%3600//1800)*30:02}' if pd.notna(x) else None)
    t['congestion_day_type']=t.day_type.where(t.day_type.isin(['평일','토요일','일요일']))
    # Ambiguous holiday templates must not be silently equated with Saturday/Sunday observations.
    s=segment.copy();keys=['line','station_key','next_station_key','direction','day_type','time_bin']
    s=s[s.mapping_status.eq('matched') & ~s.branch_id.eq('direction_placeholder')]
    unique=s.groupby(keys,dropna=False).size().rename('join_candidates').reset_index()
    s=s.merge(unique,on=keys);s=s[s.join_candidates.eq(1)]
    keep=keys+['congestion_pct','reference_date','no_service_flag','branch_id','raw_sha256']
    s=s[keep].rename(columns={'day_type':'congestion_day_type','reference_date':'congestion_reference_date','raw_sha256':'congestion_raw_sha256'})
    t=t.merge(s,on=['line','station_key','next_station_key','direction','congestion_day_type','time_bin'],how='left',validate='many_to_one')
    t['congestion_join_reason']=pd.NA
    t.loc[t.congestion_pct.isna(),'congestion_join_reason']='no_unique_station_direction_day_time_observation'
    t.loc[t.congestion_day_type.isna(),'congestion_join_reason']='ambiguous_holiday_service_template'
    t['reference_date_mismatch']=t.reference_date.ne(t.congestion_reference_date)&t.congestion_reference_date.notna()
    t['warning']=t.reference_date_mismatch.map(lambda v:'timetable_and_congestion_not_concurrent' if v else None)
    t['below_seat_line']=pd.Series(pd.NA,index=t.index,dtype='boolean')
    eligible=t.congestion_pct.notna() & t.no_service_flag.eq(False).fillna(False)
    t.loc[eligible,'below_seat_line']=t.loc[eligible,'congestion_pct'].le(settings()['seat_line']['threshold_pct'])
    return t


def query_trips(joined,query):
    t=joined[joined.line.eq(query['line']) & joined.day_type.eq(query['day_type'])].copy()
    lo=clock_seconds(query.get('from_time','00:00'));hi=clock_seconds(query.get('to_time','48:00'))
    if lo is None or hi is None or not 0<=lo<hi<=172800:raise ValueError('query time interval must be within 00:00–48:00 and increasing')
    records=[]
    for trip,g in t.groupby('trip_key',sort=False):
        g=g.sort_values('stop_sequence')
        boards=g[g.station_name.eq(query['boarding_station'])&g.departure_seconds.ge(lo)&g.departure_seconds.lt(hi)]
        if query.get('direction'):boards=boards[boards.direction.eq(query['direction'])]
        for _,board in boards.iterrows():
            remaining=g[g.stop_sequence.ge(board.stop_sequence)]
            target=remaining[remaining.station_name.eq(query['alighting_station'])&remaining.stop_sequence.gt(board.stop_sequence)]
            if target.empty:continue
            end=target.iloc[0];path=remaining[remaining.stop_sequence.lt(end.stop_sequence)]
            # The destination's outbound segment is not ridden and cannot supply a seat on this journey.
            hit=path[path.below_seat_line.eq(True)]
            first=hit.iloc[0] if len(hit) else None
            before=path if first is None else path[path.stop_sequence.le(first.stop_sequence)]
            complete=before.below_seat_line.notna().all()
            minutes=None
            if first is not None and pd.notna(first.minutes_from_origin) and pd.notna(board.minutes_from_origin):
                minutes=float(first.minutes_from_origin-board.minutes_from_origin)
            elif first is not None and pd.notna(first.departure_seconds):minutes=float((first.departure_seconds-board.departure_seconds)/60)
            records.append({'query_id':query['id'],'trip_key':trip,'line':board.line,'direction':board.direction,'origin':board.origin_name,
                'boarding_station':query['boarding_station'],'boarding_stop_sequence':int(board.stop_sequence),'alighting_station':query['alighting_station'],
                'boarding_departure_time':board.departure_time,'first_stop_below_seat_line':first.station_name if first is not None else None,
                'minutes_until_seat_line':minutes,'is_origin_trip_at_boarding':bool(board.is_origin_stop),
                'estimate_status':('threshold_observed' if complete else 'partial_path_first_observed_only') if first is not None else ('not_reached' if complete else 'insufficient_congestion_coverage'),
                'timetable_reference_date':board.reference_date,'congestion_reference_date':'|'.join(sorted(path.congestion_reference_date.dropna().unique())),
                'reference_date_mismatch':bool(path.reference_date_mismatch.any()),'missing_congestion_stops':int(path.below_seat_line.isna().sum()),
                'estimate_kind':'mean_congestion_threshold_not_seat_probability','seat_line_pct':settings()['seat_line']['threshold_pct'],
                'timetable_raw_sha256':board.raw_sha256,'congestion_raw_sha256':'|'.join(sorted(path.congestion_raw_sha256.dropna().unique()))})
    return pd.DataFrame(records)


def build_trip_features(b,queries=None):
    if not {'timetable_stops','segment_congestion'}<=b.datasets.keys():return
    p=b.out/'datasets';t=pd.read_parquet(p/'timetable_stops.parquet');s=pd.read_parquet(p/'segment_congestion.parquet')
    joined=join_trip_stops(t,s)
    b.save('trip_stop_congestion',joined,'kric_timetable','canonical 운행·정차역 출발 시각의 30분 혼잡도','평균 혼잡도 결합 추정',['route'],
        '각 정차역 출발시각에 결합. 일치하지 않는 기준일 경고; 불명확한 공휴일 템플릿/지선 복수 후보는 미결합.', ['trip_key','stop_sequence'],csv=False)
    outputs=[query_trips(joined,q) for q in (queries or settings()['trip_queries'])]
    result=pd.concat(outputs,ignore_index=True) if outputs else pd.DataFrame()
    if len(result):
        b.save('trip_seat_line_features',result,'kric_timetable','승차역 파라미터·운행·승차 정차순서','평균 혼잡도 기준 추정',['route'],
            '좌석 확률/착석 보장 아님. first_stop는 33.75% 이하 첫 관측 정차역, 누락이 앞에 있으면 최초 도달로 확정 불가. 시발 여부는 별도 표시.', ['query_id','trip_key','boarding_stop_sequence'])
    b.quality['trip_join']={'joined_stops':int(joined.congestion_pct.notna().sum()),'total_stops':len(joined),'query_rows':len(result),'status_counts':result.estimate_status.value_counts().to_dict() if len(result) else {},'date_mismatch_rows':int(joined.reference_date_mismatch.sum())}
    b.context['trip_examples']=result.head(20).to_dict('records') if len(result) else []
