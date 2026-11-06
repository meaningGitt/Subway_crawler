from __future__ import annotations

import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .source import Client, SourceError, digest, encoded, metadata, parse_xlsx, inventory_metadata, parse_inventory
from .store import Store, atomic_write

KST = ZoneInfo('Asia/Seoul')


def now():
    return datetime.now(KST).isoformat(timespec='seconds')


def collect(source, store, run_id, force=False, client=None):
    if not source.get('automation_allowed') or not source.get('license'):
        raise SourceError('license/automation permission not registered')
    client = client or Client(source.get('request_interval_seconds', 2))
    state = store.state(source['id'])
    _, _, page = client.get(source['metadata_url'])
    meta = inventory_metadata(page, source, client) if source['kind'] == 'inventory' else metadata(page, source)
    at = now()
    due = not state.get('last_downloaded') or datetime.fromisoformat(at) - datetime.fromisoformat(state['last_downloaded']) >= timedelta(days=source.get('revalidate_days', 7))
    downloaded = False
    unchanged = state.get('version') == meta['version'] and not due and not force
    if unchanged and (store.root / state.get('raw_path', 'missing')).is_file():
        body = (store.root / state['raw_path']).read_bytes()
    else:
        headers = {}
        if not force and state.get('etag'):
            headers['If-None-Match'] = state['etag']
        if not force and state.get('last_modified'):
            headers['If-Modified-Since'] = state['last_modified']
        status, response_headers, body = client.get(meta['url'], headers=headers)
        if status == 304:
            if not state.get('raw_path') or not (store.root / state['raw_path']).is_file():
                raise SourceError('304 returned but no local raw snapshot; retry with --force')
            body = (store.root / state['raw_path']).read_bytes()
        else:
            downloaded = True
        raw_path = store.raw(source['id'], body, '.xlsx')
        state.update(raw_path=raw_path, last_downloaded=at,
                     etag=response_headers.get('ETag'), last_modified=response_headers.get('Last-Modified'))
    # Retain raw bytes before parsing, including malformed inputs for diagnosis.
    records, quality = parse_inventory(body) if source['kind'] == 'inventory' else parse_xlsx(body, source['kind'])
    old_count = store.count(source['id'])
    if old_count and len(records) < old_count * 0.7:
        raise SourceError('row count fell more than 30%; raw retained, promotion blocked')
    quality['schema_changed'] = bool(state.get('columns') and state['columns'] != quality['columns'])
    quality['data_updated'] = state.get('content_hash') != digest(body)
    quality['source_modified_at'] = meta['modified']
    quality['source_title'] = meta['title']
    reference_dates = sorted(r['reference_at'] for r in records if r.get('reference_at'))
    quality['reference_date_min'] = reference_dates[0] if reference_dates else None
    quality['reference_date_max'] = reference_dates[-1] if reference_dates else None
    quality['source_age_days'] = (datetime.fromisoformat(at).date() - datetime.fromisoformat(meta['modified']).date()).days
    quality['unmapped_lines'] = sorted({r['line'] for r in records if r['line'] not in EXPECTED_LINES})
    with store.db:
        added, changed = store.merge(source['id'], records, run_id, at)
        state.update(version=meta['version'], columns=quality['columns'], content_hash=digest(body),
                     last_checked=at, metadata=meta, quality=quality)
        store.save_state(source['id'], state)
    return {'source': source['id'], 'status': 'success', 'new': added, 'changed': changed,
            'total': store.count(source['id']), 'downloaded': downloaded, 'quality': quality}


EXPECTED_LINES = set()


