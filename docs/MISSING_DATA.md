# 미확보 데이터 증분 수집 (2026-11-22)

**2026-11-25 변경:** 사용자 요청으로 P1 신규 불편 사례 수집을 중단했다. 등록 입력의 추가 반입과 실패 재시도에서도 제외하며 기존 자료는 보존한다. P2는 P0 완료 후 진행하며 P1 완료를 기다리지 않는다. 아래의 11월 22일 수집 결과는 당시 기록이다.

기존 데이터를 읽기 전용으로 확인한 뒤 신규 보완 파일만 수집한다. 과거 전체 수집기 코드는 보존하며 새 진입점에서 호출하지 않는다. n8n, 개인정보 수집, 접근 제한 우회는 없다.

## 2026-11-25 공지 API 실수집 검증

제공된 키를 GitHub Secret `DATA_GO_KR_SERVICE_KEY`에 등록하고 실제 API에서 HTTP 200 / NORMAL_CODE 응답을 확인했다. 최근 7일 조회에서 11월 20일 성수지선 장애·운행 재개 공지 2건을 수집했다. 11월 25일 현재의 장애 상태를 의미하지 않는다. 기존 Actions 실행 45442784292는 성공했고 CSV·SQLite 상태 저장까지 완료했다. P1은 STOPPED_BY_USER로 건너뛰었다.

같은 서버 실행에서 KRIC 원본 메타데이터 확인도 성공했다. 시간표 관련 누적 4행은 공식 공지 2건과 서로 다른 출처의 갱신 메타데이터 2건이며, 시간표 시행일 4건을 확보했다는 뜻이 아니다. 실제 시간표 개정·시행일, 중간 종착 판정, 환승 코드 검증, 타기관 공지는 계속 미완료다. 09:00/22:00 cron 복구는 이 API 키 등록에 포함되지 않는다.

API 수집기는 원본 JSON의 콘텐츠 해시와 페이지·행 번호를 보존하고 역 구간 코드·알림 구분·무정차 여부를 그대로 남긴다. 과거 공식 공지 표시에는 활용할 수 있지만 열차별 영향·종료 시점을 임의로 추정하지 않는다. `notice_display_status`와 `departure_planning_status`, `current_operation_status`를 구분한다. 기존 공지 식별 기준을 유지해 가공 필드 추가로 중복 레코드가 생기지 않도록 검증한다.

## 실제 확인 결과

현재 seat-evidence-v2 스냅샷의 CSV/Parquet를 직접 읽어 행 수·컬럼·SHA-256을 확인했다. 두 포맷의 행 수는 합산하지 않는다. 과거 아카이브는 존재 확인과 현재 데이터 검증을 구분한다.

| 기존 데이터 | 확인 행 수 | 처리 |
|---|---:|---|
| 열차 정차 기록 | 435,464 | 재수집 없음; 막차 후보 파생 |
| 역·일·시간대 승하차 | 1,976,520 | 재수집 제외 |
| 월·시간대 승하차 | 29,856 | 재수집 제외 |
| 코레일 OD | 244,244 | 재수집 제외 |
| 구간 혼잡도 | 65,169 | 재수집 제외 |
| 4호선 칸별 관측 | 2,658,277 | 재수집 제외 |
| 환승 수요 / 월 유입 | 219 / 3,432 | 실제 환승 연결과 구분 |
| 기존 불편 사례 | 79 | URL 및 알려진 URL 별칭으로 중복 제외 |
| 인천 승하차·혼잡도 | NOT_VERIFIED | 파일 부재를 미수집으로 단정하지 않음 |

## 신규 확보와 한계

