# 수도권 지하철 정기 수집기

매일 **09:00 / 22:00 Asia/Seoul**, 주말·공휴일 포함 GitHub Actions에서 실행합니다. 수동 실행, 실패 출처 재실행, 원본 보존, 변경 이력, CSV, 품질 검사와 보고서를 제공합니다.

## 실행

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m subway_crawler run
.venv/bin/python -m subway_crawler status
.venv/bin/python -m subway_crawler run --retry-run <보고서의-run-id>
.venv/bin/python -m subway_crawler run --source kric_stations --force
.venv/bin/python -m subway_crawler export
.venv/bin/python -m unittest discover -s tests -v
```

기본 저장 위치는 `runtime/`입니다. `--root`로 변경할 수 있습니다. GitHub에서는 `collection-data` 브랜치의 `runtime/`에 영구 누적하고, 실행별 다운로드용 artifact도 제공합니다.

- `raw/<source>/<sha256>.xlsx`: 변경된 원본을 콘텐츠 주소로 보존(XLS 코드목록도 확장자는 보관용 `.xlsx`, 파서는 시그니처로 구분).
- `collection.sqlite`: 최신 레코드·모든 변경 버전·출처 상태·실행 이력.
- `exports/`: 출처별 평탄화 CSV와 전체 레코드/변경 이력 CSV(UTF-8 BOM).
- `reports/daily/YYYY-MM-DD_0900.md`, `…_2200.md`: 예약 회차별 Markdown 및 JSON 보고서.
- 같은 회차 재실행은 run ID 접미사를 붙여 기존 보고서를 보존합니다.
- `reports/skipped/`: 중복 실행을 건너뛴 기록.

## 데이터 범위

특정 노선을 선택하는 수집기가 아닙니다. 전국 표준 원본에서 수도권 노선 코드(11/28/41/44) 전체와 해당 노선의 수도권 외 연장 구간을 가져옵니다. 별도 공식 역 코드 목록에서 수도권 전체 운영기관을 수집해 GTX-A 등 표준 원본 누락을 보완합니다.

| 등록 출처 | 내용 | 갱신 확인 |
|---|---|---|
| `kric_stations` | 전체기관 도시철도 역사: 역·노선·운영기관·좌표·주소·기준일 | 제목·수정일·다운로드 URL |
| `kric_korail_lines` | 한국철도공사 노선: 기종점·정거장 구성·노선연장 | 제목·수정일·다운로드 URL |
| `kric_station_inventory` | 최신 공식 운영기관·노선·역 코드 목록, GTX-A 포함 | 자료실 최신 게시물과 첨부파일 자동 발견 |

**노선 존재 확인은 모든 역·시설·시간표의 완전성을 증명하지 않습니다.** 기준 24개 노선의 확보 여부와 원본 결측·이상값을 보고합니다. 역사 상세와 코드 목록은 서로 다른 데이터 상품이므로 별도로 보존하며, 두 출처의 건수를 물리적 역사 수로 합산하지 않습니다. 원본은 실시간 열차 운행정보가 아닙니다. 공식 원본에 없는 좌표나 기준일은 만들지 않습니다.

[출처와 품질 기준](docs/DATA.md) · [설치·운영·복구](docs/OPERATIONS.md) · [구현·검증 기록](docs/IMPLEMENTATION.md)

## 좌석 추천 데이터 수집·분석 추가

실제 승하차·OD·시간표·구간/칸 혼잡도·환승 자료를 수집하고 검증하는 방법은
[추천 데이터 계획과 운영](docs/RECOMMENDATION_DATA.md)을 참조하세요.
`python -m subway_crawler.recommendation run`은 대용량 원본을 저장소 밖 `../seat-data`에 보관하며,
데이터 사전·품질·커버리지·HTML 분석 보고서를 생성합니다.
기존 09:00/22:00 KST 예약에는 새 출처의 갱신 확인을 연결했습니다. 전체 대용량 수집의 장기 자동 보존은 영구 저장소 설정이 필요합니다.

2026-11-09 보완: 서울시 월간 시간대별 승하차(코레일 구간·공항철도·9호선 포함)와
9호선 2·3단계 일별 자료를 공개 CSV로 추가했습니다. API 키 없이 수집합니다.
[커버리지 확장 검증](docs/COVERAGE_EXPANSION_2026-11-09.md)에서 전후 차이와 남은 범위를 확인할 수 있습니다.
월간 합계와 일별 요일 통계는 별도로 사용해야 합니다.

2026-11-10 v2: 시간표 canonical 버전, 역·지선 매핑, 공휴일 제외 통계, 구간 혼잡도 감소와 열차별 시각 결합, 월간 평일 보정 추정을 추가했습니다.
[실행·해석](docs/v2/IMPLEMENTATION.md) · [실제 검증 결과](docs/v2/RESULTS.md).
