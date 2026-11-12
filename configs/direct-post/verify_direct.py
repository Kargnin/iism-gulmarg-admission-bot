import copy,hashlib,importlib.util,json,os,subprocess,sys,tempfile,threading
from pathlib import Path
from http.server import ThreadingHTTPServer
repo=Path(__file__).resolve().parents[2];sys.path.insert(0,str(repo/'tests'))
import test_bot as support
import mock_site
ENGINE=Path(__file__).with_name('direct_loop.py')
BUSY='<html><title>IIS 10.0 Detailed Error - 503.2 - Service Unavailable</title><body>Module IIS Web Core Notification BeginRequest Handler Not yet determined</body></html>'

class ReadyHandler(mock_site.Handler):
    def get_form(self,sess,query):
        if self.site.prep_busy:
            self.site.prep_busy-=1;return self.send(503,BUSY)
        phone=query['mobile'];sess.update(mobile=phone,accepted=True)
        if self.site.cold:return self.send(200,self.form(phone,{},['4']))
        batch='83' if phone=='9000000011' else '80'
        values={'ddlCourse':'4','ddlCourseLevel':'4','ddlScheduleCourse':batch,'ddlState':'27','txtCourseFee':'20000','hdfMinimumAge':'10','hdfMaximumAge':'40','hdfCapacitySheetAllocationId':'synthetic-4-4-'+batch,'hdfTypeOfCapacitySheet':'synthetic-shared','txtAvailableSeat':'3','txtDateOfBirth':'15/05/1998'}
        return self.send(200,self.form(phone,values,['4']))
    def post_form(self,sess,query,values,files):
        if not values.get('btnSubmit'):
            if self.site.selector_busy:
                self.site.selector_busy-=1;return self.send(503,BUSY)
            return super().post_form(sess,query,values,files)
        with self.site.lock:
            self.site.post_events.append(values.get('__EVENTTARGET'))
            phone=sess['mobile'];self.site.post_counts[phone]=self.site.post_counts.get(phone,0)+1
            busy=self.site.post_counts[phone]<=self.site.busy_count
        if busy:return self.send(503,BUSY)
        if self.site.fail_before_commit:
            self.site.fail_before_commit=False;return self.send(503,mock_site.IIS_503)
        if self.site.full_once:
            self.site.full_once=False
            values['lblMsg']=('Seat not available in the selected schedule.','Red')
            return self.send(200,self.form(sess['mobile'],values,['4']))
        return super().post_form(sess,query,values,files)
    def _route(self,method,page,query,values,files,url):
        if page=='admissionformsuccess.aspx' and self.site.fail_receipt:
            return self.send(503,BUSY)
        return super()._route(method,page,query,values,files,url)

def make_site(busy_count=0,fail_receipt=False,crash=0):
    site=mock_site.Site(crash_after_commit=crash)
    ski=copy.deepcopy(site.courses['10']);ski['levels']=[('4','Basic SSC')]
    ski['batches']=[('80' if v=='71' else '83',t,d,l) for v,t,d,l in ski['batches'] if v in ('71','74')]
    site.courses={'4':ski};site.seats={('4','4',x[0]):3 for x in ski['batches']}
    site.busy_count=busy_count;site.fail_receipt=fail_receipt;site.post_counts={};site.post_events=[]
    return site

