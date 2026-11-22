"""Incremental supplements only. Never invokes any existing full collector."""
import argparse
import csv
from datetime import datetime, timedelta
import hashlib
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from zoneinfo import ZoneInfo

from ..store import atomic_write
from ..source import Client, metadata, SourceError
from ..recommendation.sources import acquire, AcquisitionError, catalog
from .inventory import inspect_existing, derive_last_trains, sha

KST=ZoneInfo('Asia/Seoul')
TABLES=['last_train_supplement','transfer_connections','timetable_updates','new_user_pain_points','station_facilities']
FIELDS={
 'last_train_supplement':['source_id','station_key','line','day_type','direction','destination_name','last_departure','effective_date','service_day_offset','service_status'],
 'transfer_connections':['source_id','source_record_id','station_name','from_line','to_line','from_station_key','to_station_key','from_direction','to_direction','raw_destination_code','duration_raw','duration_seconds','distance_m','reference_date','catalog_reference_date','filename_date','metadata_modified','issues','service_status'],
 'timetable_updates':['source_id','event_type','title','published_at','effective_start','effective_end','line','direction','source_modified','previous_source_modified','reference_date','service_status'],
 'new_user_pain_points':['platform','source_url','published_date','collected_at','commute_type','situation','cause','workaround','evidence_excerpt','original_page_status','service_status'],
 'station_facilities':['source_id','station_key','facility_type','exit_number','platform','connected_facility','official_duration_seconds','service_status']}

def now(): return datetime.now(KST).isoformat(timespec='seconds')
def enc(obj): return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def ident(obj): return hashlib.sha256(enc(obj).encode()).hexdigest()
def csv_write(path,rows,fields=None):
    fields=fields or list(dict.fromkeys(k for r in rows for k in r)) or ['status']
    out=io.StringIO();w=csv.DictWriter(out,fieldnames=fields);w.writeheader()
    for r in rows:
        vals={k:(enc(v) if isinstance(v,(dict,list)) else v) for k,v in r.items()}
        for k,v in vals.items():
            if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')): vals[k]="'"+v
        w.writerow(vals)
    atomic_write(path,'\ufeff'+out.getvalue())

