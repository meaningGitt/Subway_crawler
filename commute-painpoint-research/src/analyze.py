"""Reproduce the evidence audit and CSV tables from provisional search excerpts."""
import argparse
import csv
import html
import json
import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from common import RunLock, atomic_text, canonical_url, contains_contact, digest, write_csv, write_json

REQUIRED = ['record_id','source_platform','source_url','published_date','collected_date','user_type','commute_type',
 'transport_mode','commute_duration','situation','pain_point','pain_cause','workaround','sacrifice','unmet_need',
 'evidence_excerpt','pain_category','severity_signal','relevance_score','confidence']
DEFINITIONS = {
 'record_id':'분석 행 식별자; 현재 출처당 한 행', 'source_id':'정규화한 원문 URL의 SHA-256 앞 16자리',
 'source_platform':'원문 게시 플랫폼 또는 매체 묶음', 'source_url':'원문 추적 URL; 사용자 프로필 식별자로 사용하지 않음',
 'published_date':'확인된 YYYY-MM-DD 게시일; 상대 날짜는 환산하지 않음', 'collected_date':'검색 발췌를 반입한 날짜',
 'user_type':'검색 발췌·게시 맥락에 명시된 이용자 유형; 미확인 허용', 'commute_type':'통학·출퇴근 구분',
 'transport_mode':'발췌·게시 맥락에 확인된 교통수단; 대중교통이라고 복합으로 추정하지 않음',
 'commute_duration':'명시 이동시간 그대로; 편도/왕복 불명확하면 표시',
 'situation':'발췌에서 확인한 불편 상황의 요약', 'pain_point':'근거 발언에 대응하는 불편 요약',
 'pain_cause':'발언자가 명시한 원인만 기록; 검증된 인과가 아님', 'workaround':'실행했거나 고려한 행동; 상태 컬럼과 함께 해석',
 'workaround_status':'실행 보고 / 고려·계획 / 미확인', 'sacrifice':'직접 언급된 시간·비용·노력; 미기재는 빈칸',
 'unmet_need':'명시 근거가 없으면 빈칸; 행동의 반대말로 생성 금지', 'evidence_excerpt':'25단어 이하 최소 발췌; 원문 전체가 아님',
 'pain_category':'다중 분류 JSON 배열; 단일 사용자 총수로 합산 금지', 'severity_signal':'발췌 안의 강한 표현; 객관적 심각성 점수 아님',
 'relevance_score':'3=통학/통근 맥락과 구체 불편/행동, 2=적응 반례. 목적 관련성 서열이며 확률 아님',
 'confidence':'원문 검증 여부를 나타내는 서술형 상태; 수치 확률 아님', 'evidence_kind':'공개 글·댓글·매체 인용·문서 인용 구분',
 'collection_method':'실제 반입 경로; 현재 Codex 검색 결과 발췌', 'original_page_verified':'본문 대조 여부; true여도 발언 진위 검증이나 사람 검토 완료를 뜻하지 않음',
 'evidence_status':'원문 대조 전 잠정 사례 상태', 'action_outcome':'발췌에 명시된 행동 후 결과만 기록',
 'recurrence_signal':'매일·주기·기간 등 반복성을 나타내는 원문 표현; 관측 횟수 아님',
 'source_locator':'본문/댓글 발췌 위치 확인 방법; 작성자 정보 미저장', 'scope':'장거리 확인 여부 또는 반례 여부',
 'phase':'pilot / expansion / web_expansion; 분석 실행 단계와 구분', 'fact_or_inference':'요약·코딩 및 독립 진위 미검증 고지',
 'evidence_class':'자기 경험 본문·댓글 / 매체 인용 / 긍정 반례; 진위 인증 아님',
 'search_engine':'검색 제공 도구만 확인; Google·Naver 등 엔진을 추정하지 않음',
 'verification_method':'원문 대조 방법; 발언 자체의 진위 검증과 다름',
 'verification_locator':'대조한 본문 위치; 작성자 식별정보 없음',
 'verification_date':'원문 대조일; 수집일·게시일과 별도',
 'llm_inference':'추론이 추가되면 별도 기록; 현재 전부 빈칸', 'extraction_method':'세션 코딩/재생 방식; 외부 모델 호출 미실행',
 'context_limit':'검색 발췌의 맥락 제한과 생략 표기', 'human_review_status':'사람 검토 완료 여부; 현재 미검토',
 'excerpt_sha256':'공백 정규화한 발췌의 SHA-256', 'causal_limit':'수면·업무·수업과 이동의 인과를 분리할 수 없는 한계'
}
PLATFORMS = {'community.linkareer.com':'링커리어','theqoo.net':'더쿠','orbi.kr':'오르비','www.a-ha.io':'아하',
 'community.rememberapp.co.kr':'리멤버','www.jobplanet.co.kr':'잡플래닛','brunch.co.kr':'브런치','www.82cook.com':'82cook'}
