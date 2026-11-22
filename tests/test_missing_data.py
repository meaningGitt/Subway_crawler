import csv
import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from subway_crawler.missing_data.inventory import inspect_existing, unwrap_times, derive_last_trains, sha
from subway_crawler.missing_data.pipeline import connect, upsert, Lock, duration, transfer_rows, url_key, alerts, deduplicate_batch, validate_record
from subway_crawler.recommendation.sources import AcquisitionError
from unittest.mock import patch


class MissingDataTests(unittest.TestCase):
    def test_duplicate_conflicts_are_not_silently_overwritten(self):
        a={'source_record_id':'1','duration':20}
        self.assertEqual(deduplicate_batch([a,a]),([a],1))
        with self.assertRaises(AcquisitionError):deduplicate_batch([a,a|{'duration':30}])

    def test_bad_date_and_url_cannot_be_service_usable(self):
        row=validate_record('timetable_updates',{'event_type':'notice','title':'test','source_url':'http://example.org',
                                              'effective_start':'2026-02-30','service_status':'USABLE_REFERENCE'})
        self.assertEqual(row['service_status'],'NOT_USABLE')
        self.assertIn('invalid_effective_start',row['issues'])
        self.assertIn('invalid_source_url',row['issues'])

    def test_midnight_is_not_wall_clock_sort(self):
        self.assertEqual(unwrap_times(['23:58:00','00:02:00','00:10:00']),[86280,86520,87000])
        self.assertEqual(unwrap_times(['24:01:00','24:05:00']),[86460,86700])
        self.assertEqual(unwrap_times(['12:10:00','12:05:00','27:70:00']),[43800,None,None])
        self.assertEqual(unwrap_times(['00:10:00']),[600]) # No invented service-day anchor.

    def test_read_only_inventory_uses_actual_rows_and_not_manifest_claim(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);snapshot=root/'snapshots/a';(snapshot/'datasets').mkdir(parents=True)
            p=snapshot/'datasets/test.parquet';pd.DataFrame({'line':['1호선','2호선']}).to_parquet(p)
            (root/'latest.json').write_text(json.dumps({'path':str(snapshot)}))
            (snapshot/'manifest.json').write_text(json.dumps({'datasets':{'test':{'rows':999}}}))
            before=sha(p);rows,_,_=inspect_existing(root,root/'missing.csv')
            self.assertEqual(rows[0]['rows'],2);self.assertEqual(before,sha(p))
            self.assertEqual(rows[-1]['status'],'NOT_VERIFIED')

    def test_last_candidate_excludes_terminal_and_unknown_current_validity(self):
        with tempfile.TemporaryDirectory() as d:
            rows=[]
            for trip,times in [('a',['23:58:00','00:02:00','00:10:00']),('b',['23:50:00','23:55:00','23:59:00'])]:
                for n,t in enumerate(times):
                    rows.append(dict(trip_key=trip,train_no=trip,line='1호선',source_line_code='S1101',day_type='평일',
                        station_key=f'S1:1:{n}',station_name=str(n),origin_name='0',destination_name='2',direction='',direction_path='0 → 2',
                        stop_sequence=n+1,departure_time=t,reference_date='2026-01-01',effective_date='',quality_valid='True',raw_sha256='a',source_id='test',source_row=n))
            f=Path(d)/'stops.csv';pd.DataFrame(rows).to_csv(f,index=False)
            found,invalid=derive_last_trains(f)
            self.assertEqual(len(found),2);self.assertFalse(invalid)
            self.assertTrue(all(r['trip_key']=='a' and r['service_status']=='NOT_USABLE' for r in found))
            self.assertEqual(next(r for r in found if r['station_name']=='1')['calendar_day_offset'],1)

    def test_idempotent_merge_and_correction_history_and_rollback(self):
        with tempfile.TemporaryDirectory() as d:
            db=connect(Path(d)/'db.sqlite')
            with db:self.assertEqual(upsert(db,'transfer_connections','a',{'value':1}),'new')
            with db:self.assertEqual(upsert(db,'transfer_connections','a',{'value':1}),'unchanged')
            with db:self.assertEqual(upsert(db,'transfer_connections','a',{'value':2}),'changed')
            with self.assertRaises(RuntimeError):
                with db:upsert(db,'transfer_connections','a',{'value':3});raise RuntimeError()
            self.assertEqual(db.execute('SELECT COUNT(*) FROM versions').fetchone()[0],2)
            self.assertEqual(json.loads(db.execute('SELECT payload FROM records').fetchone()[0])['value'],2)
            db.close()

    def test_lock_released_after_exception(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'lock'
            with Lock(p):
                with self.assertRaisesRegex(RuntimeError,'SKIPPED_OVERLAP'):
                    with Lock(p):pass
            with Lock(p):pass

    def test_no_auth_key_no_http(self):
        with patch.dict('os.environ',{},clear=True),patch('requests.Session.get') as get:
            with self.assertRaises(AcquisitionError):alerts({'secret':'DATA_GO_KR_SERVICE_KEY'},{})
            get.assert_not_called()

    def test_pain_aliases_dedup(self):
        self.assertEqual(url_key('https://community.linkareer.com/mentor_intern/3151902?utm_source=x'),url_key('https://community.linkareer.com/mentor-qna/3151902'))
        with self.assertRaises(ValueError):url_key('https://secret@example.org/a')

    def test_reference_date_conflict_is_quarantined_and_time_checked(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'raw.csv').write_text('연번,호선,환승역명,환승노선,환승거리,환승소요시간\n1,1,서울역,4호선,159,02:13\n')
            receipt={'raw_path':'raw.csv','metadata':{'title':'data_20260831','modified':'2026-10-16','license':'이용허락범위 제한 없음'},
                     'receipt':{'original_filename':'data_20251231.csv','sha256':'a','collected_at':'2026-11-22'}}
            stations=[{'STIN_NM':'서울역','line':f'{i}호선','station_key':f'S1:{i}:x'} for i in [1,4]]
            rows=transfer_rows({'id':'metro_transfer_time','url':'https://www.data.go.kr/'},receipt,p,stations)
            self.assertEqual(rows[0]['duration_seconds'],133)
            self.assertEqual(rows[0]['service_status'],'NOT_USABLE');self.assertIsNone(rows[0]['reference_date'])
            self.assertIn('catalog_filename_date_conflict',rows[0]['issues'])
            self.assertEqual(duration('00:59'),59)
            with self.assertRaises(ValueError):duration('03:99')
