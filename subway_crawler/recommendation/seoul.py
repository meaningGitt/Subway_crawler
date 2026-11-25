"""Anonymous CSV downloads exposed by Seoul's public Sheet/File UI; no API key bypass."""
import html
import re
import tempfile
from pathlib import Path

from ..source import digest, encoded
from .sources import AcquisitionError


def discover_seoul(source, client, body):
    page = body.decode('utf-8')
    if '공공누리 1유형 : 출처표시 (상업적 이용 및 변경 가능)' not in page:
        raise AcquisitionError('Seoul license changed; review required', 'user_action')
    ident = source['dataset_id']
    meta = {'title': source['name'], 'license': '공공누리 제1유형: 서울특별시/서울교통공사 출처표시',
            'cadence': source['cadence']}
    if source['adapter'] == 'seoul_sheet':
        _, _, view = client.get(f'https://data.seoul.go.kr/dataList/sheetView.do?infId={ident}&srvType=S')
        endpoint = 'https://datafile.seoul.go.kr/bigfile/iot/sheet/csv/download.do'
        if '/bigfile/iot/sheet/' not in view.decode() or '/dataList/dataView.do?' not in view.decode():
            raise AcquisitionError('Seoul sheet UI changed', 'schema')
        params = dict(infId=ident, srvType='S', serviceKind='1', pageNo='1', ssUserId='SAMPLE_VIEW',
                      strWhere='', strOrderby='USE_MM DESC,SBWY_ROUT_LN_NM ASC,STTN ASC', filterCol='', txtFilter='')
        with tempfile.TemporaryDirectory() as folder:
            probe = Path(folder)/'grid.txt'
            client.request_file('POST', 'https://data.seoul.go.kr/dataList/dataView.do?onepagerow=1000', probe, data=params)
            grid = probe.read_bytes()
        # The public grid returns JS object notation. Read only a strict YYYYMM field, never eval it.
        months = re.findall(r'\bUSE_MM:\s*"(20\d{2}(?:0[1-9]|1[0-2]))"', grid.decode('utf-8'))
        if not months:
            raise AcquisitionError('Seoul grid contains no published month', 'schema')
        month = source.get('collection_period',max(months))
        if not re.fullmatch(r'20\d{2}(?:0[1-9]|1[0-2])',month) or month>max(months):
            raise AcquisitionError('invalid/unpublished requested period','schema')
        params.update(filterCol='USE_MM', txtFilter=month, gridTotalCnt='1000')
        meta.update(modified=month, period=month, download_url=endpoint, download_params=params,
                    original_filename=f'{ident}_{month}.csv', grid_sha256=digest(grid) if not source.get('collection_period') else None)
        evidence = body+b'\n<!-- sheet UI -->\n'+view+b'\n<!-- public grid -->\n'+grid
    else:
        _, _, view = client.get(f'https://data.seoul.go.kr/dataList/fileView.do?infId={ident}&srvType=F')
        v = view.decode('utf-8')
        seq = re.search(r'name="infSeq"[^>]*value="(\d+)"', v)
        attachments = []
        for row in re.findall(r'<tr[^>]*>(.*?)</tr>', v, re.S):
            link = re.search(r"downloadFile\('(\d+)'\)", row)
            cells = [html.unescape(re.sub('<[^>]+>', '', c)).strip() for c in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)]
            if link and len(cells) >= 5 and cells[1].endswith(source.get('suffix','.csv')):
                attachments.append((cells, link[1]))
        if source.get('filename_contains'):
            attachments = [a for a in attachments if source['filename_contains'] in a[0][1]]
        if not seq or not attachments:
            raise AcquisitionError('Seoul attachment list changed', 'schema')
        cells, number = attachments[0]
        meta.update(modified=cells[4], original_filename=cells[1],
                    download_url='https://datafile.seoul.go.kr/bigfile/iot/inf/nio_download.do?&useCache=false',
                    download_params=dict(infId=ident, seq=number, seqNo='', infSeq=seq[1]))
        evidence = body+b'\n<!-- file UI -->\n'+view
    meta['version'] = digest(encoded(meta).encode())
    return meta, evidence