class Lock:
    def __init__(self,path): self.path=path
    def __enter__(self):
        import fcntl
        self.path.parent.mkdir(parents=True,exist_ok=True);self.f=self.path.open('a')
        try: fcntl.flock(self.f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: self.f.close();raise RuntimeError('SKIPPED_OVERLAP')
    def __exit__(self,*args):self.f.close()

def connect(path):
    db=sqlite3.connect(path);db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=FULL')
    db.executescript('''CREATE TABLE IF NOT EXISTS records(kind TEXT,key TEXT,payload TEXT,hash TEXT,PRIMARY KEY(kind,key));
CREATE TABLE IF NOT EXISTS versions(kind TEXT,key TEXT,hash TEXT,payload TEXT,collected_at TEXT,PRIMARY KEY(kind,key,hash));
CREATE TABLE IF NOT EXISTS source_state(id TEXT PRIMARY KEY,payload TEXT);
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,started_at TEXT,ended_at TEXT,status TEXT,payload TEXT);''')
    return db

def upsert(db,kind,key,row):
    payload=enc(row);h=ident(row);old=db.execute('SELECT hash FROM records WHERE kind=? AND key=?',(kind,key)).fetchone()
    if old and old[0]==h:return 'unchanged'
    db.execute('INSERT OR IGNORE INTO versions VALUES(?,?,?,?,?)',(kind,key,h,payload,now()))
    db.execute('INSERT OR REPLACE INTO records VALUES(?,?,?,?)',(kind,key,payload,h))
    return 'changed' if old else 'new'

def deduplicate_batch(records):
    """Reject conflicting provider IDs, instead of silently choosing the last row."""
    seen={};unique=[];duplicates=0
    for row in records:
        key=row.get('source_record_id') or ident({k:v for k,v in row.items() if k!='collected_at'})
        if key in seen:
            if seen[key]!=row:raise AcquisitionError('conflicting duplicate source IDs; batch rolled back','schema')
            duplicates+=1;continue
        seen[key]=row;unique.append(row)
    return unique,duplicates

def state_get(db,sid):
    r=db.execute('SELECT payload FROM source_state WHERE id=?',(sid,)).fetchone();return json.loads(r[0]) if r else {}
def state_set(db,sid,value):db.execute('INSERT OR REPLACE INTO source_state VALUES(?,?)',(sid,enc(value)))

def validate_record(kind,row):
    issues=list(row.get('issues',[]))
    required={'transfer_connections':['station_name','from_line','source_record_id'],
              'new_user_pain_points':['platform','situation','evidence_excerpt','original_page_status'],
              'timetable_updates':['event_type','title']}.get(kind,[])
    issues += ['missing_'+k for k in required if not row.get(k)]
    try:url_key(row.get('source_url',''))
    except ValueError:issues.append('invalid_source_url')
    for field in ['published_date','reference_date','effective_start','effective_end','source_modified']:
        value=row.get(field)
        if not value:continue
        try:
            if re.fullmatch(r'\d{8}',value):datetime.strptime(value,'%Y%m%d')
            else:datetime.fromisoformat(value)
        except (ValueError,TypeError):issues.append('invalid_'+field)
    if issues:
        row['issues']=sorted(set(issues));row['service_status']='NOT_USABLE'
    return row
def url_key(url):
    p=urlsplit(url)
    if p.scheme!='https' or not p.hostname or p.username or p.password:raise ValueError('invalid source URL')
    path=p.path.rstrip('/')
    if p.hostname=='community.linkareer.com':
        path=re.sub(r'^/job-materials/(\d+)$',r'/honeytips/\1',path)
        path=re.sub(r'^/(?:mentor_intern|mentor-employed)/(\d+)$',r'/mentor-qna/\1',path)
    query=[(k,v) for k,v in parse_qsl(p.query) if k in {'id','no','num','articleNo'}]
    return urlunsplit(('https',p.netloc.lower(),path,urlencode(sorted(query)),''))

def date_in_name(value):
    m=re.search(r'(20\d{6})(?:\D|$)',value or '')
    if not m:return None
    try:return datetime.strptime(m[1],'%Y%m%d').date().isoformat()
    except ValueError:return None

def duration(text):
    m=re.fullmatch(r'(\d{1,3}):([0-5]\d)',text.strip())
    if not m: raise ValueError('invalid duration format; expected mm:ss')
    return int(m[1])*60+int(m[2])

def read_csv(path):
    raw=Path(path).read_bytes()
    for encoding in ['utf-8-sig','cp949']:
        try:return list(csv.DictReader(io.StringIO(raw.decode(encoding))))
        except UnicodeError:continue
    raise ValueError('unsupported encoding')

def transfer_rows(source,receipt,root,stations):
    raw=read_csv(root/receipt['raw_path']);out=[]
    short=source['id']=='metro_transfer_time'
    required={'연번','호선','환승역명','환승노선','환승거리','환승소요시간'} if short else {'고유번호','환승시작역','환승시작역 코드','환승시작 호선','하차 열차 방면','환승종료역','환승 열차 방면','소요시간'}
    if not raw or not required.issubset(raw[0]):raise ValueError('transfer schema changed')
    catalog_date=date_in_name(receipt['metadata']['title']);filename_date=date_in_name(receipt['receipt']['original_filename'])
    for i,r in enumerate(raw,1):
        issues=[];name=r['환승역명'] if short else r['환승시작역'];line=r['호선'] if short else r['환승시작 호선']
        line=line+'호선' if line.isdigit() else line
        target=r['환승노선'] if short else None
        # The detailed file's destination code can be an adjacent directional station.
        # It is not safe to join it as the actual transfer station or invent a target line.
        if not short:issues.append('destination_code_semantics_and_duration_unit_not_verified')
        if catalog_date!=filename_date:issues.append('catalog_filename_date_conflict')
        ref=catalog_date if catalog_date==filename_date else None
        duration_raw=r['환승소요시간'] if short else r['소요시간'];seconds=None
        try:
            parsed=duration(duration_raw)
            # The distance file describes distance / 1.2 m/s: confirm the time interpretation.
            if short and abs(float(r['환승거리'])/1.2-parsed)<=2: seconds=parsed
            elif short:issues.append('distance_time_inconsistent')
        except (ValueError,TypeError):issues.append('invalid_duration_or_distance')
        matches=[s for s in stations if s.get('station_name',s.get('STIN_NM'))==name and s.get('line')==line]
        fm=matches[0].get('station_key') if len(matches)==1 else None
        targets=[s for s in stations if s.get('station_name',s.get('STIN_NM'))==name and s.get('line')==target]
        to=targets[0].get('station_key') if len(targets)==1 else None
        if not fm or not to:issues.append('station_line_mapping_missing_or_ambiguous')
        if not ref:issues.append('reference_date_not_verified')
        row=dict(source_id=source['id'],source_record_id=r.get('연번',r.get('고유번호')),station_name=name,from_line=line,to_line=target,
            from_station_key=fm,to_station_key=to,from_direction=None if short else r['하차 열차 방면'],
            to_direction=None if short else r['환승 열차 방면'],raw_destination_code=None if short else r['환승종료역'],
            duration_raw=duration_raw,duration_seconds=seconds,distance_m=r.get('환승거리'),reference_date=ref,
            catalog_reference_date=catalog_date,filename_date=filename_date,metadata_modified=receipt['metadata']['modified'],
            issues=issues,service_status='NOT_USABLE' if issues else 'USABLE_REFERENCE',
            source_url=source['url'],raw_sha256=receipt['receipt']['sha256'],source_row=i,
            collected_at=receipt['receipt']['collected_at'],license=receipt['metadata']['license'],
            raw_fields=r,interpretation='official reference walking time; no waiting/crowding/accessibility guarantee')
        out.append(row)
    return out

def alerts(source,previous):
    key=os.environ.get(source['secret'])
    if not key:raise AcquisitionError('DATA_GO_KR_SERVICE_KEY and 15144070 approval required','user_action')
    start=datetime.fromisoformat(previous.get('last_success',now()))-timedelta(days=7)
    client=Client(interval=2);rows=[]
    for page in range(1,101):
        # Do not put key or request URL in errors, logs, receipts, or persisted payloads.
        params={'serviceKey':key,'dataType':'JSON','pageNo':page,'numOfRows':100,'srchStartNoftOcrnYmd':start.strftime('%Y%m%d')}
        _,_,body=client.get(source['endpoint']+'?'+urlencode(params))
        try:
            data=json.loads(body);data=data.get('response',data)
            if str(data['header']['resultCode']) not in {'0','00'}:raise AcquisitionError('API authentication/application error','user_action')
            b=data['body'];items=b.get('items') or {};items=items.get('item',[]) if isinstance(items,dict) else items
            if isinstance(items,dict):items=[items]
            for r in items:
                title=r.get('noftTtl','');content=r.get('noftCn','')
                if not re.search('시간표|운휴|운행|무정차|환승|통로|폐쇄|지연',title+' '+content):continue
                rows.append(dict(source_id=source['id'],event_type='official_operation_notice',title=title,
                    content=content,published_at=r.get('noftOcrnDt'),effective_start=r.get('xcseSitnBgngDt'),
                    effective_end=r.get('xcseSitnEndDt'),line=r.get('lineNmLst'),direction=r.get('upbdnbSe'),
                    reference_date=r.get('crtrYmd'),source_url=source['url'],service_status='NOT_USABLE',
                    issues=['notice_not_linked_to_trip_and_timetable_version'],collected_at=now()))
            if page*100>=int(b['totalCount']):return rows
        except (ValueError,KeyError,TypeError):raise AcquisitionError('API schema changed; no checkpoint advanced','schema') from None
    raise AcquisitionError('pagination safety bound reached; no checkpoint advanced','schema')

def prepare(root,pain,out):
    inventory,snapshot,urls=inspect_existing(root,pain)
    csv_write(out/'reports/existing_data_inventory.csv',inventory)
    derived=[];invalid=[];stations=[]
    if snapshot:
        path=snapshot/'datasets/timetable_stops.csv'
        if path.is_file():derived,invalid=derive_last_trains(path)
        if (snapshot/'station_reference.csv').is_file():stations=read_csv(snapshot/'station_reference.csv')
    csv_write(out/'derived/last_train_candidates.csv',derived)
    csv_write(out/'quarantine/derived_last_train.csv',invalid)
    raw_state=Path(root)/'state/kric_timetable.json'
    baseline={'verified_at':now(),'inventory':inventory,'pain_urls':urls,'stations':stations,
        'derived_count':len(derived),'derived_invalid_count':len(invalid),
        'timetable_metadata':json.loads(raw_state.read_text()).get('metadata',{}) if raw_state.is_file() else {}}
    atomic_write(out/'baseline.json',enc(baseline))
    # Check read-only inputs again; never repair or rewrite old data.
    checks=[]
    for r in inventory:
        if 'sha256' not in r:continue
        checks.append({'path':r['path'],'sha256_before':r['sha256'],'unchanged':sha(r['path'])==r['sha256']})
    csv_write(out/'reports/existing_files_unchanged.csv',checks)
    if not all(r['unchanged'] for r in checks):raise RuntimeError('baseline changed while analyzing; retry without modifying inputs')
    return baseline

def export(db,out):
    for kind in TABLES:
        rows=[json.loads(r[0]) for r in db.execute('SELECT payload FROM records WHERE kind=? ORDER BY key',(kind,))]
        fields=list(dict.fromkeys(FIELDS[kind]+[k for r in rows for k in r]))
        csv_write(out/'new'/f'{kind}.csv',rows,fields)
        invalid=[r for r in rows if r.get('service_status')=='NOT_USABLE']
        csv_write(out/'quarantine'/f'{kind}.csv',invalid,fields)

def run(args):
    out=Path(args.out).resolve();baseline_root=Path(args.baseline_root).resolve()
    if out==baseline_root or out.is_relative_to(baseline_root):raise ValueError('output must be outside existing baseline')
    out.mkdir(parents=True,exist_ok=True)
    log=logging.getLogger('missing-data');log.setLevel(logging.INFO)
    handler=RotatingFileHandler(out/'collection.log',maxBytes=2_000_000,backupCount=3);log.addHandler(handler)
    try:
      with Lock(out/'.collection.lock'):
        if args.prepare:
            baseline=prepare(baseline_root,Path(args.pain),out)
        else:
            try: baseline=json.loads(Path(args.baseline).read_text())
            except (OSError,ValueError):baseline={'inventory':[{'dataset':'baseline','status':'NOT_VERIFIED'}],'pain_urls':[],'stations':[]}
        if not args.prepare:
            # Hosted runners cannot independently verify a local-only source file.
            hosted=[dict(r,status='NOT_VERIFIED',prior_verification=r.get('status'),reason='prior baseline evidence reused; file bytes not independently reverified in this run') for r in baseline['inventory']]
            csv_write(out/'reports/existing_data_inventory.csv',hosted)
        db=connect(out/'collection.sqlite');run_id=datetime.now(KST).strftime('%Y%m%dT%H%M%S')+'-'+os.urandom(3).hex()
        if args.event_created_at:
            last=db.execute('SELECT ended_at FROM runs WHERE ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT 1').fetchone()
            event=datetime.fromisoformat(args.event_created_at.replace('Z','+00:00'))
            if last and event<datetime.fromisoformat(last[0]) and not args.retry_failed:
                log.warning('SKIPPED_QUEUED_OVERLAP event=%s',args.event_created_at);db.close();return 0
        with db:
            db.execute("UPDATE runs SET status='interrupted' WHERE status='running'")
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?)',(run_id,now(),None,'running','{}'))
        registry=json.loads(Path(args.registry).read_text());results=[];quality=[];counts={'new':0,'changed':0,'unchanged':0}
        previous_run=db.execute("SELECT payload FROM runs WHERE status IN ('complete','partial') ORDER BY started_at DESC LIMIT 1").fetchone()
        failed=set(json.loads(previous_run[0]).get('failed_sources',[])) if previous_run else set()
        for source in registry:
            sid=source['id'];old=state_get(db,sid);result=dict(source_id=sid,priority=source['priority'],url=source['url'],cadence=source.get('cadence'),checked_at=now())
            try:
                if args.retry_failed and sid not in failed:
                    result.update(status='SKIPPED_NOT_FAILED');results.append(result);continue
                if source['adapter']=='deferred':
                    result.update(status='DEFERRED',reason=source['reason']);results.append(result);continue
                if source['adapter']=='review':raise AcquisitionError(source['reason'],'user_action')
                # An unverified baseline may contain the proposed file. Never infer absence.
                verified=any(r.get('dataset')=='timetable_stops' and r.get('status')=='VERIFIED' for r in baseline['inventory'])
                if not verified:raise AcquisitionError('baseline NOT_VERIFIED; no acquisition to avoid duplicate downloads','user_action')
                existing_urls={r.get('source_url') for r in baseline['inventory'] if r.get('role')=='existing_read_only'}
                if source['adapter']=='data_go' and source['url'] in existing_urls:
                    result.update(status='SKIPPED_EXISTING_SOURCE');results.append(result);continue
                not_due=old.get('last_success') and datetime.now(KST)-datetime.fromisoformat(old['last_success'])<timedelta(hours=source.get('check_hours',0))
                if not_due and source['adapter'] not in {'data_go','reviewed_inbox'} and not args.retry_failed:
                    result.update(status='SKIPPED_NOT_DUE',last_success=old['last_success']);results.append(result);continue
                records=[]
                if sum(p.stat().st_size for p in out.rglob('*') if p.is_file())>int(os.getenv('MISSING_DATA_MAX_MB','250'))*1024*1024:
                    raise AcquisitionError('missing-data storage budget exceeded; move/archive new state; baseline untouched','user_action')
                if source['adapter']=='data_go':
                    cached=out/'acquisition/state'/f'{sid}.json'
                    if cached.is_file() and (not_due or args.offline):
                        receipt=json.loads(cached.read_text());receipt['downloaded']=False
                    elif args.offline:raise AcquisitionError('offline: no cached source file','retryable')
                    else:receipt=acquire(source,out/'acquisition')
                    records=transfer_rows(source,receipt,out/'acquisition',baseline.get('stations',[]))
                    result.update(downloaded=receipt['downloaded'],license=receipt['metadata']['license'],original_filename=receipt['receipt']['original_filename'],metadata_modified=receipt['metadata']['modified'])
                elif source['adapter']=='kric_metadata':
                    if args.offline:raise AcquisitionError('offline: metadata refresh not attempted','retryable')
                    _,_,body=Client(interval=2).get(source['url'])
                    evidence=hashlib.sha256(body).hexdigest()
                    atomic_write(out/'acquisition/evidence'/sid/f'{evidence}.html',body)
                    meta=metadata(body,{'metadata_url':source['url'],'dataset_id':900})
                    records=[dict(source_id=sid,event_type='source_metadata_only',title=meta['title'],source_modified=meta['modified'],
                        previous_source_modified=baseline.get('timetable_metadata',{}).get('modified'),
                        effective_start=None,effective_end=None,source_url=source['url'],reference_date=None,
                        service_status='NOT_USABLE',issues=['metadata_date_is_not_timetable_effective_date'],collected_at=now())]
                    result.update(downloaded=False,reason='catalog page only; entire timetable never downloaded')
                elif source['adapter']=='data_go_metadata':
                    if args.offline:raise AcquisitionError('offline: catalog refresh not attempted','retryable')
                    _,_,body=Client(interval=2).get(source['url']);meta=catalog(body.decode('utf-8'))
                    evidence=hashlib.sha256(body).hexdigest()
                    atomic_write(out/'acquisition/evidence'/sid/f'{evidence}.html',body)
                    if not meta.get('dateModified') or meta.get('license',{}).get('name')!='이용허락범위 제한 없음':
                        raise AcquisitionError('catalog schema/license changed','user_action')
                    records=[dict(source_id=sid,event_type='standard_catalog_metadata_only',title=meta['name'],
                        source_modified=meta['dateModified'],effective_start=None,effective_end=None,reference_date=None,
                        source_url=source['url'],service_status='NOT_USABLE',issues=['catalog_modified_date_not_operator_schedule_effective_date'],collected_at=now())]
                    result.update(downloaded=False,license=meta['license']['name'],reason='metadata only; no timetable file download')
                elif source['adapter']=='alerts':
                    if args.offline:raise AcquisitionError('offline: alerts API not attempted','retryable')
                    records=alerts(source,old)
                elif source['adapter']=='reviewed_inbox':
                    inbox=Path(args.inbox)
                    if not inbox.is_file():raise AcquisitionError('no permitted reviewed P1 inputs; search API/storage permission or review required','user_action')
                    known={url_key(u) for u in baseline.get('pain_urls',[])}
                    for r in read_csv(inbox):
                        u=url_key(r['source_url'])
                        if u in known:continue
                        if r.get('reuse_permission') not in {'confirmed','minimum_search_quote_only'} or r.get('original_page_status') not in {'VERIFIED','SEARCH_EXCERPT_ONLY'}:continue
                        excerpt=r.get('evidence_excerpt','')
                        if not excerpt or len(excerpt.split())>25 or re.search(r'[\w.+-]+@[\w.-]+|\b01[016789][- ]?\d{3,4}[- ]?\d{4}\b',excerpt):continue
                        records.append({k:r.get(k) for k in FIELDS['new_user_pain_points']}|{'source_url':u,'source_id':sid,'service_status':'RESEARCH_ONLY',
                            'reuse_scope':r['reuse_permission'],'verification_note':r.get('verification_note'),'topic':r.get('topic')})
                        known.add(u)
                    result['reason']='reviewed permitted inbox only; not an unattended web search'
                records,duplicate_count=deduplicate_batch(records)
                result['duplicate_input_rows']=duplicate_count
                source_counts={'new':0,'changed':0,'unchanged':0}
                with db:
                    for row in records:
                        kind=source['kind']
                        row=validate_record(kind,row)
                        key=row.get('source_record_id') or (url_key(row['source_url']) if kind=='new_user_pain_points' else ident({k:v for k,v in row.items() if k not in {'collected_at','previous_source_modified'}}))
                        key=sid+':'+str(key)
                        # Refresh checks do not create versions for timestamps alone.
                        existing=db.execute('SELECT payload FROM records WHERE kind=? AND key=?',(kind,key)).fetchone()
                        if existing:
                            first=json.loads(existing[0]).get('collected_at')
                            if first and datetime.fromisoformat(first)<=datetime.now(KST):row['collected_at']=first
                        status=upsert(db,kind,key,row);source_counts[status]+=1
                    # A local replay must not postpone the next provider metadata check.
                    state_set(db,sid,{'last_success':old['last_success'] if source['adapter']=='data_go' and (not_due or args.offline) and old else now(),'rows':len(records)})
                for outcome,value in source_counts.items():counts[outcome]+=value
                result.update(status='COLLECTED' if records else 'EMPTY_VERIFIED',records=len(records),usable=sum(r.get('service_status')=='USABLE_REFERENCE' for r in records))
            except Exception as e:
                category=getattr(e,'category','retryable' if isinstance(e,SourceError) else 'schema')
                # Known source errors have redacted messages; arbitrary exceptions can contain secrets.
                reason=str(e) if isinstance(e,SourceError) else type(e).__name__
                result.update(status='FAILED',error_category=category,reason=reason)
                log.warning('%s %s %s',run_id,sid,reason)
            results.append(result)
        export(db,out)
        for kind in TABLES:
            stored=[json.loads(r[0]) for r in db.execute('SELECT payload FROM records WHERE kind=?',(kind,))]
            for row in stored:
                quality.append({'dataset':kind,'source_id':row.get('source_id'),'record_id':row.get('source_record_id'),
                    'status':row.get('service_status'),'issues':row.get('issues',[]),'source_url':row.get('source_url'),
                    'url_validation':'official source actually fetched' if kind!='new_user_pain_points' else row.get('original_page_status')})
        quality.extend({'dataset':k,'status':'NOT_VERIFIED','issues':['no_new_records']} for k in TABLES if not db.execute('SELECT 1 FROM records WHERE kind=?',(k,)).fetchone())
        csv_write(out/'reports/data_quality_report.csv',quality)
        csv_write(out/'reports/quality_summary.csv',[
            {'dataset':kind,'records':sum(r['dataset']==kind and r.get('status')!='NOT_VERIFIED' for r in quality),
             'not_usable':sum(r['dataset']==kind and r.get('status')=='NOT_USABLE' for r in quality),
             'unique_key_duplicates':0,'duplicate_rule':'SQLite primary key; conflicting batch IDs rejected',
             'note':'no confidence/probability invented; see per-record issues and source date conflicts'}
            for kind in TABLES])
        csv_write(out/'reports/source_registry.csv',[s|next((r for r in results if r['source_id']==s['id']),{}) for s in registry])
        gaps=[
          {'priority':'P0','item':'역·요일·방향 경로·종착지별 막차 후보','status':'DERIVED' if baseline.get('derived_count') else 'NOT_VERIFIED','action':'existing timetable only','limitation':'시행일·현행성 미확인; data/derived 별도 저장'},
          {'priority':'P0','item':'시간표 시행일·중간종착·야간 운행일','status':'NOT_VERIFIED','action':'no invented values','limitation':'원본 메타데이터 갱신일로 시행일 대체 금지'},
          {'priority':'P0','item':'환승 연결·방향·소요시간','status':'PARTIAL' if db.execute("SELECT 1 FROM records WHERE kind='transfer_connections'").fetchone() else 'NOT_VERIFIED','action':'official files; see source_registry actual results','limitation':'기준일 충돌·종료역 코드 의미 미확인 행은 NOT_USABLE'},
          {'priority':'P0','item':'운행 변경·운휴·시설 변경 공지','status':'NOT_VERIFIED','action':'official alerts API adapter','limitation':'API 승인·키 필요; 타기관 공지 자동수집 허용 미확인'},
          {'priority':'P1','item':'신규 불편 사례','status':'PARTIAL' if db.execute("SELECT 1 FROM records WHERE kind='new_user_pain_points'").fetchone() else 'NOT_VERIFIED','action':'deduplicated reviewed minimal quotations','limitation':'검색 발췌와 본문 대조 구분; 무인 새 검색은 별도 저장 허용 검색 API 필요'},
          {'priority':'P2','item':'역 구조·출구 연결 시설','status':'DEFERRED','action':'priority gate','limitation':'P0/P1 미완료'},
          {'priority':'VERIFY','item':'인천 승하차·혼잡도','status':'NOT_VERIFIED','action':'excluded from recollection','limitation':'현재 확인한 스냅샷에서 해당 통계 근거 미확인; 부재를 미수집으로 단정하지 않음'}]
        csv_write(out/'reports/data_gap_analysis.csv',gaps)
        mvp=[{'capability':k,'status':st,'reason':reason} for k,st,reason in [
          ('출발 시각 역산','FAIL','현행 시간표 시행일과 검증된 환승 연결이 부족'),
          ('열차별 운행 정보 연결','PASS' if baseline.get('derived_count') else 'NOT_VERIFIED','기존 snapshot trip_key 범위만; 현행 운행 보장 아님'),
          ('환승 소요시간 계산','FAIL','공식 참조값은 확보했으나 기준일 충돌/방향 매핑 미확인'),
          ('막차 안내','FAIL','파생 후보만 존재; 현행 시행일 및 일부 야간 운행일 미확인'),
          ('데이터 신뢰성·적용 범위','NOT_VERIFIED','출처 추적 가능; 전 운영기관 최신성·누락 보장 불가')]]
        csv_write(out/'reports/mvp_assessment.csv',mvp)
        failures=[r for r in results if r['status']=='FAILED'];csv_write(out/'reports/collection_failures.csv',failures)
        summary=dict(run_id=run_id,started_at=db.execute('SELECT started_at FROM runs WHERE id=?',(run_id,)).fetchone()[0],ended_at=now(),status='partial' if failures else 'complete',counts=counts,
            cumulative={k:db.execute('SELECT COUNT(*) FROM records WHERE kind=?',(k,)).fetchone()[0] for k in TABLES},
            derived_last_train_candidates=baseline.get('derived_count'),failed_sources=[r['source_id'] for r in failures],
            p0_p1_complete=False,excluded_full_datasets_downloaded=0,results=results,mvp=mvp)
        with db:db.execute('UPDATE runs SET ended_at=?,status=?,payload=? WHERE id=?',(now(),summary['status'],enc(summary),run_id))
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)');assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok';db.close()
        atomic_write(out/'reports/collection_summary.json',enc(summary))
        atomic_write(out/'reports/runs'/f'{run_id}.json',enc(summary))
        print(enc({k:summary[k] for k in ['run_id','status','counts','cumulative','derived_last_train_candidates','failed_sources']}))
        return 0  # Partial source failures are explicit report states, not total process failure.
    except RuntimeError as e:
        if str(e)=='SKIPPED_OVERLAP':log.warning('SKIPPED_OVERLAP %s',now());print('SKIPPED_OVERLAP');return 0
        raise
    finally:log.removeHandler(handler);handler.close()

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',default='data');p.add_argument('--baseline-root',default='../seat-data')
    p.add_argument('--pain',default='commute-painpoint-research/outputs/current/processed_data.csv')
    p.add_argument('--registry',default='config/missing_sources.json');p.add_argument('--baseline',default='data/baseline.json')
    p.add_argument('--inbox',default='config/missing_pain_points.csv');p.add_argument('--prepare',action='store_true');p.add_argument('--retry-failed',action='store_true');p.add_argument('--offline',action='store_true')
    p.add_argument('--event-created-at',default='')
    return run(p.parse_args())

if __name__=='__main__':sys.exit(main())
