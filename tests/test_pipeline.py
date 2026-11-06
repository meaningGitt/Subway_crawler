import contextlib
import copy
import fcntl
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import openpyxl
from subway_crawler.source import SourceError, parse_xlsx, metadata
from subway_crawler.store import Store
from subway_crawler.pipeline import collect, run

SOURCE = {'id': 'stations', 'kind': 'stations', 'dataset_id': 32, 'metadata_url': 'https://data.kric.go.kr/rips/M_01_01/detail.do?id=32', 'license': 'public', 'automation_allowed': True}
PAGE = b'<h6>stations</h6><button onclick="location.href=\'/rips/dataset/download.file?type=filedata&id=32&operation=1\';"></button><th>\xec\x88\x98\xec\xa0\x95\xec\x9d\xbc</th><td>2026-01-01</td>'
HEADER = ['역번호', '역사명', '노선번호', '노선명', '운영기관명', '데이터기준일자', '역위도', '역경도']
ROW = ['001', '테스트역', 'S1102', '2호선', '운영기관', '2026-01-01', 37.5, 127.0]


def workbook(rows=None, header=None):
    w = openpyxl.Workbook(); w.active.append(header or HEADER)
    for row in rows if rows is not None else [ROW]: w.active.append(row)
    b = io.BytesIO(); w.save(b); w.close(); return b.getvalue()


