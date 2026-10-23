#!/usr/bin/env python3
"""
A stand-in for admission.iismgulmarg.in, modelled on the real pages as
recorded on 23 Oct 2026, for the tests and for rehearsing booking day without
touching the real site.

Copied from the real site:
  * URLs and redirects: "/" answers 403.14; mobile verification 302s to
    /Admission_criteria.aspx?mobile=N; chkAccept + lnkbtnNext 302s to
    /admission_form.aspx?mobile=N; criteria/form without a session 302 back
    to /Admission_instructions.aspx
  * the admission form: the same 92 fields in the same order, multipart,
    ValidatorCallout ClientState fields without a value attribute, btnSubmit
    and btnReset, labels lblMsg / lblDOBMsg / lblCheckError (lblmsg on the
    first page), the checkFileSize() script, the validator declarations and
    messages, and the startup <script> block that mixes function code with
    $create() calls
  * the cascade: ddlCourse loads levels and batches ("Course from 13 Sep to
    26 Sep 2026" style); ddlScheduleCourse loads seats, age limits and the
    age-count date (M/d/yyyy 12:00:00 AM); ddlState with no date of birth
    says "Please Enter Date Of Birth."; txtDateOfBirth says "Your Age N
    Years." and fills the fee, or "Your Age N Years , Age Allow X To Y
    Years." and clears the date
  * server-side validation (group "sv", including the site's own quirks),
    event validation of dropdown values, viewstate checks, the state list

Not observable without filing a real application, so these are best guesses
(switchable, and the tests run the bot against each variant): what success
looks like, and how a used number or a full batch is reported.
"""

import argparse
import html
import json
import random
import re
import threading
import time
import urllib.parse
import uuid
from datetime import date
from email import policy
from email.parser import BytesParser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

STATES = [("0", "Select State"), ("35", "ANDAMAN AND NICOBAR ISLANDS"), ("2", "Andhra Pradesh"),
          ("37", "ANDHRA PRADESH (NEW)"), ("28", "ANDHRA PRADESH(BEFORE DIVISION)"), ("12", "ARUNACHAL PRADESH"),
          ("18", "ASSAM"), ("10", "BIHAR"), ("4", "CHANDIGARH"), ("22", "CHATTISGARH"),
          ("26", "DADRA AND NAGAR HAVELI"), ("25", "DAMAN AND DIU"), ("7", "DELHI"), ("30", "GOA"),
          ("24", "GUJARAT"), ("6", "HARYANA"), ("38", "HIMACHAL PRADESH"), ("1", "JAMMU AND KASHMIR"),
          ("20", "JHARKHAND"), ("29", "KARNATAKA"), ("32", "KERALA"), ("39", "LADAKH "), ("31", "LAKSHWADEEP"),
          ("23", "MADHYA PRADESH"), ("27", "MAHARASHTRA"), ("14", "MANIPUR"), ("17", "MEGHLAYA"),
          ("15", "MIZORAM"), ("13", "NAGALAND"), ("21", "ODISHA"), ("34", "PUDUCHERRY"), ("3", "PUNJAB"),
          ("8", "RAJASTHAN"), ("11", "SIKKIM"), ("33", "TAMIL NADU"), ("36", "TELANGANA"), ("16", "TRIPURA"),
          ("9", "UTTAR \xa0PRADESH"), ("5", "UTTARAKHAND"), ("19", "WEST BENGAL")]  # (sic) both quirks are real
LADAKH = "39"
EDUCATION = [("0", "Select"), ("High School", "High School"), ("Intermediate", "Intermediate"),
             ("Graduation", "Graduation"), ("Post graduation", "Post graduation")]
DIET = [("0", "Select"), ("Vegetarian", "Vegetarian"), ("Non-Vegetarian", "Non-Vegetarian")]

G = [("71", date(2026, 12, 20), date(2027, 1, 2)), ("72", date(2027, 1, 3), date(2027, 1, 16)),
     ("73", date(2027, 1, 17), date(2027, 1, 30)), ("74", date(2027, 1, 31), date(2027, 2, 13)),
     ("75", date(2027, 2, 14), date(2027, 2, 27)), ("76", date(2027, 2, 28), date(2027, 3, 13))]
K = [("77", date(2027, 2, 8), date(2027, 2, 21)), ("78", date(2027, 2, 22), date(2027, 3, 7))]


def batch_text(start, end, suffix=""):
    return f"Course from {start:%d %b} to {end:%d %b %Y}{suffix}"


def batches(rows, location, suffix=""):
    return [(v, batch_text(s, e, suffix), s, location) for v, s, e in rows]


ADVENTURE = {"name": "Adventure Courses", "kind": "adventure", "levels": [("16", "None")],
             "batches": [("70", "Course from 13 Sep to 26 Sep 2026", date(2026, 9, 13), "Gulmarg")],
             "age": (12, 45), "criteria": 12, "always": True,
             "count_date": date(2026, 10, 13)}  # as recorded; for the snow courses the batch start is a guess
SKI_LEVELS = [("13", "Basic"), ("14", "Intermediate"), ("15", "Advance")]

# The real form lists only "Adventure Courses" today; how the snow courses will be
# laid out on booking day is unknown, so the tests try the bot on both shapes.
LAYOUTS = {
    # snowboarding and MOI as levels of the skiing course, Kargil as its own course
    "levels": [
        ("9", ADVENTURE),
        ("10", {"name": "Snow Skiing", "kind": "ski", "levels": SKI_LEVELS + [("17", "Snow Boarding"), ("18", "MOI")],
                "batches": batches(G, "Gulmarg"), "age": (10, 40), "criteria": 10}),
        ("11", {"name": "Snow Skiing Kargil", "kind": "ski", "levels": SKI_LEVELS,
                "batches": batches(K, "Kargil"), "age": (10, 40), "criteria": 10}),
    ],
    # every course on its own, Kargil batches inside the skiing course
    "courses": [
        ("9", ADVENTURE),
        ("12", {"name": "SNOW SKIING COURSE", "kind": "ski",
                "levels": [("13", "BASIC"), ("14", "INTERMIDIATE"), ("15", "ADVANCE")],
                "batches": batches(G, "Gulmarg") + batches(K, "Kargil", " (KARGIL)"), "age": (10, 40), "criteria": 10}),
        ("19", {"name": "SNOW BOARDING COURSE", "kind": "board", "levels": [("16", "None")],
                "batches": batches(G[3:4], "Gulmarg"), "age": (10, 40), "criteria": 10}),
        ("20", {"name": "M.O.I COURSE", "kind": "moi", "levels": [("16", "None")],
                "batches": batches(G[4:5], "Gulmarg"), "age": (18, 40), "criteria": 18}),
    ],
    # one course per level, every batch in each, levels "None"
    "level-courses": [
        ("9", ADVENTURE),
        ("21", {"name": "Snow Skiing Basic Course", "kind": "ski", "levels": [("16", "None")],
                "batches": batches(G, "Gulmarg") + batches(K, "Kargil", " (Kargil)"), "age": (10, 40), "criteria": 10}),
        ("22", {"name": "Snow Skiing Intermediate Course", "kind": "ski", "levels": [("16", "None")],
                "batches": batches(G, "Gulmarg") + batches(K, "Kargil", " (Kargil)"), "age": (10, 40), "criteria": 10}),
        ("23", {"name": "Snow Skiing Advance Course", "kind": "ski", "levels": [("16", "None")],
                "batches": batches(G, "Gulmarg") + batches(K, "Kargil", " (Kargil)"), "age": (10, 40), "criteria": 10}),
        ("24", {"name": "Snow Boarding Course", "kind": "board", "levels": [("16", "None")],
                "batches": batches(G[3:4], "Gulmarg"), "age": (10, 40), "criteria": 10}),
        ("25", {"name": "MOI Course", "kind": "moi", "levels": [("16", "None")],
                "batches": batches(G[4:5], "Gulmarg"), "age": (18, 40), "criteria": 18}),
    ],
}

