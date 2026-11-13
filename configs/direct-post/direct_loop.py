#!/usr/bin/env python3
"""Config-reloading direct POST workers with dependency-bound server state."""
import argparse
import fcntl
import hashlib
from http.cookiejar import Cookie
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import time
import tempfile
import urllib.parse

ROOT=Path(__file__).resolve().parents[2]
HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('iism_direct_runtime',ROOT/'iism-bot.py')
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)
IDS={'G1':('80','1st course from 20 Dec 2026 to 02 Jan 2027'),'G4':('83','4th course from 31 Jan to 13 Feb 2026')}

def emit(**values):
    for k,v in values.items():print(f'{k}: {json.dumps(v,ensure_ascii=False)}',flush=True)

def write_private(path,content):
    data=content if isinstance(content,bytes) else content.encode()
    temp=path.with_name('.'+path.name+'.new')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'wb') as stream:
        stream.write(data);stream.flush();os.fsync(stream.fileno())
    os.replace(temp,path);path.chmod(0o600)

def save_result(target,attempt,manual,response=None,error=None,request_started=False):
    category='once' if manual else 'loop'
    folder=HERE/'responses'/category/target
    for private_dir in [HERE/'responses',HERE/'responses'/category,folder]:b.private_dir(private_dir)
    stamp=b.now_ist().strftime('%Y%m%d-%H%M%S-%f')
    prefix=folder/stamp
    body_path=None
    if response is not None:
        body_path=prefix.with_suffix('.html')
        write_private(body_path,response.body)
    metadata={'target':target,'attempt':attempt,'manualOnce':manual,'at':b.now_ist().isoformat(),
              'requestStarted':request_started,'responseReceived':response is not None,
              'httpStatus':response.status if response is not None else None,
              'url':response.url if response is not None else None,
              'headers':response.headers if response is not None else {},
              'complete':response.complete if response is not None else False,
              'redirectCount':len(response.redirects) if response is not None else 0,
              'bodyFile':str(body_path) if body_path else None,'error':str(error) if error is not None else None}
    meta_path=prefix.with_suffix('.json')
    write_private(meta_path,json.dumps(metadata,indent=2))
    write_private(HERE/'responses'/('latest-'+target+'.json'),json.dumps(metadata,indent=2))
    if not manual:
        records=sorted({p.stem for p in folder.iterdir() if p.suffix in ('.html','.json')})
        for old in records[:-5]:
            (folder/(old+'.html')).unlink(missing_ok=True);(folder/(old+'.json')).unlink(missing_ok=True)
    emit(responseFile=str(body_path) if body_path else None,responseMetadata=str(meta_path))
    return meta_path

class ConfigPaused(b.ConfigError):
    """A local edit is not safe to apply yet; send nothing until corrected."""

class ConfigChanged(Exception):
    pass

class Snapshot:
    def __init__(self,settings,digest):
        self.settings=settings
        self.digest=digest
    def target(self,code):
        matches=[target for target in self.settings.targets if target.batch_id==code]
        if len(matches)!=1:
            raise ConfigPaused('Keep exactly one '+code+' preference in coursePriorities for this worker.')
        return matches[0]

