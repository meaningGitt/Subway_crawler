# seat-evidence-v2 실행·해석 안내

요구사항 원문은 [REQUEST.md](REQUEST.md), 실제 실행 수치는 [RESULTS.md](RESULTS.md)와 각 스냅샷 report.md의 ‘수집 개선 결과(v2)’에서 확인한다. 기존 원본·v1 스냅샷은 보존한다.

## 가공 정책

- `config/seat_evidence_v2.json`: 노선별 canonical 시간표 선택, 방향·지선 연결, 기준 혼잡도, 기본 질의. 원본에 운영기관 식별이 충분하지 않아 현재 source priority는 동일하다. 명시된 우선순위 중 최신 reference_date 하나를 선택하며 source_line_code가 여러 개면 같은 기준일의 보완 파일을 함께 사용한다. 시행일은 확보하지 못했으므로 unknown이다.
- `config/station_aliases.json`: 검토한 노선별 개명·약칭·지선 코드만 명시. 퍼지 매칭/일괄 역 접미사 제거 없음. 900x 지선 코드는 station_key와 branch_id를 함께 보존한다.
- 시간은 HH:MM:SS. 12시간을 초과한 자정 역전만 다음 날로 이월한다. 기준일 365일 이상은 stale. 대경선 운행은 분석 제외하고 timetable_excluded와 원본에 남긴다.
- 월간 승차/하차 각각의 24시간 합계 0은 원값 raw_passengers=0을 보존하고 passengers/share는 null, missing_reason을 기록한다. 다른 노선 합산이라는 원인은 공식 확인 없이 확정하지 않는다.
- 구간 혼잡도 0은 유지한다. no_service_flag는 방향 경계/시간표 운행창 바깥이면 true, 양수는 false, 나머지 0은 unknown이다. 기준일 불일치가 있어 실제 운휴 확정용으로 사용하지 않는다.
- `segment_drop_features`: 같은 방향의 이전 구간 대비 감소량. 본선·지선 식별을 유지하므로 키에 branch_id/source_station_code가 추가된다. 분기 합류의 이전 구간을 유일하게 식별할 수 없으면 감소량은 null이다.
- 33.75%는 요청의 54석/160명 가정이다. 전 차량의 실제 좌석 정원을 검증한 상수가 아니며 좌석 점유율·확률·착석 보장이 아니다. 인천교통공사 7호선 공개 기준 34%와도 구분한다.
- `trip_stop_congestion`: 각 역 출발 시각의 30분 구간, 현재역→다음역, 방향, 요일이 일치해야 결합한다. 토/일이 혼합된 시간표 템플릿은 강제로 결합하지 않는다. 서로 다른 기준일을 warning에 남긴다.
- `trip_seat_line_features`: 승차역 이후 실제 탑승 구간에서 처음 기준선 이하인 출발역. 하차역의 다음 구간은 제외한다. 누락이 먼저 있으면 최초 도달을 확정하지 않는다. 시발 열차 표시는 별도이며 빈 차라는 의미가 아니다.
- 4호선 칸 통계는 동시 10칸 완전 관측만 사용한다. 차트와 car_summary CSV는 동일한 feature 셀 산술평균이다. 호차 진행방향 정의가 미확인이라 car_position은 null, number_group은 번호 구간만 표시한다. 타 노선으로 전용하지 않는다.
- 일별 패턴 기본값은 공휴일 제외, 포함판은 별도 데이터셋이다. 9호선은 feature_defaults.json에서 2025년 공휴일 제외판을 기본으로 지정하고 2026-01은 유지한다.
- 월간 보정계수는 2026-01~06 같은 역·월의 일별 평일 비중과 월간 비중을 비교한다. 확장 노선은 별도 `monthly_weekday_adjusted_estimates`의 보정 가중치로만 보관한다. 일별 인원이나 요일별 관측을 생성하지 않는다. 개방 구간인 06시 이전/24시 이후는 개별 시각으로 나누지 않는다.

## 실행