DOC_RX = r"(.*\.([Pp][Dd][Ff])|.*\.([Gg][Ii][Ff])|.*\.([Jj][Pp][Gg])|.*\.([pP][nN][gG])|.*\.([Jj][Pp][Ee][Gg])$)"
DOB_PROOF_RX = r"(.*\.([Pp][Df][Ff])|.*\.([Gg][Ii][Ff])|.*\.([Jj][Pp][Gg])|.*\.([pP][nN][gG])|.*\.([Jj][Pp][Ee][Gg])$)"
IMG_RX = r"(.*\.([Gg][Ii][Ff])|.*\.([Jj][Pp][Gg])|.*\.([pP][nN][gG])|.*\.([Jj][Pp][Ee][Gg])$)"
DOB_RX = r"(((0|1)[0-9]|2[0-9]|3[0-1])\/(0[1-9]|1[0-2])\/((19|20)\d\d))$"
EMAIL_RX = r"\w+([-+.']\w+)*@\w+([-.]\w+)*\.\w+([-.]\w+)*"
DOC_MSG = "Only .pdf, .gif, .jpg, .png, .jpeg, files & Size 1MB are allowed."
IMG_MSG = "Only .gif, .jpg, .png, .jpeg, files & Size 1MB are allowed."

# The form's validators as declared on the real page: id, control, kind, group, initial value / regex, message.
VALIDATORS = [
    ("rfvddlCourse", "ddlCourse", "req", "sv", "0", "Please Select Course."),
    ("rfvddlCourseLevel", "ddlCourseLevel", "req", "sv", "0", "Please Select Course Level."),
    ("rfvddlScheduleCourse", "ddlScheduleCourse", "req", "sv", "0", "Please Select Schedule Course."),
    ("rfvddlState", "ddlState", "req", "sv", "0", "Please Select State."),
    ("rfvtxtCourseFee", "txtCourseFee", "req", "sv", "", "Please Enter Course Fee."),
    ("rfvtxtName", "txtName", "req", "sv", "", "Please Enter Name."),
    ("rfvtxtFatherName", "txtFatherName", "req", "sv", "", "Please Enter Father's Name."),
    ("rfvtxtNationality", "txtNationality", "req", "sv", "", "Please Enter Nationality."),
    ("rfvtxtDateOfBirth", "txtDateOfBirth", "req", "sv", "", "Please Enter DateOfBirth."),
    ("regvtxtDateOfBirth", "txtDateOfBirth", "rx", "sv", DOB_RX, "Invalid date only this format dd/MM/yyyy are allow."),
    ("rfvFupBirthCertificate", "FupBirthCertificate", "req", "sv", "", "Please Upload Date of birth."),
    ("regvFupBirthCertificate", "FupBirthCertificate", "rx", "prosv", DOB_PROOF_RX, DOC_MSG),
    ("regvFupDomicileAttachProof", "FupDomicileAttachProof", "rx", "sv", DOC_RX, DOC_MSG),
    ("rfvtxtHouseNoApartment", "txtHouseNoApartment", "req", "sv", "", "Please Enter House No. / Apartment."),
    ("rfvtxtStreetVillageColony", "txtStreetVillageColony", "req", "sv", "", "Please Enter Street/Village/Colony."),
    ("rfvtxtDistrictCity", "txtDistrictCity", "req", "sv", "", "Please Enter District/City."),
    ("rfvtxtPincode", "txtPincode", "req", "sv", "", "Please Enter Pincode."),
    ("rfvtxtMobileNo", "txtMobileNo", "req", "sv", "", "Please Enter Mobile No."),
    ("rfvtxtEmailId", "txtEmailId", "req", "sv", "", "Please Enter EmailId."),
    ("revemailid", "txtEmailId", "rx", "sv", EMAIL_RX, "Please Enter Valid Email ID"),
    ("rfvddlEduationQualification", "ddlEduationQualification", "req", "sv", "0", "Please Select Eduation Qualification."),
    ("rfvFupEducationQualification", "FupEducationQualification", "req", "sv", "", "Please Upload Bank Passbook Photo."),
    ("regvFupEducationQualification", "FupEducationQualification", "rx", "sv", DOC_RX, DOC_MSG),
    ("regvFupAnyAdventureCoursePhoto", "FupAnyAdventureCoursePhoto", "rx", "sv", DOC_RX, DOC_MSG),
    ("regvFupCurrentEmployment", "FupCurrentEmployment", "rx", "sv", DOC_RX, DOC_MSG),
    # (sic) the real page points the diet validator at the education dropdown
    ("RequiredFieldValidator1", "ddlEduationQualification", "req", "sv", "0", "Please Select Vegetarian / Non-Vegetarian ."),
    ("rfvtxtBloodGroup", "txtBloodGroup", "req", "sv", "", "Please Enter Blood Group."),
    ("rfvFupPhototOfApplicant", "FupPhototOfApplicant", "req", "sv", "", "Please Upload Photo of Applicant."),
    ("regvFupPhototOfApplicant", "FupPhototOfApplicant", "rx", "sv", IMG_RX, IMG_MSG),
    ("rfvFupSignatureOfAppicant", "FupSignatureOfAppicant", "req", "sv", "", "Please Upload Signature of Applicant."),
    ("regvFupSignatureOfAppicant", "FupSignatureOfAppicant", "rx", "sv", IMG_RX, IMG_MSG),
    ("rfvtxtEmergencyContactName", "txtEmergencyContactName", "req", "sv", "", "Please Enter Emergency Contact Name."),
    ("rfvtxtEmergencyContactPhone", "txtEmergencyContactPhone", "req", "sv", "", "Please Enter Emergency Contact Phone."),
    ("rfvFupParentGuardianSignature", "FupParentGuardianSignature", "req", "sv", "", "Please Upload Parent/Guardian's Signature."),
    ("regvFupParentGuardianSignature", "FupParentGuardianSignature", "rx", "sv", IMG_RX, IMG_MSG),
]
VALIDATOR_BY_ID = {v[0]: v for v in VALIDATORS}
FILES = {  # file input -> hidden "attached" flag
    "FupBirthCertificate": "hdfFupBirthCertificate", "FupDomicileAttachProof": "hdfFupDomicileAttachProof",
    "FupEducationQualification": "hdfEducationQualification",
    "FupAnyAdventureCoursePhoto": "hdfAnyAdventureCoursePhoto", "FupCurrentEmployment": "hdfCurrentEmployment",
    "FupPhototOfApplicant": "hdfPhototOfApplicant", "FupSignatureOfAppicant": "hdfFupSignatureOfAppicant",
    "FupParentGuardianSignature": "hdfParentGuardianSignature",
}
TEXT_MAXLEN = {"txtName": 50, "txtFatherName": 50, "txtNationality": 50, "txtDomicile": 50,
               "txtHouseNoApartment": 50, "txtStreetVillageColony": 250, "txtDistrictCity": 50, "txtPincode": 6,
               "txtMobileNo": 10, "txtEmailId": None, "txtBloodGroup": 50, "txtMedicalCategory": 50,
               "txtHeight": 50, "txtWeight": 50, "txtWaist": 50, "txtShoeSize": 50,
               "txtEmergencyContactName": 50, "txtEmergencyContactPhone": 10}