class ConfigReader:
    """Read current bytes each loop; cache only an identical validated snapshot."""
    def __init__(self,config):
        self.config=Path(config)
        self.raw=None
        self.snapshot=None
        self.sources=None
        self.document_hashes=None
        self.last_error=None
    @staticmethod
    def hashes(sources):
        return tuple(sorted((str(path),hashlib.sha256(path.read_bytes()).hexdigest())
                            for path in set(sources.values())))
    def read(self):
        try:
            raw=self.config.read_bytes()
            if self.snapshot is not None and raw==self.raw:
                hashes=self.hashes(self.sources)
                if hashes==self.document_hashes:return self.snapshot
            # Reuse the existing JSONC consumer on one immutable byte snapshot.
            with tempfile.TemporaryDirectory(prefix='config-read-',dir=HERE) as directory:
                copy=Path(directory)/'config.jsonc'
                write_private(copy,raw)
                cfg=b.load_jsonc(copy)
                settings,errors,warnings=b.parse_settings(cfg,self.config)
                if errors:raise ConfigPaused('; '.join(errors))
                settings.direct_request_timeout=cfg.get('bot',{}).get('requestTimeoutSec',15)
                settings.direct_submit_timeout=cfg.get('bot',{}).get('submitTimeoutSec',30)
                if 'parentSignature' not in settings.doc_sources:
                    raise ConfigPaused('Set documents.parentSignature explicitly for the direct worker.')
                before=self.hashes(settings.doc_sources)
                problems,_=b.prepare_documents(settings,Path(directory)/'documents')
                if problems:raise ConfigPaused('; '.join(problems))
                after=self.hashes(settings.doc_sources)
                if before!=after:raise ConfigPaused('A document changed during preparation; waiting for a complete file.')
            settings.opt['notify']=False
            digest=hashlib.sha256(raw+json.dumps(after).encode()).hexdigest()
            snapshot=Snapshot(settings,digest)
            self.raw,self.sources,self.document_hashes,self.snapshot=raw,dict(settings.doc_sources),after,snapshot
            return snapshot
        except ConfigPaused:raise
        except (OSError,ValueError,TypeError,b.ConfigError) as exc:
            raise ConfigPaused(str(exc)) from exc
    def wait(self,code):
        while True:
            try:
                snapshot=self.read();snapshot.target(code)
            except ConfigPaused as exc:
                message=str(exc)
                if message!=self.last_error:emit(status='config_paused',target=code,error=message)
                self.last_error=message
                # Only an invalid local edit waits; valid HTTP retries have no sleep.
                threading.Event().wait(0.1)
            else:
                if self.last_error is not None:emit(status='config_resumed',target=code)
                self.last_error=None
                return snapshot

def server_key(settings,target,base_url):
    value=[b.origin(base_url),settings.season,settings.phones[target.index],
           settings.applicant['dateOfBirth'],settings.state,b.target_identity(target),
           target.course_text,target.level_text,target.batch_text]
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()

def course_option(page,target):
    return next((option for option in b.course_candidates(page.options('ddlCourse'),target)
                 if not b.course_conflicts(option[1],target)),None)

def batch_option(page,target):
    # Preserve the user-confirmed G4 catalogue typo, only for its exact original dates.
    manual=(target.batch_id=='G4' and target.location.casefold()=='gulmarg'
            and target.start.isoformat()=='2027-01-31'
            and target.end is not None and target.end.isoformat()=='2027-02-13')
    for option in b.real_options(page.options('ddlScheduleCourse')):
        if b.match_batch([option],target) is not None:return option
        if manual and option==IDS['G4']:return option
    return None

def desired_selection(page,settings,target):
    course=course_option(page,target)
    level=b.match_level(page.options('ddlCourseLevel'),target,False)
    batch=batch_option(page,target)
    state=b.match_choice(page.options('ddlState'),settings.state,b.state_aliases(settings.state))
    if not all((course,level,batch,state)):return None
    if b.selection_conflicts(course,level,batch,target,allow_ski_parent=True):return None
    return course,level,batch,state

class Progress:
    """Diagnostics only: direct retries never gate on native booking/pending history."""
    @staticmethod
    def output_dir(settings,base_url):
        config=settings.config_path
        identity=json.dumps([str(config.resolve()),b.origin(base_url),settings.season])
        return ROOT/'runs'/(config.stem+'-'+hashlib.sha256(identity.encode()).hexdigest()[:16])
    def __init__(self,settings,base_url):
        self.out=self.output_dir(settings,base_url)
        # Disabled native state is used only as the response-parser context. It never
        # reads, clears or rewrites the original bot's saved submission history.
        self.bot=b.Bot(settings,self.out,base_url=base_url,dry_run=True,log=b.Log(echo=False))
        self.current=None
    def start(self,target,phone):
        self.current={'target':target.label,'batch':target.batch_id,'targetKey':b.target_key(target),'phone':phone,
                      'status':'submitting','at':b.now_ist().isoformat(),
                      'applicantIdentity':b.expected_state_identity(self.bot.s,self.bot.base_url)}
        write_private(HERE/(target.batch_id+'-attempt-state.json'),json.dumps(self.current,indent=2))
    def finish(self,target,phone,status,info=None):
        record=dict(self.current or {},target=target.label,targetKey=b.target_key(target),phone=phone,
                    status=status,at=b.now_ist().isoformat())
        if info is not None:record['result']=info
        write_private(HERE/(target.batch_id+'-attempt-state.json'),json.dumps(record,indent=2))
        if status=='confirmed':
            write_private(HERE/(target.batch_id+'-last-success.json'),json.dumps(record,indent=2))
    def clear(self,phone):
        if self.current is not None:
            record=dict(self.current,status='not_confirmed',at=b.now_ist().isoformat())
            write_private(HERE/(record['batch']+'-attempt-state.json'),json.dumps(record,indent=2))
    def confirm(self,target,phone,info):self.finish(target,phone,'confirmed',info)