def scenario(busy=0,receipt=False,crash=0,parallel=False,cold=False,full_once=False,manual_once=False,manual_reconcile=False):
    with tempfile.TemporaryDirectory(prefix='verify-direct-',dir=repo/'configs') as directory:
        root=Path(directory);(root/'configs').mkdir();(root/'capsules').mkdir()
        cfg=support.make_config(root,['9000000011','9000000012','9000000013','9000000014'],['G4 Basic','G1 Basic'])
        cfg['documents']['parentSignature']='sign.jpg'
        config=root/'configs/me.jsonc';config.write_text(json.dumps(cfg));config.chmod(0o600)
        site=make_site(busy,receipt,crash);site.cold=cold;site.full_once=full_once;site.fail_before_commit=manual_reconcile;site.prep_busy=2 if cold else 0;site.selector_busy=2 if cold else 0;server=ThreadingHTTPServer(('127.0.0.1',0),ReadyHandler);server.site=site;server.daemon_threads=True;server.handle_error=lambda *a:None
        threading.Thread(target=server.serve_forever,daemon=True).start();url=f'http://127.0.0.1:{server.server_port}'
        def command(target,prepare=True,confirm=False,once=False):
            return [sys.executable,'-c',"import importlib.util,sys;from pathlib import Path;s=importlib.util.spec_from_file_location('direct',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);m.ROOT=Path(sys.argv[2]);m.HERE=m.ROOT/'capsules';raise SystemExit(m.run(sys.argv[3],m.ROOT/'configs/me.jsonc',sys.argv[4],confirm_not_booked=sys.argv[6]=='1',limit=1 if sys.argv[7]=='1' else None))",str(ENGINE),str(root),target,url,'1' if prepare else '0','1' if confirm else '0','1' if once else '0']
        try:
            targets=['G4','G1'] if parallel else ['G4']
            processes=[subprocess.Popen(command(t,once=manual_once),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True) for t in targets]
            outputs=[]
            for process in processes:
                out,_=process.communicate(timeout=20);outputs.append(out)
            state_files=list((root/'runs').glob('*/state.json'));assert len(state_files)==1
            state=json.loads(state_files[0].read_text())
            if manual_once:
                assert processes[0].returncode==1 and not site.applications and 'once_rejected' in outputs[0],outputs
                archive=root/'capsules/responses/once/G4'
                assert len(list(archive.glob('*.html')))==1 and len(list(archive.glob('*.json')))==1
                assert not state['phones']
            elif manual_reconcile:
                assert not site.applications and state['phones']['9000000011']['status']=='submitting'
                no_ack=subprocess.run(command('G4',prepare=False),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=5)
                assert no_ack.returncode!=0 and not site.applications
                ack=subprocess.run(command('G4',prepare=False,confirm=True,once=True),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=10)
                assert ack.returncode==0 and len(site.applications)==1,ack.stdout
            elif receipt or crash:
                assert len(site.applications)==1 and 'uncertain' in outputs[0],outputs
                assert state['phones']['9000000011']['status']=='submitting'
                repeat=subprocess.run(command('G4',prepare=False),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=5)
                assert repeat.returncode!=0 and len(site.applications)==1
            else:
                assert all(p.returncode==0 for p in processes),outputs
                assert len(site.applications)==len(targets) and len(state['bookings'])==len(targets)
                assert not set(['9000000013','9000000014'])&site.used
                for application in site.applications:
                    assert application['fileHashes']['FupParentGuardianSignature']==application['fileHashes']['FupSignatureOfAppicant']
                assert site.post_events==['']*len(site.post_events) and site.level_postbacks==(len(targets) if cold else 0)
                assert all(site.post_counts[p]==busy+1+(1 if full_once else 0) for p in site.used)
                for target in targets:
                    archive=root/'capsules/responses/loop'/target
                    assert len(list(archive.glob('*.json')))==min(5,busy+1+(1 if full_once else 0))
                    assert len(list(archive.glob('*.html')))<=5
                before=site.requests
                repeat=subprocess.run(command('G4',prepare=False),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=5)
                assert repeat.returncode==0 and site.requests==before
            print(json.dumps({'busyBeforeSuccess':busy,'receipt503AfterCommit':receipt,'generic503AfterCommit':bool(crash),'parallelWorkers':parallel,'coldPreparationRetried':cold,'fullRejectionRetried':full_once,'manualOnce':manual_once,'manualReconcile':manual_reconcile,'applications':len(site.applications),'result':'pass'}))
        finally:
            for process in processes:
                if process.poll() is None:process.kill();process.wait()
            server.shutdown();server.server_close()

if __name__=='__main__':
    scenario(busy=2,parallel=True)
    scenario(receipt=True)
    scenario(crash=1)

    scenario(busy=2,parallel=True,cold=True)

    scenario(full_once=True)

    scenario(busy=7,parallel=True)
    scenario(full_once=True,manual_once=True)
    scenario(manual_reconcile=True)
