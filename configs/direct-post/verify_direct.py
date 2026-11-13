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

if __name__=='__main__':
    import verify_always_run
    verify_always_run.scenario(history='pending')
    verify_always_run.scenario(history='booked')
    verify_always_run.scenario(history='booked',allow_duplicates=True)
    verify_always_run.scenario(crash=1)
    verify_always_run.scenario(receipt=True)
    verify_always_run.scenario(once=True)
    verify_always_run.scenario(history='pending',limit=8,busy=7)
