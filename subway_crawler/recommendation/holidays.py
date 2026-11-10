"""Versioned official holiday evidence. API secrets never enter logs, receipts, or URLs on disk."""
import hashlib
import json
import os
from pathlib import Path
import time
import xml.etree.ElementTree as ET
import pandas as pd
import requests
from ..pipeline import now
from ..store import atomic_write

CONFIG = Path(__file__).resolve().parents[2]/'config'
ENDPOINT = 'https://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo'


def refresh_calendar(root):
    if not os.environ.get('DATA_GO_KR_KEY'):
        return {'mode':Calendar(root).metadata['mode'],'reason':'DATA_GO_KR_KEY absent; existing cache or static list used'}
    root=Path(root); entries=[]; receipts=[]
    try:
        for year in (2024,2025,2026):
            body=None
            for attempt in range(3):
                try:
                    response=requests.get(ENDPOINT,params={'serviceKey':os.environ['DATA_GO_KR_KEY'],'solYear':year,'numOfRows':100,'pageNo':1},timeout=(15,45))
                    if response.status_code in (429,500,502,503,504):
                        time.sleep(2**attempt);continue
                    response.raise_for_status();body=response.content;break
                except requests.RequestException:
                    if attempt==2:raise ValueError('holiday transport/auth failure; details redacted') from None
            if body is None:raise ValueError('holiday retry limit')
            xml=ET.fromstring(body)
            if xml.findtext('.//resultCode') not in ('00','0'):raise ValueError('holiday API response rejected; key approval required')
            items=xml.findall('.//item')
            if int(xml.findtext('.//totalCount') or 0)!=len(items) or not items:raise ValueError('holiday API incomplete annual response')
            sha=hashlib.sha256(body).hexdigest();path=root/'raw/holidays'/f'{sha}.xml';atomic_write(path,body)
            receipts.append({'year':year,'raw_path':str(path.relative_to(root)),'sha256':sha,'collected_at':now(),'endpoint':ENDPOINT})
            for item in items:
                if item.findtext('isHoliday')=='Y':
                    date=pd.to_datetime(item.findtext('locdate'),format='%Y%m%d').date().isoformat()
                    if not date.startswith(str(year)):raise ValueError('holiday API year mismatch')
                    entries.append({'date':date,'name':item.findtext('dateName'),'source_url':ENDPOINT})
            time.sleep(2)
        atomic_write(root/'state/holidays.json',json.dumps({'years':[2024,2025,2026],'dates':entries,'receipts':receipts,'mode':'api','collected_at':now()},ensure_ascii=False))
        return {'mode':'api','rows':len(entries)}
    except (ValueError,ET.ParseError):
        # Don't expose an upstream error body which might echo the key.
        atomic_write(root/'checks/holiday_error.json',json.dumps({'at':now(),'error':'API failed validation or permission; retaining validated cache or static list','retryable':'transport only; auth needs user action'}))
        return {'mode':Calendar(root).metadata['mode'],'reason':'API failed; retained cache/static; inspect permission/network'}


class Calendar:
    def __init__(self, root=None):
        path=Path(root)/'state/holidays.json' if root else CONFIG/'holidays_kr.json'
        if not path.exists():path=CONFIG/'holidays_kr.json'
        body=path.read_bytes();self.data=json.loads(body)
        self.dates={r['date'] for r in self.data['dates']};self.years=set(self.data['years'])
        self.metadata={'mode':self.data.get('mode','static'),'sha256':hashlib.sha256(body).hexdigest(),'years':sorted(self.years),'date_count':len(self.dates),'sources':self.data.get('sources',[ENDPOINT]),'definition':'특정 공휴일 제외; 일반 일요일은 요일로 유지','path':str(path)}
    def flags(self, dates):
        return pd.Series([str(d.date()) in self.dates if pd.notna(d) and d.year in self.years else pd.NA for d in dates],index=dates.index,dtype='boolean')
