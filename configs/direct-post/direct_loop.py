#!/usr/bin/env python3
"""Two private once-prepared final-POST workers. No retry sleep."""
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
import urllib.parse
from contextlib import contextmanager

ROOT=Path(__file__).resolve().parents[2]
HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('iism_direct_runtime',ROOT/'iism-bot.py')
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)
IDS={'G1':('80','1st course from 20 Dec 2026 to 02 Jan 2027'),'G4':('83','4th course from 31 Jan to 13 Feb 2026')}
FEE='10000'
ALLOCATIONS={'G1':('ScheduleCourseWise','0'),'G4':('CourseLevelWise','7')}

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

def load_settings(config):
    s,errors,warnings=b.parse_settings(b.load_jsonc(config),config)
    if errors:raise b.ConfigError('; '.join(errors))
    problems,_=b.prepare_documents(s,HERE/'upload-ready')
    if problems:raise b.ConfigError('; '.join(problems))
    # The user explicitly requested the supplied SIGN.jpg in this slot too.
    s.docs['parentSignature']=s.docs['signature']
    s.opt['notify']=False
    return s

class Progress:
    """Short transactions permit G1 and G4 requests in parallel, preserving native progress."""
    def __init__(self,settings,base_url):
        config=settings.config_path
        identity=json.dumps([str(config.resolve()),b.origin(base_url),settings.season])
        self.out=ROOT/'runs'/(config.stem+'-'+hashlib.sha256(identity.encode()).hexdigest()[:16])
        self.bot=b.Bot(settings,self.out,base_url=base_url,log=b.Log(echo=False))
    @contextmanager
    def transaction(self,startup=False):
        state=self.bot.state
        b.private_dir(state.path.parent)
        fd=b.private_descriptor(state.path.with_suffix('.lock'),os.O_RDWR|os.O_CREAT|os.O_APPEND)
        handle=os.fdopen(fd,'a+b')
        try:
            if startup:
                end=time.monotonic()+1
                while True:
                    try:fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB);break
                    except BlockingIOError:
                        if time.monotonic()>=end:
                            raise b.ConfigError('The original bot still holds the progress lock. Stop it with Ctrl+C before starting the direct workers; no request was sent.')
            else:fcntl.flock(handle.fileno(),fcntl.LOCK_EX)
            state.lease=handle
            state.reload();state.bind(b.expected_state_identity(self.bot.s,self.bot.base_url));self.bot._restore()
            yield self.bot
        finally:
            state.lease=None;handle.close()
    def check(self,target,phone,startup=False):
        with self.transaction(startup) as bot:
            if target.index in bot.booked:return 'booked'
            if phone in bot.pending:raise b.ConfigError('This preference has an uncertain saved submission. Verify booked/not booked before retrying.')
            if bot.assignments.get(b.target_key(target))!=phone:raise b.ConfigError('This primary no longer owns the preference; inspect saved progress.')
        return 'ready'
    def start(self,target,phone):
        with self.transaction() as bot:
            if target.index in bot.booked:raise b.ConfigError('This preference is already booked; no duplicate was sent.')
            if phone in bot.pending:raise b.ConfigError('An unresolved final request already exists for this number.')
            bot.mark_submitting(phone,target)
    def clear(self,phone):
        with self.transaction() as bot:bot.clear_submitting(phone)
    def confirm(self,target,phone,info):
        with self.transaction() as bot:bot.record_booking(phone,target,info)

