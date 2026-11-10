import json
import os
from pathlib import Path
import tempfile
import unittest
import pandas as pd
from subway_crawler.recommendation.analysis import Builder,Mapper
from subway_crawler.recommendation.holidays import Calendar,refresh_calendar
from subway_crawler.recommendation.timetable_v2 import continuous_times,clock_text,infer_trip_direction
from subway_crawler.recommendation.segment_v2 import topology
from subway_crawler.recommendation.trips import join_trip_stops,query_trips
from unittest.mock import patch

REPO=Path(__file__).resolve().parents[1]

class V2Tests(unittest.TestCase):
    def test_midnight_rollover_and_small_reversal_are_distinct(self):
        rows=[{'arrival_time':None,'departure_time':'23:58'}, {'arrival_time':'00:02','departure_time':'00:03'}, {'arrival_time':'00:01','departure_time':'00:04'}]
        result=continuous_times(rows)
        self.assertEqual(clock_text(result[1][0]),'24:02:00')
        self.assertLess(result[2][0],result[1][1])
        self.assertEqual(clock_text(result[2][1]),'24:04:00')

    def test_explicit_alias_scoped_to_correct_line_and_code(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        inventory=Path(temporary.name)/'inventory.csv'
        pd.DataFrame([dict(RAIL_OPR_ISTT_CD='S1',RAIL_OPR_ISTT_NM='서울교통공사',LN_CD=line[0],STIN_CD=code,STIN_NM=name,line=line) for line,code,name in [('4호선','409','불암산(당고개)'),('7호선','2738','총신대입구(이수)')]]).to_csv(inventory,index=False)
        mapper=Mapper(inventory)
        self.assertEqual(mapper.match('당고개','4호선','S1','409'),('S1:4:409','matched'))
        self.assertEqual(mapper.match('이수','7호선','S1','2738'),('S1:7:2738','matched'))
        self.assertIsNone(mapper.match('당고개','7호선')[0])
        self.assertIsNone(mapper.match('서울','2호선')[0])

    def test_holidays_temporary_elections_and_2026_amendment(self):
        calendar=Calendar();dates=pd.Series(pd.to_datetime(['2024-10-01','2025-01-27','2025-06-03','2026-06-03','2026-05-01','2026-07-17','2024-07-17','2026-11-10','2027-01-01']))
        result=calendar.flags(dates)
        self.assertEqual(result.iloc[:8].tolist(),[True]*6+[False,False]);self.assertTrue(pd.isna(result.iloc[-1]))
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{},clear=True),patch('requests.get',side_effect=AssertionError('network without key')):
            self.assertEqual(refresh_calendar(d)['mode'],'static')

    def test_explicit_branch_graph_and_ring_wrap(self):
        edges,branches,bounds=topology(pd.DataFrame())
        self.assertEqual(edges[('2호선','201','내선')],'243')
        self.assertEqual(edges[('2호선','243','외선')],'201')
        self.assertEqual(edges[('2호선','244','외선')],'9002')
        self.assertEqual(branches[('2호선','9003','외선')],'sinjeong')
        self.assertIsNone(edges[('5호선','2549','상선')])
        self.assertIn(('7호선','2711','상선'),bounds)

    def test_month_zero_is_missing_with_raw_evidence_retained(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);inv=pd.DataFrame([dict(RAIL_OPR_ISTT_CD='S1',RAIL_OPR_ISTT_NM='서울교통공사',LN_CD='3',STIN_CD='321',STIN_NM='충무로',line='3호선')]);inv.to_csv(root/'inventory.csv',index=False)
            source={'id':'m','kind':'monthly_ridership','url':'https://data.seoul.go.kr','cadence':'월간'}
            state={'raw_path':'raw/m.csv','receipt':{'sha256':'a'*64,'collected_at':'2026-11-10'},'metadata':{'period':'202610'}}
            b=Builder(root/'out',Mapper(root/'inventory.csv'),[source],{'m':state})
            row={'사용월':'202610','호선명':'3호선','지하철역':'충무로','작업일자':'20261103'}
            row.update({f'{h:02}시-{h+1:02}시 {flow}인원':0 for h in range(24) for flow in ['승차','하차']})
            row['08시-09시 승차인원']=1  # nonzero boarding must not hide an all-zero alighting day
            pd.DataFrame([row]).to_csv(root/'raw.csv',index=False);b.monthly_ridership(root/'raw.csv','m')
            result=pd.read_parquet(root/'out/datasets/monthly_alighting_features.parquet')
            self.assertEqual(len(result),24);self.assertTrue(result.passengers.isna().all());self.assertTrue(result.raw_passengers.eq(0).all())
            self.assertTrue(result.within_station_month_alighting_share.isna().all());self.assertTrue(result.missing_reason.notna().all())

    def test_each_stop_uses_its_own_half_hour_and_destination_is_not_ridden(self):
        t=pd.DataFrame([{'trip_key':'t','stop_sequence':i+1,'station_key':name,'station_name':name,'line':'7호선','direction':'상선','day_type':'평일','departure_seconds':secs,'departure_time':clock_text(secs),'origin_name':'A','reference_date':'2026-01-01','raw_sha256':'t','is_origin_stop':i==0,'minutes_from_origin':(secs-7*3600)/60} for i,(name,secs) in enumerate([('A',7*3600),('B',7*3600+40*60),('C',8*3600)])])
        s=pd.DataFrame([{'line':'7호선','station_key':a,'next_station_key':z,'direction':'상선','day_type':'평일','time_bin':time,'mapping_status':'matched','branch_id':'main','congestion_pct':value,'reference_date':'2026-02-01','no_service_flag':False,'raw_sha256':'s'} for a,z,time,value in [('A','B','07:00',70),('B','C','07:00',5),('B','C','07:30',80)]])
        joined=join_trip_stops(t,s)
        self.assertEqual(joined.iloc[1].congestion_pct,80);self.assertTrue(joined.iloc[1].reference_date_mismatch)
        r=query_trips(joined,{'id':'x','line':'7호선','day_type':'평일','boarding_station':'A','alighting_station':'C','from_time':'07:00','to_time':'08:00'})
        self.assertEqual(r.iloc[0].estimate_status,'not_reached');self.assertTrue(r.iloc[0].is_origin_trip_at_boarding)
        self.assertTrue(pd.isna(r.iloc[0].first_stop_below_seat_line))

    def test_actual_daily_time_label_is_in_holiday_diagnostic(self):
        from types import SimpleNamespace
        from subway_crawler.recommendation.patterns import daily_patterns
        saved={}
        b=SimpleNamespace(quality={'metro_ridership_types':{}},context={},save=lambda name,frame,*a,**kw:saved.update({name:frame.copy()}))
        rows=[dict(station_key='S1:4:409',station_name='불암산',line='4호선',weekday=0,time_bin='08-09시간대',flow='하차',quality_valid=True,mapping_status='matched',service_date=date,is_holiday=holiday,passengers=value) for date,holiday,value in [('2026-01-05',False,100),('2026-01-12',False,100),('2026-02-16',True,10)]]
        daily_patterns(b,pd.DataFrame(rows),'metro_ridership_types','')
        comparison=b.quality['metro_ridership_types']['holiday_comparison']
        self.assertEqual(comparison['exclude_holidays']['station_samples'],1)
        self.assertEqual(comparison['exclude_holidays']['ratio'],1)
        self.assertEqual(comparison['include_holidays']['ratio'],.7)
        self.assertEqual(b.context['top_alighting'][0]['mean_passengers'],100)

    def test_calibration_matches_real_hour_labels_and_months(self):
        from types import SimpleNamespace
        from subway_crawler.recommendation.calibration import build_calibration
        with tempfile.TemporaryDirectory() as td:
            out=Path(td);p=out/'datasets';p.mkdir();saved={}
            daily=pd.DataFrame([dict(station_key='S1:4:409',service_date='2026-01-05',time_bin=time,flow='하차',quality_valid=True,mapping_status='matched',weekday=0,is_holiday=False,passengers=value) for time,value in [('08-09시간대',80),('09-10시간대',20)]])
            monthly=pd.DataFrame([dict(period='2026-01',station_key='S1:4:409',time_bin=time,quality_valid=True,within_station_month_alighting_share=.5,raw_sha256='m') for time in ['08시-09시','09시-10시']])
            daily.to_parquet(p/'ridership_hourly.parquet');monthly.to_parquet(p/'calibration_202601_monthly_alighting_features.parquet')
            b=SimpleNamespace(out=out,datasets={'ridership_hourly':{},'calibration_202601_monthly_alighting_features':{}},quality={},save=lambda name,frame,*a,**kw:saved.update({name:frame.copy()}))
            build_calibration(b)
            result=saved['monthly_weekday_calibration'].set_index('time_bin')
            self.assertAlmostEqual(result.loc['08:00','weekday_correction_factor'],1.6)
            self.assertEqual(b.quality['monthly_calibration']['periods'],['2026-01'])

    def test_failed_derived_stage_or_missing_raw_is_not_success(self):
        from subway_crawler.recommendation.__main__ import run_failed
        self.assertTrue(run_failed({'sources':[],'derived':[{'status':'failed'}]}))
        self.assertTrue(run_failed({'sources':[{'adapter':'file','processing_status':'unavailable'}]}))
        self.assertFalse(run_failed({'sources':[{'adapter':'review','status':'추가 권한 필요'}],'derived':[{'status':'success'}]}))

    def test_bad_query_clock_is_rejected(self):
        empty=pd.DataFrame(columns=['line','day_type'])
        with self.assertRaises(ValueError):
            query_trips(empty,{'line':'7호선','day_type':'평일','from_time':'07:00','to_time':'bad'})

    def test_export_does_not_resurrect_superseded_canonical_trips(self):
        from subway_crawler.recommendation.__main__ import export_current
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'snapshots').mkdir()
            for name,complete in [('old',True),('new',True),('failed',False)]:
                p=root/'snapshots'/name;(p/'datasets').mkdir(parents=True)
                pd.DataFrame([{'trip_key':name,'stop_sequence':1}]).to_parquet(p/'datasets/timetable_stops.parquet')
                (p/'manifest.json').write_text(json.dumps({'complete':complete,'schema_version':'seat-evidence-v2','datasets':{'timetable_stops':{'keys':['trip_key','stop_sequence']}}}))
            (root/'snapshots/index.json').write_text(json.dumps(['old','new','failed']))
            export_current(root,'timetable_stops',root/'current.csv')
            self.assertEqual(pd.read_csv(root/'current.csv').trip_key.tolist(),['new'])

    def test_feedback_schema_has_no_location_or_probability_fields(self):
        schema=json.loads((REPO/'config/feedback.schema.json').read_text());p=schema['properties']
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(p['result']['enum'],['바로 앉음','가다가 앉음','끝까지 서서','안 탐'])
        self.assertTrue({'pseudonymous_id','recommended_train_time','actual_boarded_train_time','recorded_at'}<=set(schema['required']))
        self.assertFalse(any('probability' in k or 'latitude' in k or 'longitude' in k for k in p))

