"""Generate evidence tables, a field dictionary, and a readable report from actual outputs."""
from __future__ import annotations

import html
import json
from pathlib import Path
import pandas as pd

from ..store import atomic_write

FIELDS = {
 'holiday_policy':('특정 공휴일 포함/제외 집계 정책', '정책'),
 'raw_passengers':('정제 이전 인원; 합계 0의 원본값 보존', '명'),
 'missing_reason':('분석값 결측 처리 사유; 원본 0과 미제공 분리', '사유'),
 'no_service_flag':('원본 방향 경계의 0 또는 canonical 운행범위 밖 0; 실제 운휴 관측 아님. 미상은 null', 'boolean'),
 'no_service_reason':('운행 없음 판정 근거 또는 추가 검증 필요 사유', '사유'),
 'prev_no_service_flag':('이전 구간의 운행 없음 판정 플래그', 'boolean'),
 'prev_station_key':('진행 방향 직전 역 코드; 지선 합류 모호 시 결측', 'ID'),
 'prev_congestion_pct':('직전 역 출발 구간의 동일 요일·시간대 공표 혼잡도', '%'),
 'congestion_drop_pct':('직전 구간 혼잡도에서 현재 출발 구간 혼잡도를 뺀 값. 경계/미상 제외', '%p'),
 'below_seat_line':('평균 혼잡도 ≤ 설정 기준선 여부; 좌석 확보 여부 아님', 'boolean'),
 'seat_line_pct':('54석/정원160명이라는 지정 가정으로 얻은 33.75 기준', '%'),
 'branch_id':('원본 지선·방향 분리 측정 구분. 같은 역 코드라도 병합 금지', '구분'),
 'prev_source_station_code':('명시적 방향 그래프의 이전 원본 코드', 'ID'),
 'direction_boundary':('현재 원본의 측정 방향/종착 경계', 'boolean'),
 'topology_reason':('방향 그래프의 연결 미확정/범위 경계 사유', '사유'),
 'source_time_bin':('정규화 이전 원본 시간대 표기', 'KST'),
 'first_departure':('canonical 유효 시간표의 첫 계획 출발 시각, 영업일 기준', '초'),
 'last_departure':('canonical 유효 시간표의 마지막 계획 출발 시각', '초'),
 'version_id':('노선과 원본 기준일로 식별한 발행본', 'ID'),
 'superseded_by_version':('해당 분석 제외본을 대체한 canonical 발행본', 'ID'),
 'exclusion_reason':('기준일 대체·비수도권·상충값 등의 제외 사유', '사유'),
 'mapping_reason':('정확한 역 코드/이름 연결 실패 사유', '사유'),
 'effective_date_source':('시간표 시행일 근거; 원본 미확보는 unknown', '출처'),
 'stale':('기준일이 판정일보다 365일 이상 이전인지', 'boolean'),
 'stale_as_of':('stale 계산 기준일; 수집/시행일과 구분', '날짜'),
 'is_origin_stop':('원본 운행 첫 정차행이 명시된 시발역과 일치하는지', 'boolean'),
 'is_short_turn':('계획 종착이 설정된 정규 종착 집합 외에 있는지; 차량 회차 확정 아님', 'boolean'),
 'minutes_from_origin':('확인된 시발 정차행 출발부터 경과 계획시간', '분'),
 'arrival_seconds':('자정 넘김이 반영된 도착 계획시각', '영업일 시작 후 초'),
 'departure_seconds':('자정 넘김이 반영된 출발 계획시각', '영업일 시작 후 초'),
 'raw_travel_seconds':('다음 도착−현재 출발, 음수/이상값도 진단용 보존', '초'),
 'invalid_reason':('분석 제외 구간의 첫 검증 실패 사유', '사유'),
 'direction_path':('공표 시발→종착 경로; 정규화 상하 코드와 별도', '문자'),
 'car_position':('진행 방향 기준 앞·중간·뒤; 공식 호차 방향 명세 미확보로 결측', '구역'),
 'car_position_reason':('진행 방향 구역을 파생하지 못한 이유', '사유'),
 'number_group':('호차 번호 구간 1–3/4–7/8–10. 진행 방향 아님', '번호구간'),
 'weighting':('차트/요약 평균의 가중 기준', '정책'),
 'feature_cells':('요약에 사용된 유효 시간·구간·요일 특징 셀 수', '개'),
 'mean':('동시 10칸 완전 관측 특징 셀의 동일 가중 호차 평균', '%'),
 'n':('해당 호차에 포함된 측정 수; 독립 표본 수 아님', '건'),
 'query_id':('승차역·하차역·시간 범위 파라미터 묶음 식별자', 'ID'),
 'origin':('공표 운행 시발역', '역명'),
 'boarding_station':('조회 승차역', '역명'),
 'alighting_station':('조회 하차역', '역명'),
 'boarding_stop_sequence':('조회 운행에서의 승차 정차 순번', '순번'),
 'boarding_departure_time':('승차역의 계획 출발시각', 'KST HH:MM:SS'),
 'first_stop_below_seat_line':('이동 중 처음 관측된 평균 혼잡도 기준선 이하 출발역; 누락 앞구간 있으면 최초 확정 불가', '역명'),
 'minutes_until_seat_line':('승차부터 위 역까지 계획 경과시간; 실제 서서 간 시간 아님', '분'),
 'is_origin_trip_at_boarding':('조회 승차역이 해당 운행의 시발 정차인지', 'boolean'),
 'estimate_status':('기준선 관측/미도달/누락 구간/미확보 상태', '구분'),
 'estimate_kind':('집계 통계 기반 추정의 종류; 좌석 확률 아님', '구분'),
 'missing_congestion_stops':('조회 여정의 혼잡도 또는 운행 판정 미확정 정차 수', '개'),
 'congestion_day_type':('공표 혼잡도와 결합 가능한 정확한 요일 템플릿', '구분'),
 'congestion_join_reason':('혼잡도 결합 실패/모호한 지선·공휴일 템플릿 사유', '사유'),
 'congestion_reference_date':('결합에 사용한 혼잡도 기준일', '날짜'),
 'reference_date_mismatch':('시간표와 혼잡도의 기준일이 다른지', 'boolean'),
 'warning':('시점 불일치 등 해석 주의사항', '문자'),
 'weekday_hour_passengers':('해당 역·월에서 특정 공휴일을 제외한 월~금 시간대 하차 합', '명'),
 'weekday_total':('해당 역·월의 특정 공휴일 제외 월~금 전체 시간대 하차 합', '명'),
 'weekday_share':('평일 시간대 하차 합 / 평일 전체 하차 합', '비중'),
 'monthly_share':('공식 월간 시간대 하차 합 / 공식 월간 전체 하차 합', '비중'),
 'weekday_correction_factor':('동일 월·역 평일 비중 / 월간 비중', '배수'),
 'median_factor':('동일 시간대 역·월 보정계수의 중앙값', '배수'),
 'estimated_weekday_share_weight':('확장노선 원래 월간 비중 × 타 노선 중앙 보정계수; 요일별 실측 아님', '추정 가중치'),
 'calibration_periods':('보정계수 학습에 사용한 월 목록', 'YYYY-MM'),
 'calibration_time_bin':('보정계수와 결합한 1시간 시작구간', 'KST'),
 'station_month_samples':('보정계수 계산에 사용한 역·월 조합 수', '개'),
 'station_count':('해당 보정 표본의 고유 역 키 수', '개'),
 'monthly_raw_sha256':('결합/보정에 사용한 원본 SHA-256; 복수는 | 구분', '해시'),
 'congestion_raw_sha256':('결합/보정에 사용한 원본 SHA-256; 복수는 | 구분', '해시'),
 'timetable_raw_sha256':('결합/보정에 사용한 원본 SHA-256; 복수는 | 구분', '해시'),
 'calibration_daily_raw_sha256':('결합/보정에 사용한 원본 SHA-256; 복수는 | 구분', '해시'),
 'calibration_monthly_raw_sha256':('결합/보정에 사용한 원본 SHA-256; 복수는 | 구분', '해시'),
 'source_station_name':('명시적 별칭 적용 전 원본 역 라벨', '문자'),
 'source_origin_name':('명시적 별칭 적용 전 원본 역 라벨', '문자'),
 'source_destination_name':('명시적 별칭 적용 전 원본 역 라벨', '문자'),

 'station_key':('운영기관:노선:역 코드의 검증된 결합 식별자','ID'),
 'next_station_key':('다음 역의 검증된 결합 식별자','ID'),
 'origin_station_key':('출발역 결합 식별자; OD 코드북 미확보 시 결측','ID'),
 'destination_station_key':('도착역 결합 식별자; OD 코드북 미확보 시 결측','ID'),
 'station_name':('원본 역명','문자'), 'next_station_name':('원본 다음역명','문자'),
 'origin_name':('원본 출발/시발역명','문자'), 'destination_name':('원본 도착/종착역명','문자'),
 'line':('표준 노선명; 직통 열차의 소유기관과 별개','문자'),
 'source_line_name':('원본 노선명','문자'), 'source_line_code':('원본 노선코드; 값 오류 가능','ID'),
 'station_code':('원본 출처 내 역 코드','ID'), 'service_date':('원본 수송/운행 영업일','KST 날짜'),
 'reference_date':('원본 데이터 기준일; 시간표 시행일과 다름','날짜'),
 'effective_date':('시간표 시행일; 원본 미제공이면 결측','날짜'),
 'period':('월별 집계 기준월','YYYY-MM'), 'year':('연간 집계 기준연도','연도'),
 'provider_processed_date':('제공기관이 표시한 작업일자; 관측일·수집일과 구분','YYYYMMDD'),
 'observed_hour_bins':('해당 역·월에 유효하게 확보된 하차 시간구간 수; 관측 일수가 아님','구간'),
 'within_station_month_alighting_share':('해당 역·월의 24시간 총 하차량 중 시간대 비중; 좌석 확률 아님','0~1'),
 'source_time_column':('원본 CSV의 시간대·승하차 컬럼명','문자'),
 'weekday':('영업일에서 계산한 요일: 월0~일6, 공휴일 여부와 별개','0~6'),
 'day_type':('기관이 공표한 요일구분 그대로; 평일/휴일 의미는 출처별 상이','문자'),
 'is_holiday':('공식 공휴일 달력과 검증 전에는 미상','boolean'),
 'flow':('승차/하차 태그 구분','문자'), 'time_bin':('원본 시간대; 구간/열차 혼잡도는 HH:MM 시작 30분, 칸/승하차는 원본의 시간 집계 단위 참조','KST'),
 'direction':('원본 상하 구분 또는 현재→다음/시발→종착 표시; 통일 상하코드 아님','문자'),
 'observed_time':('원본 혼잡도 측정시각','KST HH:mm:ss'),
 'arrival_time':('원본 계획 도착시각; 24시 이상 표기 보존','KST 시각'),
 'departure_time':('원본 계획 출발시각','KST 시각'),
 'travel_seconds':('다음역 도착시각 - 현재역 출발시각; 검증된 시간차만','초'),
 'train_no':('원본 열차번호; 요일/구간/기준일 없이 단독 식별 금지','ID'),
 'formation_id':('원본 편성번호; 열차번호와 구분','ID'), 'car_no':('원본 호차 번호','칸'),
 'trip_key':('노선코드+열차번호+요일+운행유형+시종착+기준일','ID'),
 'stop_sequence':('명시된 토큰 순서 또는 원본 행 순서로 정한 정차순서','순번'),
 'source_stop_token':('원본 목록 안에서 정차역과 시각을 연결한 토큰','ID'),
 'packed_sequence':('원본 한 셀 목록 내 정차순번','순번'),
 'service_type':('원본 일반/급행/직통 구분','문자'),
 'passengers':('해당 집계 단위의 공표 인원; 개별 열차 탑승객 아님','명'),
 'mean_passengers':('같은 역·요일·시간대 일별 인원의 산술평균','명/관측일'),
 'median_passengers':('같은 집계 그룹 일별 인원의 중앙값','명/관측일'),
 'p90_passengers':('같은 그룹 일별 인원의 90백분위수(선형보간)','명/관측일'),
 'origin_total':('수집한 OD 파일 내 해당 출발 라벨의 연간 인원 합','명'),
 'destination_share':('목적지 인원 / 출발 라벨 합; 좌석 확률 아님','0~1 비중'),
 'congestion_pct':('원본 혼잡도; 정원 대비 등 출처 정의를 따름. 100 초과 허용','%'),
 'mean_congestion_pct':('그룹 관측 혼잡도 산술평균','%'),
 'median_congestion_pct':('그룹 관측 혼잡도 중앙값','%'),
 'p90_congestion_pct':('그룹 관측 혼잡도 90백분위수(선형보간)','%'),
 'mean_relative_pct':('같은 시각·편성의 10칸 평균 대비 각 칸 차이의 평균','%p'),
 'sample_observations':('유효 측정 행 수; 독립 표본수 아님','행'),
 'sample_days':('그룹에 포함된 서로 다른 영업일 수','일'),
 'sample_formations':('그룹의 서로 다른 편성 수','편성'),
 'observation_n':('공표 평균을 산출한 원표본수; 미제공은 결측','관측 수'),
 'observation_start':('그룹의 최초 관측 영업일','날짜'),
 'observation_end':('그룹의 마지막 관측 영업일','날짜'),
 'sample_check':('8일 미만 검토 플래그. 통계적 신뢰도나 정확도 등급 아님','범주'),
 'mapping_status':('matched/ambiguous/unmatched/코드북 필요 등 매핑 결과','범주'),
 'quality_valid':('해당 행의 구조·필수값·범위 검사 통과. 정확도 보증 아님','boolean'),
 'source_id':('config/recommendation_sources.json의 출처 식별자','ID'),
 'raw_sha256':('실제 다운로드 원본의 SHA-256','hash'),
 'collected_at':('원본을 다운로드한 실제 시각. 데이터 기준일과 별개','ISO8601 KST'),
 'transform_version':('가공 코드 버전; snapshot manifest의 코드 해시와 함께 사용','ID'),
 'source_row':('원본 헤더 포함 1기반 행번호. 유형 합산 시 첫 행; 전체 그룹은 자연키로 추적','행'),
 'next_source_row':('다음 정차역 원본 행번호','행'),
 'source_member':('ZIP 내부 파일 경로; !는 중첩 ZIP 경계','경로'),
 'unit':('원본 단위 표시','문자'),
 'ambiguous_od_label':('동일 출발·도착 라벨 쌍이 반복되는 출발역: 목적지 비중 분석 제외','boolean'),
}


