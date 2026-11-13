"""Always-run contract: historical status never gates direct requests."""
import copy,hashlib,importlib.util,json,os,subprocess,sys,tempfile,threading
from pathlib import Path
from http.server import ThreadingHTTPServer
sys.path.insert(0,str(Path(__file__).resolve().parent))
import verify_direct as fixture
b=fixture.support.bot
ENGINE=Path(__file__).with_name('direct_loop.py')

class Handler(fixture.ReadyHandler):
    def post_form(self,sess,query,values,files):
        if values.get('btnSubmit'):
            status=json.loads((self.site.root/'capsules/G4-attempt-state.json').read_text())
            assert status['status']=='submitting' and status['phone']==sess['mobile']
            self.site.saw_attempt_status=True
            if self.site.allow_duplicates:self.site.used.discard(sess['mobile'])
        return super().post_form(sess,query,values,files)

def scenario(history=None,limit=3,crash=0,receipt=False,allow_duplicates=False,once=False,busy=0):
    with tempfile.TemporaryDirectory(prefix='always-run-test-',dir=fixture.repo/'configs') as directory:
        root=Path(directory);(root/'configs').mkdir();(root/'capsules').mkdir()
        cfg=fixture.support.make_config(root,['9000000011','9000000012','9000000013'],['G4 Basic','G1 Basic'])
        cfg['documents']['parentSignature']='sign.jpg'
        config=root/'configs/me.jsonc';config.write_text(json.dumps(cfg));config.chmod(0o600)
        site=fixture.make_site(busy_count=busy,fail_receipt=receipt,crash=crash)
        site.root=root;site.saw_attempt_status=False;site.allow_duplicates=allow_duplicates
        site.cold=False;site.prep_busy=0;site.selector_busy=0;site.full_once=False;site.fail_before_commit=False
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.site=site;server.daemon_threads=True;server.handle_error=lambda *a:None
        threading.Thread(target=server.serve_forever,daemon=True).start();url=f'http://127.0.0.1:{server.server_port}'
        settings,errors,_=b.parse_settings(cfg,config);assert not errors
        identity=json.dumps([str(config.resolve()),b.origin(url),settings.season])
        out=root/'runs'/('me-'+hashlib.sha256(identity.encode()).hexdigest()[:16])
        native=b.Bot(settings,out,base_url=url,log=b.Log(echo=False));native.state.acquire()
        try:
            native._restore()
            if history=='pending':native.mark_submitting(settings.phones[0],settings.targets[0])
            elif history=='booked':native.record_booking(settings.phones[0],settings.targets[0],{'applicationId':'old-booking','message':'Previously confirmed','url':url+'/previous'})
            else:native.state.save()
        finally:native.state.release()
        before=native.state.path.read_bytes()
        code="import importlib.util,sys;from pathlib import Path;s=importlib.util.spec_from_file_location('direct',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);m.ROOT=Path(sys.argv[2]);m.HERE=m.ROOT/'capsules';m.run('G4',m.ROOT/'configs/me.jsonc',sys.argv[3],prepare_only=True);raise SystemExit(m.run('G4',m.ROOT/'configs/me.jsonc',sys.argv[3],limit=int(sys.argv[4])))"
        try:
            result=subprocess.run([sys.executable,'-c',code,str(ENGINE),str(root),url,str(1 if once else limit)],capture_output=True,text=True,timeout=15)
            assert site.saw_attempt_status,result.stdout+result.stderr
            assert native.state.path.read_bytes()==before,'Direct retries modified historical native progress'
            expected=1 if once else limit
            assert site.post_counts.get('9000000011')==expected,(site.post_counts,result.stdout,result.stderr)
            assert sum(r['method']=='GET' and r['page']=='admission_form.aspx' for r in site.request_log)==1,'Default run repeated server preparation'
            assert 'uncertain saved submission' not in result.stdout+result.stderr
            if allow_duplicates:assert len(site.applications)==limit and result.stdout.count('status: "confirmed"')==limit
            if crash or receipt:assert 'retrying_uncertain' in result.stdout and len(site.applications)==1
            if once:assert len(list((root/'capsules/responses/once/G4').glob('*.json')))==1
            else:assert len(list((root/'capsules/responses/loop/G4').glob('*.json')))==min(5,expected)
            print(json.dumps({'historicalStatus':history,'finalPosts':expected,'applications':len(site.applications),'continuedAfterConfirmation':allow_duplicates,'continuedAfterUnknownReply':bool(crash or receipt),'manualOnce':once,'nativeHistoryUnchanged':True,'result':'pass'}))
        finally:server.shutdown();server.server_close()

if __name__=='__main__':
    scenario(history='pending')
    scenario(history='booked')
    scenario(history='booked',allow_duplicates=True)
    scenario(crash=1)
    scenario(receipt=True)
    scenario(once=True)
    scenario(history='pending',limit=8,busy=7)