def build_packet(settings,target,page):
    phone=settings.phones[target.index]
    if page.name!=b.FORM_PAGE or page.status!=200 or not page.complete or page.value('txtMobileNo')!=phone:
        raise b.ConfigError('Expected a complete live admission form echoing this primary.')
    for key in ['__VIEWSTATE','__VIEWSTATEGENERATOR','__EVENTVALIDATION']:
        if not page.value(key) or page.value(key)=='<redacted>':raise b.ConfigError('Missing real live WebForms token: '+key)
    picked=desired_selection(page,settings,target)
    if picked is None:raise ConfigPaused('The prepared dropdown choices no longer match the configured preference.')
    course,level,batch,state=picked
    data=dict(page.fields())
    data.update(txtCourseFee=page.value('txtCourseFee'),hdfTypeOfCapacitySheet=page.value('hdfTypeOfCapacitySheet'),
                hdfCapacitySheetAllocationId=page.value('hdfCapacitySheetAllocationId'),txtAvailableSeat=page.value('txtAvailableSeat'))
    for field,value in settings.form_values.items():
        if not page.missing_enabled((field,)):data[field]=value
    data.update(__EVENTTARGET='',__EVENTARGUMENT='',ddlCourse=course[0],ddlCourseLevel=level[0],
                ddlScheduleCourse=batch[0],ddlState=state[0],chkIAgree='on')
    for field,wanted in [('ddlEduationQualification',settings.education),('ddlVegetarianOrNon',settings.diet)]:
        option=b.match_choice(page.options(field),wanted)
        if option is None:raise ConfigPaused('Configured dropdown value is not available: '+field)
        data[field]=option[0]
    files={}
    for role,input_name,flag,required in b.DOCUMENTS:
        data[flag]='No'
        upload=settings.docs.get(role) if b.document_applies(role,target) else None
        if role in ('photo','signature','dobProof','educationProof','parentSignature') and upload is None:
            raise ConfigPaused('Required prepared upload unavailable: '+role)
        if upload is not None:
            if input_name not in page.file_inputs():raise ConfigPaused('Configured upload control unavailable: '+role)
            files[input_name]=upload;data[flag]='Yes'
    button=page.submit_button()
    if not button:raise b.ConfigError('Live Submit control is absent')
    data[button[0]]=button[1]
    multipart=b.Multipart()
    for name,value in data.items():multipart.field(name,value)
    for name in page.file_inputs():multipart.file(name,files.get(name))
    # Fee, allocation and signed fields come from the matching cached server state.
    return multipart.body(),multipart.content_type(),files,data

def definite_iis_busy(page):
    text=page.body.lower()
    return page.complete and not page.redirects and page.status==503 and all(x in text for x in ['503.2','iis','beginrequest','not yet determined'])

def save_capsule(target_code,page,http,binding):
    cookies=[]
    for c in http.jar:
        cookies.append({k:getattr(c,k) for k in ('version','name','value','port','port_specified','domain','domain_specified','domain_initial_dot','path','path_specified','secure','expires','discard','comment','comment_url')} | {'rest':c._rest,'rfc2109':c.rfc2109})
    write_private(HERE/(target_code+'-ready.html'),page.body)
    write_private(HERE/(target_code+'-ready.json'),json.dumps({'url':page.url,'status':page.status,'complete':page.complete,'cookies':cookies,'capturedAt':b.now_ist().isoformat(),'serverKey':binding},indent=2))

def load_capsule(target_code,http,binding):
    meta_path=HERE/(target_code+'-ready.json')
    if not meta_path.exists():raise b.ConfigError('Run this script with --prepare-only first to capture valid allocation/fee and WebForms state.')
    meta=json.loads(meta_path.read_text());http.check_destination(meta['url'])
    if meta.get('serverKey')!=binding:raise ConfigPaused('Prepared state does not match the current config. Run --prepare-only to capture matching state.')
    for cookie in meta['cookies']:http.jar.set_cookie(Cookie(**cookie))
    return b.Page(meta['url'],meta['status'],(HERE/(target_code+'-ready.html')).read_text(),complete=meta['complete'])