class FakeClient:
    def __init__(self, body=None, page=PAGE): self.calls = []; self.body = body or workbook(); self.page = page
    def get(self, url, headers=None):
        self.calls.append((url, headers))
        return 200, {}, self.body if 'download.file' in url else self.page


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name); self.addCleanup(self.store.close)

    def test_idempotency_and_no_redundant_download(self):
        a = collect(SOURCE, self.store, 'one', client=FakeClient())
        client = FakeClient(); b = collect(SOURCE, self.store, 'two', client=client)
        self.assertEqual((a['new'], b['new'], b['changed'], self.store.count()), (1, 0, 0, 1))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM history').fetchone()[0], 1)

    def test_historical_correction_and_reversion_preserve_three_versions(self):
        collect(SOURCE, self.store, 'one', client=FakeClient())
        row = ROW.copy(); row[1] = '수정된역'
        corrected = collect(SOURCE, self.store, 'two', force=True, client=FakeClient(workbook([row])))
        reverted = collect(SOURCE, self.store, 'three', force=True, client=FakeClient())
        self.assertEqual((corrected['changed'], reverted['changed'], self.store.count()), (1, 1, 1))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM history').fetchone()[0], 3)

    def test_latest_reference_date_resolves_duplicate(self):
        old = ROW.copy(); old[1] = '이전명'; old[5] = '2025-01-01'
        rows, q = parse_xlsx(workbook([old, ROW, ROW]), 'stations')
        self.assertEqual(len(rows), 1); self.assertEqual(rows[0]['data']['역사명'], '테스트역')
        self.assertEqual(q['duplicates'], 2)

    def test_conflicting_same_date_is_rejected(self):
        other = ROW.copy(); other[1] = '충돌'
        with self.assertRaises(SourceError): parse_xlsx(workbook([ROW, other]), 'stations')

    def test_schema_break_keeps_previous_state_and_raw_evidence(self):
        collect(SOURCE, self.store, 'one', client=FakeClient())
        state = self.store.state('stations')
        with self.assertRaises(SourceError):
            collect(SOURCE, self.store, 'two', force=True, client=FakeClient(workbook(header=['wrong'])))
        self.assertEqual(self.store.state('stations'), state)
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(len(list((Path(self.tmp.name)/'raw/stations').glob('*.xlsx'))), 2)

    def test_transaction_rollback(self):
        records, _ = parse_xlsx(workbook(), 'stations')
        with self.assertRaises(RuntimeError):
            with self.store.db:
                self.store.merge('stations', records, 'test', '2026-01-01')
                raise RuntimeError()
        self.assertEqual(self.store.count(), 0)

    def test_non_metropolitan_excluded_but_new_metropolitan_line_included(self):
        other = ROW.copy(); other[2] = 'S2601'
        new = ROW.copy(); new[2] = 'S4199'; new[3] = '신규노선'
        rows, _ = parse_xlsx(workbook([ROW, other, new]), 'stations')
        self.assertEqual(len(rows), 2)

    def test_invalid_coordinates_and_missing_fields_reported(self):
        row = ROW.copy(); row[6] = 200; row[4] = None
        _, q = parse_xlsx(workbook([row]), 'stations')
        self.assertEqual(q['invalid_rows'], 1); self.assertEqual(q['missing_rates']['운영기관명'], 1)

    def test_lock_skips_without_touching_active_database(self):
        with open(Path(self.tmp.name)/'.collection.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(run(self.tmp.name, {'expected_lines': [], 'sources': []}), 0)
            self.assertTrue(list((Path(self.tmp.name)/'reports/skipped').glob('*.json')))

    def test_failure_isolation_reports_and_retry_only_failed_source(self):
        config = {'expected_lines': ['2호선'], 'sources': [SOURCE, {**SOURCE, 'id': 'bad'}]}
        def fake(source, store, run_id, force=False):
            if source['id'] == 'bad': raise SourceError('HTTP 503')
            return collect(source, store, run_id, force, client=FakeClient())
        with patch('subway_crawler.pipeline.collect', side_effect=fake):
            self.assertEqual(run(self.tmp.name, config, slot='0900'), 1)
        report = json.loads(next((Path(self.tmp.name)/'reports/daily').glob('*.json')).read_text())
        self.assertEqual(report['total'], 1); self.assertEqual(report['status'], 'partial_failure')
        with patch('subway_crawler.pipeline.collect', side_effect=fake) as mocked:
            run(self.tmp.name, config, retry_run=report['id'])
            self.assertEqual([c.args[0]['id'] for c in mocked.call_args_list], ['bad'])

    def test_queued_overlap_skips(self):
        Path(self.tmp.name, 'last_finished.json').write_text('{"ended_at":"2026-11-07T22:30:00+09:00"}')
        with patch('subway_crawler.pipeline.collect') as mocked:
            run(self.tmp.name, {'expected_lines': [], 'sources': [SOURCE]}, scheduled_at='2026-11-07T22:00:00+09:00')
            mocked.assert_not_called()

    def test_metadata_counter_changes_do_not_change_version(self):
        self.assertEqual(metadata(PAGE + b'count 1', SOURCE), metadata(PAGE + b'count 2', SOURCE))

    def test_csv_export(self):
        collect(SOURCE, self.store, 'one', client=FakeClient())
        self.store.export()
        self.assertIn('테스트역', (Path(self.tmp.name)/'exports/stations.csv').read_text())


if __name__ == '__main__': unittest.main()

class InventoryTests(unittest.TestCase):
    def test_gtx_and_all_metro_operators_with_newest_listing(self):
        from subway_crawler.source import parse_inventory, inventory_metadata
        w = openpyxl.Workbook()
        w.active.append(['RAIL_OPR_ISTT_CD','RAIL_OPR_ISTT_NM','LN_CD','LN_NM','STIN_CD','STIN_NM'])
        w.active.append(['GX','GTX 운영','A','GTX-A','X101','운정중앙'])
        w.active.append(['EV','용인 운영','E1','에버라인','Y110','기흥'])
        w.active.append(['BS','부산','1','1호선','101','부산역'])
        b=io.BytesIO(); w.save(b); w.close()
        records, q = parse_inventory(b.getvalue())
        self.assertEqual({r['line'] for r in records}, {'GTX-A','용인경전철'})
        self.assertIsNone(records[0]['reference_at'])
        listing = '<tr onclick="location.href=\'detail.do;jsessionid=variable?id=17\'"><td>코드정보 리스트(2026.07.11)</td></tr>'
        attachment = b'<a href="/rips/download.file?type=L&id=17&fileId=1">codes.xlsx</a>'
        client = FakeClient(page=attachment)
        result = inventory_metadata(listing.encode(), SOURCE, client)
        self.assertEqual(result['modified'], '2026-07-11')
        self.assertNotIn('jsessionid', result['url'])

class HttpTests(unittest.TestCase):
    def test_rate_limit_then_success_and_retry_after(self):
        from subway_crawler.source import Client
        from unittest.mock import MagicMock
        waits=[]; client=Client(interval=0, sleeper=waits.append)
        limited=MagicMock(status_code=429, headers={'Retry-After':'3'})
        success=MagicMock(status_code=200, headers={})
        success.iter_content.return_value=[b'valid']
        limited.__enter__.return_value=limited; success.__enter__.return_value=success
        client.session.get=MagicMock(side_effect=[limited,success])
        self.assertEqual(client.get('https://example.test')[2], b'valid')
        self.assertIn(3, waits); self.assertEqual(client.session.get.call_count,2)

    def test_long_retry_after_stops_without_retrying_early(self):
        from subway_crawler.source import Client
        from unittest.mock import MagicMock
        client=Client(interval=0,sleeper=lambda _:None)
        limited=MagicMock(status_code=429,headers={'Retry-After':'500'})
        limited.__enter__.return_value=limited
        client.session.get=MagicMock(return_value=limited)
        with self.assertRaises(SourceError): client.get('https://example.test')
        self.assertEqual(client.session.get.call_count,1)
