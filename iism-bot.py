#!/usr/bin/env python3
"""
IISM Gulmarg Admission Bot — Python Edition (stdlib only, zero dependencies)

Direct HTTP automation of the ASP.NET WebForms admission flow.
Handles site downtime, session expiry, duplicate mobile numbers,
multi-number fallback, and position-based dropdown selection.

Usage:
  python3 iism-bot.py                      # run immediately
  python3 iism-bot.py --dry-run            # fill everything but don't submit
  python3 iism-bot.py --target=10:00       # start ~5 min before target time
  python3 iism-bot.py --max-retries=5      # override max retries per step
  python3 iism-bot.py --wait=3             # override base wait between retries
  python3 iism-bot.py --reset-cache        # clear cached course options, re-scan
"""

import json
import os
import re
import ssl
import sys
import time
import random
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Config paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
CACHE_PATH = SCRIPT_DIR / ".iism-cache.json"

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config():
    if not CONFIG_PATH.exists():
        sys.exit("ERROR: config.json not found. Create it with your details.")
    with open(CONFIG_PATH) as f:
        raw = f.read()
    lines = []
    for line in raw.split("\n"):
        stripped = line.strip()
        if stripped.startswith("//"):
            continue
        lines.append(line)
    return json.loads("\n".join(lines))

def load_cache():
    if CACHE_PATH.exists():
        with open(CACHE_PATH) as f:
            return json.load(f)
    return {}

def save_cache(data):
    with open(CACHE_PATH, "w") as f:
        json.dump(data, f, indent=2)

def validate_config(cfg):
    required = [
        ("applicant", "name"), ("applicant", "fatherName"), ("applicant", "dateOfBirth"),
        ("applicant", "email"), ("applicant", "nationality"),
        ("address", "houseNo"), ("address", "street"), ("address", "district"),
        ("address", "pincode"), ("address", "state"),
    ]
    for section, key in required:
        if not cfg.get(section, {}).get(key):
            sys.exit(f"ERROR: Missing config: {section}.{key}")

    phones = cfg.get("applicant", {}).get("mobileNumbers", [])
    if not phones:
        single = cfg.get("applicant", {}).get("mobile", "")
        if single:
            cfg["applicant"]["mobileNumbers"] = [single]
        else:
            sys.exit("ERROR: Provide applicant.mobileNumbers or applicant.mobile")

    docs = cfg.get("documents", {})
    for name, filepath in docs.items():
        if name.startswith("_"):
            continue
        if filepath and not (SCRIPT_DIR / filepath).exists():
            print(f"WARN: Document not found: {filepath} ({name})")

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

LOG_FILE = None

def init_logger(cfg):
    global LOG_FILE
    if cfg.get("bot", {}).get("logToFile") and cfg.get("bot", {}).get("logFile"):
        log_path = SCRIPT_DIR / cfg["bot"]["logFile"]
        LOG_FILE = open(log_path, "a")

def log(level, msg):
    ts = datetime.now().isoformat()
    line = f"[{ts}] [{level}] {msg}"
    print(line)
    if LOG_FILE:
        LOG_FILE.write(line + "\n")
        LOG_FILE.flush()

# ---------------------------------------------------------------------------
# HTTP client (with cookie jar + redirect handling)
# ---------------------------------------------------------------------------

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

def make_opener():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPRedirectHandler(),
    ), jar

def http_get(opener, url, timeout=30):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    resp = opener.open(req, timeout=timeout)
    body = resp.read().decode("utf-8", errors="replace")
    return resp.status, body, dict(resp.headers), resp.url

def http_post(opener, url, body, content_type, timeout=30):
    req = urllib.request.Request(url, data=body)
    req.add_header("User-Agent", UA)
    req.add_header("Content-Type", content_type)
    try:
        resp = opener.open(req, timeout=timeout)
        resp_body = resp.read().decode("utf-8", errors="replace")
        return resp.status, resp_body, dict(resp.headers), resp.url
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        return e.code, body_text, dict(e.headers), e.url

def is_redirect(status):
    return status in (301, 302, 303, 307)

# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------

RETRYABLE_MSGS = [
    "econnrefused", "enotfound", "epipe", "timeout", "timed out",
    "socket hang up", "connection reset", "connection refused",
    "temporary failure", "network is unreachable", "no route to host",
    "ssl", "certificate", "reset by peer",
]

def is_retryable(err):
    msg = str(err).lower()
    if any(m in msg for m in RETRYABLE_MSGS):
        return True
    if hasattr(err, "code") and err.code >= 500:
        return True
    if any(str(sc) in msg for sc in ["500", "502", "503", "504"]):
        return True
    return False

def retry(fn, label="operation", max_retries=5, base_delay=2.0,
          max_delay=30.0, backoff=1.5, timeout=30):
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            result = fn()
            if attempt > 1:
                log("INFO", f"  {label}: succeeded on attempt {attempt}")
            return result
        except Exception as e:
            last_err = e
            msg = str(e)[:100]
            if not is_retryable(e):
                raise Exception(f"{label}: {msg}") from e

            delay = min(base_delay * (backoff ** (attempt - 1)), max_delay)
            delay += delay * 0.25 * (random.random() * 2 - 1)
            log("WARN", f"  {label}: attempt {attempt}/{max_retries} ({msg}), retrying in {delay:.1f}s")
            if attempt == max_retries:
                raise Exception(f"{label}: all {max_retries} attempts failed. Last: {last_err}")
            time.sleep(delay)

# ---------------------------------------------------------------------------
# ASP.NET helpers
# ---------------------------------------------------------------------------

