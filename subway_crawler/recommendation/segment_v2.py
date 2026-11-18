"""Explicit branch topology. Zero is preserved; a service-boundary zero is not a seat signal."""
import re
import pandas as pd
from .analysis import read_csv
from ..source import SourceError
from .timetable_v2 import settings


def topology(frame):
    edges={};branches={};boundaries=set()
    for line,code,direction,previous,branch,boundary in settings()['segment_topology']:
        key=(line,code,direction);edges[key]=previous;branches[key]=branch
        if boundary:boundaries.add(key)
    return edges,branches,boundaries


def build_segment(b,path,sid):
    d=read_csv(path).rename(columns={'구분':'day_type','요일구분':'day_type','호선':'line','역번호':'station_code','역명':'station_name','출발역':'station_name','상하구분':'direction'})
    keys=['day_type','line','station_code','direction'];bins=[c for c in d if re.fullmatch(r'\d+시\d+분',c)]
    if not bins or not set(keys+['station_name']).issubset(d):raise SourceError('congestion schema changed')
    d['source_row']=d.index+2
    explicit={(r['line'],r['source_code']):r for r in b.mapper.alias_config['segment_codes']}
    unique={}
    for r in d[['line','station_code','station_name']].drop_duplicates().to_dict('records'):
        rule=explicit.get((r['line'],r['station_code']))
        code=rule['target_code'] if rule else r['station_code']
        name={'성수E':'성수','응암S':'응암'}.get(r['station_name'],r['station_name'])
        if rule and r['station_name']!=rule['source_name']:result=(None,'unmatched')
        else:result=b.mapper.match(name,r['line'],'S1',code)
        unique[(r['line'],r['station_code'],r['station_name'])]=result
    mapped=[unique[(r.line,r.station_code,r.station_name)] for r in d.itertuples()]
    d['station_key']=[r[0] for r in mapped];d['mapping_status']=[r[1] for r in mapped]
    d['mapping_reason']=d.mapping_status.map({'matched':None,'unmatched':'explicit_alias_or_reference_code_missing','ambiguous':'multiple_scoped_candidates'})
    graph,branches,boundaries=topology(d)
    nodes=list(zip(d.line,d.station_code,d.direction))
    d['branch_id']=[branches.get(n,'unknown_branch') for n in nodes]
    d['prev_source_station_code']=[graph.get(n) for n in nodes]
    node_keys={(r.line,r.station_code,r.direction):r.station_key for r in d.itertuples()}
    next_keys={}
    for node,previous in graph.items():
        if previous is not None and node in node_keys:
            parent=(node[0],previous,node[2]);next_keys.setdefault(parent,set()).add(node_keys[node])
    d['next_station_key']=[next(iter(next_keys[n])) if len(next_keys.get(n,set()))==1 else None for n in nodes]
    d['topology_reason']=['merged_branch_predecessor_unknown' if n==('5호선','2549','상선') else 'source_boundary' if graph.get(n) is None else None for n in nodes]
    d['direction_boundary']=[n in boundaries for n in nodes]
    cols=keys+['station_name','station_key','mapping_status','mapping_reason','branch_id','prev_source_station_code','next_station_key','topology_reason','direction_boundary','source_row']
    d=d[cols+bins].melt(id_vars=cols,value_vars=bins,var_name='source_time_bin',value_name='congestion_pct')
    d['time_bin']=d.source_time_bin.map(lambda c:f'{int(c.split("시")[0]):02}:{int(c.split("시")[1][:-1]):02}')
    d,q=b.clean(d,keys+['time_bin'],keys,'congestion_pct')
    d['reference_date']=b.reference_date(sid);d['observation_n']=pd.NA
    # A direction boundary is a zero-valued placeholder only when the source actually has zero.
    d['no_service_flag']=pd.Series(pd.NA,index=d.index,dtype='boolean')
    d['no_service_reason']=pd.NA
    d.loc[d.congestion_pct.gt(0),'no_service_flag']=False
    boundary=d.congestion_pct.eq(0)&d.direction_boundary
    d.loc[boundary,'no_service_flag']=True;d.loc[boundary,'no_service_reason']='configured_direction_or_source_boundary_zero'
    d.loc[d.congestion_pct.eq(0)&~boundary,'no_service_reason']='zero_requires_timetable_service_check'
    # Join by original code and direction; canonical keys alone collapse branch-specific measurements.
    lookup=d[['line','day_type','direction','time_bin','station_code','station_key','congestion_pct','no_service_flag']].rename(columns={'station_code':'prev_source_station_code','station_key':'prev_station_key','congestion_pct':'prev_congestion_pct','no_service_flag':'prev_no_service_flag'})
    drops=d.merge(lookup,on=['line','day_type','direction','time_bin','prev_source_station_code'],how='left',validate='many_to_one')
    eligible=drops.quality_valid & drops.station_key.notna() & drops.prev_station_key.notna() & drops.no_service_flag.eq(False).fillna(False)&drops.prev_no_service_flag.eq(False).fillna(False)
    drops['congestion_drop_pct']=(drops.prev_congestion_pct-drops.congestion_pct).where(eligible)
    threshold=settings()['seat_line']['threshold_pct']
    drops['below_seat_line']=pd.Series(pd.NA,index=drops.index,dtype='boolean')
    drops.loc[drops.no_service_flag.eq(False).fillna(False)&drops.quality_valid,'below_seat_line']=drops.congestion_pct.le(threshold)
    drops['seat_line_pct']=threshold;drops['estimate_kind']='mean_congestion_threshold_not_seat_probability'
    b.save('segment_congestion',d,sid,'역·원본 방향/지선·요일·30분 구간','집계 통계',['alighting_prior','route'],
        '원본 0 보존. no_service_flag 미상은 0만으로 운행 중단을 단정할 수 없음. 경계의 0은 착석 기준선 판정 제외.',keys+['time_bin','reference_date'])
    natural=['line','station_key','direction','branch_id','day_type','time_bin','station_code','reference_date']
    b.save('segment_drop_features',drops,sid,'역·방향·지선·요일·30분 구간','평균 혼잡도 기준 추정',['alighting_prior','route'],
        '이전 출발구간 평균−현재 출발구간 평균(%p). 양수는 밀집도 감소, 승객 하차수/좌석 확률 아님. 지선 구분 필수. 33.75는 사용자 지정 54/160 가정.',natural)
    q.update(unmatched_rows=int(d.mapping_status.ne('matched').sum()),unmatched=d.loc[d.mapping_status.ne('matched'),['line','station_code','station_name','mapping_reason']].drop_duplicates().to_dict('records'),zero_rows=int(d.congestion_pct.eq(0).sum()),no_service_true=int(d.no_service_flag.eq(True).sum()))
    checks=[]
    for line,code,direction in settings()['terminal_zero_checks']:
        subset=d[d.line.eq(line)&d.station_code.eq(code)&d.direction.eq(direction)]
        checks.append({'line':line,'code':code,'direction':direction,'rows':len(subset),'all_zero':bool(len(subset) and subset.congestion_pct.eq(0).all())})
    q['direction_terminal_checks']=checks;b.quality[sid]=q
    b.context['segment_top']=d[d.quality_valid].sort_values('congestion_pct',ascending=False).head(10).to_dict('records')


