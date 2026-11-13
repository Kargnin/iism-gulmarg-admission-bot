"""Behavioral tests against local HTTP services; never uses real config or documents."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0,str(Path(__file__).resolve().parent))
import verify_direct as fixture
b=fixture.support.bot
ENGINE=Path(os.environ.get('DIRECT_RELOAD_ENGINE',str(Path(__file__).with_name('direct_loop.py'))))

class Handler(fixture.ReadyHandler):
    def form(self,mobile,values,listed,*args,**kwargs):
        if self.site.page_seats is not None:values=dict(values,txtAvailableSeat=str(self.site.page_seats))
        return super().form(mobile,values,listed,*args,**kwargs)
    def get_form(self,sess,query):
        with self.site.lock:self.site.form_gets+=1
        return super().get_form(sess,query)
    def post_form(self,sess,query,values,files):
        if not values.get('btnSubmit'):
            self.site.prep_events.append(values.get('__EVENTTARGET'))
            if self.site.prep_change is not None:
                change,self.site.prep_change=self.site.prep_change,None
                change()
            return super().post_form(sess,query,values,files)
        phone=sess['mobile']
        with self.site.lock:
            number=len(self.site.requests_seen)
            self.site.requests_seen.append({'fields':dict(values),'phone':phone,
                'files':{key:hashlib.sha256(content).hexdigest() for key,(name,content) in files.items() if name}})
        if number>0 and self.site.final_delay:
            threading.Event().wait(self.site.final_delay)
            return self.send(503,fixture.BUSY)
        if number==0:
            data=json.loads((self.site.root/'capsules/G4-attempt-state.json').read_text())
            self.site.attempt_was_logged=data['status']=='submitting' and data['phone']==phone
            self.site.change()
            if self.site.empty_rejection:
                return self.send(200,self.form(phone,{'lblMsg':('Seat Not Available','Red')},['4']))
            if self.site.invalid_final:
                return self.ysod('Validation of viewstate MAC failed.')
            return self.send(503,fixture.mock_site.IIS_503 if self.site.uncertain else fixture.BUSY)
        return super().post_form(sess,query,values,files)

class ReloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='reload-test-',dir=fixture.repo/'configs')
        self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        (self.root/'configs').mkdir();(self.root/'capsules').mkdir()
        self.cfg=fixture.support.make_config(self.root,['9000000011','9000000012','9000000013','9000000014'],['G4 Basic','G1 Basic'])
        self.cfg['documents']['parentSignature']='sign.jpg'
        self.config=self.root/'configs/me.jsonc';self.write(self.cfg)
        self.site=fixture.make_site();self.site.root=self.root
        self.site.cold=False;self.site.full_once=False;self.site.fail_before_commit=False
        self.site.prep_busy=0;self.site.selector_busy=0;self.site.change=lambda:None
        self.site.prep_change=None;self.site.uncertain=False;self.site.requests_seen=[]
        self.site.form_gets=0;self.site.prep_events=[];self.site.attempt_was_logged=False;self.site.page_seats=None;self.site.final_delay=0
        self.site.empty_rejection=False;self.site.invalid_final=False
        ski=self.site.courses['4'];ski['levels']=[('4','Basic SSC'),('5','Intermediate SSC'),('28','Snow Boarding')]
        ski['batches'].append(('82','Course from 17 Jan to 30 Jan 2027',fixture.mock_site.date(2027,1,17),'Gulmarg'))
        self.site.seats={(cv,lv,batch[0]):3 for cv,c in self.site.courses.items() for lv,_ in c['levels'] for batch in c['batches']}
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.site=self.site;self.server.daemon_threads=True
        self.server.handle_error=lambda *a:None
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.url=f'http://127.0.0.1:{self.server.server_port}'
        self.processes=[];self.read_threads=[];self.lines=[];self.paused=threading.Event()
        self.addCleanup(self.stop_children)
    def write(self,cfg):
        tmp=self.config.with_suffix('.tmp');tmp.write_text(json.dumps(cfg));tmp.chmod(0o600);os.replace(tmp,self.config)
    def edit(self,fn):
        cfg=json.loads(self.config.read_text());fn(cfg);self.write(cfg)
    def state(self):return json.loads(next((self.root/'runs').glob('*/state.json')).read_text())
    def attempt(self):return json.loads((self.root/'capsules/G4-attempt-state.json').read_text())
    def seed_used(self,with_history=True):
        settings,errors,_=b.parse_settings(self.cfg,self.config);self.assertFalse(errors)
        identity=json.dumps([str(self.config.resolve()),b.origin(self.url),settings.season])
        out=self.root/'runs'/('me-'+hashlib.sha256(identity.encode()).hexdigest()[:16]);b.private_dir(out)
        state=b.State(out/'state.json',True);state.acquire()
        try:
            state.bind(b.expected_state_identity(settings,self.url))
            if with_history:state.data['phones']['9000000013']={'status':'used','note':'known refused before this test'}
            state.save()
        finally:state.release()
    def start(self,limit=2,seed=True):
        if seed and not (self.root/'capsules/G4-ready.json').exists():self.prepare_once()
        code="import importlib.util,sys;from pathlib import Path;s=importlib.util.spec_from_file_location('direct',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);m.ROOT=Path(sys.argv[2]);m.HERE=m.ROOT/'capsules';m.ALLOCATIONS={'G1':('synthetic-shared','synthetic-4-4-80'),'G4':('synthetic-shared','synthetic-4-4-83')};raise SystemExit(m.run('G4',m.ROOT/'configs/me.jsonc',sys.argv[3],limit=int(sys.argv[4])))"
        proc=subprocess.Popen([sys.executable,'-u','-c',code,str(ENGINE),str(self.root),self.url,str(limit)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        self.processes.append(proc)
        def read():
            for line in proc.stdout:
                self.lines.append(line)
                if 'config_paused' in line:self.paused.set()
        thread=threading.Thread(target=read,daemon=True);thread.start();self.read_threads.append(thread);return proc,thread
    def refresh_after_edit(self,proc,thread):
        self.assertTrue(self.paused.wait(3),''.join(self.lines))
        self.assertEqual(len(self.site.requests_seen),1)
        count=self.site.requests
        threading.Event().wait(.1)
        self.assertEqual(self.site.requests,count,'Config mismatch triggered automatic preparation')
        proc.send_signal(signal.SIGINT);proc.wait(timeout=2);thread.join(timeout=2)
        self.prepare_once()
        return self.start(limit=1)
    def finish(self,proc,thread):
        try:proc.wait(timeout=10)
        except subprocess.TimeoutExpired:self.fail('Worker did not complete: '+''.join(self.lines[-25:]))
        thread.join(timeout=2)
        self.assertEqual(proc.returncode,0,''.join(self.lines));self.assertTrue(self.site.attempt_was_logged)
        self.assertEqual(len(self.site.applications),1)
    def stop_children(self):
        for proc in self.processes:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
                try:proc.wait(timeout=2)
                except subprocess.TimeoutExpired:proc.kill();proc.wait()
        for thread in self.read_threads:thread.join(timeout=2)
        for proc in self.processes:
            if proc.stdout:proc.stdout.close()
    def test_local_edits_rebuild_next_packet_without_server_preparation(self):
        (self.root/'docs/new-photo.jpg').write_bytes(b'\xff\xd8\xff\xe0'+b'photo-new'*400)
        (self.root/'docs/guardian.jpg').write_bytes(b'\xff\xd8\xff\xe0'+b'guardian-new'*300)
        def update(cfg):
            cfg['applicant'].update(name='Edited Person',email='edited@example.com',education='PG',diet='non veg')
            cfg['address'].update(houseNo='2',street='New Road',pincode='411002')
            cfg['documents'].update(photo='new-photo.jpg',parentSignature='guardian.jpg')
        self.site.change=lambda:self.edit(update)
        proc,thread=self.start();self.finish(proc,thread)
        first,last=self.site.requests_seen
        self.assertEqual(first['fields']['txtName'],'Test Person')
        self.assertEqual(last['fields']['txtName'],'Edited Person')
        self.assertEqual(last['fields']['txtEmailId'],'edited@example.com')
        self.assertEqual(last['fields']['txtPincode'],'411002')
        self.assertEqual(last['fields']['ddlEduationQualification'],'Post graduation')
        self.assertEqual(last['fields']['ddlVegetarianOrNon'],'Non-Vegetarian')
        self.assertNotEqual(last['files']['FupParentGuardianSignature'],last['files']['FupSignatureOfAppicant'])
        self.assertNotEqual(first['files']['FupPhototOfApplicant'],last['files']['FupPhototOfApplicant'])
        self.assertEqual(self.site.form_gets,1);self.assertEqual(self.site.prep_events,[])
    def test_changed_document_bytes_are_picked_up_even_with_preserved_mtime(self):
        photo=self.root/'docs/photo.jpg';old=photo.read_bytes();info=photo.stat()
        def change():
            photo.write_bytes(old[:4]+b'z'*(len(old)-4));os.utime(photo,ns=(info.st_atime_ns,info.st_mtime_ns))
        self.site.change=change
        proc,thread=self.start();self.finish(proc,thread)
        self.assertNotEqual(self.site.requests_seen[0]['files']['FupPhototOfApplicant'],self.site.requests_seen[1]['files']['FupPhototOfApplicant'])
        self.assertEqual(self.site.form_gets,1)
    def test_invalid_save_pauses_and_recovers_without_sending_old_config(self):
        self.site.change=lambda:self.config.write_text('{')
        proc,thread=self.start();self.assertTrue(self.paused.wait(3),''.join(self.lines))
        count=self.site.requests
        threading.Event().wait(0.25)
        self.assertEqual(self.site.requests,count)
        cfg=copy.deepcopy(self.cfg);cfg['applicant']['email']='repaired@example.com';self.write(cfg)
        self.finish(proc,thread)
        self.assertEqual(self.site.requests_seen[-1]['fields']['txtEmailId'],'repaired@example.com')
    def test_state_change_requires_explicit_refresh_before_using_new_value(self):
        self.site.change=lambda:self.edit(lambda cfg:cfg['address'].update(state='Karnataka'))
        proc,thread=self.start();proc,thread=self.refresh_after_edit(proc,thread);self.finish(proc,thread)
        self.assertEqual(self.site.requests_seen[-1]['fields']['ddlState'],'29')
        self.assertEqual(self.site.form_gets,2);self.assertEqual(self.site.prep_events,['txtDateOfBirth'])
    def test_dob_change_requires_explicit_refresh_and_logs_current_identity(self):
        self.site.change=lambda:self.edit(lambda cfg:cfg['applicant'].update(dateOfBirth='15/05/2005'))
        proc,thread=self.start();proc,thread=self.refresh_after_edit(proc,thread);self.finish(proc,thread)
        self.assertEqual(self.site.requests_seen[-1]['fields']['txtDateOfBirth'],'15/05/2005')
        self.assertEqual(self.site.requests_seen[-1]['fields']['txtCourseFee'],'10000')
        self.assertEqual(self.site.form_gets,2)
        settings,errors,_=b.parse_settings(json.loads(self.config.read_text()),self.config);self.assertFalse(errors)
        self.assertEqual(self.attempt()['applicantIdentity'],b.expected_state_identity(settings,self.url))
    def test_phone_change_keeps_used_history_and_reprepares_mobile(self):
        self.seed_used()
        self.site.change=lambda:self.edit(lambda cfg:cfg['phoneNumbers'].__setitem__(0,'9000000099'))
        proc,thread=self.start();proc,thread=self.refresh_after_edit(proc,thread);self.finish(proc,thread)
        self.assertEqual(self.site.applications[0]['mobile'],'9000000099')
        self.assertEqual(self.state()['phones']['9000000013']['status'],'used')
        self.assertEqual(self.site.form_gets,2)
    def test_identity_edit_ignores_old_used_history_without_erasing_it(self):
        self.seed_used();state_path=next((self.root/'runs').glob('*/state.json'));before=state_path.read_bytes()
        self.site.change=lambda:self.edit(lambda cfg:cfg['applicant'].update(name='Another Person'))
        proc,thread=self.start();self.finish(proc,thread)
        self.assertEqual(self.site.applications[0]['name'],'Another Person')
        self.assertEqual(state_path.read_bytes(),before)
    def test_uncertain_reply_retries_with_latest_phone_and_keeps_old_history(self):
        self.seed_used();state_path=next((self.root/'runs').glob('*/state.json'));before=state_path.read_bytes()
        self.site.uncertain=True
        self.site.change=lambda:self.edit(lambda cfg:cfg['phoneNumbers'].__setitem__(0,'9000000099'))
        proc,thread=self.start();proc,thread=self.refresh_after_edit(proc,thread);self.finish(proc,thread)
        self.assertIn('retrying_uncertain',''.join(self.lines))
        self.assertEqual(self.site.requests_seen[0]['phone'],'9000000011')
        self.assertEqual(self.site.requests_seen[-1]['phone'],'9000000099')
        self.assertEqual(state_path.read_bytes(),before)
    def test_level_change_refreshes_allocation_and_sends_new_level(self):
        self.site.change=lambda:self.edit(lambda cfg:cfg['coursePriorities'].__setitem__(0,'G4 Intermediate'))
        proc,thread=self.start();proc,thread=self.refresh_after_edit(proc,thread);self.finish(proc,thread)
        self.assertEqual(self.site.requests_seen[-1]['fields']['ddlCourseLevel'],'5')
        self.assertEqual(self.site.requests_seen[-1]['fields']['hdfCapacitySheetAllocationId'],'synthetic-4-5-83')
        self.assertEqual(self.site.form_gets,2)
    def test_date_override_selects_matching_server_batch(self):
        self.site.change=lambda:self.edit(lambda cfg:cfg['coursePriorities'].__setitem__(0,{'batch':'G4','course':'Snow Skiing','level':'Basic','from':'2027-01-17','to':'2027-01-30'}))
        proc,thread=self.start();proc,thread=self.refresh_after_edit(proc,thread);self.finish(proc,thread)
        self.assertEqual(self.site.requests_seen[-1]['fields']['ddlScheduleCourse'],'82')
    def test_edit_during_preparation_discards_old_server_dependencies(self):
        self.site.cold=True
        self.site.prep_change=lambda:self.edit(lambda cfg:cfg['address'].update(state='Karnataka'))
        proc,thread=self.start();self.finish(proc,thread)
        self.assertEqual(self.site.requests_seen[-1]['fields']['ddlState'],'29')
        self.assertEqual(self.site.form_gets,2)

    def test_optional_document_removal_clears_upload_and_flag(self):
        self.cfg['documents']['domicileProof']='dob.jpg';self.write(self.cfg)
        self.site.change=lambda:self.edit(lambda cfg:cfg['documents'].update(domicileProof=''))
        proc,thread=self.start();self.finish(proc,thread)
        first,last=self.site.requests_seen
        self.assertIn('FupDomicileAttachProof',first['files'])
        self.assertNotIn('FupDomicileAttachProof',last['files'])
        self.assertEqual(last['fields']['hdfFupDomicileAttachProof'],'No')
        self.assertEqual(self.site.form_gets,1)
    def test_clearing_required_guardian_path_pauses_without_reusing_signature(self):
        self.site.change=lambda:self.edit(lambda cfg:cfg['documents'].update(parentSignature=''))
        proc,thread=self.start();self.assertTrue(self.paused.wait(3),''.join(self.lines))
        self.assertEqual(len(self.site.requests_seen),1)
        self.write(self.cfg);self.finish(proc,thread)
    def test_explicit_submit_timeout_edit_applies_to_the_next_request(self):
        self.site.change=lambda:self.edit(lambda cfg:cfg['bot'].update(submitTimeoutSec=.05))
        self.site.final_delay=.3
        proc,thread=self.start();proc.wait(timeout=4);thread.join(timeout=2)
        self.assertEqual(proc.returncode,1,''.join(self.lines))
        self.assertIn('uncertain',''.join(self.lines))
        self.assertIn('within 0.05s',''.join(self.lines))
        self.assertEqual(self.attempt()['status'],'uncertain')
        self.assertEqual(self.site.form_gets,1)
    def test_course_kind_change_selects_its_own_live_level(self):
        self.site.change=lambda:self.edit(lambda cfg:cfg['coursePriorities'].__setitem__(0,'G4 Snowboarding'))
        proc,thread=self.start();proc,thread=self.refresh_after_edit(proc,thread);self.finish(proc,thread)
        self.assertEqual(self.site.requests_seen[-1]['fields']['ddlCourseLevel'],'28')
        self.assertEqual(self.site.form_gets,2)
    def prepare_once(self):
        code="import importlib.util,sys;from pathlib import Path;s=importlib.util.spec_from_file_location('direct',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);m.ROOT=Path(sys.argv[2]);m.HERE=m.ROOT/'capsules';raise SystemExit(m.run('G4',m.ROOT/'configs/me.jsonc',sys.argv[3],prepare_only=True))"
        result=subprocess.run([sys.executable,'-c',code,str(ENGINE),str(self.root),self.url],capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
    def test_restart_uses_saved_values_without_any_startup_preparation(self):
        self.site.page_seats=0;self.prepare_once()
        meta=json.loads((self.root/'capsules/G4-ready.json').read_text())
        old=b.Page(meta['url'],200,(self.root/'capsules/G4-ready.html').read_text())
        self.assertEqual(old.value('txtAvailableSeat'),'0')
        self.site.page_seats=3
        proc,thread=self.start();self.finish(proc,thread)
        self.assertEqual(self.site.requests_seen[0]['fields']['txtAvailableSeat'],'0')
        self.assertEqual(self.site.form_gets,1);self.assertEqual(self.site.prep_events,[])
        self.assertIn('saved_form_loaded',''.join(self.lines))
    def test_empty_dropdown_rejection_reuses_complete_saved_form(self):
        self.site.empty_rejection=True
        proc,thread=self.start();self.finish(proc,thread)
        self.assertEqual(self.site.form_gets,1);self.assertEqual(self.site.prep_events,[])
        first,last=self.site.requests_seen
        self.assertEqual(last['fields']['ddlCourseLevel'],'4')
        self.assertEqual(last['fields']['ddlScheduleCourse'],'83')
        self.assertEqual(first['fields']['__VIEWSTATE'],last['fields']['__VIEWSTATE'])
        self.assertIn('retrying_rejection',''.join(self.lines))
    def test_invalid_viewstate_retries_without_preparation_or_deleting_cache(self):
        self.prepare_once();self.site.invalid_final=True
        saved=(self.root/'capsules/G4-ready.json').read_bytes()
        proc,thread=self.start();self.finish(proc,thread)
        self.assertIn('retrying_invalid_saved_state',''.join(self.lines))
        self.assertEqual(self.site.form_gets,1);self.assertEqual(self.site.prep_events,[])
        self.assertEqual((self.root/'capsules/G4-ready.json').read_bytes(),saved)
    def test_missing_cache_does_not_make_any_preparation_request(self):
        proc,thread=self.start(seed=False);proc.wait(timeout=3);thread.join(timeout=2)
        self.assertNotEqual(proc.returncode,0)
        self.assertIn('--prepare-only',''.join(self.lines))
        self.assertEqual(self.site.requests,0)
    def test_plan_rejects_a_cache_for_different_server_config_without_http(self):
        self.prepare_once();count=self.site.requests
        self.edit(lambda cfg:cfg['address'].update(state='Karnataka'))
        code="import importlib.util,sys;from pathlib import Path;s=importlib.util.spec_from_file_location('direct',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);m.ROOT=Path(sys.argv[2]);m.HERE=m.ROOT/'capsules';raise SystemExit(m.run('G4',m.ROOT/'configs/me.jsonc',sys.argv[3],plan=True))"
        result=subprocess.run([sys.executable,'-c',code,str(ENGINE),str(self.root),self.url],capture_output=True,text=True,timeout=5)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('current config',result.stderr)
        self.assertEqual(self.site.requests,count)

    def test_plan_does_not_rebind_even_an_empty_ledger(self):
        self.prepare_once();self.seed_used(with_history=False)
        state_path=next((self.root/'runs').glob('*/state.json'));before=state_path.read_bytes();count=self.site.requests
        self.edit(lambda cfg:cfg['applicant'].update(name='Preview Person'))
        code="import importlib.util,sys;from pathlib import Path;s=importlib.util.spec_from_file_location('direct',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);m.ROOT=Path(sys.argv[2]);m.HERE=m.ROOT/'capsules';raise SystemExit(m.run('G4',m.ROOT/'configs/me.jsonc',sys.argv[3],plan=True))"
        result=subprocess.run([sys.executable,'-c',code,str(ENGINE),str(self.root),self.url],capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(state_path.read_bytes(),before);self.assertEqual(self.site.requests,count)
    def test_preparation_does_not_rebind_an_empty_ledger(self):
        self.seed_used(with_history=False)
        state_path=next((self.root/'runs').glob('*/state.json'));before=state_path.read_bytes()
        self.edit(lambda cfg:cfg['applicant'].update(name='Prepared Person'))
        self.prepare_once()
        self.assertEqual(state_path.read_bytes(),before);self.assertFalse(self.site.applications)

if __name__=='__main__':unittest.main(verbosity=2)
