from __future__ import annotations

import hashlib
import html
import io
import json
import re
import time
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import openpyxl
import requests


class SourceError(Exception):
    """Safe message suitable for public logs (never includes a credential URL)."""


def digest(value):
    return hashlib.sha256(value).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Client:
    def __init__(self, interval=2, sleeper=time.sleep):
        self.interval, self.sleep, self.last = interval, sleeper, 0.0
        self.session = requests.Session()
        self.session.headers['User-Agent'] = 'SubwayCrawler/1.0 (+https://github.com/meaningGitt/Subway_crawler)'

    def get(self, url, headers=None):
        # Serial requests, bounded retries, bounded response size, TLS verification on.
        for attempt in range(3):
            self.sleep(max(0, self.interval - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            try:
                with self.session.get(url, headers=headers or {}, timeout=(15, 90), stream=True) as r:
                    if r.status_code in (429, 500, 502, 503, 504):
                        if attempt == 2:
                            raise SourceError(f'HTTP {r.status_code}: retry limit reached')
                        retry = r.headers.get('Retry-After', '')
                        try:
                            delay = float(retry)
                        except ValueError:
                            try:
                                delay = (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds()
                            except (TypeError, ValueError):
                                delay = 2 ** (attempt + 1)
                        if delay > 120:
                            raise SourceError('Retry-After exceeds retry budget; retry next run')
                        self.sleep(max(0, delay))
                        continue
                    if r.status_code not in (200, 304):
                        raise SourceError(f'HTTP {r.status_code}')
                    chunks, size = [], 0
                    for chunk in r.iter_content(65536):
                        size += len(chunk)
                        if size > 32 * 1024 * 1024:
                            raise SourceError('response exceeds 32 MiB safety limit')
                        chunks.append(chunk)
                    return r.status_code, dict(r.headers), b''.join(chunks)
            except requests.RequestException:
                if attempt == 2:
                    raise SourceError('network/TLS/timeout failure after 3 attempts') from None
                self.sleep(2 ** (attempt + 1))
        raise SourceError('request failed')


def metadata(body, source):
    text = body.decode('utf-8')
    titles = re.findall(r'<h6[^>]*>(.*?)</h6>', text, re.S)
    links = re.findall(r"(?:location.href=)['\"]([^'\"]+download\.file[^'\"]+)", text)
    modified = re.search(r'<th[^>]*>\s*수정일\s*</th>\s*<td[^>]*>(.*?)</td>', text, re.S)
    if len(titles) != 1 or len(links) != 1 or not modified:
        raise SourceError('metadata structure changed: expected one XLSX operation and modification date')
    clean = lambda x: html.unescape(re.sub('<[^>]+>', '', x)).strip()
    url = urljoin(source['metadata_url'], html.unescape(links[0]))
    if urlparse(url).netloc != 'data.kric.go.kr' or f"id={source['dataset_id']}&" not in url:
        raise SourceError('download source changed; review registry')
    result = {'title': clean(titles[0]), 'modified': clean(modified[1]), 'url': url}
    result['version'] = digest(encoded(result).encode())
    return result


LINE_NAMES = {
    'I1101': '1호선', 'I4101': '1호선', 'I4102': '1호선', 'I4401': '1호선',
    'I1103': '3호선', 'I4106': '3호선', 'I1104': '4호선', 'I4103': '4호선',
    'I4104': '4호선', 'S1121': '2호선', 'S1122': '2호선',
    'I11D1': '신분당선', 'I28A1': '공항철도', 'I28K1': '수인분당선',
    'I4105': '수인분당선', 'I4108': '경의중앙선', 'I41K2': '경춘선',
    'I41K5': '경강선', 'I41WS': '서해선', 'L11SL': '신림선',
    'L11UI': '우이신설선', 'L41E1': '용인경전철', 'L41G1': '김포골드라인',
    'L41U1': '의정부경전철', 'S2801': '인천1호선', 'S2802': '인천2호선',
    'S4108': '8호선', **{f'S110{i}': f'{i}호선' for i in range(1, 10)},
}


def clean(value):
    if isinstance(value, (date, datetime)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    if value is None:
        return ''
    return re.sub(r'\s+', ' ', str(value)).strip()


def parse_xlsx(body, kind):
    try:
        book = openpyxl.load_workbook(io.BytesIO(body), read_only=True, data_only=True)
        rows = iter(book.active.values)
        columns = [clean(c) for c in next(rows)]
        required = ['노선번호', '노선명', '운영기관명', '데이터기준일자']
        if kind == 'stations':
            required += ['역번호', '역사명', '역위도', '역경도']
        if not set(required).issubset(columns) or len(columns) != len(set(columns)):
            raise SourceError('required columns missing or duplicated: schema changed')
        records, seen, duplicates, conflicts, invalid, scoped = [], {}, 0, 0, 0, 0
        missing = {c: 0 for c in required}
        for cells in rows:
            row = dict(zip(columns, map(clean, cells)))
            if not any(row.values()):
                continue
            code = row.get('노선번호', '')
            # Official geographic line codes, including out-of-area extensions of these lines.
            # No individual line allowlist: new metropolitan codes enter automatically.
            if len(code) < 3:
                invalid += 1
                continue
            if code[1:3] not in {'11', '28', '41', '44'}:
                continue
            scoped += 1
            for c in required:
                missing[c] += not bool(row.get(c))
            line = '경춘선' if row['노선명'] == '경춘선' else LINE_NAMES.get(code, row['노선명'])
            identity = [code, line, row.get('역번호', '')] if kind == 'stations' else [code, row['노선명']]
            if not all(identity) or not row.get('노선명'):
                invalid += 1
                continue
            problems = []
            if kind == 'stations':
                for field, low, high in [('역위도', 33, 39), ('역경도', 124, 132)]:
                    try:
                        if not low <= float(row.get(field, '')) <= high:
                            problems.append(field)
                    except ValueError:
                        problems.append(field)
            try:
                date.fromisoformat(row.get('데이터기준일자', ''))
            except ValueError:
                problems.append('데이터기준일자')
            if problems:
                invalid += 1  # Keep row and flag source defects; never fabricate values.
            line = '경춘선' if row['노선명'] == '경춘선' else LINE_NAMES.get(code, row['노선명'])
            record = {'key': encoded(identity), 'line': line, 'reference_at': row.get('데이터기준일자'),
                      'data': row, 'issues': problems}
            if record['key'] in seen:
                duplicates += 1
                previous = seen[record['key']]
                if previous != record:
                    if previous['reference_at'] == record['reference_at']:
                        conflicts += 1
                    elif (record['reference_at'] or '') > (previous['reference_at'] or ''):
                        records[records.index(previous)] = record
                        seen[record['key']] = record
                continue
            seen[record['key']] = record
            records.append(record)
        book.close()
        if not records or conflicts:
            raise SourceError(f'empty metropolitan dataset or conflicting identities ({conflicts})')
        quality = {'rows': scoped, 'duplicates': duplicates, 'duplicate_rate': duplicates / max(1, scoped),
                   'invalid_rows': invalid, 'missing_rates': {k: v / max(1, scoped) for k, v in missing.items()},
                   'columns': columns}
        return records, quality
    except SourceError:
        raise
    except Exception:
        raise SourceError('invalid XLSX or unexpected workbook structure') from None


def inventory_metadata(page, source, client):
    candidates = []
    for row in re.findall(r'<tr\b.*?</tr>', page.decode('utf-8'), re.S):
        if '코드정보 리스트' not in row:
            continue
        match = re.search(r'detail\.do(?:;[^?\s]+)?\?id=(\d+)', row)
        stamp = re.search(r'리스트\((\d{4}\.\d{2}\.\d{2})\)', row)
        if match and stamp:
            candidates.append((stamp[1], match[1]))
    if not candidates:
        raise SourceError('inventory listing structure changed')
    stamp, board_id = max(candidates)
    detail = f'https://data.kric.go.kr/rips/M_04_02/detail.do?id={board_id}'
    _, _, body = client.get(detail)
    links = re.findall(r'<a href="([^"]+download.file[^"]+)"[^>]*>([^<]+\.xlsx?)</a>', body.decode('utf-8'))
    if len(links) != 1:
        raise SourceError('inventory attachment structure changed')
    link, title = links[0]
    url = urljoin(detail, html.unescape(link))
    if urlparse(url).netloc != 'data.kric.go.kr':
        raise SourceError('inventory attachment host changed')
    result = {'title': title, 'modified': stamp.replace('.', '-'), 'url': url, 'detail_url': detail}
    result['version'] = digest(encoded(result).encode())
    return result


def parse_inventory(body):
    if body.startswith(b'PK'):
        book = openpyxl.load_workbook(io.BytesIO(body), read_only=True, data_only=True)
        rows = list(book.active.values); book.close()
    else:
        import xlrd
        sheet = xlrd.open_workbook(file_contents=body).sheet_by_index(0)
        rows = [sheet.row_values(i) for i in range(sheet.nrows)]
    columns = [clean(c) for c in rows[0]]
    required = ['RAIL_OPR_ISTT_CD', 'RAIL_OPR_ISTT_NM', 'LN_CD', 'LN_NM', 'STIN_CD', 'STIN_NM']
    if not set(required).issubset(columns):
        raise SourceError('inventory columns changed')
    operators = {'AR', 'DX', 'EV', 'GM', 'GU', 'GX', 'IA', 'IC', 'KR', 'NU', 'S1', 'S9', 'SL', 'SR', 'SW', 'UI', 'UL'}
    aliases = {'공항': '공항철도', '경의중앙': '경의중앙선', '경춘': '경춘선', '경강': '경강선',
               '수인분당': '수인분당선', '서해': '서해선', '우이신설': '우이신설선',
               '에버라인': '용인경전철', '용인에버라인': '용인경전철', '용인': '용인경전철', '자기부상': '자기부상철도',
               '김포': '김포골드라인', '의정부': '의정부경전철', '신림': '신림선', '신분당': '신분당선'}
    records, seen = [], set()
    for cells in rows[1:]:
        data = dict(zip(columns, map(clean, cells)))
        if data.get('RAIL_OPR_ISTT_CD') not in operators:
            continue
        if data['RAIL_OPR_ISTT_CD'] == 'KR' and data.get('LN_CD') in {'K6', 'K7'}:
            continue
        if any(not data.get(c) for c in required):
            raise SourceError('inventory required values missing')
        key = encoded([data['RAIL_OPR_ISTT_CD'], data['LN_CD'], data['STIN_CD']])
        if key in seen:
            raise SourceError('inventory identity collision')
        seen.add(key)
        records.append({'key': key, 'line': aliases.get(data['LN_NM'], data['LN_NM']),
                        'reference_at': None, 'data': data, 'issues': []})
    if not records:
        raise SourceError('empty station inventory')
    return records, {'rows': len(records), 'duplicates': 0, 'duplicate_rate': 0, 'invalid_rows': 0,
                     'missing_rates': {}, 'columns': columns,
                     'reference_date_note': '원본에 행별 기준일 없음. 게시물 파일일은 source_modified_at에 별도 기록.'}