# The admission form, in the real page's order. ("v", id) is a validator: an empty
# span plus its ValidatorCallout ClientState field.
FORM_LAYOUT = [
    ("hidden", "hdfAdmissionFormId"),
    ("select", "ddlCourse"), ("v", "rfvddlCourse"),
    ("select", "ddlCourseLevel"), ("v", "rfvddlCourseLevel"),
    ("select", "ddlScheduleCourse"), ("v", "rfvddlScheduleCourse"),
    ("select", "ddlState"), ("v", "rfvddlState"),
    ("readonly", "txtAvailableSeat"),
    ("hidden", "hdfMinimumAge"), ("hidden", "hdfMaximumAge"), ("hidden", "hdfAgeCountDate"),
    ("readonly", "txtCourseFee"), ("v", "rfvtxtCourseFee"),
    ("hidden", "hdfAgeCriteria"),
    ("text", "txtName"), ("v", "rfvtxtName"),
    ("text", "txtFatherName"), ("v", "rfvtxtFatherName"),
    ("text", "txtNationality"), ("v", "rfvtxtNationality"),
    ("dob", "txtDateOfBirth"), ("v", "rfvtxtDateOfBirth"), ("v", "regvtxtDateOfBirth"), ("label", "lblDOBMsg"),
    ("file", "FupBirthCertificate"), ("v", "rfvFupBirthCertificate"), ("v", "regvFupBirthCertificate"),
    ("text", "txtDomicile"),
    ("file", "FupDomicileAttachProof"), ("v", "regvFupDomicileAttachProof"),
    ("text", "txtHouseNoApartment"), ("v", "rfvtxtHouseNoApartment"),
    ("text", "txtStreetVillageColony"), ("v", "rfvtxtStreetVillageColony"),
    ("text", "txtDistrictCity"), ("v", "rfvtxtDistrictCity"),
    ("text", "txtPincode"), ("v", "rfvtxtPincode"),
    ("text", "txtMobileNo"), ("v", "rfvtxtMobileNo"),
    ("text", "txtEmailId"), ("v", "rfvtxtEmailId"), ("v", "revemailid"),
    ("select", "ddlEduationQualification"), ("v", "rfvddlEduationQualification"),
    ("file", "FupEducationQualification"), ("v", "rfvFupEducationQualification"), ("v", "regvFupEducationQualification"),
    ("file", "FupAnyAdventureCoursePhoto"), ("v", "regvFupAnyAdventureCoursePhoto"),
    ("file", "FupCurrentEmployment"), ("v", "regvFupCurrentEmployment"),
    ("select", "ddlVegetarianOrNon"), ("v", "RequiredFieldValidator1"),
    ("text", "txtBloodGroup"), ("v", "rfvtxtBloodGroup"),
    ("text", "txtMedicalCategory"), ("text", "txtHeight"), ("text", "txtWeight"), ("text", "txtWaist"),
    ("text", "txtShoeSize"),
    ("file", "FupPhototOfApplicant"), ("v", "rfvFupPhototOfApplicant"), ("v", "regvFupPhototOfApplicant"),
    ("file", "FupSignatureOfAppicant"), ("v", "rfvFupSignatureOfAppicant"), ("v", "regvFupSignatureOfAppicant"),
    ("text", "txtEmergencyContactName"), ("v", "rfvtxtEmergencyContactName"),
    ("text", "txtEmergencyContactPhone"), ("v", "rfvtxtEmergencyContactPhone"),
    ("file", "FupParentGuardianSignature"), ("v", "rfvFupParentGuardianSignature"),
    ("v", "regvFupParentGuardianSignature"),
    ("checkbox", "chkIAgree"), ("label", "lblCheckError"),
    ("submit", "btnSubmit"), ("submit", "btnReset"),
]

# Stand-ins for WebResource.axd / ScriptResource.axd so the pages also work in a browser.
WEBFORMS_JS = """
function WebForm_PostBackOptions(t, a, v, g, u, c, s) { this.eventTarget = t; this.eventArgument = a;
  this.validation = v; this.validationGroup = g; this.actionUrl = u; this.trackFocus = c; this.clientSubmit = s; }
function WebForm_DoPostBackWithOptions(o) { if (o.clientSubmit) { __doPostBack(o.eventTarget, o.eventArgument); } }
function WebForm_AutoFocus(id) { var e = document.getElementById(id); if (e) { e.focus(); } }
function WebForm_TextBoxKeyHandler(e) { return true; }
var Sys = { Application: { add_init: function (f) {} }, WebForms: { PageRequestManager: { _initialize: function () {} } },
            Extended: { UI: {} } };
function $create() {}
function $get(id) { return document.getElementById(id); }
"""

INSTRUCTIONS_TEXT = (  # short stand-ins for the real text; these two phrases must not fool the bot
    "<p>Please read the following instructions before applying.</p>"
    "<p>For a lost certificate, apply to the Principal for a duplicate Certificate.</p>"
    "<p>Your mobile number needs to be verified only once.</p>"
    "<p>On successful submission of online application form the fee is paid through a link sent by email.</p>")

YSOD = ("<html><head><title>Runtime Error</title></head><body><h1>Server Error in '/' Application.</h1>"
        "<h2><i>Runtime Error</i></h2><p>Timeout expired. The timeout period elapsed prior to obtaining a "
        "connection from the pool.</p></body></html>")
IIS_503 = ("<html><head><title>Service Unavailable</title></head><body><h2>Service Unavailable</h2><hr>"
           "<p>HTTP Error 503. The service is unavailable.</p></body></html>")