def report(store, result, slot, scheduled_at):
    started = datetime.fromisoformat(scheduled_at or result['started_at']).astimezone(KST)
    stamp = started.strftime('%Y-%m-%d') + '_' + (slot if slot != 'manual' else started.strftime('%H%M%S') + '_manual')
    path = store.root / 'reports' / 'daily' / (stamp + '.md')
    if path.exists():
        path = path.with_name(path.stem + '_' + result['id'] + '.md')
    lines = [f"# 수도권 지하철 수집 보고서 — {stamp}", '',
             f"- 실행 ID: {result['id']}", f"- 시간대: Asia/Seoul",
             f"- 예약 기준: {scheduled_at or '수동 실행'} / 회차 {slot}",
             f"- 시작: {result['started_at']}", f"- 종료: {result['ended_at']}",
             f"- 결과: {result['status']}", f"- 신규: {result['new']} / 변경: {result['changed']} / 누적: {result['total']}", '',
             '| 출처 | 결과 | 신규 | 변경 | 누적 | 다운로드 |', '|---|---|---:|---:|---:|---|']
    for s in result['sources']:
        lines.append(f"| {s['source']} | {s['status']} | {s.get('new', 0)} | {s.get('changed', 0)} | {s.get('total', 0)} | {s.get('downloaded', False)} |")
    lines += ['', '## 품질 및 조치 필요 사항', '',
              '누적 건수는 출처별 고유 레코드 수의 합입니다. 환승역의 노선별 레코드와 코드목록은 별개이며 물리적 역사 개수와 다릅니다.',
              '원본 미제공 노선은 확보된 것으로 간주하지 않습니다. 삭제된 원본 행은 기존 데이터에서 보존합니다.', '',
              '```json', json.dumps({'coverage': result.get('coverage', {}), 'sources': result['sources'],
                                    'error': result.get('error')}, ensure_ascii=False, indent=2), '```', '',
              '실패 재실행: `python -m subway_crawler run --retry-run ' + result['id'] + '`',
              '출처·스키마·값 오류는 config/sources.json 및 docs/OPERATIONS.md를 확인하세요.']
    atomic_write(path, '\n'.join(lines) + '\n')
    atomic_write(path.with_suffix('.json'), encoded(result))
    return str(path)


