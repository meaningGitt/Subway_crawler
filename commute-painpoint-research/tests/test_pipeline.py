import csv
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from analyze import build, structure, REQUIRED
from collect import collect, request_json
from common import RunLock, canonical_url, write_csv


class EvidenceTests(unittest.TestCase):
    def test_unknowns_and_plans_are_not_invented(self):
        row = ['https://example.org/posts/1', '환승이 너무 힘들다', '미확인', '출퇴근',
               '지하철', '', '환승 횟수', '', '환승 피로', '']
        record = structure(row, 'pilot')
        for k in ['pain_cause','sacrifice','workaround','unmet_need','commute_duration','published_date','action_outcome']:
            self.assertIsNone(record[k], k)
        row[7] = '자취 고려'
        self.assertEqual(structure(row, 'pilot')['workaround_status'], '고려·계획')

    def test_source_identity_preserves_article_queries(self):
        self.assertNotEqual(canonical_url('https://example.org/read?num=1'), canonical_url('https://example.org/read?num=2'))
        self.assertEqual(canonical_url('https://example.org/read?mode=view&no=1&utm_source=x#top'),
                         'https://example.org/read?mode=view&no=1')
        self.assertEqual(canonical_url('https://orbi.kr/000123/제목'), 'https://orbi.kr/000123')
        with self.assertRaises(ValueError): canonical_url('https://user:password@example.org/a')

    def test_csv_safe_and_utf8(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'table.csv'
            write_csv(p, [{'a':'=1+2','b':None,'c':'한글, 문장\n줄'}])
            self.assertTrue(p.read_bytes().startswith(b'\xef\xbb\xbf'))
            row = list(csv.DictReader(io.StringIO(p.read_text(encoding='utf-8-sig'))))[0]
            self.assertEqual(row, {'a':"'=1+2",'b':'','c':'한글, 문장\n줄'})

    def test_real_seed_replay_and_duplicate_handling(self):
        with tempfile.TemporaryDirectory() as d, redirect_stdout(io.StringIO()):
            seed = Path(d)/'seed'; seed.mkdir()
            for p in (ROOT/'data/seed').glob('*.json'):
                (seed/p.name).write_bytes(p.read_bytes())
            pilot = json.loads((seed/'pilot.json').read_text())
            (seed/'pilot.json').write_text(json.dumps(pilot + [pilot[0]], ensure_ascii=False))
            out = Path(d)/'out'
            rows = build(seed, out)
            self.assertEqual(len(rows), 53)
            self.assertEqual(len({r['source_id'] for r in rows}), 53)
            self.assertTrue(set(REQUIRED).issubset(rows[0]))
            self.assertTrue(all(r['human_review_status']=='미검토' and not r['original_page_verified'] for r in rows))
            self.assertTrue(all(len(r['evidence_excerpt'].split()) <= 25 for r in rows))
            summary = json.loads((out/'run_summary.json').read_text())
            self.assertFalse(summary['main_collection_complete'])
            self.assertEqual(summary['validated_original_cases'], 0)
            self.assertEqual(len(list(csv.DictReader(io.StringIO((out/'manual_review_sample.csv').read_text(encoding='utf-8-sig'))))),30)
            self.assertEqual(len(list(csv.DictReader(io.StringIO((out/'duplicate_log.csv').read_text(encoding='utf-8-sig'))))),1)

    def test_lock_releases_after_error(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'run.lock'
            with self.assertRaises(ValueError):
                with RunLock(p):
                    with self.assertRaises(SystemExit):
                        with RunLock(p): pass
                    raise ValueError('interrupt')
            with RunLock(p): pass


class CollectorTests(unittest.TestCase):
    def test_no_key_means_no_http(self):
        with patch.dict(os.environ, {}, clear=True), patch('urllib.request.urlopen') as http:
            with self.assertRaises(SystemExit): collect({'queries':['test']}, '/tmp/unused-commute-test')
            http.assert_not_called()

    def test_auth_denial_is_not_retried(self):
        error = urllib.error.HTTPError('https://example.org', 403, 'Forbidden', {}, None)
        with patch('urllib.request.urlopen', side_effect=error) as http:
            with self.assertRaises(urllib.error.HTTPError): request_json('https://example.org','test',sleeper=lambda _:None)
            self.assertEqual(http.call_count, 1)

    def test_retry_budget(self):
        error = urllib.error.HTTPError('https://example.org', 503, 'Unavailable', {}, None)
        waits=[]
        with patch('urllib.request.urlopen', side_effect=error) as http:
            with self.assertRaises(urllib.error.HTTPError): request_json('https://example.org','test',sleeper=waits.append)
            self.assertEqual(http.call_count,3)
            self.assertEqual(waits,[2,4])

    def test_rate_limit_stops_batch_without_next_query(self):
        error = urllib.error.HTTPError('https://example.org', 429, 'Rate limited', {'Retry-After':'120'}, None)
        env={'BRAVE_SEARCH_API_KEY':'test','BRAVE_STORAGE_RIGHTS_CONFIRMED':'true'}
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, env), patch('urllib.request.urlopen',side_effect=error) as http:
            with self.assertRaises(SystemExit): collect({'queries':['first','second']}, d)
            self.assertEqual(http.call_count,1)
            state=json.loads((Path(d)/'search_state.json').read_text())
            self.assertEqual(state['done'],[])
            self.assertEqual(state['failures'][0]['http_status'],429)
            self.assertTrue((Path(d)/'collection_failures.csv').exists())

    def test_resume_dedup_and_no_unreviewed_personal_text(self):
        response={'web':{'results':[{'url':'https://example.org/post/1?utm_source=x','title':'작성자 개인정보',
                                    'description':'연락처 user@example.org'},
                                   {'url':'https://example.org/post/1'},
                                   {'url':'http://example.org/insecure'},
                                   {'url':'https://example.org/u/privatehandle'}]}}
        env={'BRAVE_SEARCH_API_KEY':'test','BRAVE_STORAGE_RIGHTS_CONFIRMED':'true'}
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,env), patch('collect.request_json',return_value=response) as http, patch('collect.time.sleep'):
            cfg={'queries':['query'],'request_interval_seconds':2}
            collect(cfg,d); collect(cfg,d)
            self.assertEqual(http.call_count,1)
            rows=json.loads((Path(d)/'candidates.json').read_text())
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['review_status'],'pending')
            self.assertNotIn('evidence_excerpt',rows[0])
            self.assertNotIn('user@example.org',(Path(d)/'search_state.json').read_text())


if __name__ == '__main__': unittest.main()