def extract_hidden(html, name):
    for pat in [
        rf'<input[^>]*id="{name}"[^>]*value="([^"]*)"',
        rf'<input[^>]*name="{name}"[^>]*value="([^"]*)"',
    ]:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return (m.group(1)
                    .replace("&quot;", '"').replace("&lt;", "<")
                    .replace("&gt;", ">").replace("&amp;", "&"))
    return ""

def extract_asp_fields(html):
    return {
        "viewstate": extract_hidden(html, "__VIEWSTATE"),
        "viewstateGenerator": extract_hidden(html, "__VIEWSTATEGENERATOR"),
        "eventValidation": extract_hidden(html, "__EVENTVALIDATION"),
    }

def extract_dropdown_options(html, dropdown_id):
    """Extract all options from a <select> by its id. Returns list of {text, value}."""
    pattern = rf'<select[^>]*id="{dropdown_id}"[^>]*>(.*?)</select>'
    m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
    if not m:
        return []
    options_html = m.group(1)
    options = []
    for opt_m in re.finditer(r'<option[^>]*value="([^"]*)"[^>]*>([^<]*)</option>', options_html, re.I):
        options.append({"text": opt_m.group(2).strip(), "value": opt_m.group(1)})
    return options

# ---------------------------------------------------------------------------
# multipart/form-data builder
# ---------------------------------------------------------------------------