def table(rows, columns):
    def fmt(v):
        if v is None:return '—'
        if isinstance(v,float):return f'{v:,.2f}'
        return str(v).replace('|','/').replace('\n',' ')
    return '\n'.join(['| '+' | '.join(columns)+' |','|'+'---|'*len(columns)]+[
        '| '+' | '.join(fmt(row.get(c)) for c in columns)+' |' for row in rows])


def coverage(builder):
    inventory=builder.mapper.inventory
    daily=['alighting_features','metro9_daily_alighting_features','metro9_daily_2025_alighting_features']
    monthly=['monthly_alighting_features']
    category={'승하차 통계':daily+monthly, '일별·요일별 승하차':daily, '월별 시간대 승하차':monthly,
              'OD 통행량':['od_annual'], '열차 시간표':['timetable_stops'],
              '구간별 혼잡도':['segment_congestion'],'칸별 혼잡도':['car_observations'],
              '환승 수요':['transfer_demand']}
    rows=[]
    for (operator,line),g in inventory.groupby(['RAIL_OPR_ISTT_NM','line']):
        r={'운영기관':operator,'노선':line,'기준 역코드 수':len(g)}
        keys=set(g.station_key)
        for cat,names in category.items():
            observed=set().union(*(set(builder.datasets.get(n,{}).get('usable_station_keys',builder.datasets.get(n,{}).get('station_keys',[]))) for n in names))
            n=len(keys & observed)
            # Even all station matches do not establish complete dates/trains/directions.
            in_scope = (cat=='OD 통행량' and g.RAIL_OPR_ISTT_CD.eq('KR').all()) or (cat=='환승 수요' and g.RAIL_OPR_ISTT_CD.eq('S1').all())
            r[cat]=f'일부 확보 ({n}/{len(g)}역)' if n else ('확인 필요 (기관 원본 확보·역 식별 미검증)' if in_scope and any(nm in builder.datasets for nm in names) else '미확보')
        r['하차 예측 근거']=('일별 시간대·요일 통계; 특정 공휴일 제외' if '일부 확보' in r['일별·요일별 승하차'] else
                         '월간 시간대 패턴; 요일·방향 미제공' if '일부 확보' in r['월별 시간대 승하차'] else '미확보')
        r['칸 추천 근거']='4호선 일부 편성 관측' if '일부 확보' in r['칸별 혼잡도'] else '미확보'
        r['경로 추천 근거']='공표 시간표 일부; 시행일 확인 필요' if '일부 확보' in r['열차 시간표'] else '미확보'
        rows.append(r)
    return rows