@unittest.skipUnless(os.environ.get('SEAT_V2_SNAPSHOT'),'integration dataset not supplied')
class V2DataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root=Path(os.environ['SEAT_V2_SNAPSHOT']);cls.q=json.loads((cls.root/'quality.json').read_text())['sources']
    def test_acceptance_474_42_12_and_negative_under_one_percent(self):
        q=self.q['kric_timetable'];self.assertEqual(q['line4_weekday_hyehwa_stops'],474)
        self.assertEqual(q['line2_sindorim_daerim_07_09']['departures'],42);self.assertEqual(q['line2_sindorim_daerim_07_09']['origin_departures'],12)
        self.assertLess(q['line1_negative_duration_ratio'],.01);self.assertEqual(q['cross_version_duplicate_combinations'],0)
    def test_segment_mapping_and_terminal_direction_checks(self):
        q=self.q['metro_segment'];self.assertEqual(q['unmatched_rows'],0)
        self.assertTrue(all(c['all_zero'] for c in q['direction_terminal_checks']))
    def test_car_chart_matches_csv_exactly(self):
        p=self.root/'datasets';a=pd.read_parquet(p/'car_features.parquet');s=pd.read_parquet(p/'car_summary.parquet')
        means=a.groupby('car_no').mean_congestion_pct.mean();summary=s.set_index('car_no')['mean'];pd.testing.assert_series_equal(means,summary,check_names=False)
        self.assertIn('S1:4:409',set(a.station_key));self.assertTrue(a.car_position.isna().all())
    def test_holiday_target_and_monthly_coverage(self):
        self.assertGreaterEqual(self.q['metro_ridership_types']['holiday_comparison']['exclude_holidays']['ratio'],.95)
        self.assertEqual(len(self.q['monthly_calibration']['periods']),6)
    def test_zero_alighting_months_with_nonzero_boarding_are_missing(self):
        a=pd.read_parquet(self.root/'datasets/monthly_alighting_features.parquet')
        keys={'IC:7:756','S1:3:321','S1:6:2615','S1:6:2649','KR:K1:K222'}
        target=a[a.station_key.isin(keys)]
        self.assertEqual(len(target),120)
        self.assertTrue(target.raw_passengers.eq(0).all())
        self.assertTrue(target.passengers.isna().all())
        self.assertTrue(target.within_station_month_alighting_share.isna().all())
        self.assertTrue(target.missing_reason.notna().all())

    def test_time_local_example_values_not_injected(self):
        scenarios=pd.read_csv(self.root/'scenario_validation.csv');self.assertEqual(len(scenarios),5)
        # A real time-local query may disagree with the supplied fixed-bin expectations; preserve/report it.
        self.assertTrue(scenarios.actual_departure.notna().all())

if __name__=='__main__':unittest.main()