```sh
.venv/bin/python -m pip install -r requirements-analysis.txt
# 기존 저장 원본 재가공
.venv/bin/python -m subway_crawler.recommendation process --root ../seat-data --as-of 2026-11-10
# 새 공식 원본 확인/다운로드 후 가공
.venv/bin/python -m subway_crawler.recommendation run --root ../seat-data
# 기존 회귀 + v2 단위 + 실제 데이터 통합 검사, 보고서에 결과 반영
.venv/bin/python scripts/finalize_v2.py --root ../seat-data
# 원하는 승·하차역 질의 (시각 범위는 시작 포함/끝 제외)
.venv/bin/python -m subway_crawler.recommendation query --root ../seat-data \
  --line 2호선 --direction 외선 --day-type 평일 \
  --boarding-station 신도림 --alighting-station 삼성 \
  --from-time 07:00 --to-time 09:00 --output ../seat-data/sindorim_samseong.csv
```

실제 07:00≤출발≤09:00 검증은 신도림→대림 42대/시발 12대다. API 질의의 반개구간 [07:00,09:00)은 41대/시발 12대이다. 09:00 정각 열차가 차이를 만든다.

`latest.json`이 가리키는 스냅샷에서 `datasets/`, `data_dictionary.csv`, `quality.json`, `coverage_matrix.csv`, `coverage_research_matrix.csv`, `mapping_failures.csv`, `scenario_validation.csv`, `dataset_changes.csv`, `feature_defaults.json`, `test_results.json`, `report.html`을 확인한다. natural key가 달라 v1/v2 CSV를 자동 혼합하지 않고 최신 schema_version 내에서 누적 export한다. canonical 시간표·경로·열차 결합표는 최신 완료 스냅샷만 내보내 구버전을 재유입하지 않는다. 미완료 스냅샷은 export에서 제외한다.

## API·권한·자동 실행

공개 파일과 공식 정적 공휴일 목록만으로 실행된다. 선택 사항인 한국천문연구원 특일정보 서비스(getRestDeInfo) 승인키는 환경변수 또는 GitHub Secret `DATA_GO_KR_KEY`에 넣는다. 키 없이 API를 호출하지 않는다. API 실패 시 검증된 캐시 또는 정적 목록을 사용하고 원인을 기록한다. 현재 정적 목록은 2024~2026이며 이후 연도는 미분류 처리한다. 키를 채팅/코드에 넣지 않는다.

기존 매일 09:00·22:00 KST (= UTC 00:00·13:00) 스케줄은 원본 메타데이터 확인을 유지한다. 정각 실행은 GitHub에서 보장되지 않는다. recommendation.yml 수동 전체 수집은 기존 28개 회귀 테스트와 v2 단위 테스트, 실제 결과 통합 검증, 보고서 반영을 순서대로 실행하고 artifact를 14일 보관한다. 대용량 원본 영구 저장소가 없으므로 전체 데이터의 장기 무인 누적 완료로 표현하지 않는다. 현재 로컬 원본은 별도 seat-data 폴더에 보존한다.

7호선 인천 구간/9호선 추가 혼잡도는 이번 요청의 ‘수집 가능 여부·라이선스 우선’ 범위로 연구용 원본을 다운로드했으며 추천 데이터에 아직 합치지 않았다. research/v2의 원본·receipt와 config/coverage_research_v2.json을 함께 확인한다. 서울시 30분 OD는 이용 신청/방문/반출 확인이 필요하고 일반 API 키만으로 해결되지 않는다. 제한 자료의 허가를 우회하지 않는다.

선택 실시간 어댑터는 미구현·비활성. 빠른하차 API는 제외. feedback.schema.json은 PL-RECORD 필드/조건만 정의하며 사용자 기록 수집·가중 보정 모델·매일 예측 배치는 이번에 구현하지 않았다.

CSV 공유 묶음은 다음 명령으로 재생성한다. 전체 CSV·보고서와 로컬에 있는 추가 조사 원본을 포함하고 ZIP 내부 manifest에 파일별 SHA-256을 남긴다. 대용량 기본 원본/Parquet는 별도 seat-data와 전체 Actions artifact에 유지한다.

```sh
.venv/bin/python scripts/bundle_v2.py --root ../seat-data --output ../seat-data/seat_evidence_v2_csv.zip
```

보고서만 재생성할 때 데이터셋을 수정하지 않는다. finalize_v2.py는 기존 데이터 변환 코드 해시와 현재 보고서 생성 코드 해시를 report_generation.json에 구분해서 기록한다.
