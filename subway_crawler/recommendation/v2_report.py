"""Measured v1→v2 changes, explicit acceptance checks and source-investigation evidence."""
import hashlib
import json
from pathlib import Path
import pandas as pd
from ..store import atomic_write
from .timetable_v2 import CONFIG,settings


def prepare(b,root):
    baseline=json.loads((CONFIG/'v1_baseline.json').read_text());p=b.out/'datasets'
    rows=[{'dataset':name,'v1_rows':baseline['dataset_rows'].get(name,0),'v2_rows':info['rows'],'delta':info['rows']-baseline['dataset_rows'].get(name,0)} for name,info in b.datasets.items()]
    pd.DataFrame(rows).to_csv(b.out/'dataset_changes.csv',index=False,encoding='utf-8-sig');b.context['v2_rows']=rows
    q=b.quality.get('kric_timetable',{});seg=b.quality.get('metro_segment',{});cars=b.quality.get('metro_cars',{})
    counts=q.get('line2_sindorim_daerim_07_09',{})
    checks=[('cross_version_duplicate_combinations',q.get('cross_version_duplicate_combinations'),0),('line4_weekday_hyehwa_stops',q.get('line4_weekday_hyehwa_stops'),474),
            ('line2_07_09_inclusive_departures',counts.get('departures'),42),('line2_origin_departures',counts.get('origin_departures'),12),('segment_unmatched_rows',seg.get('unmatched_rows'),0)]
    results=[{'criterion':name,'actual':actual,'expected':expected,'passed':actual==expected} for name,actual,expected in checks]
    ratio=q.get('line1_negative_duration_ratio');results.append({'criterion':'line1_negative_duration_ratio','actual':ratio,'expected':'< 0.01','passed':ratio is not None and ratio<.01})
    hol=b.quality.get('metro_ridership_types',{}).get('holiday_comparison',{});ratio=hol.get('exclude_holidays',{}).get('ratio')
    results.append({'criterion':'Monday_08_mean_median_ratio','actual':ratio,'expected':'>= 0.95','passed':ratio is not None and ratio>=.95})
    b.quality['v2_acceptance']={'checks':results,'baseline':baseline,'holiday_ratio_comparison':hol,
        'car_station_count_before':baseline['car_feature_station_count'],'car_station_count_after':cars.get('feature_station_count')}
    # User examples are empirical expectations, never injected labels. Choose first actual departure >= requested clock.
    scenarios=[]
    if 'trip_seat_line_features' in b.datasets:
        trips=pd.read_parquet(p/'trip_seat_line_features.parquet')
        for requested,expected in [('06:30:00','학동'),('07:00:00','강남구청'),('07:30:00','어린이대공원'),('08:00:00',None),('08:30:00','청담')]:
            matches=trips[trips.query_id.eq('line7_cheolsan')&trips.boarding_departure_time.ge(requested)].sort_values('boarding_departure_time')
            first=matches.iloc[0] if len(matches) else None
            value=first.first_stop_below_seat_line if first is not None and pd.notna(first.first_stop_below_seat_line) else None
            scenarios.append({'requested_time':requested,'actual_departure':first.boarding_departure_time if first is not None else None,'expected_station':expected,'time_local_result':value,'reproduced':value==expected,'reason':'각 정차역 출발시각의 구간을 사용. 승차시각 하나를 전 구간에 고정한 근사와 구분.'})
    b.quality['v2_acceptance']['line7_scenarios']=scenarios
    pd.DataFrame(scenarios).to_csv(b.out/'scenario_validation.csv',index=False,encoding='utf-8-sig')
    b.context['v2_scenarios']=scenarios;b.context['v2_checks']=results
    audit=[]
    for name,info in b.datasets.items():
        if name not in ('timetable_stops','segment_congestion','car_observations','monthly_ridership_hourly'):continue
        d=pd.read_parquet(p/(name+'.parquet'));cols=[c for c in ['line','station_code','station_name','mapping_status','mapping_reason','missing_reason'] if c in d]
        if 'mapping_status' in d:
            bad=d[d.mapping_status.ne('matched')][cols].drop_duplicates();bad.insert(0,'dataset',name);audit.extend(bad.to_dict('records'))
    pd.DataFrame(audit).to_csv(b.out/'mapping_failures.csv',index=False,encoding='utf-8-sig')
    b.context['v2_unmapped']=audit
    research_path=CONFIG/'coverage_research_v2.json'
    b.context['coverage_research']=json.loads(research_path.read_text()) if research_path.exists() else []
    pd.DataFrame(b.context['coverage_research']).to_csv(b.out/'coverage_research_matrix.csv',index=False,encoding='utf-8-sig')
    default_name='metro9_daily_2025_alighting_features'
    defaults={'line9_daily':{'dataset':default_name if default_name in b.datasets else None,'reason':'full-year 2025 sample preferred; 2026-01 retained separately','holiday_policy':'exclude_holidays'},'car_scope':'4호선 실제 확보 구간 only','realtime_enabled':False}
    atomic_write(b.out/'feature_defaults.json',json.dumps(defaults,ensure_ascii=False,indent=2))
    test_path=Path(root)/'v2_test_results.json'
    if test_path.exists():
        b.context['v2_tests']=json.loads(test_path.read_text())
    else:b.context['v2_tests']={'status':'not_recorded','reason':'tests must be run with scripts/validate_v2.py'}
    atomic_write(b.out/'v2_acceptance.json',json.dumps(b.quality['v2_acceptance'],ensure_ascii=False,indent=2,default=str))


