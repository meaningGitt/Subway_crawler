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

VERSION = 'seat-evidence-v2'


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
        self.alias_config=json.loads((Path(__file__).resolve().parents[2]/'config/station_aliases.json').read_text())
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
        canonical_name=self.alias_config['aliases'].get(str(line),{}).get(str(name),str(name))
        rows = rows[rows.base_name.eq(name_key(canonical_name))]
        result = (rows.iloc[0].station_key, 'matched') if len(rows) == 1 else (None, 'ambiguous' if len(rows) > 1 else 'unmatched')
        self.audit.append(dict(source_name=name, source_line=line, source_operator=operator, source_code=code,
                               station_key=result[0], mapping_status=result[1], canonical_name=canonical_name, alias_evidence=self.alias_config['evidence_url'] if canonical_name!=str(name) else None, candidates='|'.join(rows.station_key)))
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
        from .holidays import Calendar
        self.calendar=Calendar(self.out.parent.parent if self.out.parent.name=='snapshots' else None)
        self.quality['holiday_calendar']=self.calendar.metadata

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
            info['usable_station_keys']=sorted(d.loc[d.quality_valid,'station_key'].dropna().unique().tolist()) if 'quality_valid' in d else info['station_keys']
        self.datasets[name] = info
        return d

    def ridership(self, path, sid):
        d = read_csv(path)
        prefix = 'historical_' if sid == 'metro_ridership' else ''
        line9 = self.sources[sid]['kind'] == 'line9_ridership'
        if line9:
            prefix = sid+'_'
            d = d.rename(columns={'날짜':'수송일자','역사명':'역명','구분':'승하차구분'})
            d['호선'] = d['호선'].map({'9':'9호선', '9호선':'9호선'})
            d['승하차구분'] = d['승하차구분'].map({'순승차':'승차','순하차':'하차'})
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
        bins = [c for c in d if (bool(re.fullmatch(r'\d{2}시-\d{2}시',c)) if line9 else '시간대' in c or c in ('06시이전', '24시이후'))]
        if len(bins) != (24 if line9 else 20):
            raise SourceError('ridership time-bin schema changed')
        if line9:
            for col in bins:
                # Some 2025 months use quoted thousands separators. Do not turn malformed
                # strings such as 1,2 into 12; only the strict integer grouping is normalized.
                grouped = d[col].str.fullmatch(r'\d{1,3}(?:,\d{3})+',na=False)
                d.loc[grouped,col] = d.loc[grouped,col].str.replace(',','',regex=False)
        d = d.rename(columns={'수송일자':'service_date','호선':'line','역번호':'station_code','역명':'station_name','승하차구분':'flow'})
        keys = ['service_date','line','station_code','flow']
        excluded = 0
        if line9:
            # Published 2025 file contains conflicting repeated dates. Preserve every conflicting
            # raw row, exclude the entire key rather than select a value or guess the intended date.
            compare = keys+['station_name']+bins
            conflict = d.drop_duplicates(compare).duplicated(keys,keep=False)
            conflict_keys = d.drop_duplicates(compare).loc[conflict,keys].drop_duplicates()
            bad = pd.MultiIndex.from_frame(d[keys]).isin(pd.MultiIndex.from_frame(conflict_keys))
            if bad.any():
                rejected = d.loc[bad].copy(); rejected['quality_valid'] = False
                rejected['exclusion_reason'] = 'conflicting_duplicate_date_station_flow'
                self.save(prefix+'conflicts',rejected,sid,'원본 행: 같은 날짜·역·승하차에 다른 값','집계 통계',[],
                          '원본 날짜 수정·값 선택 금지. 관련 키 전체를 분석 제외. 원본 행번호로 추적.', ['raw_sha256','source_row'])
            excluded = int(bad.sum()); d = d.loc[~bad].copy()
        d,q = self.clean(d, keys, keys+['station_name'])
        q['excluded_conflicting_rows'] = excluded
        dates = pd.to_datetime(d.service_date, errors='coerce')
        d['weekday'] = dates.dt.dayofweek.astype('Int64')
        d['is_holiday'] = self.calendar.flags(dates)
        d['direction'] = pd.NA
        d['quality_valid'] &= dates.notna() & d.flow.isin(['승차','하차'])
        # OA-22441's fare-system codes (4126...) are not KRIC codes (926...).
        # Unique line+name mapping is explicit; retain original codes without numeric rewriting.
        d = self.mapper.attach(d, operator='S9' if line9 else 'S1', code='absent_code' if line9 else 'station_code')
        idcols = keys+['station_name','source_row','weekday','is_holiday','direction','station_key','mapping_status','quality_valid']
        wide = d[idcols+bins].copy()
        self.save(prefix+'ridership_daily_wide',wide,sid,'역·영업일·승하차 구분·원본 시간대 열','집계 통계',['alighting','route'],
                  '개찰구 태그 통계. 방향·칸·열차·환승 하차는 구분 불가. 공식 공휴일 달력 플래그 포함.',keys,csv=False)
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
                  '원본 시간구간을 보존. 방향 없음; 결측은 0 아님.',keys+['time_bin'],csv=False)
        from .patterns import daily_patterns
        daily_patterns(self,long,sid,prefix)

    line9_ridership = ridership

    def monthly_ridership(self, path, sid):
        from .monthly import build_monthly
        build_monthly(self, path, sid)

    def segment(self,path,sid):
        from .segment_v2 import build_segment
        build_segment(self,path,sid)

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
        a['car_position']=pd.NA
        a['car_position_reason']='provider_orientation_not_verified'
        a['number_group']=a.car_no.astype(int).map(lambda n:'1-3' if n<=3 else '4-7' if n<=7 else '8-10')
        a['sample_check']=a.sample_days.map(lambda n:'under_8_days' if n<8 else 'at_least_8_days_not_validation')
        self.save('car_features',a,sid,'4호선 방향성 구간·요일·시간·호차','집계 통계',['car'],
                  '동시 10칸 관측만 집계. 상대 혼잡도는 동시 편성 평균 대비 %p. 연속 측정은 독립 표본이 아님; 타 노선 전용 금지.',group+['observation_start','observation_end'])
        summary=a.groupby('car_no').agg(mean=('mean_congestion_pct','mean'),n=('sample_observations','sum'),feature_cells=('mean_congestion_pct','size')).reset_index()
        summary['weighting']='equal_valid_feature_cells; simultaneous_10_cars_only'
        self.save('car_summary',summary,sid,'호차별 동일 가중 feature cell 평균','집계 통계',['car'],
                  '차트와 동일 CSV. car_features.mean_congestion_pct를 호차별 산술평균. 시간·구간·요일 셀 동일 가중, 측정 수 가중과 구별.', ['car_no'])
        self.context['car_means']=summary.to_dict('records')
        weekday=a[a.weekday.le(4)].groupby(['weekday','car_no']).mean_congestion_pct.mean().unstack()
        pairs=[]
        for day in weekday.index:
            for other in weekday.index:
                if day<other:
                    ranks=weekday.loc[[day,other]].T.dropna().rank()
                    if len(ranks)>1:pairs.append(float(ranks.iloc[:,0].corr(ranks.iloc[:,1])))
        self.quality[sid].update(rank_stability={'metric':'Spearman across weekday car rankings; equal feature-cell weighting','weekday_pairs':len(pairs),'median':float(pd.Series(pairs).median()) if pairs else None},
            raw_hours=sorted(d.observed_time.str[:2].dropna().unique()),raw_station_labels=sorted(d.station_name.unique()),
            unmapped_labels=sorted(d.loc[d.mapping_status.ne('matched'),'station_name'].unique()),feature_station_count=a.station_key.nunique(),
            car_orientation={'verified':False,'car_position':'unknown'},
            scope_gaps={'05_hour':'원본 미제공' if not d.observed_time.str.startswith('05:').any() else '확보','23_hour':'원본 미제공' if not d.observed_time.str.startswith('23:').any() else '확보',
                        'south_of_namtaeryeong':'확보 범위 확인 필요' if (d.station_key.str.startswith('KR:4:',na=False).any() or d.next_station_key.str.startswith('KR:4:',na=False).any()) else '원본 미제공: 확보 원본의 현재역/다음역 라벨 목록 확인','namtaeryeong_directions':d.loc[d.station_name.eq('남태령'),'next_station_name'].value_counts().to_dict()})

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
        from .timetable_v2 import canonical_timetable
        canonical_timetable(self,d,sid)


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
