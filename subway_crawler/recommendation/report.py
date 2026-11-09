"""Generate evidence tables, a field dictionary, and a readable report from actual outputs."""
from __future__ import annotations

import html
import json
from pathlib import Path
import pandas as pd

from ..store import atomic_write

FIELDS = {
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
 'flow':('승차/하차 태그 구분','문자'), 'time_bin':('원본 시간대 또는 관측시각에서 생성한 1시간 구간','KST'),
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
              '환승 수요':['transfer_demand'],'빠른하차 정보':[]}
    rows=[]
    for (operator,line),g in inventory.groupby(['RAIL_OPR_ISTT_NM','line']):
        r={'운영기관':operator,'노선':line,'기준 역코드 수':len(g)}
        keys=set(g.station_key)
        for cat,names in category.items():
            observed=set().union(*(set(builder.datasets.get(n,{}).get('station_keys',[])) for n in names))
            n=len(keys & observed)
            # Even all station matches do not establish complete dates/trains/directions.
            in_scope = (cat=='OD 통행량' and g.RAIL_OPR_ISTT_CD.eq('KR').all()) or (cat=='환승 수요' and g.RAIL_OPR_ISTT_CD.eq('S1').all())
            r[cat]=f'일부 확보 ({n}/{len(g)}역)' if n else ('확인 필요 (기관 원본 확보·역 식별 미검증)' if in_scope and any(nm in builder.datasets for nm in names) else '미확보')
        r['하차 예측 근거']=('일별 시간대·요일 통계; 공휴일 미분류' if '일부 확보' in r['일별·요일별 승하차'] else
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
    cov=coverage(builder);pd.DataFrame(cov).to_csv(out/'coverage_matrix.csv',index=False,encoding='utf-8-sig')
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
      '공휴일 달력 미확보로 is_holiday는 결측이며 요일 평균에 공휴일이 섞여 있습니다.','',
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
      '2. P1: 공식 공휴일 달력, 다른 노선의 칸별 혼잡도와 표본 설계, 방향별 환승수요.',
      '3. P2: 빠른하차 API 인증키, 칸별 승하차 원시 통계의 공개 여부 확인.',
      '4. 운영: 외부 영구 저장소 또는 상시 가동 로컬 머신이 필요합니다. GitHub 예약 확인과 지속적인 대용량 원본 보존을 구분합니다.',
      '5. 정확도 검증: 추천 경로/칸, 실제 경로/칸, 좌석 성공 여부, 확보까지 초/역 수를 동의 기반으로 수집하는 feedback.schema.json을 제안합니다. 기능은 구현하지 않았습니다.','',
      '## 추적 방법','',
      '분석행의 source_id → manifest.json의 원본 raw_path/SHA-256 → source_row 또는 자연키 그룹 → transform_version과 코드 해시를 따라갑니다. 원본/정규화/집계 데이터셋을 별도 보존합니다.',
      '데이터셋 간 행 수를 합산해 누적 이용객 또는 고유 관측 수로 해석하지 마세요. 여러 단계에서 같은 관측이 재표현됩니다.','']
    markdown='\n'.join(text);atomic_write(out/'report.md',markdown)
    charts=[svg_bars(builder.context.get('hourly',[]),'time_bin','하차','확보 역·일 기준 시간대 평균 하차 인원 (명)'),
            svg_bars(builder.context.get('car_means',[]),'car_no','mean','4호선 확보 표본의 칸별 평균 혼잡도 (%)')]
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