def section(b,table):
    a=b.quality.get('v2_acceptance',{});c=b.quality.get('metro_cars',{});t=b.quality.get('kric_timetable',{})
    return ['## 수집 개선 결과(v2)','',
        table(b.context.get('v2_checks',[]),['criterion','actual','expected','passed']),
        '신도림→대림 07~09 검증은 09:00 정각을 포함한다. 반개구간 [07:00,09:00)은 41대이며 시발 12대는 동일하다. 결과값을 맞추기 위해 관측을 복제하지 않았다.',
        f"4호선 칸 특징 역 수: {a.get('car_station_count_before')} → {a.get('car_station_count_after')}. 당고개→불암산(409) 명시적 별칭으로 복구. 원본 관측시: {c.get('raw_hours')}. 남태령 이남/05시/23시는 실제 라벨·시각 목록 기준으로 판정한다.",
        '칸 번호의 진행방향 대응은 공식 명세 미확보. car_position은 결측, number_group 1–3/4–7/8–10은 번호 구간만 뜻한다. 앞·뒤로 추천하지 않는다.',
        '칸 차트와 car_summary.csv는 동시 10칸 완전 관측을 사용한 car_features 셀의 동일 가중 평균이다. 관측수 가중 평균과 구분한다.',
        f"요일 간 호차 순위 안정성: {c.get('rank_stability')}. 성능/신뢰도 점수가 아닌 관측 순위의 일관성 지표다.",
        '공휴일 플래그는 특일정보 API 캐시 또는 출처가 기재된 정적 목록을 사용한다. 일반 일요일은 weekday로 유지한다. 기본 패턴은 특정 공휴일 제외, *_including_holidays는 포함판이다. 9호선 일별 기본 사전값 출처는 표본이 많은 2025판이며 최신 2026-01은 별도 보존한다.',
        f"공휴일 자료: {b.quality.get('holiday_calendar')}",
        f"월요일 08~09 평균/중앙값 비율: {a.get('holiday_ratio_comparison')}. 양수 중앙값인 역들의 mean/median 비율 중앙값으로 정의했다.",
        '시각은 HH:MM:SS이고 12시간 초과 자정 역전만 24시 이상으로 연속 표기한다. stale은 기준일에서 365일 경과이다. 시행일은 원본 열에서 확보하지 못해 effective_date_source=unknown. 게시/수집일을 시행일로 바꾸지 않는다.',
        f"시간표 제외 {t.get('excluded_rows')}행, 비수도권 {t.get('non_metropolitan_rows')}행, stale {t.get('stale_stops')}정차행. 제외본은 timetable_excluded.parquet와 원본에 보존한다.",
        '### 기대 시나리오와 실제 시각 결합 비교','',table(b.context.get('v2_scenarios',[]),['requested_time','actual_departure','expected_station','time_local_result','reproduced']),
        '위 기대 경향이 다르게 나오는 경우를 실패/불일치로 그대로 기록했다. 각 역 도착까지 시간이 흐르므로 승차 시각 하나의 혼잡도를 전 구간에 고정한 결과와 같다고 보장할 수 없다. 하차역에서 출발하는 구간은 이 여정에 타지 않으므로 착석 기준선 검사에서 제외한다.',
        '### 데이터셋 행 수 변화','',table(b.context.get('v2_rows',[]),['dataset','v1_rows','v2_rows','delta']),
        '### 잔여 매핑 실패','',table(b.context.get('v2_unmapped',[])[:100],['dataset','line','station_code','station_name','mapping_reason']),
        '전체 실패 목록은 mapping_failures.csv. 확인되지 않은 축약명/운영기관 경계는 강제 결합하지 않는다.',
        '### 커버리지 추가 조사','',table(b.context.get('coverage_research',[]),['target','status','scope','license','access','source']),
        '조사 원본의 접근 검증과 표준화/추천 투입 여부는 구분한다. 빠른하차는 이번 정책에서 제외하여 API를 연결하지 않았다. 선택 실시간 어댑터는 구현하지 않았고 feature flag=false로 유지한다.',
        f"월간 보정 검증: {b.quality.get('monthly_calibration')}. 동일 월만 비교하고 원값을 보존한다. 확장노선의 estimated_weekday_share_weight는 보정 추정이며 요일별 실제 인원/좌석 확률이 아니다.",
        '### 테스트 실행 기록','',str(b.context.get('v2_tests')),
        '새 산출물의 전체 컬럼 정의·단위·성격·출처·결측 의미는 data_dictionary.csv에 포함한다. v1과 v2의 자연키/공휴일 정책이 달라 CSV 누적 내보내기는 최신 schema_version 안에서만 수행하며 이전 스냅샷을 삭제하지 않는다.','']