IIS_403 = ("<html><head><title>IIS 10.0 Detailed Error - 403.14 - Forbidden</title></head><body><h3>HTTP Error "
           "403.14 - Forbidden</h3><h4>The Web server is configured to not list the contents of this directory."
           "</h4></body></html>")


def esc(value):
    return html.escape(str(value), quote=True).replace("\xa0", "&#160;")


def age_on(dob, day):
    return day.year - dob.year - ((day.month, day.day) < (dob.month, dob.day))


class Site:
    """The fake site's data. Change attributes freely between requests in tests."""

    def __init__(self, layout="levels", seats=None, chaos=0.0, session_loss=0.0, restarts=0.0, slow_seconds=3.0,
                 opens_at=0.0, used=(), crash_after_commit=0, used_style="label", full_style="label",
                 success_style="redirect", seed=None):
        self.lock = threading.Lock()
        self.rng = random.Random(seed)
        self.courses = dict(LAYOUTS[layout])
        self.seats = {}
        for cv, course in self.courses.items():
            for lv, _ in course["levels"]:
                for bv, *_ in course["batches"]:
                    self.seats[(cv, lv, bv)] = 0 if course["kind"] == "adventure" else 3
        self.seats.update(seats or {})
        self.chaos, self.session_loss, self.restarts, self.slow_seconds = chaos, session_loss, restarts, slow_seconds
        self.opens_at = opens_at          # epoch seconds; the snow courses are not listed before this
        self.used = set(used)             # numbers that already booked
        self.crash_after_commit = crash_after_commit  # this many successful submits lose their reply
        self.used_style = used_style      # "label" (lblmsg / lblMsg) or "alert"
        self.full_style = full_style      # "label" or "alert"
        self.success_style = success_style  # "redirect", "alert" or "payment"
        self.shown_seats = {}             # seat counts the batch page shows instead of the real ones (stale)
        self.check_used_at_start = True   # False: a used number gets through verification, refused at submit
        self.fail_success_page = 0        # this many success pages fail to load (503) after the redirect
        # The form asks for a parent/guardian signature "if participant is under 18", but its required check
        # stays on for adults too (seen on the real page); whether the server enforces it for adults is unknown.
        self.parent_signature_required = False
        self.sessions = {}
        self.viewstates = {}              # token -> {"allowed": {dropdown: values}, "courses": [listed course values]}
        self.applications = []
        self.requests = 0

    def batch_key(self, label, kind=None):
        """('course', 'level', 'batch') values for e.g. ('G2', 'Basic') - for tests. A level is found
        in the level list or, when the levels are just "None", in the course name."""
        bid, level = label
        start = dict(zip([f"G{n}" for n in range(1, 7)] + ["K1", "K2"], [r[1] for r in G + K]))[bid]
        for cv, course in self.courses.items():
            if kind and course["kind"] != kind:
                continue
            for bv, _, s, _ in course["batches"]:
                if s != start:
                    continue
                for lv, lt in course["levels"]:
                    if lt.lower().startswith(level.lower()[:5]):
                        return cv, lv, bv
                if course["levels"][0][1] == "None" and level.lower()[:5] in course["name"].lower():
                    return cv, course["levels"][0][0], bv
        raise KeyError(label)

    def snapshot(self):
        with self.lock:
            return {"applications": list(self.applications), "used": sorted(self.used), "requests": self.requests}


