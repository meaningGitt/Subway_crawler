import argparse
import json
from pathlib import Path
from .pipeline import run
from .store import Store


def main():
    parser = argparse.ArgumentParser(description='수도권 지하철 정기 데이터 수집')
    parser.add_argument('command', choices=['run', 'export', 'status'])
    parser.add_argument('--root', default='runtime')
    parser.add_argument('--config', default='config/sources.json')
    parser.add_argument('--source', action='append')
    parser.add_argument('--retry-run')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--slot', choices=['0900', '2200', 'manual'], default='manual')
    parser.add_argument('--scheduled-at', help='실제 스케줄러 이벤트 생성 시각(ISO 8601, timezone required)')
    args = parser.parse_args()
    if args.command == 'run':
        config = json.loads(Path(args.config).read_text())
        if config.get('timezone') != 'Asia/Seoul':
            parser.error('timezone must be Asia/Seoul')
        return run(args.root, config, args.source, args.retry_run, args.force, args.slot, args.scheduled_at)
    store = Store(args.root)
    try:
        if args.command == 'export':
            store.export()
        else:
            for row in store.db.execute('SELECT id,started_at,ended_at,status FROM runs ORDER BY started_at DESC LIMIT 10'):
                print(json.dumps(dict(row), ensure_ascii=False))
    finally:
        store.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