CATEGORIES = {'경로 선택의 어려움','계단 및 수직 이동','교통정보의 부족 또는 부정확성',
 '이동 피로를 줄이기 위한 추가 비용','이동 후 신체적 피로','이동 후 일상생활에 미치는 영향',
 '이동시간 및 지연','일정 조정','적응·긍정 반례','정신적 피로','주거 선택 부담',
 '차량 내부 혼잡','환승 도보거리','환승 횟수','장시간 입석','기타(이동 중 지루함)','기타(이동 중 활동 제약)'}
COMMENT_SOURCES = {'00079986088','00076557647','00079699955','4790046','00067589738','2921839','4369020','4844759','4dedfe9176772926afcc5cd903854b4d','4167940'}
SECONDARY_HOSTS = {'view.asiae.co.kr','imnews.imbc.com','amp.seoul.co.kr','www.univ20.com','m.kpinews.kr','www.kinternet.org',
 'www.mt.co.kr','press.cbnu.ac.kr','www.khan.co.kr','www.segye.com','www.news1.kr','www.yna.co.kr'}

def normalize_excerpt(s):
    return re.sub(r'\s+', ' ', s).strip()

def structure(row, phase, review=None):
    from urllib.parse import urlsplit
    url, excerpt, user, commute, mode, duration, categories, action, pain, published = row
    url=canonical_url(url); excerpt=normalize_excerpt(excerpt)
    if len(excerpt.split())>25 or len(excerpt)>240: raise ValueError('excerpt exceeds minimum quotation budget: '+url)
    if contains_contact(excerpt): raise ValueError('contact/handle in excerpt')
    host=urlsplit(url).hostname; platform=PLATFORMS.get(host,'티스토리' if host.endswith('.tistory.com') else '공개 매체·대학 자료')
    # A source hash is a post identifier, never a person identifier.
    sid=digest(url)[:16]; categories=categories.split('|'); counter='적응·긍정 반례' in categories
    if set(categories) - CATEGORIES: raise ValueError('unknown category: '+url)
    kind='media_or_research_account' if host in SECONDARY_HOSTS else ('public_comment' if any(x in url for x in COMMENT_SOURCES) else 'public_post')
    if url.endswith('.pdf'): kind='published_document_account'
    status='미확인'
    if action: status='고려·계획' if any(x in action for x in ['고려','결정','계획','고민','검토']) else '실행 보고'
    sacrifice=None
    if '5천원 더' in excerpt: sacrifice='하루 5천원 추가 지출'
    elif '30분 일찍' in excerpt: sacrifice='평소보다 30분 일찍 출발'
    elif '1시간 일찍' in excerpt or '1시간 더 일찍' in excerpt: sacrifice='평소보다 1시간 일찍 출발'
    elif '10~15분정도 차이' in excerpt: sacrifice='경로 간 10~15분 소요시간 차이 언급'
    elif '시간표에 엄청난 공' in excerpt: sacrifice='시간표 설계 노력; 수량 미기재'
    elif '이틀에 한 번꼴로 집' in excerpt: sacrifice='시험기간 귀가 빈도 축소'
    severity='|'.join(w for w in ['아무것도 못','죽을 것 같','힘이 없어','기운도 없','기력이','피로도가 크','몸의 피로','피곤','스트레스','못버티','힘들'] if w in excerpt) or None
    cause=None
    if '배차간격 길어서' in excerpt: cause='긴 지하철 배차간격'
    if '환승거리가 좀 멀어서' in excerpt: cause='긴 환승거리'
    if '버스가 늘 만석이라' in excerpt: cause='버스 만석'
    if '급행이 두 대가 먼저 가는 바람' in excerpt: cause='급행 우선 운행으로 일반열차 이용 지연(발언자 설명)'
    original_context = '원문 페이지 미열람; 검색 서비스가 반환한 공개 발췌와 게시 맥락만 확인. […]는 비연속 발췌 연결, …는 발췌 끝 생략일 수 있음'
    record = dict(record_id='CP-'+sid,source_id=sid,source_platform=platform,source_url=url,
      published_date=published or None,collected_date='2026-11-17',user_type=user,commute_type=commute,transport_mode=mode,
      commute_duration=duration or None,situation=pain,pain_point=pain,pain_cause=cause,
      workaround=action or None,workaround_status=status,sacrifice=sacrifice,unmet_need=None,evidence_excerpt=excerpt,pain_category=categories,
      severity_signal=severity,relevance_score=2 if counter else 3,confidence='발췌 근거 확인·원문 미검증',
      evidence_kind=kind,collection_method='codex_web_search_excerpt',original_page_verified=False,
      evidence_status='검색 발췌 기반 잠정 사례; 원문 대조 전',
      action_outcome='쾌적해졌다고 보고' if '쾌~적~' in excerpt else None,
      recurrence_signal='|'.join(w for w in ['매일','주2회','주4일','2년','3학기','오랜 기간','잦았습니다','몇달'] if w in excerpt) or None,
      source_locator='검색에 노출된 댓글; 발췌문으로 원문 내 검색' if kind=='public_comment' else '검색에 노출된 본문 발췌',
      scope='비교·반례' if counter else '통학·통근 경험; 장거리 여부는 duration 별도 확인',phase=phase,
      fact_or_inference='명시 발언의 요약·코딩; 사실성 독립 검증 아님',llm_inference=None,
      extraction_method='Codex session evidence coding; deterministic CSV replay; model version not exposed',
      context_limit=original_context,human_review_status='미검토',excerpt_sha256=digest(excerpt),
      causal_limit='업무·수업·수면 등 다른 피로 원인과 이동의 인과효과 분리 불가',
      evidence_class=None,search_engine='미특정(Codex web search)',
      verification_method=None,verification_locator=None,verification_date=None)
    for key in ['evidence_kind','sacrifice','pain_cause','action_outcome','verification_method','verification_locator','verification_date']:
        if review and key in review: record[key] = review[key]
    if review and review.get('original_page_verified') is True:
        if not all(record[k] for k in ['verification_method','verification_locator','verification_date']):
            raise ValueError('verification provenance required: '+url)
        record.update(original_page_verified=True,confidence='본문과 발췌 대조 완료·발언 진위 미검증',
            evidence_status='본문 대조된 자기 보고; 사람 검토·진위 검증 미완료',
            context_limit='검색 발췌를 web open 본문과 대조. 원문 전체 미저장. […]는 비연속 발췌 연결')
    kind=record['evidence_kind']
    record['source_locator']='댓글·답변; 발췌문으로 원문 내 검색' if kind=='public_comment' else '본문 발췌'
    record['evidence_class']='적응·긍정 반례' if counter else {'public_post':'자기 경험 본문','public_comment':'자기 경험 댓글·답변'}.get(kind,'매체·문서 인용 경험')
    return record