class Handler(BaseHTTPRequestHandler):
    server_version = "Microsoft-IIS/10.0"

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    # -- plumbing ------------------------------------------------------------

    @property
    def site(self):
        return self.server.site

    def send(self, status, body, headers=None):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", (headers or {}).pop("Content-Type", "text/html; charset=utf-8"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-AspNet-Version", "4.0.30319")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if self.new_sid:
            self.send_header("Set-Cookie", f"ASP.NET_SessionId={self.sid}; path=/; HttpOnly; SameSite=Lax")
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location):
        self.send(302, f'<html><head><title>Object moved</title></head><body><h2>Object moved to '
                       f'<a href="{esc(location)}">here</a>.</h2></body></html>', {"Location": location})

    def ysod(self, title):
        self.send(500, f"<html><head><title>{esc(title)}</title></head><body><h1>Server Error in '/' Application."
                       f"</h1><h2><i>{esc(title)}</i></h2></body></html>")

    def misbehave(self):
        site = self.site
        if site.session_loss and site.rng.random() < site.session_loss:
            site.sessions.pop(self.sid, None)
        if site.restarts and site.rng.random() < site.restarts:  # app pool recycle: sessions and page state gone
            with site.lock:
                site.sessions.clear()
                site.viewstates.clear()
        if site.rng.random() >= site.chaos:
            return False
        kind = site.rng.random()
        if kind < 0.3:
            self.send(503, IIS_503)
        elif kind < 0.55:
            self.send(500, YSOD)
        elif kind < 0.7:
            self.send(200, YSOD)
        elif kind < 0.85:
            self.close_connection = True  # drop the connection without an answer
        else:
            time.sleep(site.slow_seconds)
            return False
        return True

    def viewstate(self, allowed, courses=()):
        token = "/wEPDwUK" + uuid.uuid4().hex + uuid.uuid4().hex
        with self.site.lock:
            self.site.viewstates[token] = {"allowed": allowed, "courses": list(courses)}
        return token

    def check_viewstate(self, values):
        """The page state behind a postback, or None (after answering with the error page)."""
        stored = self.site.viewstates.get(values.get("__VIEWSTATE", ""))
        if stored is None:
            self.ysod("Validation of viewstate MAC failed.")
            return None
        for name, options in stored["allowed"].items():
            if name in values and values[name] not in options:
                self.ysod("Invalid postback or callback argument.")
                return None
        return stored

    def route(self, method):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        self.new_sid = "ASP.NET_SessionId" not in cookie
        self.sid = uuid.uuid4().hex[:24] if self.new_sid else cookie["ASP.NET_SessionId"].value
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        url = urllib.parse.urlparse(self.path)
        page = url.path.rsplit("/", 1)[-1].lower()
        query = dict(urllib.parse.parse_qsl(url.query))
        if page == "__state":
            return self.send(200, json.dumps(self.site.snapshot()), {"Content-Type": "application/json"})
        if page == "webresource.axd":
            return self.send(200, WEBFORMS_JS, {"Content-Type": "application/x-javascript"})
        with self.site.lock:
            self.site.requests += 1
            broken_success = page == "admissionformsuccess.aspx" and self.site.fail_success_page > 0
            if broken_success:
                self.site.fail_success_page -= 1
        if broken_success:
            return self.send(503, IIS_503)
        if self.misbehave():
            return
        sess = self.site.sessions.setdefault(self.sid, {"mobile": None, "accepted": False})
        values, files = {}, {}
        if method == "POST":
            ctype = self.headers.get("Content-Type", "")
            if ctype.startswith("multipart/form-data"):
                values, files = parse_multipart(body, ctype)
            else:
                values = {k: v[0] for k, v in urllib.parse.parse_qs(body.decode(), keep_blank_values=True).items()}
        handler = {
            ("GET", ""): lambda: self.send(403, IIS_403),
            ("GET", "admission_instructions.aspx"): lambda: self.send(200, self.instructions()),
            ("POST", "admission_instructions.aspx"): lambda: self.post_instructions(sess, values),
            ("GET", "admission_criteria.aspx"): lambda: self.get_criteria(sess, query),
            ("POST", "admission_criteria.aspx"): lambda: self.post_criteria(sess, query, values),
            ("GET", "admission_form.aspx"): lambda: self.get_form(sess, query),
            ("POST", "admission_form.aspx"): lambda: self.post_form(sess, query, values, files),
            ("GET", "admissionformsuccess.aspx"): lambda: self.send(200, self.simple_page(
                "AdmissionFormSuccess.aspx?" + url.query, query.get("Resp", ""), "Green")),
            ("GET", "admission_formpayment.aspx"): lambda: self.send(200, self.simple_page(
                "admission_formpayment.aspx?" + url.query, "Please pay the course fee to confirm your seat.", "Green",
                form_id=query.get("RId", "0"))),
        }.get((method, page))
        if handler is None:
            return self.send(404, "<html><head><title>404 - File or directory not found.</title></head></html>")
        handler()

    # -- shared page pieces --------------------------------------------------

    def head(self, action, multipart=False, token="", generator="", lastfocus=True, validation="/wEdAA", head_extra=""):
        enctype = ' enctype="multipart/form-data"' if multipart else ""
        focus = '<input type="hidden" name="__LASTFOCUS" id="__LASTFOCUS" value="" />\n' if lastfocus else ""
        hidden = ((focus if not multipart else "")  # the real form page puts __LASTFOCUS after the event fields
                  + '<input type="hidden" name="__EVENTTARGET" id="__EVENTTARGET" value="" />\n'
                  '<input type="hidden" name="__EVENTARGUMENT" id="__EVENTARGUMENT" value="" />\n'
                  + (focus if multipart else "")
                  + f'<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="{token}" />\n')
        return (
            '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" '
            '"http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">\n<html xmlns="http://www.w3.org/1999/xhtml">\n'
            f'<head><meta charset="utf-8" /><title>\n\tIISM\n</title>{head_extra}</head>\n<body>\n'
            f'<form method="post" action="{esc(action)}" onsubmit="javascript:return WebForm_OnSubmit();" '
            f'id="form1"{enctype}>\n<div class="aspNetHidden">\n{hidden}</div>\n'
            "<script type=\"text/javascript\">\n//<![CDATA[\nvar theForm = document.forms['form1'];\n"
            "if (!theForm) {\n    theForm = document.form1;\n}\nfunction __doPostBack(eventTarget, eventArgument) {\n"
            "    if (!theForm.onsubmit || (theForm.onsubmit() != false)) {\n"
            "        theForm.__EVENTTARGET.value = eventTarget;\n        theForm.__EVENTARGUMENT.value = eventArgument;\n"
            "        theForm.submit();\n    }\n}\n//]]>\n</script>\n"
            '<script src="/WebResource.axd?d=mock&amp;t=638628405619783110" type="text/javascript"></script>\n'
            "<script type=\"text/javascript\">\n//<![CDATA[\nfunction WebForm_OnSubmit() {\n"
            "if (typeof(ValidatorOnSubmit) == \"function\" && ValidatorOnSubmit() == false) return false;\n"
            "return true;\n}\n//]]>\n</script>\n"
            f'<div class="aspNetHidden">\n<input type="hidden" name="__VIEWSTATEGENERATOR" id="__VIEWSTATEGENERATOR" '
            f'value="{generator}" />\n<input type="hidden" name="__EVENTVALIDATION" id="__EVENTVALIDATION" '
            f'value="{validation}{token[8:40]}" />\n</div>\n'
            "<script type=\"text/javascript\">\n//<![CDATA[\nSys.WebForms.PageRequestManager._initialize("
            "'scriptmanager1', 'form1', [], [], [], 90, '');\n//]]>\n</script>\n"
            '<button class="navbar-toggler" type="button" data-toggle="collapse" '
            'data-target="#navbarSupportedContent" aria-label="Toggle navigation"></button>\n'
            '<h2 class="titleHead text-center">Online Admission for Skiing &amp; Mountaineering Programmes</h2>\n')

    @staticmethod
    def validator_scripts(ids, invalid=(), alert=None, tail=""):
        """Page_Validators, the expando block and the startup block, like ASP.NET renders them.
        A startup alert lands at the top of the startup block, next to function code."""
        out = ["<script type=\"text/javascript\">\n//<![CDATA[\nvar Page_Validators =  new Array("
               + ", ".join(f'document.getElementById("{i}")' for i in ids) + ");\n//]]>\n</script>\n",
               "<script type=\"text/javascript\">\n//<![CDATA[\n"]
        for vid in ids:
            _, control, kind, group, extra, msg = VALIDATOR_BY_ID[vid]
            js_msg = msg.replace("'", "\\'")
            out.append(f'var {vid} = document.all ? document.all["{vid}"] : document.getElementById("{vid}");\n'
                       f'{vid}.controltovalidate = "{control}";\n{vid}.errormessage = "{js_msg}";\n'
                       f'{vid}.display = "{"Dynamic" if len(ids) == 1 else "None"}";\n'
                       f'{vid}.validationGroup = "{group}";\n')
            if kind == "req":
                out.append(f'{vid}.evaluationfunction = "RequiredFieldValidatorEvaluateIsValid";\n'
                           f'{vid}.initialvalue = "{extra}";\n')
            else:
                rx = extra.replace("\\", "\\\\")
                out.append(f'{vid}.evaluationfunction = "RegularExpressionValidatorEvaluateIsValid";\n'
                           f'{vid}.validationexpression = "{rx}";\n')
            if vid in invalid:
                out.append(f'{vid}.isvalid = "False";\n')
        out.append("//]]>\n</script>\n\n<script type=\"text/javascript\">\n//<![CDATA[\n")
        if alert:
            out.append(f"alert('{alert}');")
        out.append("\nvar Page_ValidationActive = false;\nif (typeof(ValidatorOnLoad) == \"function\") {\n"
                   "    ValidatorOnLoad();\n}\n\nfunction ValidatorOnSubmit() {\n    if (Page_ValidationActive) {\n"
                   "        return ValidatorCommonOnSubmit();\n    }\n    else {\n        return true;\n    }\n}\n"
                   + tail)
        for vid in ids:
            out.append(f"\ndocument.getElementById('{vid}').dispose = function() {{\n"
                       f"    Array.remove(Page_Validators, document.getElementById('{vid}'));\n}}\n")
            if len(ids) > 1:
                state = "ValidatorCalloutExtender1_ClientState" if vid == "RequiredFieldValidator1" else f"vce{vid}_ClientState"
                out.append("Sys.Application.add_init(function() {\n    $create(Sys.Extended.UI.ValidatorCalloutBehavior, "
                           f'{{"ClientStateFieldID":"{state}","id":"vce{vid}","warningIconImageUrl":"images/wrning.gif"}}, '
                           f'null, null, $get("{vid}"));\n}});\n')
        out.append("//]]>\n</script>\n</form>\n</body>\n</html>\n")
        return "".join(out)

    def simple_page(self, action, message, color, form_id="0"):
        token = self.viewstate({})
        return (self.head("./" + action, token=token, generator="E7DBEB58", lastfocus=False)
                + f'<span id="lblMsg" style="color:{color};font-weight:bold;">{esc(message)}</span>\n'
                  '<span id="lblerrorMsg" style="color:Red;font-weight:bold;"></span>\n'
                  f'<input type="hidden" name="hdfAdmissionFormId" id="hdfAdmissionFormId" value="{esc(form_id)}" />\n'
                  "</form>\n</body>\n</html>\n")

    # -- mobile verification and criteria ------------------------------------

    def instructions(self, label="", alert=None):
        token = self.viewstate({})
        return (self.head("./Admission_instructions.aspx", token=token, generator="260CF385") + INSTRUCTIONS_TEXT
                + '<h6 class="text-success mb-1">Mobile Verification</h6>\n<div class="input-group mb-0">\n'
                  '<input name="txtMobileNo" type="text" maxlength="10" id="txtMobileNo" class="form-control" '
                  'aria-describedby="basic-addon2" placeholder="Enter 10 digit mobile no." />\n'
                  '<div class="input-group-append">\n<a id="lnkbtnSubmit" class="input-group-text" '
                  'href="javascript:WebForm_DoPostBackWithOptions(new WebForm_PostBackOptions(&quot;lnkbtnSubmit&quot;, '
                  '&quot;&quot;, true, &quot;sv&quot;, &quot;&quot;, false, true))">Go</a>\n</div>\n</div>\n'
                  '<span id="rfvtxtMobileNo" style="color:Red;display:none;">Please Enter Mobile No.</span>\n'
                  f'<span id="lblmsg" style="color:Red;">{esc(label)}</span>\n'
                + self.validator_scripts(["rfvtxtMobileNo"], alert=alert, tail=(
                    "WebForm_AutoFocus('txtMobileNo');Sys.Application.add_init(function() {\n"
                    '    $create(Sys.Extended.UI.FilteredTextBoxBehavior, {"FilterType":2,"id":"ftrtxtMobileNo"}, '
                    'null, null, $get("txtMobileNo"));\n});\n')))

    def post_instructions(self, sess, values):
        if not self.check_viewstate(values):
            return
        mobile = values.get("txtMobileNo", "").strip()
        if values.get("__EVENTTARGET") != "lnkbtnSubmit":
            return self.send(200, self.instructions())
        if not re.fullmatch(r"\d{10}", mobile):
            return self.send(200, self.instructions("Please Enter Valid Mobile No."))
        if mobile in self.site.used and self.site.check_used_at_start:
            msg = "This Mobile No. is already registered."
            if self.site.used_style == "alert":
                return self.send(200, self.instructions(alert=msg))
            return self.send(200, self.instructions(msg))
        sess.update(mobile=mobile, accepted=False)
        self.redirect(f"/Admission_criteria.aspx?mobile={mobile}")

    def criteria(self, mobile, alert=None):
        token = self.viewstate({})
        script = f"<script type=\"text/javascript\">\n//<![CDATA[\nalert('{alert}');//]]>\n</script>\n" if alert else ""
        return (self.head(f"./Admission_criteria.aspx?mobile={mobile}", token=token, generator="40C6F7E4",
                          lastfocus=False)
                + "<p>Admission procedure: first come, first served. Read the course brochure before applying.</p>\n"
                  '<input id="chkAccept" type="checkbox" name="chkAccept" /><label for="chkAccept">I have read the '
                  "information above and agree with the terms and conditions.</label>\n"
                  '<a id="lnkbtnNext" class="btn btn_com mt-3 mt-md-0" '
                  'href="javascript:__doPostBack(&#39;lnkbtnNext&#39;,&#39;&#39;)">Next</a>\n'
                + script + "</form>\n</body>\n</html>\n")

    def get_criteria(self, sess, query):
        if not sess["mobile"] or sess["mobile"] != query.get("mobile"):
            return self.redirect("/Admission_instructions.aspx")
        self.send(200, self.criteria(sess["mobile"]))

    def post_criteria(self, sess, query, values):
        if not sess["mobile"] or sess["mobile"] != query.get("mobile"):
            return self.redirect("/Admission_instructions.aspx")
        if not self.check_viewstate(values):
            return
        if values.get("__EVENTTARGET") != "lnkbtnNext" or values.get("chkAccept") != "on":
            return self.send(200, self.criteria(sess["mobile"], "Please accept the terms and conditions."))
        sess["accepted"] = True
        self.redirect(f"/admission_form.aspx?mobile={sess['mobile']}")

    # -- the admission form ---------------------------------------------------

    def listed_now(self):
        is_open = time.time() >= self.site.opens_at
        return [cv for cv, c in self.site.courses.items() if is_open or c.get("always")]

    def state_of(self, v, listed):
        """Course, level and batch the posted values point at (None where nothing valid is chosen)."""
        course = self.site.courses.get(v.get("ddlCourse", "0")) if v.get("ddlCourse") in listed else None
        level = batch = None
        if course:
            level = next((lv for lv in course["levels"] if lv[0] == v.get("ddlCourseLevel")), None)
            batch = next((b for b in course["batches"] if b[0] == v.get("ddlScheduleCourse")), None)
        return course, level, batch

    def fee(self, course, level, batch, age, state):
        if course["kind"] == "adventure":
            return 8000
        if course["kind"] == "moi" or (level and level[1].upper() == "MOI"):
            return 20000  # snowboarding ("board") is charged like skiing
        if batch[3] == "Kargil":
            return 5000 if state == LADAKH else 10000
        return 10000 if age <= 25 else 20000

    def refresh_dob(self, v, course, level, batch):
        """What the real site does when the date of birth (or the state) changes."""
        dob_text = v.get("txtDateOfBirth", "").strip()
        if not dob_text:
            v["lblDOBMsg"] = ("Please Enter Date Of Birth.", "Red")
            v["txtCourseFee"] = "0"
            return
        try:
            d, m, y = (int(x) for x in dob_text.split("/"))
            dob = date(y, m, d)
        except ValueError:
            v["lblDOBMsg"] = ("Invalid Date Of Birth.", "Red")
            v["txtDateOfBirth"], v["txtCourseFee"] = "", "0"
            return
        count_date = (course.get("count_date") or batch[2]) if batch else date.today()
        age = age_on(dob, count_date)
        low, high = int(v.get("hdfMinimumAge") or 0), int(v.get("hdfMaximumAge") or 0)
        if not (low <= age <= high):
            v["lblDOBMsg"] = (f"Your Age {age} Years , Age Allow {low} To {high} Years.", "Red")
            v["txtDateOfBirth"], v["txtCourseFee"] = "", "0"
            return
        v["lblDOBMsg"] = (f"Your Age {age} Years.", "Green")
        v["txtCourseFee"] = str(self.fee(course, level, batch, age, v.get("ddlState"))) if batch else "0"

    def form(self, mobile, v, listed, invalid=(), alert=None):
        """The admission form. `listed` are the course values in ddlCourse: bound when the page is
        first loaded and then carried in the page state, so new courses only show on a fresh GET."""
        course, level, batch = self.state_of(v, listed)
        selects = {
            "ddlCourse": [("0", "Select Course")] + [(cv, self.site.courses[cv]["name"]) for cv in listed],
            "ddlCourseLevel": [("0", "Select Course Level")] + (course["levels"] if course else []),
            "ddlScheduleCourse": [("0", "Select Schedule Course")] + ([b[:2] for b in course["batches"]] if course else []),
            "ddlState": STATES, "ddlEduationQualification": EDUCATION, "ddlVegetarianOrNon": DIET,
        }
        token = self.viewstate({name: [o for o, _ in opts] for name, opts in selects.items()}, listed)
        today = date.today()
        defaults = {"hdfAdmissionFormId": "0", "txtAvailableSeat": "0", "hdfMinimumAge": "0", "hdfMaximumAge": "0",
                    "hdfAgeCountDate": f"{today:%Y-%m-%d}", "txtCourseFee": "0", "hdfAgeCriteria": "0",
                    "txtMobileNo": mobile}
        check_file_size = (  # sits in the real page's <head>, before the form
            "<script type=\"text/javascript\">\nfunction checkFileSize(inputFile) {\n"
            "    if (inputFile.files && inputFile.files[0].size > 1048576) {\n"
            '        alert("File Size Too Large.Only File Upload Lessthan 1MB"); // Do your thing to handle the error.\n'
            "        inputFile.value = null; // Clear the field.\n    }\n}\n</script>\n")
        out = [self.head(f"./admission_form.aspx?mobile={mobile}", multipart=True, token=token, generator="25909948",
                         head_extra=check_file_size),
               '<span id="lblMsg"' + (f' style="color:{v["lblMsg"][1]};"' if v.get("lblMsg") else "")
               + f'>{esc(v["lblMsg"][0]) if v.get("lblMsg") else ""}</span>\n']
        validators = []
        for kind, name in FORM_LAYOUT:
            value = v.get(name, defaults.get(name, ""))
            if kind == "hidden":
                out.append(f'<input type="hidden" name="{name}" id="{name}" value="{esc(value)}" />\n')
            elif kind == "select":
                onchange = (f' onchange="javascript:setTimeout(&#39;__doPostBack(\\&#39;{name}\\&#39;,\\&#39;\\&#39;)&#39;, 0)"'
                            if name in ("ddlCourse", "ddlScheduleCourse", "ddlState") else "")
                out.append(f'<select name="{name}"{onchange} id="{name}" class="form-control">\n')
                chosen = value if value in [o for o, _ in selects[name]] else None
                for option, text in selects[name]:
                    sel = ' selected="selected"' if option == chosen else ""
                    out.append(f'\t<option{sel} value="{esc(option)}">{esc(text)}</option>\n')
                out.append("</select>\n")
            elif kind == "readonly":
                out.append(f'<input name="{name}" type="text" value="{esc(value)}" readonly="readonly" id="{name}" '
                           'class="form-control mb-1" />\n')
            elif kind == "text":
                maxlen = TEXT_MAXLEN.get(name)
                attr = f' value="{esc(value)}"' if value else ""
                attr += f' maxlength="{maxlen}"' if maxlen else ""
                out.append(f'<input name="{name}" type="text"{attr} id="{name}" class="form-control mb-1" />\n')
            elif kind == "dob":
                attr = f' value="{esc(value)}"' if value else ""
                out.append(f'<input name="{name}" type="text"{attr} maxlength="10" onchange="javascript:setTimeout('
                           f'&#39;__doPostBack(\\&#39;{name}\\&#39;,\\&#39;\\&#39;)&#39;, 0)" onkeypress="if '
                           f'(WebForm_TextBoxKeyHandler(event) == false) return false;" id="{name}" '
                           'class="form-control Mb-1" placeholder="Date Of Birth (dd/MM/yyyy)" />\n')
            elif kind == "file":
                flag = FILES[name]
                out.append(f'<input type="file" name="{name}" id="{name}" class="form-control mb-1" '
                           'onchange="checkFileSize(this)" />\n'
                           f'<input type="hidden" name="{flag}" id="{flag}" value="{esc(v.get(flag, "No"))}" />\n')
            elif kind == "v":
                validators.append(name)
                state = "ValidatorCalloutExtender1_ClientState" if name == "RequiredFieldValidator1" else f"vce{name}_ClientState"
                out.append(f'<span id="{name}" style="color:Red;display:none;"></span>\n'
                           f'<input type="hidden" name="{state}" id="{state}" />\n')
            elif kind == "label":
                text, color = v.get(name) or ("", "Red")
                if name == "lblDOBMsg":
                    style = f' style="color:{color};font-size:10px;"'
                else:
                    style = f' style="color:{color};"' if text else ""
                out.append(f'<p><span id="{name}"{style}>{esc(text)}</span></p>\n')
            elif kind == "checkbox":
                out.append(f'<span class="checkbox"><input id="{name}" type="checkbox" name="{name}" />'
                           f'<label for="{name}">I agree to the terms and the course rules.</label></span>\n')
            elif kind == "submit":
                if name == "btnSubmit":
                    out.append('<input type="submit" name="btnSubmit" value="Submit" onclick="javascript:WebForm_DoPostBack'
                               'WithOptions(new WebForm_PostBackOptions(&quot;btnSubmit&quot;, &quot;&quot;, true, '
                               '&quot;sv&quot;, &quot;&quot;, false, false))" id="btnSubmit" class="btn btn_com px-4" />\n')
                else:
                    out.append('<input type="submit" name="btnReset" value="Reset" id="btnReset" class="btn btn_com px-4" />\n')
        out.append(self.validator_scripts(validators, invalid=invalid, alert=alert))
        return "".join(out)

    def get_form(self, sess, query):
        if not sess["accepted"] or sess["mobile"] != query.get("mobile"):
            return self.redirect("/Admission_instructions.aspx")
        self.send(200, self.form(sess["mobile"], {}, self.listed_now()))

    def post_form(self, sess, query, v, files):
        site = self.site
        if not sess["accepted"] or sess["mobile"] != query.get("mobile"):
            return self.redirect("/Admission_instructions.aspx")
        stored = self.check_viewstate(v)
        if stored is None:
            return
        mobile, listed = sess["mobile"], stored["courses"]
        event = v.get("__EVENTTARGET", "")
        course, level, batch = self.state_of(v, listed)
        if event == "ddlCourse":
            v.update(ddlCourseLevel="0", ddlScheduleCourse="0", txtAvailableSeat="0", txtCourseFee="0",
                     hdfMinimumAge="0", hdfMaximumAge="0",
                     hdfAgeCriteria=str(course["criteria"]) if course else "0")
            return self.send(200, self.form(mobile, v, listed))
        if event == "ddlScheduleCourse":
            if course and batch:
                key = (v["ddlCourse"], level and level[0], batch[0])
                seats = site.shown_seats.get(key, site.seats.get(key, 0)) if level else 0
                low, high = course["age"]
                v.update(txtAvailableSeat=str(seats), hdfMinimumAge=str(low), hdfMaximumAge=str(high),
                         hdfAgeCountDate="{0.month}/{0.day}/{0.year} 12:00:00 AM".format(course.get("count_date") or batch[2]),
                         txtCourseFee="0")
                if v.get("txtDateOfBirth"):
                    self.refresh_dob(v, course, level, batch)
            return self.send(200, self.form(mobile, v, listed))
        if event in ("ddlState", "txtDateOfBirth"):
            self.refresh_dob(v, course, level, batch)
            return self.send(200, self.form(mobile, v, listed))
        if v.get("btnReset") or not v.get("btnSubmit"):
            return self.send(200, self.form(mobile, {}, listed))
        return self.submit(mobile, v, files, listed, course, level, batch)

    def is_minor(self, v, course, batch):
        try:
            d, m, y = (int(x) for x in v.get("txtDateOfBirth", "").split("/"))
            day = (course.get("count_date") or batch[2]) if course and batch else date.today()
            return age_on(date(y, m, d), day) < 18
        except ValueError:
            return False

    def submit(self, mobile, v, files, listed, course, level, batch):
        site = self.site
        invalid = []
        parent_needed = site.parent_signature_required or self.is_minor(v, course, batch)
        for vid, control, kind, group, extra, _ in VALIDATORS:
            if group != "sv" or (vid == "rfvFupParentGuardianSignature" and not parent_needed):
                continue
            value = files.get(control, ("", b""))[0] if control in FILES else v.get(control, "")
            if kind == "req" and value.strip() == extra:
                invalid.append(vid)
            elif kind == "rx" and value and not re.fullmatch(extra, value):
                invalid.append(vid)
        too_big = [name for name, (fn, data) in files.items() if fn and len(data) > 1048576]
        if invalid or too_big:
            if too_big:
                v["lblMsg"] = ("File Size Too Large.Only File Upload Lessthan 1MB", "Red")
            return self.send(200, self.form(mobile, v, listed, invalid=invalid))
        if v.get("chkIAgree") != "on":
            v["lblCheckError"] = ("Please accept the declaration.", "Red")
            return self.send(200, self.form(mobile, v, listed))
        key = (v["ddlCourse"], v["ddlCourseLevel"], v["ddlScheduleCourse"])
        problem = None
        with site.lock:
            if mobile in site.used:
                problem = ("This Mobile No. is already registered.", site.used_style)
            elif site.seats.get(key, 0) <= 0:
                problem = ("Seat not available in the selected schedule.", site.full_style)
            elif not course or not batch or v.get("txtCourseFee", "0") in ("", "0"):
                problem = ("Please select the course, schedule and a valid date of birth.", "label")
            else:
                site.seats[key] -= 1
                site.used.add(mobile)
                app_id = 1600 + len(site.applications) + 1
                site.applications.append({"id": app_id, "mobile": mobile, "course": key[0], "level": key[1],
                                          "schedule": key[2], "name": v.get("txtName"), "fee": v.get("txtCourseFee"),
                                          "files": sorted(n for n, (f, _) in files.items() if f)})
                crash = site.crash_after_commit > 0
                if crash:
                    site.crash_after_commit -= 1
        if problem:
            text, style = problem
            if style == "alert":
                return self.send(200, self.form(mobile, v, listed, alert=text))
            v["lblMsg"] = (text, "Red")
            return self.send(200, self.form(mobile, v, listed))
        if crash:
            return self.send(503, IIS_503)
        message = "Admission Form submitted successfully. Payment link has been sent to your email id."
        if site.success_style == "alert":
            v2 = {"lblMsg": (message, "Green")}
            return self.send(200, self.form(mobile, v2, listed, alert=message))
        if site.success_style == "payment":
            return self.redirect(f"/admission_formpayment.aspx?RId={app_id}")
        self.redirect("/AdmissionFormSuccess.aspx?Resp=" + urllib.parse.quote(message) + f"&RId={app_id}")