def run(root, config, source_ids=None, retry_run=None, force=False, slot='manual', scheduled_at=None):
    global EXPECTED_LINES
    EXPECTED_LINES = set(config['expected_lines'])
    root = Path(root); root.mkdir(parents=True, exist_ok=True)
    lock = (root / '.collection.lock').open('a+')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Unique log files avoid writing to the active process's DB or rotating log.
        path = root / 'reports' / 'skipped' / f'{uuid.uuid4().hex}.json'
        atomic_write(path, encoded({'status': 'skipped_overlap', 'at': now()}))
        lock.close()
        print('skipped_overlap: another collector owns the lock')
        return 0
    logger = logging.getLogger('subway')
    handler = RotatingFileHandler(root / 'collection.log', maxBytes=5_000_000, backupCount=5, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger.addHandler(handler); logger.setLevel(logging.INFO)
    store = None
    try:
        # Actions serializes jobs. A queued invocation created during the previous run is logged and skipped.
        last_file = root / 'last_finished.json'
        if scheduled_at and last_file.exists():
            last = json.loads(last_file.read_text())
            if datetime.fromisoformat(scheduled_at) < datetime.fromisoformat(last['ended_at']):
                record = {'status': 'skipped_overlap', 'at': now(), 'scheduled_at': scheduled_at, 'previous': last}
                atomic_write(root / 'reports' / 'skipped' / f'{uuid.uuid4().hex}.json', encoded(record))
                logger.info('skipped_overlap queued invocation %s', scheduled_at)
                return 0
        store = Store(root)
        sources = config['sources']
        if retry_run:
            exists = store.db.execute('SELECT 1 FROM runs WHERE id=?', (retry_run,)).fetchone()
            if not exists:
                raise ValueError('unknown retry run ID')
            source_ids = [r[0] for r in store.db.execute("SELECT source FROM source_runs WHERE run_id=? AND status!='success'", (retry_run,))]
        if source_ids is not None:
            unknown = set(source_ids) - {s['id'] for s in sources}
            if unknown:
                raise ValueError('unknown source IDs')
            sources = [s for s in sources if s['id'] in source_ids]
        result = {'id': uuid.uuid4().hex[:12], 'started_at': now(), 'sources': [], 'new': 0, 'changed': 0}
        with store.db:
            store.db.execute("UPDATE runs SET status='interrupted',ended_at=? WHERE status='running'", (now(),))
            store.db.execute('INSERT INTO runs VALUES (?,?,?,?,?)', (result['id'], result['started_at'], None, 'running', '{}'))
            for s in sources:
                store.db.execute('INSERT INTO source_runs VALUES (?,?,?,?)', (result['id'], s['id'], 'pending', '{}'))
        logger.info('run started %s', result['id'])
        exit_code = 0
        previous_handler = signal.getsignal(signal.SIGTERM)
        def interrupted(signum, frame):
            raise KeyboardInterrupt('termination signal')
        signal.signal(signal.SIGTERM, interrupted)
        try:
            for source in sources:
                try:
                    used = sum(p.stat().st_size for p in root.rglob('*') if p.is_file())
                    if used > int(os.getenv('MAX_STORAGE_MB', '1024')) * 1024 * 1024:
                        raise SourceError('storage budget exceeded; archive or migrate storage before retry')
                    item = collect(source, store, result['id'], force=force)
                except Exception as exc:
                    # Never emit arbitrary request exceptions containing auth/query values.
                    message = str(exc) if isinstance(exc, SourceError) else f'internal error: {type(exc).__name__}'
                    item = {'source': source['id'], 'status': 'failed', 'error': message}
                    exit_code = 1
                    logger.error('%s %s', source['id'], message)
                result['sources'].append(item)
                result['new'] += item.get('new', 0); result['changed'] += item.get('changed', 0)
                with store.db:
                    store.db.execute('UPDATE source_runs SET status=?,payload=? WHERE run_id=? AND source=?',
                                     (item['status'], encoded(item), result['id'], source['id']))
            result['status'] = 'partial_failure' if exit_code else 'success'
        except KeyboardInterrupt:
            result['status'] = 'interrupted'; result['error'] = 'process interrupted; retry pending/failed sources'
            exit_code = 130
        finally:
            signal.signal(signal.SIGTERM, previous_handler)
            coverage = {r[0]: r[1] for r in store.db.execute("SELECT line,count(*) FROM records WHERE source IN (SELECT source FROM source_state) AND source IN (%s) GROUP BY line" % ','.join('?' for s in config['sources'] if s['kind'] in {'stations', 'inventory'}), tuple(s['id'] for s in config['sources'] if s['kind'] in {'stations', 'inventory'}))}
            missing = sorted(EXPECTED_LINES - set(coverage))
            result['coverage'] = {'records_by_line_across_sources': coverage, 'records_by_source_and_line': {s['id']: {r[0]: r[1] for r in store.db.execute('SELECT line,count(*) FROM records WHERE source=? GROUP BY line', (s['id'],))} for s in config['sources']}, 'missing_lines': missing,
                                  'all_expected_lines_present': not missing, 'full_station_completeness_verified': False, 'basis': 'retained records; source freshness is reported separately'}
            if not exit_code and (missing or any(s.get('quality', {}).get('invalid_rows') or s.get('quality', {}).get('schema_changed') for s in result['sources'])):
                result['status'] = 'quality_warning'
            result['ended_at'] = now(); result['total'] = store.count()
            with store.db:
                store.db.execute('UPDATE runs SET ended_at=?,status=?,payload=? WHERE id=?',
                                 (result['ended_at'], result['status'], encoded(result), result['id']))
            path = report(store, result, slot, scheduled_at)
            store.export()
            atomic_write(last_file, encoded({'ended_at': result['ended_at'], 'run_id': result['id']}))
            logger.info('run finished %s %s', result['id'], result['status'])
            print(encoded({'run_id': result['id'], 'status': result['status'], 'report': path,
                           'new': result['new'], 'changed': result['changed'], 'total': result['total'], 'missing_lines': missing}))
        return exit_code
    finally:
        if store:
            store.close()
        handler.close(); logger.removeHandler(handler)
        fcntl.flock(lock, fcntl.LOCK_UN); lock.close()