def build_packet(settings,target,page):
    phone=settings.phones[target.index]
    if page.name!=b.FORM_PAGE or page.status!=200 or not page.complete or page.value('txtMobileNo')!=phone:
        raise b.ConfigError('Expected a complete live admission form echoing this primary.')
    for key in ['__VIEWSTATE','__VIEWSTATEGENERATOR','__EVENTVALIDATION']:
        if not page.value(key) or page.value(key)=='<redacted>':raise b.ConfigError('Missing real live WebForms token: '+key)
    # IDs were read from the current browser dropdowns, not guessed from old fixtures.
    expected_type,expected_id=ALLOCATIONS[target.batch_id]
    if (page.value('hdfTypeOfCapacitySheet'),str(page.value('hdfCapacitySheetAllocationId'))) != (expected_type,expected_id):
        raise b.ConfigError('The captured allocation changed; inspect it before using the pinned allocation ID.')
    data=dict(page.fields())
    data.update(txtCourseFee=FEE,hdfTypeOfCapacitySheet=expected_type,hdfCapacitySheetAllocationId=expected_id)
    for field,value in settings.form_values.items():
        if not page.missing_enabled((field,)):data[field]=value
    data.update(__EVENTTARGET='',__EVENTARGUMENT='',ddlCourse='4',ddlCourseLevel='4',ddlScheduleCourse=IDS[target.batch_id][0],ddlState='27',ddlEduationQualification='Graduation',ddlVegetarianOrNon='Vegetarian',chkIAgree='on')
    files={}
    for role,input_name,flag,required in b.DOCUMENTS:
        if role not in ('photo','signature','dobProof','educationProof','parentSignature'):continue
        upload=settings.docs.get(role)
        if not upload or input_name not in page.file_inputs():raise b.ConfigError('Required prepared upload unavailable: '+role)
        files[input_name]=upload;data[flag]='Yes'
    button=page.submit_button()
    if not button:raise b.ConfigError('Live Submit control is absent')
    data[button[0]]=button[1]
    multipart=b.Multipart()
    for name,value in data.items():multipart.field(name,value)
    for name in page.file_inputs():multipart.file(name,files.get(name))
    # Fee and allocation are pinned to user-authorized/observed values for this run; signed state stays server-generated.
    return multipart.body(),multipart.content_type(),files,data

def definite_iis_busy(page):
    text=page.body.lower()
    return page.complete and not page.redirects and page.status==503 and all(x in text for x in ['503.2','iis','beginrequest','not yet determined'])

def save_capsule(target_code,page,http):
    cookies=[]
    for c in http.jar:
        cookies.append({k:getattr(c,k) for k in ('version','name','value','port','port_specified','domain','domain_specified','domain_initial_dot','path','path_specified','secure','expires','discard','comment','comment_url')} | {'rest':c._rest,'rfc2109':c.rfc2109})
    write_private(HERE/(target_code+'-ready.html'),page.body)
    write_private(HERE/(target_code+'-ready.json'),json.dumps({'url':page.url,'status':page.status,'complete':page.complete,'cookies':cookies,'capturedAt':b.now_ist().isoformat()},indent=2))

def load_capsule(target_code,http):
    meta_path=HERE/(target_code+'-ready.json')
    if not meta_path.exists():raise b.ConfigError('Run this script with --prepare-only first to capture valid allocation/fee and WebForms state.')
    meta=json.loads(meta_path.read_text());http.check_destination(meta['url'])
    for cookie in meta['cookies']:http.jar.set_cookie(Cookie(**cookie))
    return b.Page(meta['url'],meta['status'],(HERE/(target_code+'-ready.html')).read_text(),complete=meta['complete'])

def is_ready(page,settings,target):
    return (page.status==200 and page.complete and page.name==b.FORM_PAGE
            and page.value('txtMobileNo')==settings.phones[target.index]
            and page.value('ddlCourse')=='4' and page.value('ddlCourseLevel')=='4'
            and page.value('ddlScheduleCourse')==IDS[target.batch_id][0] and page.value('ddlState')=='27'
            and page.value('txtDateOfBirth')==settings.applicant['dateOfBirth']
            and str(page.value('hdfCapacitySheetAllocationId') or '').strip() != ''
            and (str(page.value('hdfCapacitySheetAllocationId')).strip()!='0' or page.value('hdfTypeOfCapacitySheet')=='ScheduleCourseWise')
            and (b.parse_server_number(page.value('txtCourseFee'),allow_decimal=True) or 0)>0
            and b.parse_server_number(page.value('txtAvailableSeat')) is not None)