def parse_multipart(body, content_type):
    msg = BytesParser(policy=policy.HTTP).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
    values, files = {}, {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name is None:
            continue
        data = part.get_payload(decode=True) or b""
        filename = part.get_param("filename", header="content-disposition")
        if filename is not None:
            files[name] = (filename, data)
        else:
            values[name] = data.decode("utf-8", "replace")
    return values, files


def start(site, port=0):
    """Run the fake site in a background thread; returns (server, base_url)."""
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.handle_error = lambda request, address: None  # clients hanging up on slow answers is expected
    server.site = site
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--layout", choices=sorted(LAYOUTS), default="levels")
    ap.add_argument("--chaos", type=float, default=0.3, help="share of requests that fail (0-1)")
    ap.add_argument("--session-loss", type=float, default=0.02, help="share of requests that drop the session")
    ap.add_argument("--restarts", type=float, default=0.0, help="share of requests that recycle the app")
    ap.add_argument("--opens-in", type=float, default=0, help="seconds until the snow courses appear")
    ap.add_argument("--seats", type=int, default=3, help="seats per course/level/batch")
    ap.add_argument("--full", default="", help="comma-separated batch values with no seats, e.g. 71,72")
    ap.add_argument("--parent-signature-required", action="store_true",
                    help="refuse adults' forms without a parent/guardian signature")
    args = ap.parse_args()
    site = Site(layout=args.layout, chaos=args.chaos, session_loss=args.session_loss, restarts=args.restarts,
                opens_at=time.time() + args.opens_in)
    site.parent_signature_required = args.parent_signature_required
    full = {s for s in args.full.split(",") if s}
    for key in site.seats:
        if site.courses[key[0]]["kind"] != "adventure":
            site.seats[key] = 0 if key[2] in full else args.seats
    server, url = start(site, args.port)
    print(f"Fake IISM site on {url}/Admission_instructions.aspx (layout {args.layout}, chaos {args.chaos:.0%})"
          " - Ctrl+C to stop", flush=True)
    try:
        while True:
            time.sleep(5)
            snap = site.snapshot()
            print(f"requests {snap['requests']}, applications {len(snap['applications'])}", flush=True)
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