def svg_bars(items,label,value,title):
    items=[i for i in items if i.get(value) is not None][:20]
    if not items:return ''
    maximum=max(float(i[value]) for i in items) or 1
    h=60+len(items)*28
    lines=[f'<svg xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{html.escape(title)}" viewBox="0 0 850 {h}"><title>{html.escape(title)}</title><text x="10" y="24" font-size="18">{html.escape(title)}</text>']
    for n,item in enumerate(items):
        y=50+n*28;w=max(0,float(item[value])/maximum*560)
        lines += [f'<text x="10" y="{y+15}" font-size="13">{html.escape(str(item[label]))}</text>',
                  f'<rect x="145" y="{y}" width="{w:.2f}" height="18" fill="#337a89"/>',
                  f'<text x="{155+w:.2f}" y="{y+14}" font-size="12">{float(item[value]):,.1f}</text>']
    return ''.join(lines)+'</svg>'


def generate(builder,run):
    out=builder.out
    dictionary=[]
    for name,info in builder.datasets.items():
        for col,stat in info['columns'].items():
            definition,unit=FIELDS.get(col,(f'원본 컬럼 {col}: 변경 없이 보존; 해당 출처 설명 참조', '명' if '시간대' in col or col in ('06시이전','24시이후') else '원본 단위'))
            dictionary.append({'dataset':name,'column':col,'definition':definition,'type':stat['dtype'],'unit':unit,
                 'missing_count':stat['missing'],'missing_meaning':'미제공·매핑실패·검증불가 또는 원본 결측. 0과 구분',
                 'nature':info['nature'],'source':info['source_url'],'cadence':info['cadence'],'grain':info['grain'],
                 'uses':','.join(info['uses']),'caution':info['caution']})
    pd.DataFrame(dictionary).to_csv(out/'data_dictionary.csv',index=False,encoding='utf-8-sig')
    atomic_write(out/'data_dictionary.json',json.dumps(dictionary,ensure_ascii=False,indent=2))
    cov=coverage(builder)
    for row in cov:
        for col in ('승하차 통계','일별·요일별 승하차','월별 시간대 승하차','OD 통행량','열차 시간표','구간별 혼잡도','칸별 혼잡도','환승 수요'):
            value=row.get(col,'')
            row[col]=value.replace('일부 확보','확보 (일부)').replace('확인 필요','확보·역 식별 확인 필요') if value!='미확보' else '원본 미제공 (현재 수집본)'
    research=builder.context.get('coverage_research',[])
    for row in cov:
        relevant=[r for r in research if (row['노선'] in r.get('lines',[]) and (not r.get('operators') or any(op in row['운영기관'] for op in r['operators'])))]
        row['추가 조사 상태']='; '.join(r['target']+': '+r['status']+' ('+r['access']+')' for r in relevant)
    pd.DataFrame(cov).to_csv(out/'coverage_matrix.csv',index=False,encoding='utf-8-sig')
    mapping=pd.DataFrame(builder.mapper.audit);mapping.to_csv(out/'station_mapping.csv',index=False,encoding='utf-8-sig')
    builder.mapper.inventory.to_csv(out/'station_reference.csv',index=False,encoding='utf-8-sig')
    failures=[r for r in run['sources'] if r.get('processing_status')!='success' or r.get('status')!='수집 완료']
    atomic_write(out/'failures.json',json.dumps(failures,ensure_ascii=False,indent=2,default=str))
    quality={'criteria':{
        'completeness':'필수값/전체 컬럼 결측 수, 기준 코드목록 대비 실제 매핑된 역 수. 기대 운행/관측 수 미상은 완전성 확정 불가',
        'consistency':'자연키 중복·상충값, 음수, 시각 형식, 역코드+노선+운영기관 일치. 외부 실측 정확도 검증 없음',
        'freshness':'원본 실제 관측 범위/기준일과 수집 시각 분리. 시간표 시행일 미제공은 현재 유효성 미검증',
        'sample':'sample_days < 8은 검토 대상. 8일 이상도 독립성·추천 정확도 보장 아님',
        'probabilities':'좌석 관측 피드백 없음. 좌석 확률/신뢰도 점수/모델 성능 생성 안 함'},
        'sources':builder.quality,'datasets':builder.datasets}
    atomic_write(out/'quality.json',json.dumps(quality,ensure_ascii=False,indent=2,default=str))
    suitability=[]
    for name,info in builder.datasets.items():
        suitability.append({'데이터셋':name,'행 수':info['rows'],'관측 단위':info['grain'],'성격':info['nature'],
                            '활용':','.join(info['uses']),'한계':info['caution']})
    pd.DataFrame(suitability).to_csv(out/'recommendation_suitability.csv',index=False,encoding='utf-8-sig')
    attempted=[s for s in run['sources'] if s.get('adapter')!='review']
    success=sum(s.get('status')=='수집 완료' for s in attempted)
    source_rows=[{'출처':s['source_id'],'원본 확보':s.get('status'),'가공':s.get('processing_status','미실행'),
                  '바이트':s.get('receipt',{}).get('bytes'),'사유':s.get('reason',s.get('error',''))} for s in run['sources']]
    text=['# 앉을 지도 — 실제 수집·추천 근거 분석','',f"실행 {run['id']} / {run['started_at']} ~ {run['ended_at']} (Asia/Seoul)",'',
      f'다운로드 가능한 등록 출처의 확보 성공률: **{success}/{len(attempted)}**. 인증/이용허락 검토 출처는 분모에서 제외. 전체 등록 출처의 성공률과 구분합니다.',
      '같은 원본을 재사용한 경우에도 확보 성공으로 표시하며, downloaded 필드는 실제 재다운로드 여부를 구분합니다.',
      '좌석 확보 확률은 계산하지 않았습니다. 개찰구 하차량·연간 OD·혼잡도는 추천의 보조 근거이며 실제 좌석 발생과 다릅니다.','',
      '## 공식 출처와 실제 확보 상태','',table(source_rows,['출처','원본 확보','가공','바이트','사유']),'',
      '## 노선·운영기관 커버리지','',
      '분모는 보유 기준 역코드 목록입니다. 물리적 역사 수가 아니며 노선·운영기관 경계역이 중복될 수 있습니다. 전 노선 전체 기간·열차 완전 확보를 의미하는 완료 판정은 하지 않았습니다.',
      '승하차 커버리지는 유효한 분석용 행의 역 키를 기준으로 합니다. 일별·요일별 통계와 월간 시간대 합계는 별도 표시하며 서로 합산하지 않습니다. OD/환승의 기관 원본 확보는 해당 노선 확보를 보장하지 않습니다.',
      table(cov,list(cov[0])) if cov else '기준목록 없음','',
      '## 품질과 추천 적합도','',table(suitability,['데이터셋','행 수','관측 단위','성격','활용','한계']),'',
      '상세 결측 수·타입·관측 범위는 data_dictionary.csv 및 quality.json, 미매핑 원본 라벨은 station_mapping.csv에서 확인합니다.',
      table([{'출처':sid,'입력 행':q.get('input_rows'),'중복 행':q.get('duplicate_rows'), '음수·수치 결측':q.get('invalid_value_rows'), '라벨 중복':q.get('duplicate_label_rows',0),'상충 행 제외':q.get('excluded_conflicting_rows',0),'경로 제외':q.get('unusable_edges',0)} for sid,q in builder.quality.items()],['출처','입력 행','중복 행','음수·수치 결측','라벨 중복','상충 행 제외','경로 제외']),
      '중복은 자연키 기준이며 상충값은 자동 덮어쓰기하지 않습니다. 9호선의 상충 키는 전체 행을 별도 conflicts 파일로 보존하고 분석 제외하며, 다른 출처의 상충값은 가공을 중단합니다. 음수/잘못된 시각은 quality_valid=false로 표시합니다. 100% 초과 혼잡도는 그 자체로 오류가 아닙니다.',
      '시간표 기준일, 승하차 관측연도, 혼잡도 관측월이 다릅니다. 별도 유효기간 검증 없이 동시 관측처럼 결합하지 않습니다.',
      '기본 요일 통계는 공휴일 제외이며 포함판을 별도 저장합니다. calendar quality에 API/정적 출처와 해시를 남깁니다.','',
      '## 역·시간대 승하차 패턴','',table(builder.context.get('top_alighting',[]),['line','station_name','weekday','time_bin','mean_passengers','median_passengers','sample_days']),
      '위 값은 요일별 시간대의 개찰구 하차 수요입니다. 이 순위를 열차 내 하차율 또는 좌석 확률로 해석할 수 없습니다.','',
      '## 수도권 월간 시간대 승하차 보완','',
      '관측월: '+', '.join(builder.context.get('monthly_periods',[])),
      table(builder.context.get('monthly_scope',[]),['source_line_name','station_rows','mapped_rows']),
      '서울특별시 서울 열린데이터광장 OA-12252, 공공누리 제1유형(출처표시). 원본 노선명과 표준 노선명은 다릅니다. 예: 경부·경인·경원·장항선은 1호선 구간이며, 분당·수인선은 수인분당선입니다.',
      '월별 시간대 합계는 하차 수요 패턴을 보완하지만 요일별 평균·중앙값·일별 표본 수를 만들 수 없습니다. 역별 월간 하차량 내 시간대 비중은 좌석 발생률이 아닙니다.',
      '9호선 2·3단계 일별 자료는 서울교통공사 OA-22441(공공누리 제1유형)에서 확보했습니다. KRIC의 9호선 통합 참조 코드에 연결하되, 원본 제공기관과 참조 목록 기관은 동일하다고 간주하지 않습니다.','',
      '## OD 이동 패턴','',table(builder.context.get('od_top',[]),['year','origin_name','destination_name','passengers','destination_share']),
      '비중은 원본 파일 내 해당 출발 라벨의 연간 합계를 분모로 합니다. 코드북이 없어 노선별 역에 강제 매핑하지 않았으며, 시간대별 OD로 분해하지 않았습니다.','',
      '## 구간·칸 혼잡도 패턴','',table(builder.context.get('segment_top',[]),['line','station_name','direction','day_type','time_bin','congestion_pct']),
      '구간 혼잡도는 기관 공표 평균입니다. 칸별 통계는 4호선 관측에서 동시 10칸이 모두 있는 측정만 비교하고 관측일·편성 수를 함께 제공합니다. 연속 측정을 독립 표본으로 볼 수 없습니다.',
      '칸별 평균 차이는 관측된 편성과 구간에만 적용하며 다른 노선으로 전용하지 않습니다.','',
      '## 추가 확보 우선순위','',
      '1. P0: 시간표 시행일·매핑 오류 정정, 수도권 전체 기관 승하차 확대, OD 라벨 코드북과 시간대별 공식 OD.',
      '2. P1: 공휴일 API 갱신 연결, 확보한 인천·9호선 조사 원본의 표준화, 다른 노선 칸별 혼잡도와 방향별 환승수요.',
      '3. P2: 칸별 승하차 원시 통계의 공개 여부 확인. 빠른하차 API는 지도 앱 연결 정책에 따라 제외.',
      '4. 운영: 외부 영구 저장소 또는 상시 가동 로컬 머신이 필요합니다. GitHub 예약 확인과 지속적인 대용량 원본 보존을 구분합니다.',
      '5. 정확도 검증: 가명 ID, 여정·추천/실제 열차 시각, 착석 결과 4종과 선택 호차·착석 역·자리 구역을 담는 feedback.schema.json을 정의했습니다. 좌표와 피드백 수집 기능은 포함하지 않았습니다.','',
      '## 추적 방법','',
      '분석행의 source_id → manifest.json의 원본 raw_path/SHA-256 → source_row 또는 자연키 그룹 → transform_version과 코드 해시를 따라갑니다. 원본/정규화/집계 데이터셋을 별도 보존합니다.',
      '데이터셋 간 행 수를 합산해 누적 이용객 또는 고유 관측 수로 해석하지 마세요. 여러 단계에서 같은 관측이 재표현됩니다.','']
    from .v2_report import section
    text.extend(section(builder,table))
    markdown='\n'.join(text);atomic_write(out/'report.md',markdown)
    charts=[svg_bars(builder.context.get('hourly',[]),'time_bin','하차','확보 역·일 기준 시간대 평균 하차 인원 (명)'),
            svg_bars(builder.context.get('car_means',[]),'car_no','mean','4호선 동시 10칸 완전 관측 · feature 셀 동일 가중 평균 (%)')]
    for i,svg in enumerate(charts,1):
        if svg:atomic_write(out/f'chart_{i}.svg',svg)
    body=[];in_table=False
    for line in markdown.splitlines():
        if line.startswith('|'):
            if not in_table:body.append('<div class="scroll"><table>');in_table=True
            if set(line.replace('|','').replace('-','').strip())==set():continue
            body.append('<tr>'+''.join('<td>'+html.escape(v.strip())+'</td>' for v in line.strip('|').split('|'))+'</tr>')
        else:
            if in_table:body.append('</table></div>');in_table=False
            if line.startswith('# '):body.append('<h1>'+html.escape(line[2:])+'</h1>')
            elif line.startswith('## '):body.append('<h2>'+html.escape(line[3:])+'</h2>')
            elif line:body.append('<p>'+html.escape(line)+'</p>')
    if in_table:body.append('</table></div>')
    page='<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>앉을 지도 데이터 분석</title><style>body{font:16px/1.65 system-ui,sans-serif;max-width:1200px;margin:36px auto;padding:0 24px;color:#172b36}h1,h2{color:#235d6b}table{border-collapse:collapse;font-size:13px}td{border:1px solid #d0dee2;padding:8px;vertical-align:top}tr:first-child{background:#e5f1f2;font-weight:600}.scroll{overflow-x:auto}svg{max-width:850px;width:100%;margin:20px 0}p{max-width:1000px}</style><body>'+''.join(charts)+''.join(body)+'</body></html>'
    atomic_write(out/'report.html',page)
