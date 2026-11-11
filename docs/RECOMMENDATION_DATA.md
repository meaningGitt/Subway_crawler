# 앉을 지도 수집·분석 계획과 운영

v2 변경 사항과 최신 실행 방법은 [v2 문서](v2/IMPLEMENTATION.md)가 우선한다.

목표는 역 목록의 확대가 아니라 하차 수요·칸·경로 추천에 쓸 수 있는 검증 가능한 근거 구축이다. 좌석 발생/확보 확률은 관측 없이 산출하지 않는다. 기존 기준 역목록·SQLite 수집기와 09:00/22:00 KST 예약을 유지하고 `subway_crawler.recommendation` 모듈을 추가했다.

## 우선순위와 공식 출처

| 우선순위 | 출처 | 실제 확보/가공 범위 | 한계 및 주기 |
|---|---|---|---|
| P0 | [서울교통공사 승객유형별 승하차](https://www.data.go.kr/data/15099330/fileData.do) | 2026-01~06 일별·역별·시간대·승객유형 CSV | 게이트 통계, 방향/열차 없음. 포털 최신 파일 동적 탐색 |
| P0 | [서울교통공사 승하차](https://www.data.go.kr/data/15048032/fileData.do) | 2024년 일별 통계 CSV, historical_* 별도 | 연간. 최신 자료와 기간 분리 |
| P0 | [서울시 수도권 월간 시간대별 승하차 OA-12252](https://data.seoul.go.kr/dataList/OA-12252/S/1/datasetView.do) | 2026-10, 27개 원본 노선 구분, 622개 노선·역 조합 CSV 실제 확보 | 공개 Sheet CSV 다운로드, API 키 불필요. 월간 합계로 요일별 통계 생성 금지. 공공누리1유형 출처표시 |
| P0 | [9호선 2·3단계 일별 시간대별 승하차 OA-22441](https://data.seoul.go.kr/dataList/OA-22441/F/1/datasetView.do) | 2025년 전체 및 최신 게시 2026-01 CSV 2개 실제 확보 | 13역. 2025년 상충 날짜 키는 원본 행 별도 보존·분석 제외. 공공누리1유형 출처표시 |
| P0 | [코레일 수도권 역간 여객발착](https://www.data.go.kr/data/3037647/fileData.do) | 2024년 연간 OD CSV | 시각·열차·역코드 없음. 동명 라벨 중복 출발역은 분포 계산 제외 |
| P0 | [KRIC 전체 운행정보](https://data.kric.go.kr/rips/M_01_01/detail.do?id=900) / [표준데이터 이용조건](https://www.data.go.kr/data/15013206/standard.do) | XLSX, 수도권 노선코드 행, 정차 목록/시각을 토큰으로 결합 | 비정기. 기준일 2022~2026 혼재. 시행일 미제공. 원본 코드 오기/비수도권 혼입은 매핑 실패로 격리 |
| P1 | [서울교통공사 구간 혼잡도](https://www.data.go.kr/data/15071311/fileData.do) | 2026-09 기준, 역·방향·요일·30분 공표 평균 | 분기. 원표본 수·분위수 없음 |
| P1 | [서울교통공사 4호선 칸별 혼잡도](https://www.data.go.kr/data/15167486/fileData.do) | 2026-06~07 ZIP 안 CSV 전체 | 월간 게시, 일부 신조 편성 한정. 편성번호≠열차번호. 원자료의 호차 방향은 물리적 앞/뒤로 재정의하지 않음 |
| P1 | [환승역 환승인원](https://www.data.go.kr/data/15062858/fileData.do) | 2026-09 역명·평일/토/일 공표 CSV | 시간·노선·방향 없음. 역 복합체 단위 유지 |
| P1 | [월별 환승유입](https://www.data.go.kr/data/15044245/fileData.do) | 2025년 월별·도착역 CSV | 타선 승차 후 자선 하차 인원. 해당 역에서 실제 갈아탄 인원 아님 |
| 제외 | 빠른하차 API | v2 기획에서 제외 | 지도 앱 연결로 대체 |
| P2 | 칸별 승하차 분포 | 수집 불가: 조사 범위 내 공식 원시파일/API 미확인 | 데이터가 존재하지 않는다고 단정하지 않음. 운영기관에 제공 문의 필요 |

[KTDB 자료 신청](https://www.ktdb.go.kr/www/contents.do?key=24)은 추가 조사 경로다. 교통존별 모형 OD를 역간 실측 OD로 바꾸지 않는다. [코레일 월별 시간대 자료](https://info.korail.com/info/selectBbsNttView.do?bbsNo=425&key=867&nttNo=26267)는 2026-09 원본 다운로드를 확인했으나 공공누리4유형(상업적 이용금지·변경금지)이라 추천 데이터 가공에 넣지 않았다. 이용허락 확인 후 사용한다. 서울시 OA-12921 원본도 변경금지 표시가 있어 제한 없음으로 표시된 공공데이터포털 배포본을 사용한다. 출처마다 현재 라이선스를 확인하며 캡차/인증을 우회하지 않는다. 정확한 최신 파일명·다운로드 해시·처리 범위는 실행 manifest가 이 문서보다 우선한다.

## 실행

Python 3.12 이상, 기존 가상환경에서:

```sh
.venv/bin/python -m pip install -r requirements-analysis.txt
# 기존 기준 역목록 준비: 필요할 때만 기존 수집기 실행
.venv/bin/python -m subway_crawler run
# 기본 저장 위치는 저장소 바깥 ../seat-data
.venv/bin/python -m subway_crawler.recommendation run --root ../seat-data
# 네트워크 요청 없이 기존 원본 재가공
.venv/bin/python -m subway_crawler.recommendation process --root ../seat-data
# 갱신 확인만 (실제 다운로드 성공과 구분)
.venv/bin/python -m subway_crawler.recommendation check --root ../seat-data
# 실패 원본 출처 재시도, 이후 가공은 가용 원본 전체에서 재현
.venv/bin/python -m subway_crawler.recommendation run --root ../seat-data --retry-run RUN_ID
# 원본 강제 재검증
.venv/bin/python -m subway_crawler.recommendation run --root ../seat-data --source metro_cars --force
# 이전 기간을 보존하고 자연키별 최신 수정값을 적용한 CSV
.venv/bin/python -m subway_crawler.recommendation export --root ../seat-data --dataset alighting_features --output ../seat-data/alighting_current.csv
```

일부 출처 오류는 다른 출처를 중단시키지 않으며 exit 1과 failures.json으로 보고한다. 코드 해시·원본 해시·기준 역목록 해시가 같으면 기존 완료 스냅샷을 재사용한다. 수집 원본은 메타데이터가 같으면 재다운로드하지 않고 7일(시간표)/30일(통계)에 한 번 내용 재검증한다. `--force`는 이를 무시한다. 수동 조회/다운로드는 초당 0.5요청, 최대 3회 재시도한다. 긴 Retry-After는 다음 실행으로 이월한다.

## 결과 폴더와 추적

- `raw/<source>/<SHA256>.<ext>`: 실제 원본. 해시별 보존, 인접 JSON은 다운로드 출처/시각/메타데이터.
- `evidence/`: 공식 목록 HTML 증거. `state/`: 출처별 마지막 성공 다운로드 포인터.
- `snapshots/<fingerprint>/datasets/`: 정규화 Parquet와 분석 CSV. 행마다 source_id, raw_sha256, collected_at, transform_version 연결.
- `source_row`는 원본 행번호다. 승객유형 합계는 첫 행번호와 역·일·승하차 자연키로 모든 원본 유형 행을 재조회한다. 집계는 명시된 그룹키/관측기간으로 정규화 데이터에 연결한다.
- `latest.json`: 가장 최근 분석 폴더. `snapshots/index.json`: 이력. 이전 원본/스냅샷은 삭제하지 않는다.
- `manifest.json`: 실제 데이터셋 크기/해시/기간/고유키/코드 해시. `quality.json`, `data_dictionary.csv/json`, `station_mapping.csv`, `coverage_matrix.csv`, `recommendation_suitability.csv`, `failures.json`, `report.md/html`을 자동 생성한다.
- `runs/`: 시도/실패/중단/중복 실행 생략 이력. 다음 시작 시 중단된 실행을 식별한다. 파일은 임시 파일 후 rename으로 공개하고 원본은 fsync한다. 원본 파일은 1 GiB 제한, 전체 폴더는 SEAT_MAX_STORAGE_MB(기본 10240 MiB) 도달 시 새 다운로드를 멈추며 이전 자료를 삭제하지 않는다. 분석 출력/한 번의 다운로드로 한도를 초과할 수 있어 디스크 모니터링이 필요하다.

현재 CSV는 분석표 중심이며 수백만 행 정규화본은 Parquet로 보관한다. `export`로 원하는 데이터셋 전체를 CSV로 변환할 수 있다. 누적 export는 자연키가 같은 수정값을 마지막 스냅샷 값으로 바꾸고 이전 기간의 행을 남긴다. 관측기간이 다른 통계는 별도 행으로 보존하며 평균/중앙값을 다시 평균내지 않는다. 원본과 가공 단계의 행 수를 합쳐 고유 데이터 수로 보고하지 않는다. 매우 큰 누적 export는 메모리 내 concat이므로 PostgreSQL 전환이 필요하다.

## 추천 적합성 판정

A 하차: 월별 시간대 합계와 일별 기반 요일 통계를 분리한다. `monthly_alighting_features`는 월간 하차량과 해당 역·월 내 시간대 비중이며 일별 표본 수·평균·중앙값을 생성하지 않는다. 경원선 원본 구분에는 1호선과 경의중앙선 역이 함께 있으므로 두 노선 중 유일하게 일치하는 참조 역만 채택한다. `monthly.py`의 명시적 4개 별칭은 KRIC 참조 라벨과 대조한 것이며, 모든 역 이름에서 역 글자를 제거하지 않는다. 9호선 4126 등 원본 코드는 KRIC 926과 구분해 보존하고 유일한 노선·역명으로 연결한다.

일별 하차: 역·요일·시간대 평균/중앙값/p90/관측일 수. 방향 미상은 null이고 공휴일은 공식 정적 목록/API 캐시로 분류한다. OD는 연간 출발 라벨 조건부 비중이며 코드북 확보 전 역 매핑에 사용하지 않는다.

B 칸: 같은 일·시각·편성·현재→다음역에서 10칸이 모두 관측된 경우만 상대 %p를 계산한다. 표본 관측 수·일 수·편성 수를 제공한다. 8일 미만을 운영상 검토 대상으로 표시하지만 신뢰도나 정확도 점수는 아니다. 일부 편성·노선의 결과를 다른 노선에 적용하지 않는다.

C 경로: 원본 시종착·일반/급행·요일·정차순서·시각과 인접역 시간차. 양끝 역 매핑과 시간차 검증을 통과한 quality_valid 행만 후보 연결로 사용한다. 시간표 시행일 미상, 오래된 기준일, 환승 보행시간 부재 때문에 실서비스 경로 추천 완료를 뜻하지 않는다. 시간표로 차량 회차나 빈 좌석을 확정하지 않는다.

역 식별자는 운영기관+노선+역코드이며 이름만 같다는 이유로 환승역을 합치지 않는다. 괄호 부제 제거 후에도 후보가 여러 개면 격리한다. 별칭/개명/표준 코드 오류는 station_mapping.csv에 남겨 공식 매핑 근거가 추가될 때 수정한다. 전역 상하 방향을 추정하지 않고 원본 상하 값/방향성 구간을 구분한다.

## 예약 실행과 실제 등록 검증

기존 `.github/workflows/collect.yml`의 UTC 00:00/13:00 (= 매일 KST 09:00/22:00, 주말·공휴일 포함)에 추천 출처 메타데이터 확인을 추가했다. `concurrency`와 기존 중복 이벤트 감지·별도 파일 잠금을 유지한다. GitHub 예약은 기본 브랜치에 반영되어야 활성화되며 지연/누락될 수 있어 정확한 시각을 보장하지 않는다. 기존 기본 데이터만 collection-data Git 브랜치에 보관하고, 신규 대용량 원본은 절대로 그 경로에 넣지 않는다.

`.github/workflows/recommendation.yml`은 수동 전체 수집·분석을 지원하며 원본과 결과를 Git 밖 Actions artifact에 14일 보관한다. 이는 영구 저장소가 아니다. 사용자에게 API 승인/저장소가 없는 것을 확인했으므로 유료 저장소를 임의 생성하지 않았다. **신규 대용량 데이터의 무인 장기 보존·갱신은 외부 영구 저장소 또는 항상 켜진 로컬 수집 환경을 구성해야 완료된다.** PR 브랜치의 스케줄 파일만으로 등록/실행 완료라고 보고하지 않는다.

기존 docs/OPERATIONS.md의 launchd/systemd 절차는 그대로 사용할 수 있고 실행 명령을 `python -m subway_crawler.recommendation run --root /절대경로/seat-data --inventory /절대경로/기준목록.csv`로 지정한다. launchd 달력은 macOS 시스템 시간대가 KST여야 하며 PC 전원/절전에 영향을 받는다. 현재 실행에서 새 launchd/systemd를 등록하지 않는다.

## 장기 저장 설계 및 추가 권한

초기에는 Parquet 원본 이력+SQLite 기준 테이블을 사용한다. 장기 운영은 접근 권한이 준비된 객체저장소의 SHA256 경로에 원본을 저장하고 PostgreSQL에 source/raw_version/ingest_run/station_mapping/observation/feature_version을 둔다. 관측 테이블 PK는 출처+위 manifest 자연키, 변경 이력 테이블은 원본해시+자연키이며 트랜잭션으로 최신 포인터와 이력을 함께 갱신한다. 원본 삭제는 자동으로 수행하지 않는다. 저장 공간/보유 정책은 기관 갱신량을 측정한 뒤 결정한다.

필요한 사용자 권한: 외부 저장소 쓰기 권한 및 보존정책, 코레일 제한 자료의 이용허락. 특일정보 API 키는 선택 사항이다. 키는 환경변수/GitHub Secrets/선택한 비밀 저장소에만 넣는다. 신청이나 유료 계정 생성은 대신 하지 않았다. 피드백은 config/feedback.schema.json만 제안했으며 개인정보 수집 기능은 없다.

## 2026-11-09 커버리지 보완 및 다음 권한

이번 추가 CSV 3개는 로그인/API 키 없이 내려받았다. 최신 월은 공개 표의 내림차순 사용월에서 확인하며, 해당 월 CSV만 받고 30일 재검증과 원본 해시 이력을 유지한다. `seoul_monthly_hourly`, `metro9_daily`, `metro9_daily_2025`는 기존 수동 수집·예약 메타데이터 검사 레지스트리에 통합된다. OA-12252의 소개 문구보다 실제 CSV 노선 범위가 넓으므로 확보 범위는 원본 값/매핑 결과로 판정한다. 월간 자료와 기존 일별 자료의 이용객 수를 합산하지 않는다.

`coverage_matrix.csv`는 일별·요일별 근거와 월별 시간대 근거를 별도로 보여 준다. OD/환승 원본이 있다는 이유로 다른 모든 운영기관의 노선을 ‘확인 필요’로 표시하던 문제도 수정했다. 기준 목록과 미연결인 원본은 삭제하지 않는다.

다음 우선순위:

1. **시간대별 OD**: [서울시 빅데이터캠퍼스 공식 목록](https://bigdata.seoul.go.kr/data/selectPageListSampleDataSet.do?currentPage=2&r_id=P213&sample_data_seq=3&sch_text=&sch_type=)에서 ‘지하철 30분단위 출발-도착 데이터’와 ‘30분단위 이용 통계’ 제공을 확인했다(목록 갱신 2026-10-30). 목록에 캠퍼스 방문 후 열람, 홈페이지 전체 다운로드 불가가 명시돼 있다. **원본 미확보/이용 신청·방문·반출 허용 확인 필요**이며 공공 API 키로 해결되는 항목이 아니다. 서울시 스마트카드사 자료의 역 코드북·제공기간·상업 추천용 가공/반출 조건도 함께 확인해야 한다.
2. **공휴일 API 갱신(선택)**: 한국천문연구원 특일정보 활용승인과 `DATA_GO_KR_KEY`. 키가 없어도 출처가 있는 정적 목록으로 가공한다. 빠른하차 API는 v2 정책에서 제외했다.
3. **칸 추천**: 실제 확보된 칸별 관측은 4호선 일부 편성뿐이다. 다른 노선의 칸별 관측·칸별 승하차는 기관 협의 또는 공개 추가 확인이 필요하다. 구간 평균 혼잡도를 칸별 값으로 복제하지 않는다.
4. **잔여 승하차 범위**: 현재 CSV에 없는 인천1·2, 신분당, 김포·용인·의정부, GTX-A 등의 운영기관별 공식 자료를 계속 보완해야 한다. 서울시 API 키 발급이 모든 기관 범위를 보장하지 않는다.

실제 처리 건수/상충 제외/새 커버리지는 `docs/COVERAGE_EXPANSION_2026-11-09.md`와 생성 보고서를 확인한다.