def update_service_flags(b):
    if not {'timetable_stops','segment_congestion','segment_drop_features'}<=b.datasets.keys():return
    p=b.out/'datasets';t=pd.read_parquet(p/'timetable_stops.parquet');s=pd.read_parquet(p/'segment_congestion.parquet')
    keys=['line','station_key','direction','day_type']
    t=t[t.station_key.notna() & t.departure_seconds.notna() & ~t.stale & t.day_type.isin(['평일','토요일','일요일'])]
    bounds=t.groupby(keys).departure_seconds.agg(first_departure='min',last_departure='max').reset_index()
    s=s.merge(bounds,on=keys,how='left',validate='many_to_one')
    sec=s.time_bin.str[:2].astype(int)*3600+s.time_bin.str[3:].astype(int)*60
    sec=sec.where(sec.ge(4*3600),sec+86400)
    outside=(sec+1800<=s.first_departure)|(sec>s.last_departure)
    flag=s.congestion_pct.eq(0)&outside&s.first_departure.notna()
    s.loc[flag,'no_service_flag']=True;s.loc[flag,'no_service_reason']='outside_canonical_operating_window; reference_dates_may_differ'
    info=b.datasets['segment_congestion'];sid=info['source_id']
    b.save('segment_congestion',s,sid,info['grain'],info['nature'],info['uses'],info['caution'],info['keys'])
    drops=pd.read_parquet(p/'segment_drop_features.parquet')
    join=['line','station_code','direction','day_type','time_bin']
    flags=s[join+['no_service_flag','no_service_reason']]
    drops=drops.drop(columns=['no_service_flag','no_service_reason','prev_no_service_flag']).merge(flags,on=join,validate='one_to_one')
    prev=flags.drop(columns='no_service_reason').rename(columns={'station_code':'prev_source_station_code','no_service_flag':'prev_no_service_flag'})
    drops=drops.merge(prev,on=['line','prev_source_station_code','direction','day_type','time_bin'],how='left',validate='many_to_one')
    valid=drops.no_service_flag.eq(False).fillna(False)&drops.prev_no_service_flag.eq(False).fillna(False)
    drops['congestion_drop_pct']=(drops.prev_congestion_pct-drops.congestion_pct).where(valid)
    drops['below_seat_line']=drops.congestion_pct.le(settings()['seat_line']['threshold_pct']).astype('boolean').where(drops.no_service_flag.eq(False).fillna(False))
    info=b.datasets['segment_drop_features'];b.save('segment_drop_features',drops,sid,info['grain'],info['nature'],info['uses'],info['caution'],info['keys'])
    b.quality[sid]['outside_canonical_operating_window_zero_rows']=int(flag.sum())
    b.quality[sid]['no_service_true']=int(s.no_service_flag.eq(True).sum())
