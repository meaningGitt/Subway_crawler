import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from subway_crawler.source import SourceError
from subway_crawler.recommendation.analysis import Builder, Mapper, parse_keyed, clock_seconds
from subway_crawler.recommendation.sources import acquire, download, AcquisitionError
from subway_crawler.recommendation.__main__ import export_current


class RecommendationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        inventory=pd.DataFrame([
            dict(RAIL_OPR_ISTT_CD='S1',RAIL_OPR_ISTT_NM='서울교통공사',LN_CD='1',STIN_CD='150',STIN_NM='서울역',line='1호선'),
            dict(RAIL_OPR_ISTT_CD='KR',RAIL_OPR_ISTT_NM='한국철도공사',LN_CD='1',STIN_CD='1000',STIN_NM='서울역',line='1호선'),
            dict(RAIL_OPR_ISTT_CD='S1',RAIL_OPR_ISTT_NM='서울교통공사',LN_CD='2',STIN_CD='200',STIN_NM='시청(부제)',line='2호선')])
        self.inv=self.root/'inventory.csv';inventory.to_csv(self.inv,index=False)
        self.mapper=Mapper(self.inv)
        self.source=dict(id='test',url='https://www.data.go.kr/data/test/fileData.do',cadence='연간',adapter='data_go',suffix='.csv')
        self.state=dict(raw_path='raw/test.csv',receipt={'sha256':'a'*64,'collected_at':'2026-11-08T09:00:00+09:00'},metadata={'title':'test_20241231'})
        self.builder=Builder(self.root/'out',self.mapper,[self.source],{'test':self.state})

    def test_operator_line_and_code_required_for_ambiguous_name(self):
        self.assertEqual(self.mapper.match('서울역','1호선'),(None,'ambiguous'))
        self.assertEqual(self.mapper.match('서울역','1호선','S1','0150'),('S1:1:150','matched'))
        self.assertEqual(self.mapper.match('서울역','2호선','S1','150'),(None,'unmatched'))
        self.assertEqual(self.mapper.match('시청','2호선','S1','200'),('S1:2:200','matched'))

    def test_identical_duplicate_different_source_row_vs_conflict(self):
        frame=pd.DataFrame({'id':['a','a'],'value':[4,4],'source_row':[2,3]})
        result,q=self.builder.clean(frame,['id'],['id'],'value')
        self.assertEqual(len(result),1);self.assertEqual(q['duplicate_rows'],1)
        frame.loc[1,'value']=5
        with self.assertRaises(SourceError):self.builder.clean(frame,['id'],['id'],'value')

    def test_missing_and_negative_values_not_imputed_or_valid(self):
        frame=pd.DataFrame({'id':['a','b','c','d'],'value':['',None,'-1','0']})
        result,_=self.builder.clean(frame,['id'],['id'],'value')
        self.assertEqual(result.quality_valid.tolist(),[False,False,False,True])
        self.assertTrue(pd.isna(result.value.iloc[0]))

    def test_timetable_joins_explicit_tokens_and_extended_hours(self):
        stops=parse_keyed('001-가역+002-나역+003-다역')
        arrivals=dict(parse_keyed('002-05:10+003-05:20'))
        self.assertIsNone(arrivals.get(stops[0][0]))
        self.assertEqual(arrivals[stops[1][0]],'05:10')
        self.assertEqual(clock_seconds('25:05:00'),90300)
        self.assertIsNone(clock_seconds('운행시격 3분'))
        self.assertIsNone(clock_seconds('07:61'))

    def test_od_ambiguous_origins_excluded_without_inventing_time(self):
        path=self.root/'od.csv'
        pd.DataFrame({'출발':['A','A','B','B'],'도착역':['X','X','X','Y'],'인원':[2,3,1,3],
                      '단위':['명']*4,'연도':['2024']*4}).to_csv(path,index=False)
        self.builder.od(path,'test')
        raw=pd.read_parquet(self.root/'out/datasets/od_annual.parquet')
        d=pd.read_parquet(self.root/'out/datasets/od_destination_distribution.parquet')
        self.assertEqual(len(raw),4);self.assertEqual(set(d.origin_name),{'B'})
        self.assertEqual(d.destination_share.tolist(),[.25,.75]);self.assertTrue(d.time_bin.isna().all())
        self.assertTrue(d.origin_station_key.isna().all())

    def test_captcha_is_a_stop_not_a_download_attempt(self):
        class Fake:
            calls=[]
            def post_json(self,url,data):
                self.calls.append(url)
                return {'status':True,'atchFileId':'f','fileDetailSn':'1'} if 'selectFile' in url else {'needCaptcha':True}
            def request_file(self,*a,**kw):raise AssertionError('must not download')
        fake=Fake()
        with self.assertRaises(AcquisitionError) as error:
            download(self.source,{'download_args':['a','b','','1']},fake,self.root/'file')
        self.assertEqual(error.exception.category,'user_action')

    def test_unchanged_metadata_reuses_raw_and_does_not_replace_on_check(self):
        raw=self.root/'raw/test.csv';raw.parent.mkdir();raw.write_text('a,b\n1,2\n')
        state=self.state|{'status':'수집 완료','last_downloaded':'2999-01-01T00:00:00+09:00','metadata':{'version':'same'}}
        folder=self.root/'state';folder.mkdir();p=folder/'test.json';p.write_text(json.dumps(state))
        with patch('subway_crawler.recommendation.sources.discover',return_value=({'version':'same'},b'metadata')),patch('subway_crawler.recommendation.sources.download',side_effect=AssertionError('unexpected download')):
            result=acquire(self.source,self.root);self.assertFalse(result['downloaded'])
            saved=p.read_bytes();result=acquire(self.source,self.root,check_only=True)
            self.assertEqual(p.read_bytes(),saved);self.assertEqual(result['status'],'metadata_checked')

    def test_current_export_preserves_old_period_and_applies_correction(self):
        for snapshot,rows in [('old',[('2025',1),('2026',2)]),('new',[('2026',3)])]:
            folder=self.root/'snapshots'/snapshot/'datasets';folder.mkdir(parents=True)
            pd.DataFrame(rows,columns=['period','value']).to_parquet(folder/'facts.parquet')
            (folder.parent/'manifest.json').write_text(json.dumps({'datasets':{'facts':{'keys':['period']}}}))
        (self.root/'snapshots/index.json').write_text(json.dumps(['old','new']))
        destination=self.root/'current.csv';export_current(self.root,'facts',destination)
        d=pd.read_csv(destination);self.assertEqual(d.value.tolist(),[1,3])


if __name__=='__main__':unittest.main()