def prepare_form(settings,target,http,deadline):
    phone=settings.phones[target.index];session=b.Session.__new__(b.Session)
    session.http,session.phone,session.static=http,phone,{}
    page=None
    while True:
        if deadline is not None and time.monotonic()>deadline:raise b.ConfigError('Preparation deadline reached; no final application was sent.')
        try:
            if page is None:
                page=http.get('admission_form.aspx?'+urllib.parse.urlencode({'mobile':phone}))
                if page.status!=200 or page.name!=b.FORM_PAGE or not page.complete:
                    emit(status='preparing',target=target.batch_id,phase='form_get',httpStatus=page.status);page=None;continue
            if is_ready(page,settings,target):return page
            values={'ddlCourse':'4'}
            if page.value('ddlCourse')!='4' or not any(v=='4' for v,t in page.options('ddlCourseLevel')):
                event='ddlCourse'
            elif page.value('ddlCourseLevel')!='4' or not any(v==IDS[target.batch_id][0] for v,t in page.options('ddlScheduleCourse')):
                event='ddlCourseLevel';values['ddlCourseLevel']='4'
            else:
                values.update(ddlCourseLevel='4',ddlScheduleCourse=IDS[target.batch_id][0],ddlState='27',txtDateOfBirth=settings.applicant['dateOfBirth'])
                event='ddlScheduleCourse' if page.value('ddlScheduleCourse')!=IDS[target.batch_id][0] or str(page.value('hdfCapacitySheetAllocationId') or '') in ('','0') else 'txtDateOfBirth'
            emit(status='preparing',target=target.batch_id,phase=event)
            response=session.postback(page,event,values,timeout=15 if deadline is None else min(15,max(1,deadline-time.monotonic())))
            if response.complete and any(x in response.body.lower() for x in b.VIEWSTATE_ERRORS):
                page=None;continue
            if response.status!=200 or response.name!=b.FORM_PAGE or not response.complete:
                emit(status='preparing_retry',target=target.batch_id,httpStatus=response.status);continue
            page=response
            write_private(HERE/(target.batch_id+'-preparing.html'),page.body)
        except b.Transient as exc:
            if isinstance(exc,b.SessionLost):page=None
            emit(status='preparing_retry',target=target.batch_id,reason=str(exc))