| 출처 | 실제 처리 | 서비스 사용 판정 |
|---|---|---|
| [서울교통공사 환승 거리·시간](https://www.data.go.kr/data/15044419/fileData.do) | CSV 145행 다운로드 | NOT_USABLE: 카탈로그 기준일 2026-08-31, 파일명 2025-12-31 충돌. 145개 고유 역을 뜻하지 않음 |
| [서울 도시철도 환승정보](https://www.data.go.kr/data/15098252/fileData.do) | CSV 1,024행 다운로드 | NOT_USABLE: 카탈로그/파일 날짜 차이, 환승종료역 코드의 의미·시간 단위·노선 매핑 미확인 |
| [도시철도 운행정보 표준 카탈로그](https://www.data.go.kr/data/15013206/standard.do) | 갱신일 메타데이터 1행 | NOT_USABLE: 원본 시간표 시행일·개정 공지가 아님 |
| 기존 KRIC 시간표 상세 메타데이터 | 네트워크/TLS 실패, 제한 재시도 | 갱신 여부 미확인. 시간표 전체 다운로드하지 않음 |
| [서울교통공사 지하철알림정보](https://www.data.go.kr/data/15144070/openapi.do) | API 어댑터 구현, 실호출 미검증 | 활용승인 및 키 필요 |
| 기타 운영기관 공지 | 자동 수집 허용·구조 미검증 | 서울교통공사 데이터로 타기관 범위 대체 안 함 |
| 신규 불편 사례 | 기존 79건과 중복 없는 최소 발췌 3건 | RESEARCH_ONLY: 본문 대조 1건, 검색 발췌만 2건; 게시일 불명 1건 |
| 시설·출구 P2 | 미실행 | P0/P1 미완료 우선순위 게이트 |

145행의 시간은 공식 설명의 거리/1.2m/s와 대조하여 mm:ss 해석을 검사했다. 개인 보행속도, 대기시간, 혼잡·교통약자 접근성을 보장하지 않는다. 상세 환승정보의 종료역 코드가 서울역에서 숙대입구처럼 방면상 인접역인 사례가 있어 같은 이름의 환승역으로 임의 결합하지 않는다.

기존 시간표에서 **막차 후보 9,580건**을 파생했다. 역·요일·다음 정차역·종착역·기준일별 최종 출발 후보이며 전체 경로의 막차 보장이 아니다. 열차 내 정차 순서로 자정 통과를 처리하고 종착역 도착은 승차 출발에서 제외한다. 00시 시작 열차처럼 전날 운행을 입증할 수 없으면 운행일을 채우지 않는다. 시행일·현행성은 미확인이며 전부 NOT_USABLE이다. 중간 종착 여부는 임의로 만들지 않았다. 42,470개 출발 후보 행은 시각·원본 품질·매핑 문제로 제외했다.

## 실행과 파일

```sh
python -m pip install -r requirements-analysis.txt
# 최초 1회: 기존 파일을 읽고 검증/파생만 수행, 새 출력은 data/에 저장
python -m subway_crawler.missing_data.pipeline --prepare --baseline-root ../seat-data
# 이후: 보존된 검증 인벤토리를 기준으로 증분 수집
python -m subway_crawler.missing_data.pipeline
python -m subway_crawler.missing_data.pipeline --retry-failed
python -m subway_crawler.missing_data.pipeline --offline
python -m unittest discover -s tests -v
```

`--prepare` 없는 실행은 기존 검증 결과를 재사용한다. 실행 호스트에 원본이 마운트되지 않았다고 파일 부재를 미수집으로 판단하지 않는다. 실행별 인벤토리에는 NOT_VERIFIED와 prior_verification을 함께 기록한다. 원본을 새로 검증하려면 원본이 있는 로컬에서 --prepare를 실행한다. baseline.json이 없으면 신규 다운로드도 막는다.

- `data/reports/existing_data_inventory.csv`: 파일·컬럼·실측 행 수·기존 검증 상태
- `data/reports/existing_files_unchanged.csv`: 기존 파일 변경 없음 SHA 검증
- `data/reports/data_gap_analysis.csv`: 실제 누락/미검증/파생 구분
- `data/reports/source_registry.csv`: 출처·갱신 주기·권한·실제 성공/실패
- `data/reports/data_quality_report.csv`, `quality_summary.csv`: 행별 검증·집계
- `data/reports/collection_summary.json`, `runs/*.json`: 실행별 신규·변경·누적·실패
- `data/reports/collection_failures.csv`: retryable / schema / user_action
- `data/reports/mvp_assessment.csv`: PASS / FAIL / NOT_VERIFIED
- `data/new/{last_train_supplement,transfer_connections,timetable_updates,new_user_pain_points,station_facilities}.csv`: 신규 수집분만 누적. 미확보 데이터는 헤더만 존재
- `data/derived/last_train_candidates.csv`: 기존 시간표 파생값 (신규 수집 아님)
- `data/quarantine/`: 사용 불가 행을 별도 보관
- `data/acquisition/`: 신규 원본, 원본 해시, 공식 페이지 증거, 수집 영수증
- `data/collection.sqlite`: 현재 레코드·변경 버전·출처 체크포인트·실행 이력

기존 파일을 수정하지 않고 새 출력은 Git 코드 브랜치에서 제외한다. 실제 신규 자료만 기존 collection-data 브랜치의 `missing-data/`에 보관하며 기존 `runtime/`는 건드리지 않는다. 현재 신규 원본은 작은 CSV 두 개다. 250MB 저장 한도 도달 시 추가 수집을 중단하고 사용자 조치를 기록한다. 용량이 늘면 외부 저장소로 이 경로를 옮겨야 하며 기존 대용량 원본을 Git에 복제하지 않는다.

## 기존 Actions 연동

기존 `.github/workflows/collect.yml`만 미확보 수집기로 교체했다. 기존 추천 전체 수집 workflow는 비활성 상태를 유지한다. 불편 사례 재분석 workflow는 변경하지 않는다.

- 기존 예약이 이미 제거·비활성화되어 있었으므로 새 cron을 임의로 만들지 않았다. 현재 workflow_dispatch 수동 실행만 연결한다. 09:00/22:00 예약 복구는 사용자 답변을 기다린다.
- 복구 승인 시 기존 workflow에 UTC `0 0 * * *`, `0 13 * * *`를 추가한다. 각각 KST 09:00, 22:00이며 GitHub Actions는 지연/누락 가능성이 있어 정확한 시각을 보장하지 않는다. 작업 시간대 TZ는 Asia/Seoul이다.
- 공유 concurrency group으로 상태 쓰기를 직렬화하고, 이벤트 생성 시각이 직전 실행 종료보다 이전이면 대기 중 중복 요청을 건너뛴다. 로컬은 fcntl 잠금으로 중복 실행을 막는다. 명시적 실패 재시도는 허용한다.
- API 키는 GitHub Secrets `DATA_GO_KR_SERVICE_KEY`에 등록한다. 코드·원본·로그에 키를 저장하지 않는다. 승인 대상은 15144070 지하철알림정보다. API는 문서 기반 구현이며 인증 전 실호출을 성공했다고 주장하지 않는다.
- 환승 파일은 주 1회 메타데이터 점검, 변경 없으면 다운로드하지 않는다. KRIC는 12시간, 표준 카탈로그는 24시간 점검한다. 공지는 승인 후 최대 7일 중첩 조회하고 중복 제거하며, 100페이지 안전 한도 초과 시 체크포인트를 전진시키지 않는다. 공급자의 과거 공지 보관 범위는 실호출 검증이 필요하다.
- 네트워크 제한 재시도 3회, 요청 간격과 Retry-After 준수, CAPTCHA/인증/라이선스 변경 시 중단한다.
- 실패 출처는 독립 처리한다. Actions 성공은 프로세스 완료이며 전체 수집 성공을 뜻하지 않는다. collection_summary의 partial과 실패 목록을 확인한다.
- SQLite 트랜잭션·WAL·원자적 파일 저장으로 중단 시 복구한다. DB에서 CSV를 다시 내보낼 수 있다. 변경 전후 버전은 보존한다. 로그는 2MB×최대 4개이며 Actions 산출물 보존은 90일이다.

## P1 자동화 범위와 남은 조건

이번 실행은 공개 검색으로 확인한 최소 인용 3건을 `config/missing_pain_points.csv`에 등록하고, 기존 URL/별칭과 비교하여 누적했다. 이름·계정 ID는 저장하지 않았다. 검색 발췌를 본문 검증으로 승격하지 않는다. 이 설정은 승인된 입력 재처리용이며 **새 게시물을 무인으로 계속 검색하는 기능은 아직 연결되지 않았다**. 자동 검색의 저장/이용 조건을 충족하는 검색 API 또는 공식 허용 피드 확인이 필요하다. 과거 채팅의 Naver 키를 코드에 재사용하거나 저장하지 않았다.

MVP 현재 판정: 기존 snapshot 내 열차 연결 PASS, 출발 시각 역산 FAIL, 환승 소요시간 계산 FAIL, 현재 막차 안내 FAIL, 전 운영기관 최신성 NOT_VERIFIED. 수집·파생 성공을 서비스 배포 가능으로 해석하지 않는다. 우선 해결할 항목은 시간표 시행일 확인, 환승 코드 정의/날짜 충돌 확인, 공지 API 승인, 타기관 공지 이용 조건, P1 자동 검색 이용 조건 순서다.