def is_ready(page,settings,target):
    if page.status!=200 or not page.complete or page.name!=b.FORM_PAGE:return False
    if page.value('txtMobileNo')!=settings.phones[target.index]:return False
    selection=desired_selection(page,settings,target)
    if selection is None:return False
    fields=['ddlCourse','ddlCourseLevel','ddlScheduleCourse','ddlState']
    if any(page.value(field)!=option[0] for field,option in zip(fields,selection)):return False
    allocation=str(page.value('hdfCapacitySheetAllocationId') or '').strip()
    return (page.value('txtDateOfBirth')==settings.applicant['dateOfBirth']
            and allocation!=''
            and (allocation!='0' or page.value('hdfTypeOfCapacitySheet')=='ScheduleCourseWise')
            and (b.parse_server_number(page.value('txtCourseFee'),allow_decimal=True) or 0)>0
            and b.parse_server_number(page.value('txtAvailableSeat')) is not None)

def prepare_form(settings,target,http,deadline,check_config=None):
    phone=settings.phones[target.index];session=b.Session.__new__(b.Session)
    session.http,session.phone,session.static=http,phone,{}
    page=None
    while True:
        if check_config is not None:settings,target=check_config()
        http.timeout=settings.direct_request_timeout
        if deadline is not None and time.monotonic()>deadline:raise b.ConfigError('Preparation deadline reached; no final application was sent.')
        try:
            if page is None:
                page=http.get('admission_form.aspx?'+urllib.parse.urlencode({'mobile':phone}))
                if page.status!=200 or page.name!=b.FORM_PAGE or not page.complete:
                    emit(status='preparing',target=target.batch_id,phase='form_get',httpStatus=page.status);page=None;continue
            if is_ready(page,settings,target):return page
            course=course_option(page,target)
            if course is None:
                emit(status='preparing_retry',target=target.batch_id,reason='Requested course is not currently listed');page=None;continue
            values={'ddlCourse':course[0]}
            if page.value('ddlCourse')!=course[0] or not b.real_options(page.options('ddlCourseLevel')):
                event='ddlCourse'
            else:
                level=b.match_level(page.options('ddlCourseLevel'),target,False)
                if level is None:
                    emit(status='preparing_retry',target=target.batch_id,reason='Requested level is not currently listed');page=None;continue
                values['ddlCourseLevel']=level[0]
                if page.value('ddlCourseLevel')!=level[0] or not b.real_options(page.options('ddlScheduleCourse')):
                    event='ddlCourseLevel'
                else:
                    batch=batch_option(page,target)
                    state=b.match_choice(page.options('ddlState'),settings.state,b.state_aliases(settings.state))
                    if batch is None:
                        emit(status='preparing_retry',target=target.batch_id,reason='Requested dates are not currently listed');page=None;continue
                    if state is None:raise ConfigPaused('Configured state is not available on the live form.')
                    values.update(ddlScheduleCourse=batch[0],ddlState=state[0],txtDateOfBirth=settings.applicant['dateOfBirth'])
                    allocation=str(page.value('hdfCapacitySheetAllocationId') or '')
                    event='ddlScheduleCourse' if page.value('ddlScheduleCourse')!=batch[0] or allocation=='' or (allocation=='0' and page.value('hdfTypeOfCapacitySheet')!='ScheduleCourseWise') else 'txtDateOfBirth'
            emit(status='preparing',target=target.batch_id,phase=event)
            response=session.postback(page,event,values,timeout=settings.direct_request_timeout if deadline is None else min(settings.direct_request_timeout,max(0.001,deadline-time.monotonic())))
            if response.complete and any(x in response.body.lower() for x in b.VIEWSTATE_ERRORS):page=None;continue
            if response.status!=200 or response.name!=b.FORM_PAGE or not response.complete:
                emit(status='preparing_retry',target=target.batch_id,httpStatus=response.status);continue
            page=response
            write_private(HERE/(target.batch_id+'-preparing.html'),page.body)
        except b.Transient as exc:
            if isinstance(exc,b.SessionLost):page=None
            emit(status='preparing_retry',target=target.batch_id,reason=str(exc))