def build(seed, out, phase='all'):
    seed=Path(seed);out=Path(out);out.mkdir(parents=True,exist_ok=True)
    rows=[];seen=set();quotes=set();duplicates=[]
    review_path=seed/'source_reviews.json'
    reviews={canonical_url(r['source_url']):r for r in json.loads(review_path.read_text())} if review_path.exists() else {}
    for name in ['pilot'] + (['expansion','web_expansion'] if phase=='all' else []):
        if not (seed/f'{name}.json').exists(): continue
        for item in json.loads((seed/f'{name}.json').read_text()):
            r=structure(item,name,reviews.get(canonical_url(item[0])))
            if r['source_id'] in seen or r['excerpt_sha256'] in quotes:
                duplicates.append({'source_url':r['source_url'],'reason':'same_source_or_excerpt'});continue
            seen.add(r['source_id']);quotes.add(r['excerpt_sha256']);rows.append(r)
    raw_keys=['record_id','source_id','source_platform','source_url','published_date','collected_date','evidence_excerpt',
              'evidence_kind','collection_method','original_page_verified','source_locator','phase','excerpt_sha256']
    raw=[{k:r[k] for k in raw_keys} for r in rows]
    write_csv(out/'raw_data.csv',raw);write_json(out/'raw_data.json',raw)
    write_csv(out/'processed_data.csv',rows);write_json(out/'processed_data.json',rows)
    write_csv(out/'new_web_cases.csv',[r for r in rows if r['phase']=='web_expansion'],list(rows[0]))
    discovery=json.loads((seed/'discovery.json').read_text());candidates={}
    if phase=='all' and (seed/'discovery_web.json').exists():
        discovery += json.loads((seed/'discovery_web.json').read_text())
    for d in discovery:
        try:url=canonical_url(d['source_url'])
        except ValueError:continue
        if url not in candidates:
            candidates[url]={'source_url':url,'source_id':digest(url)[:16],'collection_method':'search_result_url_only',
             'review_status':'발췌 분석 포함' if url in {r['source_url'] for r in rows} else '미채택·원문 검증 필요',
             'collected_date':'2026-11-17','source_body_stored':False}
    write_csv(out/'discovery_candidates.csv',list(candidates.values()))
    selected={r['source_url']:r for r in rows}; audit=[]
    for url in dict.fromkeys([*candidates,*reviews,*selected]):
        r=selected.get(url); review=reviews.get(url,{})
        audit.append(dict(source_url=url,disposition='채택' if r else ('제외·보류' if review else '미검토'),
          classification=r['evidence_class'] if r else review.get('classification','미분류·검토 필요'),
          reason=review.get('reason','구체 경험 발췌 확인' if r else 'URL 후보만 발견; 경험 수에 포함하지 않음'),
          record_id=r['record_id'] if r else None,original_page_verified=r['original_page_verified'] if r else False,
          search_engine='미특정(Codex web search)',human_review_status='미검토'))
    write_csv(out/'search_classification_audit.csv',audit)
    write_csv(out/'classification_distribution.csv',[{'classification':k,'candidate_count':n}
        for k,n in Counter(r['classification'] for r in audit).most_common()])
    write_csv(out/'duplicate_log.csv',duplicates,['source_url','reason'])
    near=[]
    for i,r in enumerate(rows):
        for s in rows[:i]:
            sim=SequenceMatcher(None,r['evidence_excerpt'],s['evidence_excerpt']).ratio()
            if sim>=.82:near.append({'record_id_a':r['record_id'],'record_id_b':s['record_id'],'similarity':round(sim,4),'action':'review_not_auto_delete'})
    write_csv(out/'similarity_review.csv',near,['record_id_a','record_id_b','similarity','action'])
    count=Counter(c for r in rows for c in r['pain_category'] if c!='적응·긍정 반례')
    summary=[]
    for cat,n in count.most_common():
        group=[r for r in rows if cat in r['pain_category']]
        summary.append(dict(pain_category=cat,analysis_unit_count=n,unique_source_count=len({r['source_id'] for r in group}),
          executed_action_count=sum(r['workaround_status']=='실행 보고' for r in group),
          stated_sacrifice_count=sum(r['sacrifice'] is not None for r in group),
          evidence_record_ids=[r['record_id'] for r in group],interpretation='검색 편의표본 내 언급 수; 모집단 발생률·심각성 순위 아님'))
    write_csv(out/'pain_point_summary.csv',summary)
    behaviors=[{k:r[k] for k in ['record_id','source_url','pain_point','workaround','workaround_status','sacrifice','action_outcome','unmet_need','evidence_excerpt']} for r in rows if r['workaround']]
    write_csv(out/'user_behavior_analysis.csv',behaviors)
    # Balanced review order across platforms; all human-review answers remain blank.
    buckets={p:[r for r in rows if r['source_platform']==p] for p in sorted({r['source_platform'] for r in rows})};sample=[]
    while len(sample)<min(30,len(rows)):
        for group in buckets.values():
            if group and len(sample)<30:sample.append(group.pop(0))
    sample=[dict(r,reviewer_confirmed_excerpt='',reviewer_confirmed_action='',reviewer_confirmed_cost='',reviewer_notes='') for r in sample]
    write_csv(out/'manual_review_sample.csv',sample)
    quality=[{'check':'고유 출처 수','value':len(seen),'result':'출처 URL 기준; 고유 사용자 수 아님'},
      {'check':'분석 단위 수','value':len(rows),'result':'현재 출처당 1개 단위'},
      {'check':'최소 200개 유효 경험','value':len(rows),'result':'미달; 본 수집 미완료' if len(rows)<200 else '수량 충족; 질 별도 확인'},
      {'check':'원문 페이지 대조','value':sum(r['original_page_verified'] for r in rows),'result':'본문 대조 수; 발언 진위 검증·사람 검토와 다름'},
      {'check':'사람의 표본 검토','value':0,'result':'미수행; 30개 검토용 CSV 생성'},
      {'check':'정확 게시일 미확인','value':sum(not r['published_date'] for r in rows),'result':'null 유지; 검색 수집일로 대체 안 함'},
      {'check':'이동시간 미확인','value':sum(not r['commute_duration'] for r in rows),'result':'장거리만의 표본으로 해석 금지'},
      {'check':'중복 제외','value':len(duplicates),'result':'동일 URL 또는 동일 발췌 해시'},
      {'check':'유사 발췌 검토 후보','value':len(near),'result':'문자열 유사도 0.82 이상; 의미 임베딩 아님'},
      {'check':'저장 발췌 연락처·핸들 패턴','value':sum(contains_contact(r['evidence_excerpt']) for r in rows),'result':'0; 닉네임·작성자 별도 컬럼 없음'},
      {'check':'관측 또는 학습 임베딩 모델','value':0,'result':'미실행; 다중 라벨 주제별 그룹화만 수행'},
      {'check':'적응·긍정 반례','value':sum('적응·긍정 반례' in r['pain_category'] for r in rows),'result':'불만 빈도에서 제외·보존'}]
    write_csv(out/'quality_report.csv',quality)
    for name,col in [('platform_distribution','source_platform'),('user_distribution','user_type'),('transport_distribution','transport_mode')]:
        write_csv(out/(name+'.csv'),[{col:k,'analysis_units':v} for k,v in Counter(r[col] for r in rows).most_common()])
    opportunities=[]
    themes=[('이동 뒤 일상 활동을 유지하기 어려움',['이동 후 일상생활에 미치는 영향']),
     ('환승 부담을 줄이기 위한 시간과 편의의 교환',['환승 횟수','환승 도보거리']),
     ('운행 불확실성에 대비해 추가 시간을 부담함',['이동시간 및 지연','일정 조정']),
     ('통학·통근 부담과 거주 선택 제약이 충돌함',['주거 선택 부담']),
     ('이동 중 시간 활용과 신체적 회복은 다를 수 있음',['이동 후 신체적 피로'])]
    for title,cats in themes:
        group=[r for r in rows if set(cats)&set(r['pain_category'])]
        if len(group)<2:continue
        opportunities.append({'opportunity':title,'target_users':'|'.join(sorted({r['user_type'] for r in group})),
          'observed_situations':'|'.join(dict.fromkeys(r['pain_point'] for r in group[:3])),
          'evidence_record_ids':[r['record_id'] for r in group], 'reported_actions':'|'.join(sorted({r['workaround'] for r in group if r['workaround']})),
          'cause':'개별 명시 근거만 확인; 공통 인과 미검증','unmet_need':'미확인; 인터뷰 검증 필요',
          'existing_solution_limits':'개별 사례의 해결 후 잔여 문제는 대체로 미확인; 행동 선택만으로 해결 실패를 단정할 수 없음',
          'hypothesis':'이 문제를 반복 경험하는 사람은 어떤 상황에서 기존 행동으로 충분히 회복하지 못하는가?',
          'status':'탐색 가설; 기능 제안 또는 시장성 검증 아님'})
    write_csv(out/'problem_opportunities.csv',opportunities)
    dictionary=[]
    for k in rows[0]:dictionary.append({'column':k,'definition':{'relevance_score':'3=통학/통근 맥락+자기경험/인용경험+구체 불편/행동, 2=적응 반례. 확률 아님',
      'confidence':'발췌에 근거가 있으나 원문·진위 미검증; 수치 점수 아님','unmet_need':'명시 없으면 null; 해결 행동의 반대말로 생성 금지',
      'commute_duration':'원문 표현 보존; 편도/왕복 불명확하면 미확인', 'published_date':'정확 날짜만 YYYY-MM-DD; 상대 날짜 환산 안 함',
      'source_url':'원문 추적용 URL; URL에 내재된 계정 경로는 별도 사용자 식별자로 추출하지 않음'}.get(k,k),
      'missing_value':'빈 CSV 셀 = 미확인; 0 아님','provenance':'search excerpt + provisional coding seed','observation_unit':'한 출처의 선택된 경험 발췌'})
    for item in dictionary:
        key=item['column']; item['definition']=DEFINITIONS[key]
        item['data_type']='boolean' if key=='original_page_verified' else ('integer' if key=='relevance_score' else ('JSON array' if key=='pain_category' else 'string'))
        item['unit']='서열; 확률 아님' if key=='relevance_score' else '해당 없음; 시간·금액은 원문 단위를 문자열에 보존'
        item['evidence_role']='발언 인용; 진위 미검증' if key=='evidence_excerpt' else '출처 메타데이터 또는 발췌 코딩; 직접 관측·추천 확률 아님'
    write_csv(out/'data_dictionary.csv',dictionary)
    verified=sum(r['original_page_verified'] for r in rows)
    limitations=f'검색 발췌 기반 편의표본이며 본문 대조 {verified}개, 발언 진위 독립 검증·사람 검토 0개. 대조 여부는 행별 컬럼 확인. 댓글은 발췌문으로 위치 확인 가능. 같은 사람이 여러 글을 썼는지는 식별하지 않는다. 검색 노출 편향·오래된 글·매체 인용·장거리 여부 미확인이 섞여 있다. 업무와 이동의 피로 인과를 분리할 수 없다. 단순 건수는 전체 통근자의 발생률이 아니다.'
    from datetime import datetime
    from zoneinfo import ZoneInfo
    metrics={'analysis_date':datetime.now(ZoneInfo('Asia/Seoul')).date().isoformat(),
      'source_collection_date':'2026-11-17','phase':phase,'analysis_units':len(rows),'source_posts':len(seen),
      'source_count_note':'문서·매체 포함 출처 수; 사람 수 아님','candidate_urls':len(candidates),
      'minimum_target':200,'target':300,'main_collection_complete':False,'provisional_excerpt_cases':len(rows),
      'validated_original_cases':0,'original_pages_verified':verified,'human_reviews_completed':0,
      'new_web_cases':sum(r['phase']=='web_expansion' for r in rows),
      'excluded_or_deferred_candidates':sum(r['disposition']=='제외·보류' for r in audit),
      'unreviewed_candidates':sum(r['disposition']=='미검토' for r in audit),
      'platform_counts':dict(Counter(r['source_platform'] for r in rows)),'pipeline_version':'commute-painpoint-v1.1',
      'seed_sha256':{f.name:digest(f.read_text()) for f in seed.glob('*.json')}}
    write_json(out/'run_summary.json',metrics)
    write_csv(out/'run_summary.csv',[{k:v for k,v in metrics.items() if k!='seed_sha256'}])
    write_csv(out/'data_collection_report.csv',[{k:v for k,v in metrics.items() if k!='seed_sha256'}])
    write_csv(out/'pain_point_analysis.csv',summary)
    write_csv(out/'pipeline_lineage.csv',[{'stage':'seed_import','file':f.name,'sha256':digest(f.read_text()),
      'method':'2026-11-17 Codex web search excerpts; no direct community crawler; no full pages stored',
      'model':'GPT-6 based Codex session; exact model snapshot not exposed; external LLM API not executed'} for f in seed.glob('*.json')])
    for config_csv in (seed.parent.parent/'config').glob('*.csv'):
        atomic_text(out/config_csv.name, config_csv.read_text(encoding='utf-8'))
    write_csv(out/'collection_failures.csv',[
      {'stage':'live_repeated_search','status':'추가 권한 필요','reason':'검색 API 키·검색 결과 저장 허용 플랜 미설정','next_action':'BRAVE_SEARCH_API_KEY와 저장 권한 설정 또는 허용된 검색결과 반입'},
      {'stage':'main_200','status':'일부 수집','reason':f'{len(rows)}개 잠정 사례; 원문 대조·사람 검토 포함한 본 수집 미완료','next_action':'미검토 후보 선별·원문 대조·표본 검토 후 확대'},
      {'stage':'original_page_open','status':'일부 수집','reason':'penda.tistory.com/102 web open Cache miss; 검색 발췌만 반입','next_action':'일시 접근 실패로 기록; 원문 대조 전 상태 유지'},
      {'stage':'embeddings','status':'미실행','reason':'한국어 임베딩 모델 미설치; 주제 코딩만 실시','next_action':'원문 대조된 표본 확대 후 모델 선택'}])
    report_names=['data_collection_report','pain_point_analysis','user_behavior_analysis','problem_opportunities']
    paragraphs={
      'data_collection_report':f'파일럿 40개 이후 추가 {max(0,len(rows)-40)}개. 고유 출처 {len(seen)}개, 분석 단위 {len(rows)}개. 최소 200개 미달. 검색 URL 후보 {len(candidates)}개는 유효 사례 수가 아니다. 실시간 API 수집은 키 없이 실행되지 않는다. 기존 GitHub 지하철 수집 2개 비활성화; 로컬 cron 등록 없음. 플랫폼별 robots/약관 검토는 platform_access.csv 참고.',
      'pain_point_analysis':'\n'.join(f"- {r['pain_category']}: {r['analysis_unit_count']}개; 실행 행동 보고 {r['executed_action_count']}개" for r in summary),
      'user_behavior_analysis':'실행 보고와 고려·계획을 분리했다. 원문에 없는 비용과 해결 여부는 미확인이다.\n'+'\n'.join(f"- {r['record_id']}: {r['workaround']} ({r['workaround_status']}); 감수: {r['sacrifice'] or '미확인'}" for r in behaviors),
      'problem_opportunities':'\n'.join(f"- {r['opportunity']}: {r['hypothesis']} 근거 {', '.join(r['evidence_record_ids'])}" for r in opportunities)}
    for name in report_names:atomic_text(out/(name+'.md'),f'# {name}\n\n{paragraphs[name]}\n\n{limitations}\n')
    bars=''.join(f'<tr><td>{html.escape(r["pain_category"])}</td><td><meter min="0" max="{len(rows)}" value="{r["analysis_unit_count"]}"></meter> {r["analysis_unit_count"]}</td></tr>' for r in summary)
    evidence=''.join(f'<tr><td>{r["record_id"]}</td><td>{html.escape(r["pain_point"])}</td><td>{html.escape(r["evidence_excerpt"])}</td><td><a rel="noreferrer" href="{html.escape(r["source_url"],quote=True)}">출처</a></td></tr>' for r in rows)
    atomic_text(out/'pain_point_dashboard.html',f'<!doctype html><html lang="ko"><meta charset="utf-8"><title>이동 피로 탐색</title><style>body{{font:16px system-ui;max-width:1100px;margin:40px auto;padding:20px}}td{{padding:12px;border-bottom:1px solid #ddd}}meter{{width:200px}}</style><h1>이동 피로 탐색: {len(rows)}개 발췌 사례</h1><p>200개 목표 미달 · 본문 대조 {verified}개 · 사람 검토 0개</p><p>{limitations}</p><table>{bars}</table><h2>근거 발췌</h2><table>{evidence}</table></html>')
    print(json.dumps(metrics,ensure_ascii=False))
    return rows

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--seed',default='data/seed');p.add_argument('--out',default='outputs');p.add_argument('--phase',choices=['pilot','all'],default='all');a=p.parse_args()
    with RunLock(Path(a.out)/'.analysis.lock'):
        build(a.seed,a.out,a.phase)
