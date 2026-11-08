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
from subway_crawler.recommendation.report import coverage
from subway_crawler.recommendation.seoul import discover_seoul


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

    def test_monthly_keeps_month_grain_and_does_not_invent_weekdays(self):
        row={'사용월':'202610','호선명':'2호선','지하철역':'시청','작업일자':'20261103'}
        for hour in range(24):
            for flow in ('승차','하차'):
                row[f'{hour:02}시-{hour+1:02}시 {flow}인원']=hour+1
        path=self.root/'monthly.csv';pd.DataFrame([row]).to_csv(path,index=False)
        self.builder.monthly_ridership(path,'test')
        d=pd.read_parquet(self.root/'out/datasets/monthly_alighting_features.parquet')
        self.assertEqual(len(d),24);self.assertEqual(set(d.period),{'2026-10'})
        self.assertTrue(d.weekday.isna().all());self.assertTrue(d.direction.isna().all())
        self.assertNotIn('sample_days',d);self.assertAlmostEqual(d.within_station_month_alighting_share.sum(),1)
        self.state['metadata']['period']='202609'
        with self.assertRaises(SourceError):self.builder.monthly_ridership(path,'test')

    def test_line9_conflicting_dates_are_preserved_and_excluded(self):
        inv=pd.read_csv(self.inv,dtype=str)
        inv.loc[len(inv)]={'RAIL_OPR_ISTT_CD':'S9','RAIL_OPR_ISTT_NM':'9호선 기준기관','LN_CD':'9','STIN_CD':'926','STIN_NM':'언주','line':'9호선'}
        inv.to_csv(self.inv,index=False);self.builder.mapper=Mapper(self.inv)
        self.builder.sources['test']['kind']='line9_ridership'
        row={'날짜':'2025-03-22','호선':'9','역번호':'4126','역사명':'언주','구분':'순하차'}
        row.update({f'{h:02}시-{h+1:02}시':1 for h in range(24)})
        path=self.root/'nine.csv'
        pd.DataFrame([row,row|{'00시-01시':2},row|{'날짜':'2025-03-24','00시-01시':'1,234'}]).to_csv(path,index=False)
        self.builder.line9_ridership(path,'test')
        raw=pd.read_parquet(self.root/'out/datasets/test_conflicts.parquet')
        features=pd.read_parquet(self.root/'out/datasets/test_alighting_features.parquet')
        self.assertEqual(len(raw),2);self.assertTrue((~raw.quality_valid).all())
        self.assertEqual(len(features),24);self.assertEqual(set(features.sample_days),{1})
        self.assertEqual(set(features.station_key),{'S9:9:926'})
        self.assertEqual(features.loc[features.time_bin.eq('00시-01시'),'mean_passengers'].iloc[0],1234)

    def test_seoul_latest_month_and_public_download_parameters(self):
        class Fake:
            def get(self,url):return None,None,b'/bigfile/iot/sheet/ /dataList/dataView.do?'
            def request_file(self,method,url,path,**kw):
                path.write_text('{list:[{USE_MM:"202610"},{USE_MM:"202609"},]}')
        source=self.source|{'adapter':'seoul_sheet','dataset_id':'OA-12252','name':'Seoul'}
        body='공공누리 1유형 : 출처표시 (상업적 이용 및 변경 가능)'.encode()
        meta,_=discover_seoul(source,Fake(),body)
        self.assertEqual(meta['period'],'202610');self.assertEqual(meta['download_params']['filterCol'],'USE_MM')
        self.assertEqual(meta['download_params']['txtFilter'],'202610')
        with self.assertRaises(AcquisitionError):discover_seoul(source,Fake(),b'license changed')

    def test_coverage_does_not_spread_od_to_other_operators(self):
        self.builder.datasets={'od_annual':{},'monthly_alighting_features':{'station_keys':['KR:1:1000']}}
        cov=coverage(self.builder)
        kr=next(r for r in cov if r['운영기관']=='한국철도공사')
        seoul=next(r for r in cov if r['운영기관']=='서울교통공사')
        self.assertIn('확인 필요',kr['OD 통행량']);self.assertEqual(seoul['OD 통행량'],'미확보')
        self.assertIn('월간',kr['하차 예측 근거']);self.assertEqual(kr['일별·요일별 승하차'],'미확보')


if __name__=='__main__':unittest.main()