def run(target_code,config,base_url,plan=False,limit=None,snapshot=None,prepare_only=False,prepare_seconds=None):
    reader=ConfigReader(config)
    first=reader.read() if plan else reader.wait(target_code)
    first.target(target_code)
    lockfile=None
    if not plan:
        lockfd=b.private_descriptor(HERE/(target_code+'-worker.lock'),os.O_RDWR|os.O_CREAT|os.O_APPEND)
        lockfile=os.fdopen(lockfd,'a+b')
        try:fcntl.flock(lockfile.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise b.ConfigError('This direct worker is already running.')
    active=None;settings=None;target=None;phone=None;progress=None
    http=None;page=None;binding=None;prepared_binding=None;packet=None;attempts=0
    last_pause=None
    try:
        while True:
            candidate=reader.read() if plan else reader.wait(target_code)
            next_settings=candidate.settings
            next_target=candidate.target(target_code)
            next_phone=next_settings.phones[next_target.index]
            next_binding=server_key(next_settings,next_target,base_url)
            try:
                if active is None or candidate.digest!=active.digest:
                    candidate_progress=Progress(next_settings,base_url)
                    server_changed=binding!=next_binding
                    settings,target,phone,progress=next_settings,next_target,next_phone,candidate_progress
                    if server_changed:
                        http=b.Http(base_url,next_settings.direct_request_timeout,b.Log(echo=False));page=None;prepared_binding=None
                    http.timeout=next_settings.direct_request_timeout
                    packet=None
                    if active is not None:emit(status='config_reloaded',target=target_code,serverRefresh=server_changed)
                    active=candidate;binding=next_binding
                if page is None:
                    if plan:
                        if snapshot:
                            meta=json.loads(snapshot.with_suffix('.json').read_text())
                            http.check_destination(meta['url'])
                            if meta.get('serverKey')!=binding:raise ConfigPaused('Snapshot does not match the current server-dependent config.')
                            page=b.Page(meta['url'],meta['status'],snapshot.read_text(),complete=meta['complete'])
                        else:page=load_capsule(target_code,http,binding)
                    else:
                        def check_config():
                            latest=reader.wait(target_code)
                            latest_target=latest.target(target_code)
                            if server_key(latest.settings,latest_target,base_url)!=binding:raise ConfigChanged()
                            return latest.settings,latest_target
                        deadline=None if prepare_seconds is None else time.monotonic()+prepare_seconds
                        page=prepare_form(settings,target,http,deadline,check_config)
                        prepared_binding=binding;save_capsule(target_code,page,http,binding)
                        # A local-only edit during preparation must reach the final packet too.
                        continue
                    prepared_binding=binding
                if prepared_binding!=binding or not is_ready(page,settings,target):
                    if plan:raise ConfigPaused('Prepared form no longer matches the current config.')
                    page=None;packet=None;continue
                if packet is None:
                    packet=build_packet(settings,target,page)
                    if not plan:write_private(HERE/(target_code+'-request.multipart'),packet[0])
                body,ctype,files,data=packet
                last_pause=None
                if plan or prepare_only:
                    emit(status='packet_ready' if plan else 'prepared',target=target_code,primaryEnding=phone[-4:],
                         course=data['ddlCourse'],level=data['ddlCourseLevel'],batch=data['ddlScheduleCourse'],state=data['ddlState'],
                         uploads=len(files),allocationId=data['hdfCapacitySheetAllocationId'],availableSeats=page.value('txtAvailableSeat'),
                         fee=data['txtCourseFee'],requestBytes=len(body))
                    return 0
            except ConfigChanged:
                page=None;packet=None;prepared_binding=None;continue
            except ConfigPaused as exc:
                if plan:raise
                message=str(exc)
                if message!=last_pause:emit(status='config_paused',target=target_code,error=message)
                last_pause=message
                threading.Event().wait(0.1)
                continue
            # Freeze every value used to submit and classify this request until it settles.
            request_progress,request_target,request_phone=progress,target,phone
            request_settings,request_page,request_binding=settings,page,binding
            session=b.Session.__new__(b.Session)
            session.bot=request_progress.bot;session.http=http;session.phone=request_phone;session.static={}
            attempts+=1;started=False
            def before_write():
                nonlocal started
                if not started:
                    request_progress.start(request_target,request_phone);started=True
            emit(status='sending',target=target_code,attempt=attempts)
            try:response=http.send('POST',request_page.post_url,body,ctype,referer=request_page.url,timeout=request_settings.direct_submit_timeout,on_start=before_write)
            except Exception as exc:
                save_result(target_code,attempts,limit==1,error=exc,request_started=started)
                if started:
                    request_progress.finish(request_target,request_phone,'uncertain',{'error':str(exc)})
                    emit(status='once_uncertain' if limit==1 else 'retrying_uncertain',target=target_code,reason=str(exc))
                    if limit and attempts>=limit:return 1
                    continue
                if isinstance(exc,b.ConfigError):
                    try:latest=reader.read()
                    except ConfigPaused:continue
                    if latest.digest!=active.digest:
                        emit(status='config_changed_before_send',target=target_code);continue
                    raise
                emit(status='once_not_sent' if limit==1 else 'retrying',target=target_code,reason=str(exc))
                if limit and attempts>=limit:return 1
                continue
            result_metadata=save_result(target_code,attempts,limit==1,response=response,request_started=started)
            if definite_iis_busy(response):
                request_progress.clear(request_phone)
                emit(status='once_rejected_503' if limit==1 else 'retrying',target=target_code,httpStatus=503,reason='IIS 503.2 rejected before application handler')
                if limit and attempts>=limit:return 1
                continue
            if response.complete and not response.redirects and any(x in response.body.lower() for x in b.VIEWSTATE_ERRORS):
                request_progress.clear(request_phone)
                emit(status='once_invalid_state' if limit==1 else 'refreshing_webforms_state',target=target_code)
                if limit and attempts>=limit:return 1
                (HERE/(target_code+'-ready.json')).unlink(missing_ok=True)
                page=None;prepared_binding=None;packet=None;continue
            course,level,batch,state=desired_selection(request_page,request_settings,request_target)
            selection=b.Selection(course,level,batch,state,b.parse_server_number(request_page.value('txtAvailableSeat')),
                                  request_page.value('txtCourseFee'),request_page,b.observed_server_age(request_page))
            try:info=session.judge_submit(response,request_target,selection,result_metadata,set(files))
            except (b.TargetFull,b.NotOpen,b.RateLimited) as exc:
                request_progress.clear(request_phone)
                emit(status='once_rejected' if limit==1 else 'retrying_rejection',target=target_code,reason=str(exc))
                if is_ready(response,request_settings,request_target):
                    page=response;prepared_binding=request_binding;save_capsule(target_code,page,http,request_binding)
                else:page=None;prepared_binding=None
                packet=None
                if limit and attempts>=limit:return 1
                continue
            except (b.Rejected,b.PhoneUsed) as exc:
                request_progress.finish(request_target,request_phone,'rejected',{'reason':str(exc)})
                emit(status='once_rejected' if limit==1 else 'retrying_rejection',target=target_code,reason=str(exc))
                if limit and attempts>=limit:return 1
                continue
            except Exception as exc:
                request_progress.finish(request_target,request_phone,'uncertain',{'error':str(exc)})
                emit(status='once_uncertain' if limit==1 else 'retrying_uncertain',target=target_code,httpStatus=response.status,reason=str(exc))
                if limit and attempts>=limit:return 1
                continue
            request_progress.confirm(request_target,request_phone,info)
            emit(status='confirmed',target=target_code,applicationId=info.get('applicationId'),receipt=info.get('url'))
            if limit and attempts>=limit:return 0
            # User-requested always-run mode continues even after confirmed acceptance.
            continue
    finally:
        if lockfile is not None:lockfile.close()


def entry(target):
    parser=argparse.ArgumentParser(description=f'Always-retrying direct final POST for {target}; --once makes a single attempt.')
    parser.add_argument('--once',action='store_true',help='make one final-request attempt for manual inspection')
    parser.add_argument('--plan',action='store_true',help='inspect the prepared packet without submitting')
    parser.add_argument('--prepare-only',action='store_true',help='capture server fee, seats, allocation ID and signed form state; never submit')
    parser.add_argument('--prepare-seconds',type=float,default=None,help='optional preparation deadline; default is continuous retry')
    parser.add_argument('--snapshot',type=Path,help='private HTML snapshot with matching .json metadata, for offline packet inspection')
    args=parser.parse_args()
    if args.snapshot and not args.plan:parser.error('--snapshot is only permitted with --plan')
    try:return run(target,ROOT/'configs/me.jsonc',b.DEFAULT_BASE_URL,plan=args.plan,limit=1 if args.once else None,snapshot=args.snapshot,prepare_only=args.prepare_only,prepare_seconds=args.prepare_seconds)
    except KeyboardInterrupt:emit(status='stopped',target=target,reason='Stopped by user; prior booking status does not block the next direct run.');return 130
    except Exception as exc:emit(error=str(exc),target=target);return 2
