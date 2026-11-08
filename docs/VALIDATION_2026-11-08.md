# 2026-11-08 실제 수집 및 검증

스냅샷 `10e989503b4e6873d3e0`. 원본 8종 다운로드 및 가공 성공. 원본은 Git 저장소 밖에 보관.

| 데이터셋 | 행 수 |
|---|---:|
| historical_ridership_daily_wide | 199,424 |
| historical_ridership_hourly | 3,988,480 |
| historical_station_weekday_patterns | 75,320 |
| historical_alighting_features | 37,660 |
| ridership_passenger_types | 773,508 |
| ridership_daily_wide | 98,826 |
| ridership_hourly | 1,976,520 |
| station_weekday_patterns | 75,600 |
| alighting_features | 37,800 |
| od_annual | 244,244 |
| od_destination_distribution | 243,012 |
| segment_congestion | 65,169 |
| car_observations | 2,658,277 |
| car_features | 60,110 |
| transfer_demand | 219 |
| transfer_inflow_monthly | 3,432 |
| timetable_stops | 577,057 |
| route_features | 555,712 |

기존 기능 포함 로컬 테스트 24개 통과. 재실행에서 원본 8종을 재다운로드하지 않았고, 동일 코드/원본의 가공 스냅샷 재사용 확인. OD 중복 라벨 1,232행의 출발역 2개는 목적지 분포 계산에서 제외. 역 매핑 불가/시간차 검증 실패 경로 연결은 quality_valid=false.

기존 GitHub collect.yml은 active이며 2026-11-08 예약 실행 성공 이력(run 42447347985)을 확인했다. 이 기록은 신규 추천 기능 배포 전 기본 수집기 실행이다. 신규 워크플로 배포/실행 검증은 PR과 Actions 실행 기록에서 별도로 확인한다.

미완료: 전체 수도권·전체 기간 완전 커버리지, 공휴일 통계, OD 코드북, 시간표 시행일, 칸별 승하차 분포, 빠른하차 API 인증, 외부 영구 저장소. 코레일 월별 제한 라이선스 자료는 가공 제외. 좌석 확률이나 추천 정확도 점수는 생성하지 않았다.