class MultipartBuilder:
    def __init__(self):
        self.boundary = "----IISM" + str(int(time.time() * 1000))[-10:]
        self.parts = []

    def add_field(self, name, value):
        self.parts.append(
            f"--{self.boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        )

    def add_file(self, name, filepath):
        if not filepath or not os.path.exists(filepath):
            self.parts.append(
                f"--{self.boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"; filename=""\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n\r\n"
            )
            return
        filename = os.path.basename(filepath)
        ext = os.path.splitext(filepath)[1].lower()
        mime = "image/png" if ext == ".png" else "image/jpeg"
        with open(filepath, "rb") as f:
            content = f.read()
        header = (
            f"--{self.boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        )
        self.parts.append(header.encode("utf-8"))
        self.parts.append(content)
        self.parts.append(b"\r\n")

    def build(self):
        footer = f"--{self.boundary}--\r\n".encode("utf-8")
        result = bytearray()
        for part in self.parts:
            if isinstance(part, str):
                result.extend(part.encode("utf-8"))
            else:
                result.extend(part)
        result.extend(footer)
        return bytes(result)

    def content_type(self):
        return f"multipart/form-data; boundary={self.boundary}"

# ---------------------------------------------------------------------------
# Static value mappings (fallbacks when cache is unavailable)
# ---------------------------------------------------------------------------

STATE_VALUES = {
    "MAHARASHTRA": "21", "DELHI": "5", "UTTAR PRADESH": "11",
    "JAMMU AND KASHMIR": "14", "HIMACHAL PRADESH": "13", "UTTARAKHAND": "12",
    "PUNJAB": "25", "HARYANA": "9", "RAJASTHAN": "26", "GUJARAT": "8",
    "MADHYA PRADESH": "20", "BIHAR": "3", "WEST BENGAL": "34",
    "KARNATAKA": "16", "KERALA": "17", "TAMIL NADU": "29", "TELANGANA": "30",
    "ANDHRA PRADESH (NEW)": "36", "ASSAM": "2", "GOA": "7", "SIKKIM": "28",
    "LADAKH": "18", "ODISHA": "23", "CHANDIGARH": "4", "JHARKHAND": "15",
    "CHATTISGARH": "6", "TRIPURA": "31", "MANIPUR": "22", "MEGHLAYA": "24",
    "ARUNACHAL PRADESH": "37", "LAKSHWADEEP": "19", "PUDUCHERRY": "27",
    "DADRA AND NAGAR HAVELI": "38", "DAMAN AND DIU": "39",
}

# Education and Vegetarian dropdowns use TEXT values (NOT numeric).
# Ground truth from browser submission confirms this.
EDUCATION_VALUES = {
    "High School": "High School", "Intermediate": "Intermediate",
    "Graduation": "Graduation", "Post graduation": "Post graduation",
}

VEG_VALUES = {"Vegetarian": "Vegetarian", "Non-Vegetarian": "Non-Vegetarian"}

def resolve_state(state_name):
    if state_name in STATE_VALUES:
        return STATE_VALUES[state_name]
    upper = state_name.upper()
    for k, v in STATE_VALUES.items():
        if k.upper() == upper:
            return v
    log("WARN", f'Unknown state "{state_name}", using value 0')
    return "0"

# ---------------------------------------------------------------------------
# Position-based option picker
# ---------------------------------------------------------------------------

BASE = "https://admission.iismgulmarg.in"

def _pick_option(options, raw, dropdown_id):
    """Pick a dropdown option by position (1-based) or text match.
    Returns (value, text). Raises Exception if not found."""
    # Try 1-based position (skip "Select..." options with value="0")
    try:
        pos = int(raw)
        actual = [o for o in options if o["value"] != "0"]
        if 1 <= pos <= len(actual):
            chosen = actual[pos - 1]
            log("INFO", f"  {dropdown_id}: position {pos} → '{chosen['text']}'")
            return chosen["value"], chosen["text"]
        else:
            avail = "\n".join(f"  [{i+1}] {o['text']}" for i, o in enumerate(actual))
            raise Exception(f"{dropdown_id}: position {pos} out of range (1-{len(actual)}).\n{avail}")
    except ValueError:
        pass

    # Try exact text match
    for o in options:
        if o["text"].strip().lower() == raw.strip().lower():
            log("INFO", f"  {dropdown_id}: exact match '{o['text']}'")
            return o["value"], o["text"]

    # Try partial match
    for o in options:
        if raw.strip().lower() in o["text"].strip().lower():
            log("INFO", f"  {dropdown_id}: partial match '{raw}' → '{o['text']}'")
            return o["value"], o["text"]

    avail = "\n".join(f"  [{i+1 if o['value']!='0' else 0}] {o['text']}" for i, o in enumerate(options))
    raise Exception(f"Cannot find '{raw}' in {dropdown_id}. Available:\n{avail}")

# ---------------------------------------------------------------------------
# Postback helper (for cascading dropdowns)
# ---------------------------------------------------------------------------

def postback(opener, mobile, fields, event_target, extra=None):
    """Submit a partial postback to update dependent dropdowns."""
    if extra is None:
        extra = {}
    mb = MultipartBuilder()
    mb.add_field("__EVENTTARGET", event_target)
    mb.add_field("__EVENTARGUMENT", "")
    mb.add_field("__LASTFOCUS", "")
    mb.add_field("__VIEWSTATE", fields["viewstate"])
    mb.add_field("__VIEWSTATEGENERATOR", fields["viewstateGenerator"])
    mb.add_field("__EVENTVALIDATION", fields["eventValidation"])
    _sd = load_config().get("serverDefaults", {})
    mb.add_field("hdfAdmissionFormId", "0")
    mb.add_field("txtAvailableSeat", extra.get("txtAvailableSeat", "1"))
    mb.add_field("hdfMinimumAge", extra.get("hdfMinimumAge", _sd.get("hdfMinimumAge", "10")))
    mb.add_field("hdfMaximumAge", extra.get("hdfMaximumAge", _sd.get("hdfMaximumAge", "40")))
    mb.add_field("hdfAgeCountDate", extra.get("hdfAgeCountDate", _sd.get("hdfAgeCountDate", "6/15/2026 12:00:00 AM")))
    mb.add_field("txtCourseFee", extra.get("txtCourseFee", _sd.get("txtCourseFee", "10000")))
    mb.add_field("hdfAgeCriteria", extra.get("hdfAgeCriteria", _sd.get("hdfAgeCriteria", "14")))

    for dd in ["ddlCourse", "ddlCourseLevel", "ddlScheduleCourse", "ddlState"]:
        mb.add_field(dd, extra.get(dd, "0"))
        mb.add_field(f"vcerfv{dd}_ClientState", "")

    empty_text = [
        "txtName", "txtFatherName", "txtNationality", "txtDateOfBirth",
        "txtDomicile", "txtHouseNoApartment", "txtStreetVillageColony",
        "txtDistrictCity", "txtPincode", "txtEmailId",
        "txtBloodGroup", "txtMedicalCategory", "txtHeight", "txtWeight",
        "txtWaist", "txtShoeSize", "txtEmergencyContactName", "txtEmergencyContactPhone",
    ]
    for name in empty_text:
        mb.add_field(name, extra.get(name, ""))
    mb.add_field("txtMobileNo", mobile)

    validators = [
        "vcerfvtxtName_ClientState", "vcerfvtxtFatherName_ClientState",
        "vcerfvtxtNationality_ClientState", "vcerfvtxtDateOfBirth_ClientState",
        "vceregvtxtDateOfBirth_ClientState", "vcerfvtxtCourseFee_ClientState",
        "vcerfvFupBirthCertificate_ClientState", "vceregvFupBirthCertificate_ClientState",
        "vceregvFupDomicileAttachProof_ClientState",
        "vcerfvtxtHouseNoApartment_ClientState", "vcerfvtxtStreetVillageColony_ClientState",
        "vcerfvtxtDistrictCity_ClientState", "vcerfvtxtPincode_ClientState",
        "vcerfvtxtMobileNo_ClientState", "vcerfvtxtEmailId_ClientState",
        "vcerevemailid_ClientState", "vcerfvddlEduationQualification_ClientState",
        "vcerfvFupEducationQualification_ClientState", "vceregvFupEducationQualification_ClientState",
        "vceregvFupAnyAdventureCoursePhoto_ClientState",
        "vceregvFupCurrentEmployment_ClientState", "ValidatorCalloutExtender1_ClientState",
        "vcerfvtxtBloodGroup_ClientState",
        "vcerfvFupPhototOfApplicant_ClientState", "vceregvFupPhototOfApplicant_ClientState",
        "vcerfvFupSignatureOfAppicant_ClientState", "vceregvFupSignatureOfAppicant_ClientState",
        "vcerfvtxtEmergencyContactName_ClientState", "vcerfvtxtEmergencyContactPhone_ClientState",
        "vcerfvFupParentGuardianSignature_ClientState", "vceregvFupParentGuardianSignature_ClientState",
    ]
    for name in validators:
        mb.add_field(name, "")

    file_fields = [
        "FupBirthCertificate", "FupDomicileAttachProof", "FupEducationQualification",
        "FupAnyAdventureCoursePhoto", "FupCurrentEmployment",
        "FupPhototOfApplicant", "FupSignatureOfAppicant", "FupParentGuardianSignature",
    ]
    for name in file_fields:
        mb.add_file(name, None)

    for name in ["hdfFupBirthCertificate", "hdfFupDomicileAttachProof", "hdfEducationQualification",
                 "hdfAnyAdventureCoursePhoto", "hdfCurrentEmployment",
                 "hdfPhototOfApplicant", "hdfFupSignatureOfAppicant", "hdfParentGuardianSignature"]:
        mb.add_field(name, "No")

    mb.add_field("ddlEduationQualification", "0")
    mb.add_field("ddlVegetarianOrNon", "0")
    mb.add_field("chkIAgree", "off")

    body = mb.build()
    status, resp_body, _, final_url = http_post(
        opener, f"{BASE}/admission_form.aspx?mobile={mobile}",
        body, mb.content_type()
    )
    return {"status": status, "body": resp_body, "url": final_url}

# ---------------------------------------------------------------------------
# Result codes
# ---------------------------------------------------------------------------

ALREADY_REGISTERED = "ALREADY_REGISTERED"
SUCCESS = "SUCCESS"
ERROR = "ERROR"

# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------

def step1_verify_mobile(opener, cfg, mobile, max_retries, base_delay, max_delay):
    log("INFO", f"Step 1: Mobile verification for {mobile}...")

    def _do():
        status, body, _, _ = http_get(opener, f"{BASE}/Admission_instructions.aspx")
        if status != 200:
            raise Exception(f"GET returned {status}")
        fields = extract_asp_fields(body)

        post_body = urllib.parse.urlencode({
            "__LASTFOCUS": "", "__EVENTTARGET": "lnkbtnSubmit",
            "__EVENTARGUMENT": "", "__VIEWSTATE": fields["viewstate"],
            "__VIEWSTATEGENERATOR": fields["viewstateGenerator"],
            "__EVENTVALIDATION": fields["eventValidation"],
            "txtMobileNo": mobile,
        }).encode()

        status, body, headers, final_url = http_post(
            opener, f"{BASE}/Admission_instructions.aspx",
            post_body, "application/x-www-form-urlencoded"
        )

        body_lower = body.lower()
        already_kw = [
            "already registered", "already exist", "already submitted",
            "duplicate", "already been used", "mobile number already",
            "already applied", "this number is already",
        ]
        if any(kw in body_lower for kw in already_kw):
            log("WARN", f"  Mobile {mobile} appears to be already registered!")
            return ALREADY_REGISTERED

        # Check if we're on or redirected to the criteria page
        if "Admission_criteria" in str(final_url) or "admission_criteria" in body_lower:
            log("INFO", "  Step 1 complete — on criteria page")
            return SUCCESS

        if is_redirect(status):
            log("INFO", f"  Step 1 complete — redirected to {headers.get('Location', '?')}")
            return SUCCESS

        raise Exception(f"Unexpected response: HTTP {status} — {body[:200]}")

    return retry(_do, label=f"Step 1 (mobile={mobile})",
                 max_retries=max_retries, base_delay=base_delay, max_delay=max_delay)


def step2_accept_criteria(opener, cfg, mobile, max_retries, base_delay, max_delay):
    log("INFO", f"Step 2: Accepting admission criteria (mobile={mobile})...")

    def _do():
        status, body, _, _ = http_get(
            opener, f"{BASE}/Admission_criteria.aspx?mobile={mobile}"
        )
        if status != 200:
            raise Exception(f"GET returned {status}")

        fields = extract_asp_fields(body)

        # The criteria page uses chkAccept (NOT chkIAgree)
        post_body = urllib.parse.urlencode({
            "__LASTFOCUS": "", "__EVENTTARGET": "lnkbtnNext",
            "__EVENTARGUMENT": "", "__VIEWSTATE": fields["viewstate"],
            "__VIEWSTATEGENERATOR": fields["viewstateGenerator"],
            "__EVENTVALIDATION": fields["eventValidation"],
            "chkAccept": "on",
        }).encode()

        status, body, headers, final_url = http_post(
            opener, f"{BASE}/Admission_criteria.aspx?mobile={mobile}",
            post_body, "application/x-www-form-urlencoded"
        )

        body_lower = body.lower()
        already_kw = ["already registered", "already submitted", "duplicate"]
        if any(kw in body_lower for kw in already_kw):
            log("WARN", "  Already registered detected")
            return ALREADY_REGISTERED

        # Check if we reached the form page
        if "admission_form.aspx" in str(final_url) or "admission_form" in body_lower:
            log("INFO", "  Step 2 complete — on admission form page")
            return SUCCESS

        if is_redirect(status):
            log("INFO", f"  Step 2 complete — redirected to {headers.get('Location', '?')}")
            return SUCCESS

        raise Exception(f"Unexpected response: HTTP {status} — {body[:200]}")

    return retry(_do, label=f"Step 2 (mobile={mobile})",
                 max_retries=max_retries, base_delay=base_delay, max_delay=max_delay)


def step3_select_course(opener, cfg, cache, mobile, max_retries, base_delay, max_delay):
    """GET form page → progressively select course→schedule→state→DOB.
    Only controls with onchange handlers trigger postbacks:
      ddlCourse (onchange) → ddlScheduleCourse (onchange) → ddlState (onchange) → txtDateOfBirth (onchange)
    ddlCourseLevel has NO onchange — we pick it but include its value in subsequent postbacks.
    Results are cached so subsequent runs skip discovery."""
    log("INFO", f"Step 3: Selecting course, level, schedule (mobile={mobile})...")

    course_cfg = cfg.get("course", {})
    addr_cfg = cfg.get("address", {})
    course_options_cache = cache.get("course_options", {})
    force_rediscover = "--reset-cache" in sys.argv

    def _do():
        # GET form page
        status, body, _, _ = http_get(
            opener, f"{BASE}/admission_form.aspx?mobile={mobile}"
        )
        if status != 200:
            raise Exception(f"GET returned {status}")
        body_lower = body.lower()
        if "no seats" in body_lower or "registration closed" in body_lower:
            raise Exception(f"Course unavailable: {body[:500]}")

        fields = extract_asp_fields(body)

        # --- Select Course (has onchange) ---
        course_opts_key = "ddlCourse"
        if course_opts_key not in course_options_cache or force_rediscover:
            opts = extract_dropdown_options(body, "ddlCourse")
            course_options_cache[course_opts_key] = opts
            log("INFO", f"  Discovered {len(opts)} course options")
        course_val, course_name = _pick_option(
            course_options_cache[course_opts_key], course_cfg.get("name", "1"), "ddlCourse"
        )

        log("INFO", f"  POST ddlCourse={course_val}")
        res = postback(opener, mobile, fields, "ddlCourse", {
            "ddlCourse": course_val, "ddlCourseLevel": "0",
            "ddlScheduleCourse": "0", "ddlState": "0",
        })
        if res["status"] != 200:
            raise Exception(f"Course select returned {res['status']}")
        fields = extract_asp_fields(res["body"])

        # --- Pick Level (NO onchange — just discover options, include in next postback) ---
        level_opts_key = f"ddlCourseLevel_course{course_val}"
        if level_opts_key not in course_options_cache or force_rediscover:
            opts = extract_dropdown_options(res["body"], "ddlCourseLevel")
            course_options_cache[level_opts_key] = opts
            log("INFO", f"  Discovered {len(opts)} level options for course {course_val}")
        level_val, level_name = _pick_option(
            course_options_cache[level_opts_key], course_cfg.get("level", "1"), "ddlCourseLevel"
        )

        # --- Select Schedule (has onchange; include level_val). Auto-fallback if 0 seats. ---
        schedule_opts_key = f"ddlScheduleCourse_course{course_val}"
        if schedule_opts_key not in course_options_cache or force_rediscover:
            opts = extract_dropdown_options(res["body"], "ddlScheduleCourse")
            course_options_cache[schedule_opts_key] = opts
            log("INFO", f"  Discovered {len(opts)} schedule options for course {course_val}")
        sched_opts = [o for o in course_options_cache[schedule_opts_key] if o["value"] != "0"]

        schedule_val = schedule_name = None
        seats = fee = "0"
        start_pos = int(course_cfg.get("schedule", "1"))

        for sched_idx in range(len(sched_opts)):
            pos = start_pos + sched_idx
            if pos > len(sched_opts):
                break
            sched_val, sched_name = _pick_option(
                course_options_cache[schedule_opts_key], str(pos), "ddlScheduleCourse"
            )

            log("INFO", f"  POST ddlScheduleCourse={sched_val} '{sched_name}' (level={level_val})")
            res = postback(opener, mobile, fields, "ddlScheduleCourse", {
                "ddlCourse": course_val, "ddlCourseLevel": level_val,
                "ddlScheduleCourse": sched_val, "ddlState": "0",
            })
            if res["status"] != 200:
                raise Exception(f"Schedule select returned {res['status']}")

            seat_m = re.search(r'(?:id|name)="txtAvailableSeat"[^>]*value="(\d+)"', res["body"])
            fee_m = re.search(r'(?:id|name)="txtCourseFee"[^>]*value="([^"]*)"', res["body"])
            seats = seat_m.group(1) if seat_m else "0"
            fee = fee_m.group(1) if fee_m else "0"

            if seats not in ("0", "?"):
                schedule_val, schedule_name = sched_val, sched_name
                fields = extract_asp_fields(res["body"])
                log("INFO", f"  Schedule '{sched_name}' has {seats} seat(s)")
                break
            log("WARN", f"  Schedule '{sched_name}' has 0 seats, trying next...")

        if schedule_val is None:
            raise Exception(f"All {len(sched_opts)} schedule(s) have 0 seats")

        # --- Select State (has onchange) ---
        state_opts_key = "ddlState"
        if state_opts_key not in course_options_cache or force_rediscover:
            opts = extract_dropdown_options(res["body"], "ddlState")
            course_options_cache[state_opts_key] = opts
            log("INFO", f"  Discovered {len(opts)} state options")
        state_val, state_name = _pick_option(
            course_options_cache[state_opts_key], addr_cfg.get("state", "MAHARASHTRA"), "ddlState"
        )

        log("INFO", f"  POST ddlState={state_val}")
        res = postback(opener, mobile, fields, "ddlState", {
            "ddlCourse": course_val, "ddlCourseLevel": level_val,
            "ddlScheduleCourse": schedule_val, "ddlState": state_val,
        })
        if res["status"] != 200:
            raise Exception(f"State select returned {res['status']}")
        fields = extract_asp_fields(res["body"])

        # --- DOB postback (has onchange) ---
        dob = cfg.get("applicant", {}).get("dateOfBirth", "")
        log("INFO", f"  POST txtDateOfBirth={dob}")
        res = postback(opener, mobile, fields, "txtDateOfBirth", {
            "ddlCourse": course_val, "ddlCourseLevel": level_val,
            "ddlScheduleCourse": schedule_val, "ddlState": state_val,
            "txtDateOfBirth": dob,
        })
        if res["status"] != 200:
            raise Exception(f"DOB postback returned {res['status']}")
        fields = extract_asp_fields(res["body"])

        # Extract server-populated fields from the final postback response
        min_age = extract_hidden(res["body"], "hdfMinimumAge") or "0"
        max_age = extract_hidden(res["body"], "hdfMaximumAge") or "0"
        age_count_date = extract_hidden(res["body"], "hdfAgeCountDate") or "2026-06-10"
        log("INFO", f"  Server: minAge={min_age}, maxAge={max_age}, ageDate={age_count_date}")

        # --- Save cache ---
        cache["course_options"] = course_options_cache
        cache["resolved"] = {
            "course": course_name, "level": level_name,
            "schedule": schedule_name, "state": state_name,
        }
        save_cache(cache)

        log("INFO", f"  Available seats: {seats}, Course fee: {fee}")

        return fields, seats, fee, {
            "course_val": course_val, "level_val": level_val,
            "schedule_val": schedule_val, "state_val": state_val,
        }, min_age, max_age, age_count_date

    fields, seats, fee, sel, min_age, max_age, age_count_date = retry(
        _do, label=f"Step 3 (mobile={mobile})",
        max_retries=max_retries, base_delay=base_delay, max_delay=max_delay
    )
    return {
        "fields": fields,
        "course_val": sel["course_val"],
        "level_val": sel["level_val"],
        "schedule_val": sel["schedule_val"],
        "state_val": sel["state_val"],
        "seats": seats,
        "fee": fee,
        "min_age": min_age,
        "max_age": max_age,
        "age_count_date": age_count_date,
    }


def step4_submit_form(opener, cfg, mobile, form_state, max_retries, base_delay, max_delay):
    applicant = cfg["applicant"]
    address = cfg["address"]
    docs = cfg.get("documents", {})
    dry_run = cfg.get("bot", {}).get("dryRun", False)

    if dry_run:
        log("INFO", "DRY RUN — skipping form submission")
        return {"success": True, "dry_run": True}

    log("INFO", f"Step 4: Building and submitting final form (mobile={mobile})...")

    def _resolve(p):
        if not p:
            return None
        return str(SCRIPT_DIR / p)

    def _build_and_submit():
        mb = MultipartBuilder()
        f = form_state["fields"]

        mb.add_field("__EVENTTARGET", "")
        mb.add_field("__EVENTARGUMENT", "")
        mb.add_field("__LASTFOCUS", "")
        mb.add_field("__VIEWSTATE", f["viewstate"])
        mb.add_field("__VIEWSTATEGENERATOR", f["viewstateGenerator"])
        mb.add_field("__EVENTVALIDATION", f["eventValidation"])
        mb.add_field("hdfAdmissionFormId", "0")

        mb.add_field("ddlCourse", form_state["course_val"])
        mb.add_field("vcerfvddlCourse_ClientState", "")
        mb.add_field("ddlCourseLevel", form_state["level_val"])
        mb.add_field("vcerfvddlCourseLevel_ClientState", "")
        mb.add_field("ddlScheduleCourse", form_state["schedule_val"])
        mb.add_field("vcerfvddlScheduleCourse_ClientState", "")
        mb.add_field("ddlState", form_state["state_val"])
        mb.add_field("vcerfvddlState_ClientState", "")
        mb.add_field("txtAvailableSeat", form_state.get("seats", "0"))
        _sd = cfg.get("serverDefaults", {})
        mb.add_field("hdfMinimumAge", form_state.get("min_age") or _sd.get("hdfMinimumAge", "10"))
        mb.add_field("hdfMaximumAge", form_state.get("max_age") or _sd.get("hdfMaximumAge", "40"))
        mb.add_field("hdfAgeCountDate", form_state.get("age_count_date") or _sd.get("hdfAgeCountDate", "6/15/2026 12:00:00 AM"))
        mb.add_field("txtCourseFee", form_state.get("fee") or _sd.get("txtCourseFee", "10000"))
        mb.add_field("vcerfvtxtCourseFee_ClientState", "")
        mb.add_field("hdfAgeCriteria", _sd.get("hdfAgeCriteria", "14"))

        mb.add_field("txtName", applicant["name"])
        mb.add_field("vcerfvtxtName_ClientState", "")
        mb.add_field("txtFatherName", applicant["fatherName"])
        mb.add_field("vcerfvtxtFatherName_ClientState", "")
        mb.add_field("txtNationality", applicant["nationality"])
        mb.add_field("vcerfvtxtNationality_ClientState", "")
        mb.add_field("txtDateOfBirth", applicant["dateOfBirth"])
        mb.add_field("vcerfvtxtDateOfBirth_ClientState", "")
        mb.add_field("vceregvtxtDateOfBirth_ClientState", "")

        mb.add_file("FupBirthCertificate", _resolve(docs.get("dobProof")))
        mb.add_field("hdfFupBirthCertificate", "Yes" if _resolve(docs.get("dobProof")) else "No")
        mb.add_field("vcerfvFupBirthCertificate_ClientState", "")
        mb.add_field("vceregvFupBirthCertificate_ClientState", "")

        mb.add_field("txtDomicile", applicant.get("domicile", ""))
        mb.add_file("FupDomicileAttachProof", _resolve(docs.get("domicileProof")))
        mb.add_field("hdfFupDomicileAttachProof", "Yes" if _resolve(docs.get("domicileProof")) else "No")
        mb.add_field("vceregvFupDomicileAttachProof_ClientState", "")

        mb.add_field("txtHouseNoApartment", address["houseNo"])
        mb.add_field("vcerfvtxtHouseNoApartment_ClientState", "")
        mb.add_field("txtStreetVillageColony", address["street"])
        mb.add_field("vcerfvtxtStreetVillageColony_ClientState", "")
        mb.add_field("txtDistrictCity", address["district"])
        mb.add_field("vcerfvtxtDistrictCity_ClientState", "")
        mb.add_field("txtPincode", address["pincode"])
        mb.add_field("vcerfvtxtPincode_ClientState", "")
        mb.add_field("txtMobileNo", mobile)
        mb.add_field("vcerfvtxtMobileNo_ClientState", "")
        mb.add_field("txtEmailId", applicant["email"])
        mb.add_field("vcerfvtxtEmailId_ClientState", "")
        mb.add_field("vcerevemailid_ClientState", "")

        edu_val = EDUCATION_VALUES.get(applicant.get("education", ""), "0")
        mb.add_field("ddlEduationQualification", edu_val)
        mb.add_field("vcerfvddlEduationQualification_ClientState", "")
        mb.add_file("FupEducationQualification", _resolve(docs.get("educationProof")))
        mb.add_field("hdfEducationQualification", "Yes" if _resolve(docs.get("educationProof")) else "No")
        mb.add_field("vcerfvFupEducationQualification_ClientState", "")
        mb.add_field("vceregvFupEducationQualification_ClientState", "")

        mb.add_file("FupAnyAdventureCoursePhoto",
                    _resolve(docs.get("skiingCertificate")) if form_state["level_val"] != "1" else None)
        mb.add_field("hdfAnyAdventureCoursePhoto",
                     "Yes" if (form_state["level_val"] != "1" and _resolve(docs.get("skiingCertificate"))) else "No")
        mb.add_field("vceregvFupAnyAdventureCoursePhoto_ClientState", "")

        mb.add_file("FupCurrentEmployment", None)
        mb.add_field("hdfCurrentEmployment", "No")
        mb.add_field("vceregvFupCurrentEmployment_ClientState", "")

        veg_val = VEG_VALUES.get(applicant.get("vegetarian", ""), "0")
        mb.add_field("ddlVegetarianOrNon", veg_val)
        mb.add_field("ValidatorCalloutExtender1_ClientState", "")
        mb.add_field("txtBloodGroup", applicant.get("bloodGroup", ""))
        mb.add_field("vcerfvtxtBloodGroup_ClientState", "")
        mb.add_field("txtMedicalCategory", applicant.get("medicalCategory", ""))
        mb.add_field("txtHeight", applicant.get("height", ""))
        mb.add_field("txtWeight", applicant.get("weight", ""))
        mb.add_field("txtWaist", applicant.get("waist", ""))
        mb.add_field("txtShoeSize", applicant.get("shoeSize", ""))

        mb.add_file("FupPhototOfApplicant", _resolve(docs.get("photo")))
        mb.add_field("hdfPhototOfApplicant", "Yes" if _resolve(docs.get("photo")) else "No")
        mb.add_field("vcerfvFupPhototOfApplicant_ClientState", "")
        mb.add_field("vceregvFupPhototOfApplicant_ClientState", "")

        mb.add_file("FupSignatureOfAppicant", _resolve(docs.get("signature")))
        mb.add_field("hdfFupSignatureOfAppicant", "Yes" if _resolve(docs.get("signature")) else "No")
        mb.add_field("vcerfvFupSignatureOfAppicant_ClientState", "")
        mb.add_field("vceregvFupSignatureOfAppicant_ClientState", "")

        mb.add_field("txtEmergencyContactName", applicant.get("emergencyContactName", ""))
        mb.add_field("vcerfvtxtEmergencyContactName_ClientState", "")
        mb.add_field("txtEmergencyContactPhone", applicant.get("emergencyContactPhone", ""))
        mb.add_field("vcerfvtxtEmergencyContactPhone_ClientState", "")

        mb.add_file("FupParentGuardianSignature", _resolve(docs.get("parentSignature")))
        mb.add_field("hdfParentGuardianSignature", "Yes" if _resolve(docs.get("parentSignature")) else "No")
        mb.add_field("vcerfvFupParentGuardianSignature_ClientState", "")
        mb.add_field("vceregvFupParentGuardianSignature_ClientState", "")

        mb.add_field("chkIAgree", "on")
        mb.add_field("btnSubmit", "Submit")

        body = mb.build()
        status, resp_body, headers, final_url = http_post(
            opener, f"{BASE}/admission_form.aspx?mobile={mobile}",
            body, mb.content_type(), timeout=60
        )
        return status, resp_body, headers, final_url

    status, resp_body, headers, final_url = retry(
        _build_and_submit,
        label=f"Step 4 (mobile={mobile})",
        max_retries=max_retries, base_delay=base_delay, max_delay=max_delay, timeout=60
    )

    log("INFO", f"Submit response: HTTP {status}, URL: {final_url}")
    body_lower = resp_body.lower()

    success_kw = ["successfully", "thank you", "submitted", "successful", "payment", "acknowledgement"]
    error_kw = ["error", "required", "invalid", "must be"]

    if any(kw in body_lower for kw in success_kw) or "admissionformsuccess" in str(final_url).lower():
        # Extract server message from URL or VIEWSTATE
        msg = ""
        parsed = urllib.parse.urlparse(str(final_url))
        params = urllib.parse.parse_qs(parsed.query)
        if "Resp" in params:
            msg = urllib.parse.unquote(params["Resp"][0])
        else:
            vm = re.search(r'Admission Form submitted successfully[^"]*', resp_body, re.I)
            if vm:
                msg = vm.group(0)

        log("INFO", "=" * 50)
        log("INFO", "FORM SUBMITTED SUCCESSFULLY!")
        if msg:
            log("INFO", f"Server: {msg}")
        log("INFO", "Check your email for the payment link.")
        log("INFO", "=" * 50)
        (SCRIPT_DIR / "submission-response.html").write_text(resp_body, errors="replace")
        return {"success": True, "mobile": mobile, "message": msg}

    if any(kw in body_lower for kw in error_kw):
        error_spans = re.findall(r'<span[^>]*id="[^"]*Validator"[^>]*>([^<]+)</span>', resp_body, re.I)
        errors = [re.sub(r'<[^>]+>', '', e).strip() for e in error_spans if e.strip()]
        log("ERROR", f"Validation errors: {'; '.join(errors) if errors else 'unknown'}")
        (SCRIPT_DIR / "error-response.html").write_text(resp_body, errors="replace")
        return {"success": False, "errors": errors, "mobile": mobile}

    log("WARN", f"Unexpected response ({len(resp_body)} bytes)")
    (SCRIPT_DIR / "unknown-response.html").write_text(resp_body, errors="replace")
    return {"success": False, "unknown": True, "mobile": mobile}

# ---------------------------------------------------------------------------
# One full attempt
# ---------------------------------------------------------------------------

def attempt_with_mobile(cfg, cache, mobile, max_retries, base_delay, max_delay, dry_run):
    opener, jar = make_opener()

    try:
        r1 = step1_verify_mobile(opener, cfg, mobile, max_retries, base_delay, max_delay)
        if r1 == ALREADY_REGISTERED:
            return {"success": False, "already_registered": True, "mobile": mobile}

        r2 = step2_accept_criteria(opener, cfg, mobile, max_retries, base_delay, max_delay)
        if r2 == ALREADY_REGISTERED:
            return {"success": False, "already_registered": True, "mobile": mobile}

        form_state = step3_select_course(opener, cfg, cache, mobile, max_retries, base_delay, max_delay)

        seats_str = form_state.get("seats", "0")
        seats = int(seats_str) if seats_str.isdigit() else -1
        if seats == 0 and not dry_run:
            log("WARN", f"0 seats available for mobile {mobile}")
            return {"success": False, "no_seats": True, "mobile": mobile}

        result = step4_submit_form(opener, cfg, mobile, form_state, max_retries, base_delay, max_delay)
        return result

    except Exception as e:
        log("ERROR", f"Attempt with mobile {mobile} failed: {e}")
        return {"success": False, "error": str(e), "mobile": mobile}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    cfg = load_config()
    validate_config(cfg)
    init_logger(cfg)

    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    reset_cache = "--reset-cache" in args

    target_time = None
    for a in args:
        if a.startswith("--target="):
            target_time = a.split("=", 1)[1]

    cli_max_retries = None
    for a in args:
        if a.startswith("--max-retries="):
            cli_max_retries = int(a.split("=", 1)[1])

    cli_wait = None
    for a in args:
        if a.startswith("--wait="):
            cli_wait = float(a.split("=", 1)[1])

    if dry_run:
        cfg.setdefault("bot", {})["dryRun"] = True
        log("INFO", "=== DRY RUN MODE — will not submit the form ===")

    max_retries = cli_max_retries or cfg.get("bot", {}).get("maxRetries", 5)
    base_delay = cli_wait or (cfg.get("bot", {}).get("retryDelayMs", 2000) / 1000)
    max_delay = cfg.get("bot", {}).get("maxRetryDelayMs", 30000) / 1000
    pipeline_max = cfg.get("bot", {}).get("pipelineMaxAttempts", 50)

    mobile_numbers = cfg["applicant"]["mobileNumbers"]

    log("INFO", f"=== IISM Admission Bot ===")
    log("INFO", f"Applicant: {cfg['applicant']['name']}")
    log("INFO", f"Email: {cfg['applicant']['email']}")
    log("INFO", f"Phone numbers: {mobile_numbers}")
    log("INFO", f"Max retries/step: {max_retries}, Base wait: {base_delay}s, Pipeline max: {pipeline_max}")

    # Load options cache
    cache = load_cache()
    if reset_cache:
        cache.pop("course_options", None)
        cache.pop("resolved", None)
        save_cache(cache)
        log("INFO", "Cache reset — will re-discover course options on next run")

    # Wait until target time
    if target_time:
        h, m = map(int, target_time.split(":"))
        now = datetime.now()
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        start_before = cfg.get("bot", {}).get("startBeforeTargetMs", 300000) / 1000
        wake_at = target - timedelta(seconds=start_before)
        if wake_at > now:
            wait_min = round((wake_at - now).total_seconds() / 60)
            log("INFO", f"Waiting {wait_min} min until {wake_at.strftime('%H:%M:%S')}...")
            time.sleep((wake_at - now).total_seconds())

    exhausted_numbers = set()

    for attempt in range(1, pipeline_max + 1):
        available = [m for m in mobile_numbers if m not in exhausted_numbers]
        if not available:
            log("ERROR", "All phone numbers exhausted (all already registered). Add more numbers to config.")
            sys.exit(1)

        idx = (attempt - 1) % len(available)
        mobile = available[idx]

        log("INFO", f"=== Pipeline attempt {attempt}/{pipeline_max} with mobile={mobile} ===")

        result = attempt_with_mobile(cfg, cache, mobile, max_retries, base_delay, max_delay, dry_run)

        if result.get("success"):
            log("INFO", "Pipeline completed successfully!")
            sys.exit(0)

        if result.get("already_registered"):
            exhausted_numbers.add(mobile)
            continue

        if result.get("no_seats"):
            log("WARN", "No seats — will retry")
        elif result.get("errors"):
            log("ERROR", f"Validation errors: {'; '.join(result['errors'])}")
            log("ERROR", "Check config.json values. Exiting.")
            sys.exit(1)
        else:
            log("WARN", "Attempt failed — will retry")

        if attempt < pipeline_max:
            delay = min(base_delay * (1.5 ** (attempt - 1)), max_delay)
            delay += delay * 0.25 * (random.random() * 2 - 1)
            log("INFO", f"Retrying pipeline in {delay:.1f}s...")
            time.sleep(delay)

    log("ERROR", "All pipeline attempts exhausted.")
    sys.exit(1)


if __name__ == "__main__":
    main()
