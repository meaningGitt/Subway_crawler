"""Small metadata probes; content-addressed, atomic downloads outside the Git checkout."""
from __future__ import annotations

import hashlib
import html
import json
import os
from pathlib import Path
import re
import tempfile
import time
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests

from ..pipeline import now
from ..source import Client, SourceError, digest, encoded, metadata
from ..store import atomic_write


class AcquisitionError(SourceError):
    def __init__(self, message, category='retryable'):
        super().__init__(message)
        self.category = category


class DownloadClient(Client):
    def request_file(self, method, url, destination, **kwargs):
        """Public URLs only. Retry transport errors; honor server limits, never bypass CAPTCHA."""
        if urlparse(url).hostname not in {'www.data.go.kr', 'data.kric.go.kr', 'data.seoul.go.kr', 'info.korail.com'}:
            raise AcquisitionError('download host changed; registry review needed', 'schema')
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(3):
            self.sleep(max(0, self.interval - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            temp = None
            try:
                with self.session.request(method, url, timeout=(15, 90), stream=True, **kwargs) as r:
                    if r.status_code in (429, 500, 502, 503, 504):
                        retry = r.headers.get('Retry-After', '')
                        try:
                            delay = float(retry)
                        except ValueError:
                            try:
                                delay = (parsedate_to_datetime(retry) - datetime.now().astimezone()).total_seconds()
                            except (ValueError, TypeError):
                                delay = 2 ** (attempt + 1)
                        if attempt == 2 or delay > 60:
                            raise AcquisitionError(f'HTTP {r.status_code}; retry next run')
                        self.sleep(max(0, delay)); continue
                    if r.status_code in (401, 403):
                        raise AcquisitionError(f'HTTP {r.status_code}; permission or interactive check required', 'user_action')
                    if r.status_code != 200:
                        raise AcquisitionError(f'HTTP {r.status_code}', 'schema')
                    fd, temp = tempfile.mkstemp(prefix='.download-', dir=destination.parent)
                    h = hashlib.sha256(); size = 0
                    with os.fdopen(fd, 'wb') as out:
                        for block in r.iter_content(1024 * 1024):
                            size += len(block)
                            if size > 1024 ** 3:
                                raise AcquisitionError('raw file exceeds configured 1 GiB limit', 'user_action')
                            h.update(block); out.write(block)
                        out.flush(); os.fsync(out.fileno())
                    os.replace(temp, destination)
                    return {'sha256': h.hexdigest(), 'bytes': size, 'http_status': 200,
                            'content_type': r.headers.get('Content-Type'), 'collected_at': now()}
            except requests.RequestException:
                if attempt == 2:
                    raise AcquisitionError('network/TLS/timeout after 3 attempts') from None
                self.sleep(2 ** (attempt + 1))
            finally:
                if temp and os.path.exists(temp):
                    os.unlink(temp)
        raise AcquisitionError('retry budget exhausted')

    def post_json(self, url, data):
        # Reuse bounded request implementation, keep response off public logs.
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'response.json'
            self.request_file('POST', url, path, data=data)
            try:
                return json.loads(path.read_bytes())
            except (ValueError, UnicodeError):
                raise AcquisitionError('expected JSON; provider response changed', 'schema') from None


def catalog(page):
    for body in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S):
        result = json.loads(body)
        if result.get('@type') == 'Dataset':
            return result
    return {}


def discover(source, client):
    _, _, body = client.get(source['url'])
    page = body.decode('utf-8')
    if source['adapter'] == 'kric':
        meta = metadata(body, {'metadata_url': source['url'], 'dataset_id': 900})
        # License evidence from the linked national standard catalog is retained too.
        _, _, license_body = client.get(source['license_url'])
        if '이용허락범위 제한 없음' not in license_body.decode('utf-8'):
            raise AcquisitionError('standard catalog license changed', 'user_action')
        meta.update(license='이용허락범위 제한 없음', license_evidence_sha256=digest(license_body))
        return meta, body
    if source['adapter'] == 'data_go':
        cat = catalog(page)
        if cat.get('license', {}).get('name') != '이용허락범위 제한 없음':
            raise AcquisitionError('license requires review before processing', 'user_action')
        links = re.findall(r"fn_fileDataDown\('([^']+)',\s*'([^']+)',\s*'([^']*)',\s*'([^']+)',\s*'([^']+)'", page)
        if not links:
            raise AcquisitionError('no public file attachment; API or schema review required', 'user_action')
        a, b, c, d, _ = links[0]
        meta = {'title': cat.get('alternateName', cat.get('name')), 'modified': cat.get('dateModified'),
                'license': cat['license']['name'], 'download_args': [a, b, c, d],
                'description': cat.get('description', ''), 'cadence': cat.get('additionalProperty', [])}
        meta['version'] = digest(encoded(meta).encode())
        return meta, body
    return {'title': source['name'], 'modified': None, 'version': digest(body)}, body


def download(source, meta, client, path):
    if source['adapter'] == 'kric':
        return client.request_file('GET', meta['url'], path)
    a, b, c, d = meta['download_args']
    j = client.post_json('https://www.data.go.kr/tcs/dss/selectFileDataDownload.do',
                         {'publicDataPk': a, 'publicDataDetailPk': b, 'atchFileId': c,
                          'fileDetailSn': d, 'publicDataTyCode': 'PR0051'})
    if not j.get('status'):
        raise AcquisitionError('public portal requires interactive approval', 'user_action')
    params = {'atchFileId': j['atchFileId'], 'fileDetailSn': j['fileDetailSn']}
    limit = client.post_json('https://www.data.go.kr/cmm/cmm/check-limit.json', params)
    if limit.get('needCaptcha', True):
        raise AcquisitionError('portal CAPTCHA required; no bypass attempted', 'user_action')
    result = client.request_file('GET', 'https://www.data.go.kr/cmm/cmm/fileDownload.do', path, params=params)
    result.update(download_url='https://www.data.go.kr/cmm/cmm/fileDownload.do', download_params=params,
                  original_filename=j['fileDataRegistVO'].get('orginlFileNm'))
    return result


def acquire(source, root, check_only=False, force=False):
    root = Path(root); sid = source['id']
    state_path = root / 'state' / f'{sid}.json'
    check_path=root / 'checks' / f'{sid}.json'
    old = json.loads(state_path.read_text()) if state_path.exists() else (json.loads(check_path.read_text()) if check_path.exists() else {})
    client = DownloadClient(interval=2)
    meta, page = discover(source, client)
    evidence = root / 'evidence' / sid / (digest(page) + '.html')
    if not evidence.exists():
        atomic_write(evidence, page)
    result = {'source_id': sid, 'checked_at': now(), 'metadata': meta,
              'evidence': str(evidence.relative_to(root)), 'url': source['url'],
              'changed': meta['version'] != old.get('metadata', {}).get('version')}
    if source['adapter'] == 'review':
        result.update(status=source['status'], reason=source['reason'], error_category=source.get('error_category', 'user_action'))
        return result
    if check_only:
        result.update(status='metadata_checked', collection_status='미수집: 메타데이터 확인만 실행')
        # Separate check state must never replace the successfully collected raw pointer.
        atomic_write(root / 'checks' / f'{sid}.json', encoded(result))
        return result
    due = not old.get('last_downloaded') or datetime.fromisoformat(now()) - datetime.fromisoformat(old['last_downloaded']) >= timedelta(days=source.get('revalidate_days', 30))
    if not force and not result['changed'] and not due and (root / old.get('raw_path', 'missing')).is_file():
        result.update({k: old[k] for k in ('raw_path', 'receipt', 'last_downloaded')})
        result.update(status='수집 완료', downloaded=False)
    else:
        budget = int(os.environ.get('SEAT_MAX_STORAGE_MB', '10240')) * 1024 * 1024
        occupied = sum(p.stat().st_size for p in root.rglob('*') if p.is_file())
        if occupied >= budget:
            raise AcquisitionError('storage budget reached; archive/move existing data before collecting', 'user_action')
        pending = root / 'raw' / sid / '.pending'
        receipt = download(source, meta, client, pending)
        with pending.open('rb') as f:
            prefix = f.read(200).lstrip().lower()
        if prefix.startswith((b'<!doctype html', b'<html')):
            quarantine = pending.with_name(receipt['sha256'] + '.unexpected.html')
            os.replace(pending, quarantine)
            raise AcquisitionError('HTML response instead of dataset; raw quarantined', 'schema')
        path = pending.with_name(receipt['sha256'] + source['suffix'])
        os.replace(pending, path)
        result.update(status='수집 완료', downloaded=True, raw_path=str(path.relative_to(root)),
                      receipt=receipt, last_downloaded=now())
        atomic_write(path.with_suffix(path.suffix + '.json'), encoded(result))
    atomic_write(state_path, encoded(result))
    return result