def run(target_code,config,base_url,plan=False,limit=None,snapshot=None,prepare_only=False,prepare_seconds=None,confirm_not_booked=False):
    settings=load_settings(config)
    target=next(t for t in settings.targets if t.batch_id==target_code)
    phone=settings.phones[target.index]
    lockfile=None
    if not plan:
        lock_path=HERE/(target_code+'-worker.lock')
        lockfd=b.private_descriptor(lock_path,os.O_RDWR|os.O_CREAT|os.O_APPEND)
        lockfile=os.fdopen(lockfd,'a+b')
        try:fcntl.flock(lockfile.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise b.ConfigError('This direct worker is already running.')
    progress=Progress(settings,base_url)
    http=b.Http(base_url,15,b.Log(echo=False))
    if prepare_only:
        page=prepare_form(settings,target,http,None if prepare_seconds is None else time.monotonic()+prepare_seconds)
        save_capsule(target_code,page,http)
        body,ctype,files,data=build_packet(settings,target,page)
        write_private(HERE/(target_code+'-request.multipart'),body)
        emit(status='prepared',target=target_code,allocationId=page.value('hdfCapacitySheetAllocationId'),availableSeats=page.value('txtAvailableSeat'),fee=page.value('txtCourseFee'),requestBytes=len(body),guardianAttached=True)
        return 0
    if not plan and confirm_not_booked:
        with progress.transaction(startup=True) as bot:
            if target.index in bot.booked:
                emit(status='already_booked',target=target_code);return 0
            previous=bot.state.data['phones'].get(phone)
            if phone in bot.pending:bot.clear_submitting(phone)
        write_private(HERE/(target_code+'-manual-confirmation.json'),json.dumps({'target':target_code,'at':b.now_ist().isoformat(),'operatorConfirmation':'previous attempt not booked','previousRecord':previous},indent=2))
    if not plan and progress.check(target,phone,startup=True)=='booked':
        emit(status='already_booked',target=target_code);return 0
    phase='final';attempts=0;packet=None
    if snapshot:
        meta=json.loads(snapshot.with_suffix('.json').read_text())
        page=b.Page(meta['url'],meta['status'],snapshot.read_text(),complete=meta['complete'])
    else:
        if not (HERE/(target_code+'-ready.json')).exists():
            page=prepare_form(settings,target,http,None if prepare_seconds is None else time.monotonic()+prepare_seconds)
            save_capsule(target_code,page,http)
        else:page=load_capsule(target_code,http)
    if not is_ready(page,settings,target):raise b.ConfigError('Captured form is not initialized for this batch/state/DOB with a server fee and allocation ID. Prepare it first.')
    session=b.Session.__new__(b.Session)
    session.bot=progress.bot;session.http=http;session.phone=phone;session.static={}
    while True:
        if packet is None:
            packet=build_packet(settings,target,page)
            if not plan:write_private(HERE/(target_code+'-request.multipart'),packet[0])
        body,ctype,files,data=packet
        if plan:
            emit(status='packet_ready',target=target_code,primaryEnding=phone[-4:],course='4',level='4',batch=IDS[target_code][0],state='27',uploads=len(files),guardianSignature='same supplied signature',bytes=len(body),serverFeeCarried=data.get('txtCourseFee'),serverAllocationCarried=data.get('hdfCapacitySheetAllocationId'));return 0
        attempts+=1;started=False
        def before_write():
            nonlocal started
            if not started:
                progress.start(target,phone);started=True
        emit(status='sending',target=target_code,attempt=attempts)
        try:
            response=http.send('POST',page.post_url,body,ctype,referer=page.url,timeout=30,on_start=before_write)
        except Exception as exc:
            save_result(target_code,attempts,limit==1,error=exc,request_started=started)
            if started:
                emit(status='uncertain',target=target_code,reason=str(exc),savedProgress=str(progress.bot.state.path));return 1
            if isinstance(exc,b.ConfigError):raise
            emit(status='once_not_sent' if limit==1 else 'retrying',target=target_code,reason=str(exc))
            if limit and attempts>=limit:return 1
            continue
        save_result(target_code,attempts,limit==1,response=response,request_started=started)
        if definite_iis_busy(response):
            progress.clear(phone)
            emit(status='once_rejected_503' if limit==1 else 'retrying',target=target_code,httpStatus=503,reason='IIS 503.2 rejected before application handler')
            if limit and attempts>=limit:return 1
            continue
        if response.complete and not response.redirects and any(x in response.body.lower() for x in b.VIEWSTATE_ERRORS):
            progress.clear(phone)
            emit(status='once_invalid_state' if limit==1 else 'refreshing_webforms_state',target=target_code)
            if limit and attempts>=limit:return 1
            page=prepare_form(settings,target,http,None);save_capsule(target_code,page,http);packet=None;continue
        selection=b.Selection(('4','Snow Skiing Courses at Gulmarg'),('4','Basic SSC'),IDS[target_code],('27','MAHARASHTRA'),None,page.value('txtCourseFee'),page,b.observed_server_age(page))
        try:info=session.judge_submit(response,target,selection,HERE/'responses'/('latest-'+target_code+'.json'),set(files))
        except (b.TargetFull,b.NotOpen,b.RateLimited) as exc:
            progress.clear(phone);emit(status='once_rejected' if limit==1 else 'retrying_rejection',target=target_code,reason=str(exc))
            if is_ready(response,settings,target):
                page=response;save_capsule(target_code,page,http);packet=None
            if limit and attempts>=limit:return 1
            continue
        except (b.Rejected,b.PhoneUsed) as exc:
            progress.clear(phone);emit(status='rejected',target=target_code,reason=str(exc));return 1
        except Exception as exc:
            emit(status='uncertain',target=target_code,httpStatus=response.status,reason=str(exc),savedProgress=str(progress.bot.state.path));return 1
        progress.confirm(target,phone,info)
        emit(status='confirmed',target=target_code,applicationId=info.get('applicationId'),receipt=info.get('url'));return 0

def entry(target):
    parser=argparse.ArgumentParser(description=f'Direct final multipart POST for {target}; serial immediate retry after definite IIS 503.2.')
    parser.add_argument('--confirm-not-booked',action='store_true',help='record your confirmation that the previous pending attempt was not accepted; clears only this batch')
    parser.add_argument('--once',action='store_true',help='make one final-request attempt for manual inspection')
    parser.add_argument('--plan',action='store_true',help='inspect the prepared packet without submitting')
    parser.add_argument('--prepare-only',action='store_true',help='capture server fee, seats, allocation ID and signed form state; never submit')
    parser.add_argument('--prepare-seconds',type=float,default=None,help='optional preparation deadline; default is continuous retry')
    parser.add_argument('--snapshot',type=Path,help='private HTML snapshot with matching .json metadata, for offline packet inspection')
    args=parser.parse_args()
    if args.confirm_not_booked and (args.plan or args.prepare_only):parser.error('--confirm-not-booked is for a live run, not preparation/inspection')
    if args.snapshot and not args.plan:parser.error('--snapshot is only permitted with --plan')
    try:return run(target,ROOT/'configs/me.jsonc',b.DEFAULT_BASE_URL,plan=args.plan,limit=1 if args.once else None,snapshot=args.snapshot,prepare_only=args.prepare_only,prepare_seconds=args.prepare_seconds,confirm_not_booked=args.confirm_not_booked)
    except KeyboardInterrupt:emit(status='stopped',target=target,reason='Any possibly-sent request remains pending in saved progress.');return 130
    except Exception as exc:emit(error=str(exc),target=target);return 2
