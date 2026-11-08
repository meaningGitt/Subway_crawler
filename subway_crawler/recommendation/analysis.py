"""Schema-specific transforms. Missing granularity stays missing; all results carry lineage."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import re
import zipfile

import openpyxl
import pandas as pd

from ..source import LINE_NAMES, SourceError
from ..store import atomic_write

VERSION = 'seat-evidence-v1'


def read_csv(path):
    for encoding in ('utf-8-sig', 'cp949'):
        try:
            return pd.read_csv(path, encoding=encoding, dtype=str)
        except UnicodeDecodeError:
            pass
    raise SourceError('unsupported CSV encoding')


def name_key(value):
    # Remove station subtitles only. Never strip a final 역 or apply fuzzy matching.
    return re.sub(r'\([^)]*\)', '', str(value)).strip()


class Mapper:
    def __init__(self, path):
        self.inventory = pd.read_csv(path, dtype=str).fillna('')
        self.inventory['station_key'] = self.inventory[['RAIL_OPR_ISTT_CD', 'LN_CD', 'STIN_CD']].agg(':'.join, axis=1)
        self.inventory['base_name'] = self.inventory.STIN_NM.map(name_key)
        self.cache = {}; self.audit = []

    def match(self, name, line=None, operator=None, code=None):
        token = (str(name), str(line), str(operator), str(code))
        if token in self.cache:
            return self.cache[token]
        rows = self.inventory
        if operator:
            rows = rows[rows.RAIL_OPR_ISTT_CD.eq(operator)]
        if line:
            rows = rows[rows.line.eq(str(line))]
        if code is not None and pd.notna(code):
            code = str(code).removesuffix('.0').lstrip('0') or '0'
            rows = rows[rows.STIN_CD.str.lstrip('0').eq(code)]
        rows = rows[rows.base_name.eq(name_key(name))]
        result = (rows.iloc[0].station_key, 'matched') if len(rows) == 1 else (None, 'ambiguous' if len(rows) > 1 else 'unmatched')
        self.audit.append(dict(source_name=name, source_line=line, source_operator=operator, source_code=code,
                               station_key=result[0], mapping_status=result[1], candidates='|'.join(rows.station_key)))
        self.cache[token] = result
        return result

    def attach(self, frame, operator=None, code='station_code', name='station_name', line='line', prefix=''):
        distinct = frame[[c for c in (name, line, code) if c in frame]].drop_duplicates()
        lookup = {}
        for r in distinct.to_dict('records'):
            k = tuple(str(r.get(c)) for c in (name, line, code))
            lookup[k] = self.match(r[name], r.get(line), operator, r.get(code))
        keys = [lookup[tuple(str(r.get(c)) for c in (name, line, code))] for r in frame.to_dict('records')]
        frame[prefix+'station_key'] = [k[0] for k in keys]
        frame[prefix+'mapping_status'] = [k[1] for k in keys]
        return frame


class Builder:
    def __init__(self, out, mapper, sources, states):
        self.out = Path(out); self.out.mkdir(parents=True, exist_ok=True)
        self.mapper = mapper; self.sources = {s['id']: s for s in sources}; self.states = states
        self.datasets = {}; self.quality = {}; self.context = {}

    def clean(self, d, keys, required, value=None):
        missing = {c: int(d[c].isna().sum()) for c in required}
        duplicate = int(d.duplicated(keys).sum())
        compare = [c for c in d if c not in ('source_row', 'source_member', '연번')]
        if d.drop_duplicates(compare).duplicated(keys, keep=False).any():
            raise SourceError('conflicting duplicate keys; promotion blocked')
        d = d.drop_duplicates(keys).copy()
        invalid = pd.Series(False, index=d.index)
        if value:
            d[value] = pd.to_numeric(d[value], errors='coerce')
            invalid = d[value].lt(0) | (d[value].isna())
        q = dict(input_rows=len(d)+duplicate, duplicate_rows=duplicate, missing_required=missing,
                 invalid_value_rows=int(invalid.sum()), unique_keys=keys)
        d['quality_valid'] = ~invalid & d[required].notna().all(axis=1)
        return d, q

    def save(self, name, d, sid, grain, nature, uses, caution, keys=None, csv=True):
        d = d.copy()
        state = self.states[sid]
        d['source_id'] = sid
        d['raw_sha256'] = state['receipt']['sha256']
        d['collected_at'] = state['receipt']['collected_at']
        d['transform_version'] = VERSION
        path = self.out / 'datasets' / (name + '.parquet'); path.parent.mkdir(exist_ok=True)
        temp = path.with_suffix('.tmp'); d.to_parquet(temp, index=False); temp.replace(path)
        if csv:
            p = path.with_suffix('.csv'); tmp = p.with_suffix('.tmp')
            d.to_csv(tmp, index=False, encoding='utf-8-sig'); tmp.replace(p)
        dates = []
        for col in ('service_date', 'reference_date', 'period', 'observation_start', 'observation_end', 'year'):
            if col in d and d[col].notna().any():
                dates.append({'field': col, 'min': str(d[col].dropna().min()), 'max': str(d[col].dropna().max())})
        info = dict(rows=len(d), source_id=sid, source_url=self.sources[sid]['url'],
                    raw_path=state['raw_path'], raw_sha256=state['receipt']['sha256'],
                    grain=grain, nature=nature, uses=uses, caution=caution, keys=keys,
                    cadence=self.sources[sid]['cadence'], dates=dates, columns={},
                    parquet_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        for col in d:
            info['columns'][col] = {'dtype': str(d[col].dtype), 'missing': int(d[col].isna().sum())}
        if 'mapping_status' in d:
            info['mapping'] = d.mapping_status.value_counts().to_dict()
        if 'line' in d:
            info['line_rows'] = d.line.value_counts().to_dict()
        if 'station_key' in d:
            info['station_keys'] = sorted(d.station_key.dropna().unique().tolist())
        self.datasets[name] = info
        return d

    def ridership(self, path, sid):
        d = read_csv(path)
        prefix = 'historical_' if sid == 'metro_ridership' else ''
        if sid == 'metro_ridership_types':
            d=d.rename(columns={'호선명':'호선','06시간대이전':'06시이전','24시간대이후':'24시이후'})
            d['source_row']=d.index+2
            type_keys=['수송일자','호선','역번호','역명','승하차구분','승객유형']
            if not set(type_keys).issubset(d):raise SourceError('passenger-type schema changed')
            d,qtypes=self.clean(d,type_keys,type_keys)
            bins=[c for c in d if '시간대' in c or c in ('06시이전','24시이후')]
            for c in bins:d[c]=pd.to_numeric(d[c],errors='coerce').where(lambda s:s.ge(0))
            self.save('ridership_passenger_types',d,sid,'역·영업일·승하차 구분·승객유형·시간대 열','집계 통계',['alighting','route'],
                      '승객유형별 원본. 시간대 합산은 모든 승객유형 값이 유효한 경우에만 수행.',type_keys,csv=False)
            if d['승객유형'].str.contains('합계|총계|소계',regex=True).any():raise SourceError('passenger subtotals need review')
            group=type_keys[:-1]
            grouped=d.groupby(group,dropna=False)
            totals=grouped[bins].sum(min_count=1).where(grouped[bins].count().eq(grouped.size(),axis=0))
            d=totals.join(grouped.source_row.min()).reset_index()
        required = {'수송일자', '호선', '역번호', '역명', '승하차구분'}
        if not required.issubset(d):
            raise SourceError('ridership schema changed')
        if 'source_row' not in d:d['source_row'] = d.index + 2
        bins = [c for c in d if '시간대' in c or c in ('06시이전', '24시이후')]
        if len(bins) != 20:
            raise SourceError('ridership time-bin schema changed')
        d = d.rename(columns={'수송일자':'service_date','호선':'line','역번호':'station_code','역명':'station_name','승하차구분':'flow'})
        keys = ['service_date','line','station_code','flow']
        d,q = self.clean(d, keys, keys+['station_name'])
        dates = pd.to_datetime(d.service_date, errors='coerce')
        d['weekday'] = dates.dt.dayofweek.astype('Int64')
        d['is_holiday'] = pd.Series(pd.NA, index=d.index, dtype='boolean')
        d['direction'] = pd.NA
        d['quality_valid'] &= dates.notna() & d.flow.isin(['승차','하차'])
        d = self.mapper.attach(d, operator='S1')
        idcols = keys+['station_name','source_row','weekday','is_holiday','direction','station_key','mapping_status','quality_valid']
        wide = d[idcols+bins].copy()
        self.save(prefix+'ridership_daily_wide',wide,sid,'역·영업일·승하차 구분·원본 시간대 열','집계 통계',['alighting','route'],
                  '개찰구 태그 통계. 방향·칸·열차·환승 하차는 구분 불가. 공휴일 미분류.',keys,csv=False)
        # Long form by month keeps memory bounded. Cross-month median uses all daily observations.
        parts=[]
        for _, block in d.groupby(d.service_date.str[:7]):
            part=block[idcols+bins].melt(id_vars=idcols,value_vars=bins,var_name='time_bin',value_name='passengers')
            part['passengers']=pd.to_numeric(part.passengers,errors='coerce')
            part['quality_valid'] &= part.passengers.notna() & part.passengers.ge(0)
            parts.append(part)
        long=pd.concat(parts,ignore_index=True)
        q['invalid_cells']=int((~long.quality_valid).sum());self.quality[sid]=q
        self.save(prefix+'ridership_hourly',long,sid,'역·영업일·원본 시간대·승하차 구분','집계 통계',['alighting','route'],
                  '06시 이전/24시 이후는 개방 구간 그대로 보존. 방향 없음; 결측은 0 아님.',keys+['time_bin'],csv=False)
        valid=long[long.quality_valid & long.mapping_status.eq('matched')]
        group=['station_key','station_name','line','weekday','time_bin','flow']
        agg=valid.groupby(group,dropna=False).agg(mean_passengers=('passengers','mean'),median_passengers=('passengers','median'),
                p90_passengers=('passengers',lambda s:s.quantile(.9)),sample_days=('service_date','nunique'),
                observation_start=('service_date','min'),observation_end=('service_date','max')).reset_index()
        agg['direction']=pd.NA;agg['is_holiday']=pd.NA;agg['sample_check']=agg.sample_days.map(lambda n:'under_8_days' if n<8 else 'at_least_8_days_not_validation')
        self.save(prefix+'station_weekday_patterns',agg,sid,'역·요일(월0)·시간대·승하차 구분','집계 통계',['alighting','route'],
                  '8일 미만은 운영상 검토 기준이며 정확도 보장이 아님. 공휴일은 일반 요일 안에 섞임.',group+['observation_start','observation_end'])
        self.save(prefix+'alighting_features',agg[agg.flow.eq('하차')],sid,'역·요일·시간대','집계 통계',['alighting'],
                  '하차 수요 사전값 후보. 열차 하차량·빈 좌석·좌석 확률이 아님. 방향 미상.',group+['observation_start','observation_end'])
        self.context['hourly']=valid.groupby(['time_bin','flow']).passengers.mean().unstack().reindex(bins).reset_index().to_dict('records')
        self.context['top_alighting']=agg[agg.flow.eq('하차')].sort_values('mean_passengers',ascending=False).head(15).to_dict('records')

    def segment(self,path,sid):
        d=read_csv(path).rename(columns={'구분':'day_type','요일구분':'day_type','호선':'line','역번호':'station_code','역명':'station_name','출발역':'station_name','상하구분':'direction'})
        keys=['day_type','line','station_code','direction']; bins=[c for c in d if re.fullmatch(r'\d+시\d+분',c)]
        if not bins or not set(keys+['station_name']).issubset(d):raise SourceError('congestion schema changed')
        d['source_row']=d.index+2;d=self.mapper.attach(d,operator='S1')
        cols=keys+['station_name','station_key','mapping_status','source_row']
        d=d[cols+bins].melt(id_vars=cols,value_vars=bins,var_name='time_bin',value_name='congestion_pct')
        d,q=self.clean(d,keys+['time_bin'],keys,'congestion_pct');self.quality[sid]=q
        d['reference_date']=self.reference_date(sid);d['observation_n']=pd.NA
        self.save('segment_congestion',d,sid,'역·상하 구분·요일구분·30분 공표 평균','집계 통계',['car_context','route'],
                  '100% 초과는 오류가 아님. 원표본 수·중앙값·분위수 미제공; 칸별 수치 아님.',keys+['time_bin','reference_date'])
        self.context['segment_top']=d[d.quality_valid].sort_values('congestion_pct',ascending=False).head(10).to_dict('records')

    def reference_date(self,sid):
        title=self.states[sid]['metadata']['title'] or ''
        found=re.findall(r'(20\d{6})',title)
        return pd.Timestamp(found[-1]).date().isoformat() if found else None

    def cars(self,path,sid):
        parts=[]
        with zipfile.ZipFile(path) as outer:
            for member in outer.infolist():
                if not member.filename.endswith('.zip'):continue
                if member.file_size>256*1024**2:raise SourceError('nested archive exceeds limit')
                with zipfile.ZipFile(io.BytesIO(outer.read(member))) as inner:
                    for file in inner.infolist():
                        if not file.filename.endswith('.csv'):continue
                        if file.file_size>512*1024**2:raise SourceError('expanded CSV exceeds limit')
                        with inner.open(file) as f:d=pd.read_csv(f,encoding='utf-8-sig',dtype=str)
                        d['source_member']=member.filename+'!'+file.filename;d['source_row']=d.index+2;parts.append(d)
        if not parts:raise SourceError('car archive structure changed')
        d=pd.concat(parts,ignore_index=True).rename(columns={'운행일자':'service_date','요일구분':'day_type','측정시각':'observed_time',
          '편성번호':'formation_id','호차':'car_no','현재역':'station_name','다음역':'next_station_name','혼잡도':'congestion_pct'})
        keys=['service_date','observed_time','formation_id','car_no','station_name','next_station_name']
        if not set(keys+['day_type','congestion_pct']).issubset(d):raise SourceError('car columns changed')
        d['congestion_pct']=d.congestion_pct.str.rstrip('%');d,q=self.clean(d,keys,keys,'congestion_pct')
        dates=pd.to_datetime(d.service_date,errors='coerce');d['weekday']=dates.dt.dayofweek.astype('Int64')
        d['time_bin']=d.observed_time.str[:2]+':00';d['line']='4호선'
        # Name mapping is scoped to line, but not to vehicle owner: trains may cross operators.
        pairs={n:self.mapper.match(n,'4호선') for n in pd.concat([d.station_name,d.next_station_name]).unique()}
        d['station_key']=d.station_name.map(lambda n:pairs[n][0]);d['mapping_status']=d.station_name.map(lambda n:pairs[n][1])
        d['next_station_key']=d.next_station_name.map(lambda n:pairs[n][0])
        d['direction']=d.station_name+' → '+d.next_station_name
        times={t:clock_seconds(t) is not None for t in d.observed_time.dropna().unique()}
        d['quality_valid'] &= dates.notna() & d.car_no.isin([str(i) for i in range(1,11)]) & d.observed_time.map(times).fillna(False)
        q['invalid_cells']=int((~d.quality_valid).sum());self.quality[sid]=q
        self.save('car_observations',d,sid,'운행일·측정시각·편성번호·현재역→다음역·호차','실제 관측값',['car'],
                  '제공기관 측정/산출 혼잡도. 4호선 일부 신조 편성만, 좌석 점유 아님. 편성번호는 운행 열차번호와 다름.',keys,csv=False)
        v=d[d.quality_valid & d.station_key.notna() & d.next_station_key.notna()].copy()
        # Compare cars within the SAME observation to avoid unequal train sample bias.
        events=['service_date','observed_time','formation_id','station_name','next_station_name']
        v['observed_car_count']=v.groupby(events).car_no.transform('nunique')
        v['relative_congestion_pct']=v.congestion_pct-v.groupby(events).congestion_pct.transform('mean')
        v=v[v.observed_car_count.eq(10)]
        group=['line','station_key','next_station_key','station_name','next_station_name','direction','day_type','weekday','time_bin','car_no']
        a=v.groupby(group,dropna=False).agg(mean_congestion_pct=('congestion_pct','mean'),median_congestion_pct=('congestion_pct','median'),
              p90_congestion_pct=('congestion_pct',lambda s:s.quantile(.9)),mean_relative_pct=('relative_congestion_pct','mean'),
              sample_observations=('congestion_pct','size'),sample_days=('service_date','nunique'),sample_formations=('formation_id','nunique'),
              observation_start=('service_date','min'),observation_end=('service_date','max')).reset_index()
        a['sample_check']=a.sample_days.map(lambda n:'under_8_days' if n<8 else 'at_least_8_days_not_validation')
        self.save('car_features',a,sid,'4호선 방향성 구간·요일·시간·호차','집계 통계',['car'],
                  '동시 10칸 관측만 집계. 상대 혼잡도는 동시 편성 평균 대비 %p. 연속 측정은 독립 표본이 아님; 타 노선 전용 금지.',group+['observation_start','observation_end'])
        self.context['car_means']=v.groupby('car_no').agg(mean=('congestion_pct','mean'),n=('congestion_pct','size')).reset_index().to_dict('records')

    def od(self,path,sid):
        d=read_csv(path).rename(columns={'출발':'origin_name','도착역':'destination_name','인원':'passengers','단위':'unit','연도':'year'})
        keys=['year','origin_name','destination_name'];d['source_row']=d.index+2
        # Public labels are not unique: e.g. two station/line entries share 부평구청.
        # Preserve every source row and exclude the entire ambiguous origin denominator.
        ambiguous=d.duplicated(keys,keep=False)
        ambiguous_origins=set(d.loc[ambiguous,'origin_name'])
        d,q=self.clean(d,['source_row'],keys,'passengers')
        d['ambiguous_od_label']=d.origin_name.isin(ambiguous_origins)
        q['duplicate_label_rows']=int(ambiguous.sum());q['ambiguous_origins']=sorted(ambiguous_origins)
        self.quality[sid]=q
        # OD labels lack an authoritative per-label operator/line dictionary. Do not guess.
        d['origin_station_key']=pd.NA;d['destination_station_key']=pd.NA;d['mapping_status']='needs_OD_codebook'
        self.save('od_annual',d,sid,'연간·원본 출발역 라벨·원본 도착역 라벨','집계 통계',['alighting_prior'],
                  '역 코드·노선·요일·시각 없음. 서울(1) 등 라벨은 별도 코드북 확보 전 자동 매핑 금지. 중복 라벨은 행번호로 보존.', ['raw_sha256','source_row'])
        v=d[d.quality_valid & ~d.ambiguous_od_label].copy();v['origin_total']=v.groupby(['year','origin_name']).passengers.transform('sum')
        v['destination_share']=v.passengers.div(v.origin_total.where(v.origin_total.gt(0)))
        v['time_bin']=pd.NA
        self.save('od_destination_distribution',v,sid,'연간 출발역 원본 라벨 조건부 목적지 비중','집계 통계',['alighting_prior'],
                  '중복 라벨이 있는 출발역은 전체 제외. 나머지 수집 파일 내 목적지 비중. 좌석확률 아님. 원표의 0 유지; 시각·열차·칸 분해 금지.',keys)
        self.context['od_top']=v[v.passengers.gt(0)].sort_values('passengers',ascending=False).head(10).to_dict('records')

    def inflow(self,path,sid):
        d=read_csv(path).rename(columns={'호선':'line','고유역번호(외부역코드)':'station_code','역명':'station_name','수송연월':'period','환승유입인원수':'passengers'})
        d['line']=d.line.str.replace('호선','',regex=False)+'호선';d['source_row']=d.index+2
        keys=['period','line','station_code'];d,q=self.clean(d,keys,keys,'passengers');self.quality[sid]=q
        d=self.mapper.attach(d,operator='S1')
        self.save('transfer_inflow_monthly',d,sid,'월·도착역의 타선 유입하차','집계 통계',['alighting_context'],
                  '타선에서 승차하여 자선에서 하차한 수요. 이 역에서 갈아탄 인원 또는 시간대별 환승수요가 아님.',keys)

    def transfer(self,path,sid):
        d=read_csv(path).rename(columns={'역명':'station_name'})
        if not {'station_name','평일','토요일','일요일'}.issubset(d):raise SourceError('transfer schema changed')
        d['source_row']=d.index+2
        d=d.melt(id_vars=['station_name','source_row'],value_vars=['평일','토요일','일요일'],var_name='day_type',value_name='passengers')
        keys=['station_name','day_type'];d,q=self.clean(d,keys,keys,'passengers');self.quality[sid]=q
        d['reference_date']=self.reference_date(sid);d['station_key']=pd.NA;d['mapping_status']='station_complex_only'
        self.save('transfer_demand',d,sid,'환승역명·평일/토요일/일요일 공표 인원','집계 통계',['alighting_context','route'],
                  '호선·환승방향·시각 미제공. 환승역 전체 통계를 각 호선에 복제하지 않음. 표본수 미제공.',keys+['reference_date'])

    def timetable(self,path,sid):
        w=openpyxl.load_workbook(path,read_only=True,data_only=True);rows=iter(w.active.values);cols=next(rows)
        data=[]
        for i,r in enumerate(rows,2):
            if len(str(r[1]))>3 and str(r[1])[1:3] in {'11','28','41','44'}:
                raw=dict(zip(cols[:13],r[:13]))|{'source_row':i}
                station_text=str(r[7] or '')
                packed=parse_keyed(station_text)
                if packed:
                    arrivals=dict(parse_keyed(str(r[8] or '')))
                    departures=dict(parse_keyed(str(r[9] or '')))
                    for n,(token,station) in enumerate(packed,1):
                        item=raw|{'운행구간정거장':station,'정거장도착시각':arrivals.get(token),
                                  '정가장출발시각':departures.get(token),'source_stop_token':token,'packed_sequence':n}
                        if n==1 and item['정거장도착시각']=='00:00':item['정거장도착시각']=None
                        if n==len(packed) and item['정가장출발시각']=='00:00':item['정가장출발시각']=None
                        data.append(item)
                else:data.append(raw|{'source_stop_token':None,'packed_sequence':None})
        w.close();d=pd.DataFrame(data).rename(columns={'열차번호':'train_no','노선번호':'source_line_code','노선명':'source_line_name',
          '운행구간기점명':'origin_name','운행구간종점명':'destination_name','운행유형':'service_type','요일구분':'day_type',
          '운행구간정거장':'station_name','정거장도착시각':'arrival_time','정가장출발시각':'departure_time','데이터기준일자':'reference_date'})
        needed=['train_no','source_line_code','source_line_name','origin_name','destination_name','service_type','day_type','station_name','reference_date']
        if not set(needed).issubset(d):raise SourceError('timetable schema changed')
        d['line']=[LINE_NAMES.get(c,n) for c,n in zip(d.source_line_code,d.source_line_name)]
        d['reference_date']=pd.to_datetime(d.reference_date,errors='coerce',format='mixed').dt.strftime('%Y-%m-%d');d['effective_date']=pd.NA
        for c in ('arrival_time','departure_time'):d[c]=d[c].map(lambda x:x.isoformat() if hasattr(x,'isoformat') else (str(x) if pd.notna(x) else None))
        trip=['source_line_code','train_no','day_type','service_type','origin_name','destination_name','reference_date']
        d=d.drop(columns=['운행속도','운영기관전화번호'],errors='ignore')
        # Preserve the published row sequence, validate temporal order before producing edges.
        d['stop_sequence']=d.groupby(trip,dropna=False).cumcount()+1
        d['train_no']=d.train_no.astype(str)
        d['trip_key']=d[trip].fillna('').astype(str).agg('|'.join,axis=1)
        d,q=self.clean(d,['trip_key','stop_sequence'],needed);self.quality[sid]=q
        d=self.mapper.attach(d,code='absent_code')
        d['direction']=d.origin_name+' → '+d.destination_name
        self.save('timetable_stops',d,sid,'공표 운행·요일구분·정차순서','공표 계획',['route'],
                  '정차순서는 원본 행 순서. 기준일≠시행일. 운영기관 원본 열 없음; 직통 구간 운영기관 추정 금지.', ['trip_key','stop_sequence'])
        edges=[]
        for key,g in d.groupby('trip_key',sort=False):
            records=g.to_dict('records')
            for a,b in zip(records,records[1:]):
                dep=clock_seconds(a['departure_time']);arr=clock_seconds(b['arrival_time'])
                duration=None
                if dep is not None and arr is not None:
                    diff=arr-dep
                    if diff < -12*3600:diff+=86400
                    if 0<=diff<=7200:duration=diff
                edges.append({k:a[k] for k in ['trip_key','train_no','line','source_line_code','day_type','service_type','origin_name','destination_name','direction','reference_date','effective_date','stop_sequence','station_key','station_name','departure_time']}|
                   {'next_station_key':b['station_key'],'next_station_name':b['station_name'],'arrival_time':b['arrival_time'],
                    'travel_seconds':duration,'quality_valid':duration is not None and pd.notna(a['station_key']) and pd.notna(b['station_key']),
                    'source_row':a['source_row'],'next_source_row':b['source_row']})
        e=pd.DataFrame(edges)
        self.save('route_features',e,sid,'공표 운행의 연속 정차역 간 연결','공표 계획에서 계산',['route'],
                  '시간차 0~7200초만 채택, 큰 자정 역전만 익일 처리. 환승 보행시간 없음. 시발·종착은 빈 좌석/동일 차량 회차의 증거 아님.', ['trip_key','stop_sequence'])
        self.quality[sid]['unusable_edges']=int((~e.quality_valid).sum())


def clock_seconds(value):
    if value is None or pd.isna(value):return None
    m=re.fullmatch(r'(\d{1,2}):(\d{2})(?::(\d{2}))?',str(value))
    if not m:return None
    h,minute,sec=int(m[1]),int(m[2]),int(m[3] or 0)
    return h*3600+minute*60+sec if h<48 and minute<60 and sec<60 else None


def parse_keyed(text):
    """KRIC lists carry explicit stop tokens; align on those, never zip missing times."""
    parts=re.split(r'[+,/]',text)
    if not parts or not all(re.match(r'^[A-Za-z]?\d+-',p.strip()) for p in parts):return []
    return [tuple(p.strip().split('-',1)) for p in parts]
