# 설치 및 운영

## GitHub Actions (선택한 실제 운영 환경)

`collect.yml`은 기본 브랜치에서 매일 UTC 00:00와 13:00 (= Asia/Seoul 09:00/22:00)에 실행합니다. 프로세스 TZ는 Asia/Seoul, Python 보고서 시각도 명시적 ZoneInfo입니다. [GitHub 공식 schedule 문서](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)에 따라 예약 실행은 기본 브랜치에 워크플로가 있어야 하고, 혼잡 시 지연되거나 드롭될 수 있습니다. 정확한 09:00/22:00 실행을 보장하지 않습니다. 공개 저장소는 60일 활동이 없으면 예약이 비활성화될 수 있으며, 활동 조건과 Actions 정책을 점검해야 합니다. collection-data의 정기 push만으로 이 정책을 피한다고 가정하지 않습니다.

처음 설치 시 main과 collection-data 브랜치가 필요합니다. PR 머지 후 Actions에서 `Metropolitan subway collection`을 수동 실행하여 실제 네트워크·권한·누적 보관을 검증합니다. 저장소 Actions 정책이 checkout/setup-python/upload-artifact를 허용해야 합니다. GITHUB_TOKEN에는 contents:write, actions:read가 필요합니다. 소스 API 키는 필요 없습니다.

Actions → 워크플로 → Run workflow에서 전체 실행, force, retry_run을 선택할 수 있습니다. retry_run은 보고서의 수집 ID이며 GitHub run 번호와 다릅니다. Re-run jobs는 해당 워크플로 전체 재실행이며 수집 자체는 멱등적입니다.

동일 concurrency group으로 수동/예약 작업을 직렬화하고 활성 작업을 취소하지 않습니다. 대기 중이던 이벤트 생성 시각이 이전 실행 종료보다 이르면 실제 수집을 생략하고 reports/skipped에 기록합니다. GitHub의 기본 concurrency 정책은 pending 실행을 최대 하나 유지하여 더 오래 대기 중인 실행이 교체될 수 있습니다. 이것도 정확한 실행 횟수를 보장하지 못하는 이유입니다. 로컬 실행에는 별도의 비차단 flock을 사용하며 이전 프로세스가 죽으면 OS가 잠금을 해제합니다.

**저장**: collection-data/runtime은 누적 DB·원본·이력·보고서를 Git 커밋으로 보존합니다. source 실패 시에도 always 단계에서 DB integrity_check와 WAL checkpoint 후 저장합니다. 원격 push 실패는 workflow 실패로 표시하고 복구 snapshot artifact를 90일 보존합니다. artifact는 영구 저장의 유일한 사본이 아닙니다. 러너 강제 폐기 직전의 미push 데이터는 유실될 수 있으나 이전 커밋은 유지되며 재수집으로 복구합니다.

## 로컬 macOS launchd

GitHub와 동시에 같은 운영 DB를 쓰지 마세요. 로컬 검증용 별도 DB는 가능합니다. Python 3.12+ 및 README의 venv 설치 후:

```sh
.venv/bin/python scripts/launchd_install.py
mkdir -p "$HOME/Library/LaunchAgents"
cp deploy/kr.subway.collector.plist "$HOME/Library/LaunchAgents/"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/kr.subway.collector.plist"
launchctl print "gui/$(id -u)/kr.subway.collector"
# 즉시 수동 검증
launchctl kickstart "gui/$(id -u)/kr.subway.collector"
```

launchd의 StartCalendarInterval은 **macOS 시스템 시간대**를 따릅니다. TZ 환경변수만으로 달력 시간대를 바꿀 수 없습니다. 설치 생성기는 /etc/localtime이 Asia/Seoul인지 검사하고 다르면 중단합니다. 시스템 시간대를 변경하면 다시 검증해야 합니다. 로그인 세션과 PC 전원/절전 상태에 영향을 받습니다. 전원이 꺼져 있으면 정확한 시각에 실행할 수 없습니다. 이 작업에서는 GitHub를 선택했으므로 launchd를 실제 등록하지 않습니다.

## 서버 systemd / cron

`/opt/subway-crawler`에 checkout과 venv를 설치하고 전용 subway 사용자를 생성합니다. `/var/lib/subway-crawler`를 subway 소유로 생성한 뒤:

```sh
sudo cp deploy/subway-collector.service deploy/subway-collector.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now subway-collector.timer
systemctl list-timers subway-collector.timer
systemd-analyze calendar '*-*-* 09:00:00 Asia/Seoul'
sudo systemctl start subway-collector.service
journalctl -u subway-collector.service
```

Persistent=true는 서버 중단 중 빠진 일정을 복구하지만 과거 모든 회차를 재생하지는 않습니다. service가 실행 중이면 timer는 같은 service를 중복 시작하지 않습니다. 동시 수동 실행은 Python 잠금으로 로그를 남기고 생략됩니다.

cron 대안은 deploy/crontab.example을 실제 경로에 맞춰 crontab에 등록하고 `crontab -l`로 확인합니다. **CRON_TZ 지원 cron에서만 사용**하며 macOS cron에서 같은 동작을 가정하지 않습니다. 미지원 서버는 systemd를 사용합니다. cron 외부 stdout과 launchd stdout은 OS logrotate/newsyslog를 적용해야 합니다.

## 실패/중단/용량

- 출처별 오류: 보고서의 source/error, collection.log를 확인하고 --retry-run으로 실패/pending 출처만 재실행합니다.
- 인증/HTML/열 구조 변경: 원본이 raw에 남아 있으면 검토하고 adapter/config 수정 후 --force 재실행합니다. 임의로 필수 열 검사를 끄지 않습니다.
- SIGTERM/SIGINT: 실행을 interrupted로 기록하고 완료한 출처를 보존합니다. SIGKILL은 즉시 기록할 수 없어 다음 시작 시 이전 running을 interrupted로 표시합니다. 원본/보고서는 임시 파일+fsync+rename, DB는 WAL+FULL 트랜잭션을 사용합니다.
- 핵심 로그는 5 MB × 6개로 회전합니다. 원본·이력은 자동 삭제하지 않습니다. MAX_STORAGE_MB(기본 1024 MiB)를 넘으면 수집을 멈추고 보고서에 이관 필요를 기록합니다. 쓰기 한 건으로 한도를 소폭 초과할 수 있습니다.
- Git 이력 자체는 이 용량 한도에 포함되지 않습니다. 장기 운영은 저장소/Git 크기도 점검하고 PostgreSQL+객체저장소로 이전합니다. 원본·이력 삭제나 git history rewrite는 자동 실행하지 않습니다.
- 원격 push 장애: 해당 run artifact의 DB·CSV·보고서로 복구하고 마지막 collection-data 커밋과 대조합니다. 코드 버전은 workflow run의 commit SHA로 확인합니다.
