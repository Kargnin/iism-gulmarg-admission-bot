#!/usr/bin/env python3
"""
IISM Gulmarg admission bot (Python 3.8+, standard library only).

Books seats on https://admission.iismgulmarg.in by driving its ASP.NET
WebForms pages over plain HTTP - no browser needed. Built for booking day,
when the site is overloaded and keeps falling over:

  * Every configured preference has its own worker and primary phone number.
    Missing and full slots remain pending, and extra numbers are reserves.
  * Recoverable timeouts, 5xx errors, ASP.NET error pages and lost sessions
    retry with capped backoff; Ctrl+C cancels further requests.
  * Resumable - confirmed and unresolved outcomes are saved in an
    identity-isolated run directory.

Usage:
  python3 iism-bot.py --check        check config + documents, then probe the site if valid (sends no form data)
  python3 iism-bot.py --discover     list the live courses/batches using your first number (never submits)
  python3 iism-bot.py --dry-run      full run that stops right before each final submit
  python3 iism-bot.py                book (waits until shortly before the opening time)
  python3 iism-bot.py --now          book, starting immediately
  python3 iism-bot.py --config configs/friend.jsonc   use someone else's config
"""

import argparse
import hashlib
import html
import http.client
import http.cookiejar
import json
import math
import os
import queue
import random
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_BASE_URL = "https://admission.iismgulmarg.in"
IST = timezone(timedelta(hours=5, minutes=30), "IST")

INSTRUCTIONS_PAGE = "admission_instructions.aspx"
CRITERIA_PAGE = "admission_criteria.aspx"
FORM_PAGE = "admission_form.aspx"

# The site takes only these image types, at most 500 KB each.
UPLOAD_EXTS = (".jpg", ".jpeg", ".png")
MAX_UPLOAD_BYTES = 500 * 1000
CONVERTED_MAX_BYTES = 450 * 1000

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")

BOT_DEFAULTS = {
    "parallel": True,            # book for several numbers at the same time
    "notify": True,              # Mac notification + terminal bell on a booking
    "preventSleep": True,        # keep the Mac awake while the bot runs (caffeinate)
    "startEarlySec": 600,        # start looking this long before the published opening time
    "pollSec": 2,                # each unfinished preference checks for its current date slot
    "requestTimeoutSec": 45,
    "submitTimeoutSec": 120,
    "retryDelaySec": 1.0,        # first retry delay; grows with repeated failures ...
    "maxRetryDelaySec": 10.0,    # ... up to this
    "maxRejections": 3,          # site rejected the form this often for a choice -> move on
    "rejectedRetrySec": 5,       # pause after the site rejected the form
    # The form's own check asks for a parent/guardian signature even from adults. If the site
    # refuses an adult's form for that reason, resend with the applicant's signature in that slot.
    "parentSignatureFallback": True,
    "statusEverySec": 30,
}

BOT_BOOLEAN_OPTIONS = {"parallel", "notify", "preventSleep", "parentSignatureFallback"}
BOT_INTEGER_MINIMUMS = {"maxRejections": 1}
BOT_NUMBER_MINIMUMS = {
    "startEarlySec": (0, True),
    "pollSec": (0, False),
    "requestTimeoutSec": (0, False),
    "submitTimeoutSec": (0, False),
    "retryDelaySec": (0, False),
    "maxRetryDelaySec": (0, False),
    "rejectedRetrySec": (0, True),
    "statusEverySec": (0, False),
}


def private_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)
    return path


def private_file(path):
    path = Path(path)
    if os.name != "nt":
        path.chmod(0o600)
    return path


def private_descriptor(path, flags):
    descriptor = os.open(path, flags, 0o600)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor

CONFIG_ROOT_KEYS = {
    "applicant", "address", "phoneNumbers", "coursePriorities",
    "bookingOpensAt", "scheduleFile", "batches", "documentsDir", "documents", "bot",
}

# config value -> text box on admission_form.aspx
TEXT_FIELDS = [
    ("txtName", "applicant", "name"),
    ("txtFatherName", "applicant", "fatherName"),
    ("txtNationality", "applicant", "nationality"),
    ("txtDateOfBirth", "applicant", "dateOfBirth"),
    ("txtDomicile", "applicant", "domicile"),
    ("txtHouseNoApartment", "address", "houseNo"),
    ("txtStreetVillageColony", "address", "street"),
    ("txtDistrictCity", "address", "district"),
    ("txtPincode", "address", "pincode"),
    ("txtEmailId", "applicant", "email"),
    ("txtBloodGroup", "applicant", "bloodGroup"),
    ("txtMedicalCategory", "applicant", "medicalCategory"),
    ("txtHeight", "applicant", "height"),
    ("txtWeight", "applicant", "weight"),
    ("txtWaist", "applicant", "waist"),
    ("txtShoeSize", "applicant", "shoeSize"),
    ("txtEmergencyContactName", "applicant", "emergencyContactName"),
    ("txtEmergencyContactPhone", "applicant", "emergencyContactPhone"),
]
# The fields the form marks with * (state, mobile, course and fee come from elsewhere).
# Domicile, medical category, height, weight, waist and shoe size are optional.
REQUIRED_FIELDS = [
    ("applicant", "name"), ("applicant", "fatherName"), ("applicant", "nationality"),
    ("applicant", "dateOfBirth"), ("applicant", "email"), ("applicant", "education"), ("applicant", "diet"),
    ("applicant", "bloodGroup"), ("applicant", "emergencyContactName"), ("applicant", "emergencyContactPhone"),
    ("address", "houseNo"), ("address", "street"), ("address", "district"), ("address", "state"),
    ("address", "pincode"),
]

# config key, file input, "file attached" flag, required: True, False, or "minor" (under 18 only)
DOCUMENTS = [
    ("photo", "FupPhototOfApplicant", "hdfPhototOfApplicant", True),
    ("signature", "FupSignatureOfAppicant", "hdfFupSignatureOfAppicant", True),
    ("dobProof", "FupBirthCertificate", "hdfFupBirthCertificate", True),
    ("educationProof", "FupEducationQualification", "hdfEducationQualification", True),
    ("parentSignature", "FupParentGuardianSignature", "hdfParentGuardianSignature", "minor"),
    ("domicileProof", "FupDomicileAttachProof", "hdfFupDomicileAttachProof", False),
    ("skiingCertificate", "FupAnyAdventureCoursePhoto", "hdfAnyAdventureCoursePhoto", False),
    ("employmentProof", "FupCurrentEmployment", "hdfCurrentEmployment", False),
]

# ---------------------------------------------------------------------------
# What the site says. Matched (lower-case) against the server's messages:
# alert() popups, message labels and failed validators. Keep these specific -
# the instructions page itself contains "duplicate" and "successful
# submission", which must never count as a verdict.
# ---------------------------------------------------------------------------

PHONE_USED_PHRASES = [
    "already registered", "already been registered", "already exist", "already submitted",
    "already been submitted", "already applied", "already been used", "already in use", "already used",
    "already taken", "duplicate mobile", "number already",
]
PERMANENT_MOBILE_REFUSAL_PHRASES = [
    "enter valid mobile", "invalid mobile", "mobile no is invalid", "mobile number is invalid",
    "mobile no not valid", "mobile number not valid", "not a valid mobile",
]
SUCCESS_PHRASES = [
    "submitted successfully", "successfully submitted", "has been submitted", "application submitted",
    "form submitted", "registered successfully", "successfully registered", "saved successfully",
    "successfully saved",
]
OUTCOME_NEGATIVE_PHRASES = [
    "not successfully", "unsuccessful", "not submitted", "failed", "failure", "could not", "error",
    "invalid", "reject", "not accepted", "declined", "unable", "cannot confirm", "cannot verify",
    "not confirmed", "not verified",
]
OUTCOME_CONTRACTIONS = (
    ("ain't", "is not"), ("aren't", "are not"), ("can't", "cannot"), ("couldn't", "could not"),
    ("didn't", "did not"), ("doesn't", "does not"), ("don't", "do not"), ("hadn't", "had not"),
    ("hasn't", "has not"), ("haven't", "have not"), ("isn't", "is not"), ("shouldn't", "should not"),
    ("wasn't", "was not"), ("weren't", "were not"), ("won't", "will not"),
    ("wouldn't", "would not"),
)
AFFIRMATIVE_OUTCOME_PATTERNS = (
    r"(?:(?:the|your)\s+)?(?:admission\s+form|application|form)\s+"
    r"(?:(?:has|have)\s+been\s+|was\s+)?submitted\s+successfully",
    r"(?:(?:the|your)\s+)?(?:admission\s+form|application|form)\s+"
    r"(?:(?:has|have)\s+been\s+|was\s+)?successfully\s+submitted",
    r"(?:(?:the|your)\s+)?(?:admission\s+form|application|form)\s+(?:has|have)\s+been\s+submitted",
    r"(?:(?:the|your)\s+)?(?:admission\s+form|application|form)\s+submitted",
    r"(?:(?:the|your)\s+)?(?:application|form|registration)\s+"
    r"(?:(?:has|have)\s+been\s+|was\s+)?(?:registered|saved)\s+successfully",
    r"(?:(?:the|your)\s+)?(?:application|form|registration)\s+"
    r"(?:(?:has|have)\s+been\s+|was\s+)?successfully\s+(?:registered|saved)",
    r"(?:submitted\s+successfully|successfully\s+submitted|registered\s+successfully|"
    r"successfully\s+registered|saved\s+successfully|successfully\s+saved)",
)
AFFIRMATIVE_RECEIPT_SUFFIX = (
    r"(?:(?:,\s*|\.\s+)payment\s+(?:details|link)\s+(?:has|have)\s+been\s+sent"
    r"(?:\s+(?:to|on)\s+(?:your\s+)?(?:registered\s+)?(?:email|email\s+id))?)?\.?"
)
AGE_PHRASES = ["age allow", "not eligible", "age should", "age must", "age limit", "invalid date"]

# Words in the site's (or our own) message -> what in the config to look at. Most specific first.
FIX_HINTS = [
    (("address.state",), "address.state"), (("applicant.education",), "applicant.education"),
    (("applicant.diet",), "applicant.diet"),
    (("email",), "applicant.email"),
    (("upload date of birth",), "documents.dobProof"),
    (("dateofbirth", "date of birth", "dd/mm/yyyy", "your age", "age allow"), "applicant.dateOfBirth"),
    (("photo of applicant",), "documents.photo"),
    (("signature of applicant",), "documents.signature"),
    (("parent", "guardian"), "documents.parentSignature"),
    (("passbook",), "documents.educationProof"),            # (sic) the site's message for the education upload
    (("eduation qualification", "education qualification"), "applicant.education"),
    (("domicile",), "applicant.domicile / documents.domicileProof"),
    (("1mb", "file size", "files & size", "only ."), "the document files (JPG/PNG under 500 KB)"),
    (("father",), "applicant.fatherName"), (("enter name",), "applicant.name"),
    (("nationality",), "applicant.nationality"), (("house", "apartment"), "address.houseNo"),
    (("street", "village", "colony"), "address.street"), (("district",), "address.district"),
    (("pincode", "pin code"), "address.pincode"), (("mobile",), "phoneNumbers"),
    (("blood",), "applicant.bloodGroup"), (("emergency contact name",), "applicant.emergencyContactName"),
    (("emergency contact phone",), "applicant.emergencyContactPhone"), (("state",), "address.state"),
    (("vegetarian",), "applicant.diet"),
]
RESTART_HINT = ("then stop and rerun the same invocation, preserving its --config, --base-url, --dry-run "
                "and other safety flags (bookings made so far are kept)")


def fix_hint(message):
    said = message.lower()
    keys = []
    for words, key in FIX_HINTS:
        if has_any(said, words) and key not in keys:
            keys.append(key)
    if keys:
        return f"check {', '.join(keys)} in your config, {RESTART_HINT}"
    return f"read the message above (and the saved page in runs/), fix your config, {RESTART_HINT}"
FULL_PHRASES = [
    "no seat", "seats are full", "seat is full", "seats full", "seat full",
    "seat not available", "seats not available", "no vacanc", "vacancies are full",
    "course is full", "batch is full", "house full", "housefull",
]
CLOSED_PHRASES = [
    "registration closed", "registration is closed", "registrations are closed",
    "admission closed", "admission is closed", "admissions are closed",
    "not yet open", "will open", "will be opened", "will start",
]
VIEWSTATE_ERRORS = [
    "validation of viewstate mac failed", "invalid viewstate",
    "invalid postback or callback argument", "the state information is invalid",
]

KIND_PATTERNS = {  # course kind -> (words that identify it in the site's text, words that rule it out)
    "Snow Skiing": (r"ski", r"water|board|\bmoi\b|\bm o i\b|instruct"),
    "Snow Boarding": (r"board", r"water"),
    "MOI": (r"\bmoi\b|\bm o i\b|method of instruction|instructor", r"water"),
}
CONFIG_LEVEL_PATTERNS = {
    "Basic": r"\b(?:basic|beginner)\b",
    "Intermediate": r"\b(?:inter|intermediate|intermidiate)\b",
    "Advance": r"\b(?:adv|advance|advanced)\b",
}
LEVEL_PATTERNS = {"Basic": r"\bbasic|\bbeginner", "Intermediate": r"\binter",  # the site's text ("Intermidiate")
                  "Advance": r"\badvance"}                                      # ("adv" would match "Adventure")
GENERIC_LEVELS = ("none", "na", "n a", "nil", "general", "all", "not applicable")  # the real form has a level "None"
LOCATION_WORDS = {"gulmarg": ("gulmarg",), "kargil": ("kargil", "ladakh")}
STATE_ALIASES = [("odisha", "orissa"), ("puducherry", "pondicherry"), ("delhi", "nct of delhi", "new delhi"),
                 ("jammu and kashmir", "j and k", "jammu kashmir"), ("uttarakhand", "uttaranchal"),
                 ("chhattisgarh", "chattisgarh"), ("telangana", "telengana")]
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


class ConfigError(Exception):
    """Something in the config or documents needs fixing."""


class Transient(Exception):
    """Temporary trouble (network, 5xx, error page): retry the same step."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class RateLimited(Transient):
    """The final request was explicitly refused with HTTP 429 and is safe to retry."""


class Cancelled(Exception):
    """Stop requested; do not start another network operation."""


class Uncertain(Exception):
    """A final request may have committed; ask the user before another attempt."""


class Refused(Transient):
    """The site answered with a message instead of moving on - not a server error, so something
    we send may need fixing. Retried like Transient, but counted and announced."""

    def __init__(self, message, what):
        super().__init__(message)
        self.what = what  # "number": the mobile number was refused; "criteria": the terms page


class Resend(Transient):
    """The site refused the form for something we can fix ourselves: send it again."""


class SessionLost(Transient):
    """The site dropped our session or page state: verify the number again."""


class RedirectFailure(Transient):
    """A request failed after the server redirected it."""


class RedirectBlocked(ConfigError):
    """A redirect left the configured origin and was blocked before contact."""


class PhoneUsed(Exception):
    """The site says this number has already been used."""


class NotOpen(Exception):
    """Booking is not open yet, or nothing is listed yet."""


class TargetFull(Exception):
    """This choice shows no seats, or is not on the list."""


class Rejected(Exception):
    """The site refused immutable local data; this preference needs a restart after correction."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def norm(text):
    """Lower-case, '&' -> 'and', punctuation squashed to single spaces."""
    t = str(text or "").lower().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


def has_any(text, phrases):
    return any(p in text for p in phrases)


def mask(phone):
    return ".." + phone[-4:]


def parse_server_number(value, allow_decimal=False):
    text = str(value or "").strip()
    fraction = r"(?:\.\d+)?" if allow_decimal else ""
    if re.fullmatch(rf"(?:\d+|\d{{1,3}}(?:,\d{{3}})+){fraction}", text) is None:
        return None
    normalized = text.replace(",", "")
    return Decimal(normalized) if allow_decimal else int(normalized)


def now_ist():
    return datetime.now(IST)


def age_on(born, day):
    return day.year - born.year - ((day.month, day.day) < (born.month, born.day))


def observed_server_age(page, born):
    for message in page.messages():
        match = re.search(r"\byour\s+age\s+(\d+)\s+years?\b", message, re.I)
        if match:
            return int(match.group(1))
    raw = (page.value("hdfAgeCountDate") or "").strip()
    for pattern in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return age_on(born, datetime.strptime(raw, pattern).date())
        except ValueError:
            pass
    return None


def document_applies(key, target, dob):
    if key == "parentSignature":
        return True  # optional for adults when explicitly configured; required only for minors
    if key == "skiingCertificate":
        return target.kind == "Snow Skiing" and target.level in ("Intermediate", "Advance")
    return True


def fmt_when(moment):
    return moment.astimezone(IST).strftime("%a %d %b %Y %H:%M IST")


def fmt_duration(seconds):
    seconds = int(max(0, seconds))
    d, rest = divmod(seconds, 86400)
    h, rest = divmod(rest, 3600)
    m, s = divmod(rest, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s" if m else f"{s}s"


def clean_html(fragment):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def visible_text(body):
    body = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1\s*>", " ", body)
    body = re.sub(r"(?s)<!--.*?-->", " ", body)
    return clean_html(body)


def js_text(s):
    s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)
    s = re.sub(r"\\(.)", lambda m: {"n": " ", "r": " ", "t": " "}.get(m.group(1), m.group(1)), s)
    return clean_html(s)


def top_level_js(script):
    """Script text without the bodies of named functions (checkFileSize, ValidatorOnSubmit, ...),
    which only run when called. What is left runs on page load - that is where the
    server's startup alert() lands, in the same block as the validator code."""
    out, i = [], 0
    for m in re.finditer(r"\bfunction\s+[\w$]+\s*\([^)]*\)\s*\{", script):
        if m.start() < i:
            continue  # nested in a body we already skipped
        out.append(script[i:m.start()])
        depth, j, quote = 1, m.end(), None
        while j < len(script) and depth:
            c = script[j]
            if quote:
                if c == "\\":
                    j += 1
                elif c == quote:
                    quote = None
            elif script.startswith("//", j):
                j = script.find("\n", j)
                j = len(script) if j < 0 else j
                continue
            elif c in "'\"":
                quote = c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            j += 1
        i = j
    out.append(script[i:])
    return "".join(out)


def server_messages(body):
    """What the server is telling the user: startup alert() popups, message
    labels (lblMsg, lblDOBMsg, lblCheckError, ...) and validators it marked as failed."""
    found = []
    for script in re.findall(r"(?is)<script\b[^>]*>(.*?)</script\s*>", body):
        for _, text in re.findall(r"""alert\(\s*(['"])(.*?)(?<!\\)\1""", top_level_js(script), re.S):
            found.append(js_text(text))
    for attrs, inner in re.findall(r"(?is)<span\b([^>]*)>(.*?)</span\s*>", body):
        m = re.search(r'\bid\s*=\s*"([^"]+)"', attrs, re.I)
        sid = m.group(1).lower() if m else ""
        if sid.startswith("lbl") and has_any(sid, ("msg", "err", "message", "alert", "status", "result")):
            found.append(clean_html(inner))
    found.extend(failed_validator_messages(body))
    out = []
    for text in found:
        if text and text not in out:
            out.append(text)
    return out


def failed_validator_messages(body):
    found = []
    for vid in re.findall(r'(\w+)\.isvalid\s*=\s*"False"', body):
        match = re.search(re.escape(vid) + r'\.errormessage\s*=\s*"((?:[^"\\]|\\.)*)"', body)
        message = js_text(match.group(1)) if match else vid + " failed"
        if message not in found:
            found.append(message)
    return found


def real_options(options):
    """Drop placeholder entries such as 'Select', '--Select Course--' or value 0."""
    out = []
    for value, text in options or []:
        v, t = (value or "").strip(), norm(text)
        if v in ("", "0", "-1") or not t or t.startswith(("select", "choose", "please select")):
            continue
        out.append((value, text))
    return out


def listing(page, name):
    opts = real_options(page.options(name))
    return f" (the site lists: {' | '.join(t for _, t in opts)})" if opts else " (the list is empty)"


def first_values(page, *names):
    """First option of each dropdown - what a browser posts after the list was reloaded."""
    out = {}
    for name in names:
        opts = page.options(name)
        if opts:
            out[name] = opts[0][0]
    return out


# ---------------------------------------------------------------------------
# Dates and matching the site's dropdown texts
# ---------------------------------------------------------------------------

class DateReading:
    __slots__ = ("year", "month", "day", "reading", "span")

    def __init__(self, year, month, day, reading, span):
        self.year, self.month, self.day = year, month, day
        self.reading, self.span = reading, span


def dates_in(text):
    """Parsed date interpretations with the source span of each written date."""
    t = str(text or "").lower()
    out = []

    def add(y, m, d, how, span):
        if y is not None and y < 100:
            y += 2000
        if 1 <= m <= 12 and 1 <= d <= 31:
            out.append(DateReading(y, m, d, how, span))

    for match in re.finditer(r"(\d{1,4})[/.\-](\d{1,2})[/.\-](\d{2,4})", t):
        a, b, c = match.groups()
        if len(a) == 4:
            add(int(a), int(b), int(c), "iso", match.span())
        else:
            add(int(c), int(b), int(a), "dmy", match.span())
            add(int(c), int(a), int(b), "mdy", match.span())
    day_month = (r"\b(\d{1,2})(?:st|nd|rd|th)?[\s\-/.,]*([a-z]{3,9})\.?"
                 r"(?:[\s\-/.,']*(\d{4})\b|[\-/.'](\d{2})\b)?")
    for match in re.finditer(day_month, t):
        d, mon, y4, y2 = match.groups()
        if mon[:3] in MONTHS:
            add(int(y4 or y2) if (y4 or y2) else None, MONTHS[mon[:3]], int(d), "text", match.span())
    month_day = r"\b([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b,?(?:\s*(\d{4}))?"
    for match in re.finditer(month_day, t):
        mon, d, y = match.groups()
        if mon[:3] in MONTHS:
            add(int(y) if y else None, MONTHS[mon[:3]], int(d), "text", match.span())
    return out


def date_match(found, day):
    """2 = same date read day-first (or unambiguous), 1 = only month-first, 0 = no."""
    if day is None:
        return 0
    best = 0
    for reading in found:
        if ((reading.month, reading.day) == (day.month, day.day)
                and (reading.year is None or reading.year == day.year)):
            best = max(best, 1 if reading.reading == "mdy" else 2)
    return best


def parse_day(value):
    """'2027-01-03', '03/01/2027' or '3 Jan 2027' -> date (day-first for numbers)."""
    if not value:
        return None
    if isinstance(value, date):
        return value
    for reading in dates_in(value):
        if reading.reading != "mdy" and reading.year:
            try:
                return date(reading.year, reading.month, reading.day)
            except ValueError:
                pass
    return None


def configured_day(value, where, required=False):
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ConfigError(f"{where} is missing")
        return None
    parsed = parse_day(value)
    if parsed is None:
        raise ConfigError(f"{where} date {value!r} is unreadable")
    return parsed


def require_date_range(start, end, where):
    if end is not None and end < start:
        raise ConfigError(f"{where} ends before it starts")


def location_score(text, location):
    if not location:
        return 0
    t = norm(text)
    mine = LOCATION_WORDS.get(norm(location), (norm(location),))
    if has_any(t, mine):
        return 5
    others = [w for loc, words in LOCATION_WORDS.items() if loc != norm(location) for w in words]
    return -100 if has_any(t, others) else 0


def kind_score(text, kind):
    t = norm(text)
    include, exclude = KIND_PATTERNS[kind]
    if re.search(exclude, t) or not re.search(include, t):
        return 0
    return 10 + (2 if "snow" in t else 0)


def level_score(text, target):
    """+6 if the text names the wanted skiing level, -100 if it names only other levels
    (in case the site lists "Snow Skiing Basic", "... Intermediate" as separate courses)."""
    if target.kind != "Snow Skiing" or not target.level:
        return 0
    named = [level for level, pattern in LEVEL_PATTERNS.items() if re.search(pattern, norm(text))]
    if not named:
        return 0
    return 6 if target.level in named else -100


def selection_conflicts(course, level, batch, target, allow_ski_parent=False):
    texts = [item[1] for item in (course, level, batch) if item]
    if any(location_score(text, target.location) < 0 or level_score(text, target) < 0 for text in texts):
        return True
    if any(re.search(KIND_PATTERNS[target.kind][1], norm(text)) for text in texts):
        return True
    kinds = {kind for kind in KIND_PATTERNS if any(kind_score(text, kind) > 0 for text in texts)}
    if target.kind == "Snow Skiing":
        return bool(kinds.intersection(("Snow Boarding", "MOI")))
    named_ski_levels = {name for name, pattern in LEVEL_PATTERNS.items()
                        if any(re.search(pattern, norm(text)) for text in texts)}
    if named_ski_levels or any(kind != target.kind for kind in kinds if kind != "Snow Skiing"):
        return True
    if target.kind in kinds or not kinds:
        return False
    return "Snow Skiing" in kinds and not allow_ski_parent


def course_conflicts(text, target):
    return selection_conflicts(("", text), None, None, target, allow_ski_parent=True)


def course_candidates(options, target, hint=None):
    """Course dropdown entries that could hold this choice, most likely first."""
    real = real_options(options)
    fits = lambda text: not course_conflicts(text, target)
    ranked = []
    for n, (value, text) in enumerate(real):
        s = kind_score(text, target.kind)
        if s <= 0:
            continue
        s += location_score(text, target.location) + level_score(text, target)
        if date_match(dates_in(text), target.start):
            s += 8
        if s > 0:
            ranked.append((-s, n, (value, text)))
    included = {option for _, _, option in ranked}
    if target.kind != "Snow Skiing":
        for n, option in enumerate(real):
            if option not in included and kind_score(option[1], "Snow Skiing") > 0 and fits(option[1]):
                ranked.append((0, n, option))
                included.add(option)
    exclude = KIND_PATTERNS[target.kind][1]
    for n, option in enumerate(real):
        if option not in included and not re.search(exclude, norm(option[1])) and fits(option[1]):
            ranked.append((1, n, option))
            included.add(option)
    configured = norm(target.course_text)
    return [option for _, _, option in sorted(
        ranked, key=lambda item: (item[0], bool(configured and configured not in norm(item[2][1])),
                                  bool(hint and item[2][0] != hint), item[1]))]


def level_candidates(options, target, loose):
    real = real_options(options)
    if target.level:
        picks = [o for o in real if re.search(LEVEL_PATTERNS[target.level], norm(o[1]))]
    else:
        picks = [o for o in real if kind_score(o[1], target.kind) > 0]
        if not picks and loose:
            picks = list(real)
    if not picks and real and all(norm(t) in GENERIC_LEVELS for _, t in real):
        picks = list(real)
    if target.level_text:
        picks.sort(key=lambda o: norm(target.level_text) not in norm(o[1]))
    return picks


def match_level(options, target, loose):
    picks = level_candidates(options, target, loose)
    return picks[0] if picks else None


def state_aliases(name):
    n = norm(name)
    return next(([a for a in group if a != n] for group in STATE_ALIASES if n in group), [])


def batch_candidates(options, target):
    ranked = []
    for index, (value, text) in enumerate(real_options(options)):
        found = dates_in(text)
        start = date_match(found, target.start)
        if not start:
            continue
        if target.end and len({reading.span for reading in found}) >= 2 and not date_match(found, target.end):
            continue
        location = location_score(text, target.location)
        level = level_score(text, target)
        if location < 0 or level < 0:
            continue
        score = (10 * start + 5 * date_match(found, target.end) + location + level
                 + (100 if target.batch_text and norm(target.batch_text) in norm(text) else 0))
        if score > 0:
            ranked.append((-score, index, (value, text)))
    return [option for _, _, option in sorted(ranked)]


def match_batch(options, target):
    choices = batch_candidates(options, target)
    return choices[0] if choices else None


def has_semantic_evidence(course, level, batch, target):
    texts = [course[1], batch[1]] + ([level[1]] if level else [])
    if any(location_score(text, target.location) > 0 for text in texts):
        return True
    if any(kind_score(text, target.kind) > 0 for text in texts):
        return True
    return target.kind == "Snow Skiing" and any(level_score(text, target) > 0 for text in texts)


def is_season_batch(text, batches):
    """Whether a dropdown text is one of this season's batches (by its start date)."""
    found = dates_in(text)
    return any(date_match(found, b["from"]) > 0 for b in batches.values())


def choice_similarity(wanted, candidate):
    wanted, candidate = norm(wanted), norm(candidate)
    if not wanted or not candidate:
        return 0.0
    wanted_tokens, candidate_tokens = wanted.split(), candidate.split()
    matched = set()
    token_scores = []
    for wanted_token in wanted_tokens:
        choices = []
        for index, candidate_token in enumerate(candidate_tokens):
            if wanted_token == candidate_token:
                score = 1.0
            elif wanted_token.startswith(candidate_token) or candidate_token.startswith(wanted_token):
                score = 0.92
            else:
                score = SequenceMatcher(None, wanted_token, candidate_token).ratio()
                if score < 0.75:
                    score = 0.0
            choices.append((score, index))
        score, index = max(choices, default=(0.0, -1))
        token_scores.append(score)
        if score and index >= 0:
            matched.add(index)
    coverage = sum(token_scores) / len(wanted_tokens)
    precision = len(matched) / len(candidate_tokens)
    token_score = 0.6 * coverage + 0.4 * precision
    compact_wanted, compact_candidate = wanted.replace(" ", ""), candidate.replace(" ", "")
    prefix_score = 0.0
    if min(len(compact_wanted), len(compact_candidate)) >= 4 and (
            compact_wanted.startswith(compact_candidate) or compact_candidate.startswith(compact_wanted)):
        prefix_score = 0.9 + 0.05 * min(len(compact_wanted), len(compact_candidate)) / max(
            len(compact_wanted), len(compact_candidate))
    return max(token_score, prefix_score, SequenceMatcher(None, wanted, candidate).ratio(),
               SequenceMatcher(None, compact_wanted, compact_candidate).ratio())


def choice_acronym(value):
    tokens = [token for token in norm(value).split() if token not in ("and", "of", "the")]
    return "".join(token[0] for token in tokens)


def acronym_choices(options, wanted):
    key = "".join(norm(wanted).split())
    if not 2 <= len(key) <= 5 or not key.isalpha():
        return []
    return [(value, text) for value, text in real_options(options) if choice_acronym(text) == key]


def choice_suggestions(options, wanted):
    acronym = acronym_choices(options, wanted)
    if acronym:
        return [text for _, text in acronym]
    ranked = sorted(((choice_similarity(wanted, text), text) for _, text in real_options(options)), reverse=True)
    if not ranked or ranked[0][0] < 0.5:
        return []
    threshold = max(0.5, ranked[0][0] - 0.08)
    return [text for score, text in ranked if score >= threshold][:3]


def match_choice(options, wanted, aliases=()):
    """Resolve a configured answer to one plausible dropdown entry."""
    real = real_options(options)
    wanted_key = norm(wanted)
    if not real or not wanted_key:
        return None
    exact = [(value, text) for value, text in real if wanted_key in (norm(text), norm(value))]
    if exact:
        return exact[0] if len(exact) == 1 else None
    alias_keys = {norm(alias) for alias in aliases if norm(alias)}
    exact_aliases = [(value, text) for value, text in real
                     if alias_keys.intersection((norm(text), norm(value)))]
    if exact_aliases:
        return exact_aliases[0] if len(exact_aliases) == 1 else None
    acronym = acronym_choices(real, wanted_key)
    if acronym:
        return acronym[0] if len(acronym) == 1 else None
    ranked = sorted(((choice_similarity(wanted_key, text), index, (value, text))
                     for index, (value, text) in enumerate(real)), reverse=True)
    if not ranked or ranked[0][0] < 0.78:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.01:
        return None
    return ranked[0][2]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def unique_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key {key!r}")
        value[key] = item
    return value


def strict_json_loads(text):
    return json.loads(text, object_pairs_hook=unique_json_object)


def load_jsonc(path):
    """JSON that may contain // and /* */ comments and trailing commas."""
    text = Path(path).read_text(encoding="utf-8-sig")
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    cleaned = "".join(out)
    # drop trailing commas outside strings
    result, in_str, i = [], False, 0
    while i < len(cleaned):
        c = cleaned[i]
        if in_str:
            if c == "\\":
                result.append(cleaned[i:i + 2])
                i += 2
                continue
            in_str = c != '"'
        elif c == '"':
            in_str = True
        elif c == ",":
            j = i + 1
            while j < len(cleaned) and cleaned[j] in " \t\r\n":
                j += 1
            if j < len(cleaned) and cleaned[j] in "}]":
                i += 1
                continue
        result.append(c)
        i += 1
    try:
        return strict_json_loads("".join(result))
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path}: {e.msg} at line {e.lineno}, column {e.colno}")
    except ValueError as e:
        raise ConfigError(f"{path}: {e}")


def find_placeholders(obj, where=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not str(k).startswith("_"):
                yield from find_placeholders(v, f"{where}.{k}" if where else str(k))
    elif isinstance(obj, list):
        for n, v in enumerate(obj):
            yield from find_placeholders(v, f"{where}[{n}]")
    elif isinstance(obj, str) and "FILL_ME" in obj:
        yield where


def resolve_path(value, base):
    p = Path(os.path.expanduser(str(value)))
    return p if p.is_absolute() else Path(base) / p


class Target:
    """One entry of the priority list, e.g. 'G2 Basic'."""

    def __init__(self, index, label, batch_id, location, start, end, kind, level,
                 course_text="", level_text="", batch_text=""):
        self.index, self.label, self.batch_id = index, label, batch_id
        self.location, self.start, self.end = location, start, end
        self.kind, self.level = kind, level
        self.course_text, self.level_text, self.batch_text = course_text, level_text, batch_text

    def when(self):
        end = self.end.strftime(" - %d %b %Y") if self.end else ""
        return f"{self.location} {self.start.strftime('%d %b')}{end}".strip()

    def __str__(self):
        return self.label


def course_kind(text):
    t = norm(text)
    found = []
    if re.search(r"\bsnow\s*board(?:ing)?\b|\bboard(?:ing)?\b", t):
        found.append("Snow Boarding")
    if re.search(KIND_PATTERNS["MOI"][0], t):
        found.append("MOI")
    if re.search(r"\bski(?:ing)?\b", t):
        found.append("Snow Skiing")
    if len(found) > 1:
        raise ConfigError(f"course {text!r} names more than one course kind")
    return found[0] if found else None


def configured_levels(text):
    t = norm(text)
    return [level for level, pattern in CONFIG_LEVEL_PATTERNS.items() if re.search(pattern, t)]


def parse_priority(item, index, batches):
    text_priority = isinstance(item, str)
    if isinstance(item, str):
        words = item.replace(",", " ").split()
        if not words:
            raise ConfigError("coursePriorities has an empty entry")
        rest = " ".join(words[1:])
        spec = {"batch": words[0], "course": rest, "level": rest}
    elif isinstance(item, dict):
        spec = item
    else:
        raise ConfigError(f"coursePriorities entry {item!r} should be text like \"G2 Basic\"")
    bid = str(spec.get("batch") or "").strip().upper()
    batch = batches.get(bid) if bid else None
    if bid and batch is None:
        raise ConfigError(f"coursePriorities: unknown batch \"{bid}\" in {item!r} "
                          f"(batches: {', '.join(batches)})")
    batch = batch or {}
    start = configured_day(spec.get("from"), f"coursePriorities[{index}].from") or batch.get("from")
    if start is None:
        raise ConfigError(f"coursePriorities: {item!r} needs a batch code (G1..G6, K1, K2) or a \"from\" date")
    course_value = str(spec.get("course") or "").strip()
    level_value = str(spec.get("level") or "").strip()
    kind = course_kind(course_value)
    levels = configured_levels(level_value)
    if len(levels) > 1:
        raise ConfigError(f"coursePriorities[{index}].level {level_value!r} is ambiguous")
    level = levels[0] if levels else None
    if kind is None:
        if not course_value:
            kind = "Snow Skiing"
        elif text_priority and level is not None and norm(course_value) == norm(level_value):
            kind = "Snow Skiing"
        else:
            raise ConfigError(f"coursePriorities[{index}].course {course_value!r} is not a recognized course kind")
    if level_value and level is None and not (text_priority and norm(course_value) == norm(level_value)):
        raise ConfigError(f"coursePriorities[{index}].level {level_value!r} is not Basic, Intermediate or Advance")
    if kind != "Snow Skiing" and level is not None:
        raise ConfigError(f"coursePriorities[{index}] combines {kind} with skiing level {level}")
    if kind == "Snow Skiing" and level is None:
        hinted_levels = configured_levels(spec.get("levelText") or "")
        level = hinted_levels[0] if len(hinted_levels) == 1 else "Basic"
    name = bid or start.strftime("%d%b")
    label = spec.get("label") or f"{name} {level if kind == 'Snow Skiing' else kind}"
    end = configured_day(spec.get("to"), f"coursePriorities[{index}].to") or batch.get("to")
    require_date_range(start, end, f"coursePriorities[{index}]")
    return Target(index, label, bid, spec.get("location") or batch.get("location", ""), start,
                  end, kind, level,
                  spec.get("courseText", ""), spec.get("levelText", ""), spec.get("batchText", ""))


def parse_phone(raw):
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def validate_batch_records(records, where):
    if not isinstance(records, dict):
        raise ConfigError(f"{where} must be an object")
    for bid, record in records.items():
        if not isinstance(bid, str) or not bid.strip():
            raise ConfigError(f"{where} has an invalid batch code {bid!r}")
        if not isinstance(record, dict):
            raise ConfigError(f"{where}.{bid} must be an object")
        known = {"from", "to", "location", "offers", "seats"}
        for key, value in record.items():
            if key not in known:
                raise ConfigError(f"{where}.{bid}.{key} is not supported")
            if key != "offers" and not isinstance(value, str):
                raise ConfigError(f"{where}.{bid}.{key} must be text")
        offers = record.get("offers", [])
        if not isinstance(offers, list) or any(not isinstance(item, str) for item in offers):
            raise ConfigError(f"{where}.{bid}.offers must be a list of course names")
    return records


def validate_config_structure(cfg):
    if not isinstance(cfg, dict):
        raise ConfigError("config root must be an object")
    errors = []
    unknown = sorted((key for key in cfg if key not in CONFIG_ROOT_KEYS), key=str)
    if unknown:
        errors.append("config has unknown option(s): " + ", ".join(map(str, unknown)))
        if "maxBookings" in unknown:
            errors.append("maxBookings was removed: every configured preference is booked; remove that key")
    for key in ("applicant", "address", "documents", "bot", "batches"):
        if key in cfg and not isinstance(cfg[key], dict):
            errors.append(f"{key} must be an object")
    for key in ("phoneNumbers", "coursePriorities"):
        if key in cfg and not isinstance(cfg[key], list):
            errors.append(f"{key} must be a list")
    for key in ("bookingOpensAt", "scheduleFile", "documentsDir"):
        if key in cfg and not isinstance(cfg[key], str):
            errors.append(f"{key} must be text")
    allowed = {
        "applicant": {key for _, section, key in TEXT_FIELDS if section == "applicant"} | {"education", "diet"},
        "address": {key for _, section, key in TEXT_FIELDS if section == "address"} | {"state"},
        "documents": {key for key, _, _, _ in DOCUMENTS},
    }
    for section, keys in allowed.items():
        values = cfg.get(section, {})
        if not isinstance(values, dict):
            continue
        for key, value in values.items():
            if key not in keys:
                errors.append(f"{section}.{key} is not a supported config key")
            if not isinstance(value, str):
                errors.append(f"{section}.{key} must be text")
    if isinstance(cfg.get("phoneNumbers"), list):
        for index, phone in enumerate(cfg["phoneNumbers"]):
            if type(phone) not in (str, int):
                errors.append(f"phoneNumbers[{index}] must be a phone number in text or integer form")
    priority_keys = {"batch", "course", "level", "from", "to", "location", "label", "courseText", "levelText", "batchText"}
    if isinstance(cfg.get("coursePriorities"), list):
        for index, item in enumerate(cfg["coursePriorities"]):
            if isinstance(item, dict):
                for key, value in item.items():
                    if key not in priority_keys or not isinstance(value, str):
                        errors.append(f"coursePriorities[{index}].{key} must be a supported text field")
            elif not isinstance(item, str):
                errors.append(f"coursePriorities[{index}] must be text or a preference object")
    if isinstance(cfg.get("batches"), dict):
        try:
            validate_batch_records(cfg["batches"], "batches")
        except ConfigError as exc:
            errors.append(str(exc))
    if errors:
        raise ConfigError("Startup preflight found:\n  - " + "\n  - ".join(errors))


def load_season(cfg, config_dir):
    path = resolve_path(cfg["scheduleFile"], config_dir) if cfg.get("scheduleFile") else SCRIPT_DIR / "schedule.json"
    if cfg.get("scheduleFile") and not path.is_file():
        raise ConfigError(f"scheduleFile not found: {path}")
    data = load_jsonc(path) if path.exists() else {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: schedule root must be an object")
    batches = {}
    scheduled = data["batches"] if "batches" in data else {}
    configured = cfg["batches"] if "batches" in cfg else {}
    merged = dict(validate_batch_records(scheduled, f"{path}: batches"))
    merged.update(validate_batch_records(configured, "batches"))
    for bid, b in merged.items():
        source = "batches" if bid in configured else f"{path}: batches"
        start = configured_day(b.get("from"), f"{source}.{bid}.from", required=True)
        end = configured_day(b.get("to"), f"{source}.{bid}.to")
        require_date_range(start, end, f"{source}.{bid}")
        batches[bid.upper()] = {"location": b.get("location", ""), "from": start,
                                "to": end, "offers": b.get("offers") or [],
                                "seats": b.get("seats", "")}
    opens = cfg.get("bookingOpensAt") or data.get("bookingOpensAt")
    opens_at = None
    if opens:
        try:
            opens_at = datetime.fromisoformat(str(opens))
        except ValueError:
            raise ConfigError(f"bookingOpensAt \"{opens}\" should look like 2026-11-12T10:00:00+05:30")
        if opens_at.tzinfo is None:
            opens_at = opens_at.replace(tzinfo=IST)
    return batches, opens_at, data.get("season", "")


class Settings:
    """Everything the bot needs from one person's config, checked."""


def validate_tuning(cfg, errors):
    opt = dict(BOT_DEFAULTS)
    raw_opt = cfg.get("bot", {})
    unknown = sorted((key for key in raw_opt if key not in BOT_DEFAULTS), key=str)
    if unknown:
        errors.append("bot has unknown option(s): " + ", ".join(map(str, unknown)))

    for key, value in raw_opt.items():
        if key not in BOT_DEFAULTS:
            continue
        if key in BOT_BOOLEAN_OPTIONS:
            if type(value) is not bool:
                errors.append(f"bot.{key} must be true or false")
                continue
        elif key in BOT_INTEGER_MINIMUMS:
            minimum = BOT_INTEGER_MINIMUMS[key]
            if type(value) is not int or value < minimum:
                errors.append(f"bot.{key} must be an integer of {minimum} or greater")
                continue
        else:
            minimum, inclusive = BOT_NUMBER_MINIMUMS[key]
            is_number = not isinstance(value, bool) and isinstance(value, (int, float))
            is_finite = is_number and (not isinstance(value, float) or math.isfinite(value))
            in_range = is_finite and (value >= minimum if inclusive else value > minimum)
            if not in_range or value > threading.TIMEOUT_MAX:
                comparison = "at least" if inclusive else "greater than"
                errors.append(f"bot.{key} must be a finite number {comparison} {minimum} "
                              f"and no greater than {threading.TIMEOUT_MAX}")
                continue
        opt[key] = value

    if opt["maxRetryDelaySec"] < opt["retryDelaySec"]:
        errors.append("bot.maxRetryDelaySec must be at least bot.retryDelaySec")
    if opt["maxRetryDelaySec"] > 10 or opt["retryDelaySec"] > 10:
        errors.append("bot retry delays must be at most 10 seconds")
    return opt


def parse_settings(cfg, config_path):
    validate_config_structure(cfg)
    errors, warnings = [], []
    s = Settings()
    s.config_path = Path(config_path) if config_path else None
    config_dir = s.config_path.parent if s.config_path else Path.cwd()
    s.batches, s.opens_at, s.season = load_season(cfg, config_dir)
    a = dict(cfg.get("applicant") or {})
    addr = cfg.get("address") or {}
    sections = {"applicant": a, "address": addr}

    placeholders = list(find_placeholders(cfg))
    for where in placeholders:
        errors.append(f"{where} is still FILL_ME")
    for section, key in REQUIRED_FIELDS:
        where = f"{section}.{key}"
        if not str(sections[section].get(key) or "").strip() and where not in placeholders:
            errors.append(f"{where} is empty")

    s.applicant = {k: str(v).strip() for k, v in a.items() if not str(k).startswith("_")}
    s.form_values = {field: str(sections[sec].get(key) or "").strip() for field, sec, key in TEXT_FIELDS}
    s.state = str(addr.get("state") or "").strip()
    s.education = str(a.get("education") or "").strip()
    s.diet = str(a.get("diet") or "").strip()

    s.dob = None
    dob_text = s.applicant.get("dateOfBirth", "")
    if dob_text and "FILL_ME" not in dob_text:
        m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", dob_text)
        try:
            s.dob = date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None
        except ValueError:
            s.dob = None
        if s.dob is None:
            errors.append(f"applicant.dateOfBirth \"{dob_text}\" must be dd/mm/yyyy, e.g. 15/05/1998")
    rules = load_jsonc(SCRIPT_DIR / "form-rules.json")
    email = s.applicant.get("email", "")
    if email and "FILL_ME" not in email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        errors.append(f"applicant.email \"{email}\" does not look like an email address")
    pin = str(addr.get("pincode") or "")
    if pin and "FILL_ME" not in pin and not re.fullmatch(r"\d{6}", pin.strip()):
        errors.append(f"address.pincode \"{pin}\" must be 6 digits")
    if email and "FILL_ME" not in email and not re.fullmatch(rules["emailPattern"], email):
        errors.append("applicant.email does not satisfy the current form's email validator")
    if s.dob and s.dob > now_ist().date():
        errors.append("applicant.dateOfBirth is in the future")
    emergency = a.get("emergencyContactPhone", "")
    if emergency and "FILL_ME" not in emergency and not re.fullmatch(r"\d{10}", emergency):
        errors.append("applicant.emergencyContactPhone must be 10 digits")
    for field, value in s.form_values.items():
        if len(value) > rules["maxLengths"].get(field, sys.maxsize):
            errors.append(f"{field} exceeds the live form's {rules['maxLengths'][field]} character limit")
    for field, key, attribute, aliases in (
            ("ddlState", "address.state", "state", state_aliases(s.state)),
            ("ddlEduationQualification", "applicant.education", "education", ()),
            ("ddlVegetarianOrNon", "applicant.diet", "diet", ())):
        wanted = getattr(s, attribute)
        if wanted and "FILL_ME" not in wanted:
            match = match_choice(rules["dropdowns"][field], wanted, aliases)
            if match is None:
                choices = ", ".join(text for _, text in real_options(rules["dropdowns"][field]))
                suggestions = choice_suggestions(rules["dropdowns"][field], wanted)
                hint = f"; possible matches: {' | '.join(suggestions)}" if suggestions else ""
                errors.append(f'{key} "{wanted}" cannot be reasonably resolved to one current form choice; '
                              f"accepted choices: {choices}{hint}")
            else:
                setattr(s, attribute, match[1])
                if attribute in ("education", "diet"):
                    s.applicant[attribute] = match[1]

    raw_phones = cfg.get("phoneNumbers") or []
    s.phones = []
    for raw in raw_phones:
        if "FILL_ME" in str(raw):
            continue
        phone = parse_phone(raw)
        if phone is None:
            errors.append(f"phone number \"{raw}\" is not a 10-digit Indian mobile number")
        elif phone in s.phones:
            errors.append(f"phone number {raw} is listed twice; primary and reserve numbers must be unique")
        else:
            s.phones.append(phone)
    if not s.phones and not any(p.startswith("phoneNumbers") for p in placeholders):
        errors.append("phoneNumbers is empty - add at least one mobile number")

    s.targets = []
    for item in cfg.get("coursePriorities") or []:
        if isinstance(item, str) and "FILL_ME" in item:
            continue
        try:
            t = parse_priority(item, len(s.targets), s.batches)
        except ConfigError as e:
            errors.append(str(e))
            continue
        if any(o.label == t.label or target_key(o) == target_key(t) for o in s.targets):
            errors.append(f"coursePriorities duplicates a label or date/course preference: {t.label}")
            continue
        offers = (s.batches.get(t.batch_id) or {}).get("offers") or []
        if offers and t.kind not in offers:
            warnings.append(f"{t.label}: the published schedule has no {t.kind} seats in batch {t.batch_id}")
        s.targets.append(t)
    if not s.targets and not any(p.startswith("coursePriorities") for p in placeholders):
        errors.append("coursePriorities is empty - list your choices, e.g. [\"G2 Basic\", \"G3 Basic\"]")
    if (any(document_applies("skiingCertificate", t, s.dob) for t in s.targets)
            and not cfg.get("documents", {}).get("skiingCertificate")):
        warnings.append("Intermediate/Advance choices need your previous course certificate in documents.skiingCertificate")
    if any(target.level in ("Intermediate", "Advance") for target in s.targets):
        warnings.append("Intermediate/Advance courses are not supported or validated yet; Basic skiing is the supported path")

    s.opt = validate_tuning(cfg, errors)
    if len(s.phones) < len(s.targets):
        errors.append("phoneNumbers needs one primary number per course preference; additional numbers are reserves")
    if s.opens_at is None:
        warnings.append("no booking opening time configured (bookingOpensAt) - the bot starts right away")

    docs_dir = cfg.get("documentsDir") or "."
    s.docs_dir = resolve_path(docs_dir, config_dir)
    s.doc_sources, s.doc_placeholders = {}, set()
    for key, _, _, _ in DOCUMENTS:
        raw = str((cfg.get("documents") or {}).get(key) or "").strip()
        if "FILL_ME" in raw:
            s.doc_placeholders.add(key)
        elif raw:
            s.doc_sources[key] = resolve_path(raw, s.docs_dir)
    s.docs = {}
    return s, errors, warnings


# ---------------------------------------------------------------------------
# Documents: the site wants JPG/PNG under 500 KB
# ---------------------------------------------------------------------------

def can_convert():
    return sys.platform == "darwin" and shutil.which("sips") is not None


def image_size(path):
    out = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
                         capture_output=True, text=True, timeout=60).stdout
    nums = [int(n) for n in re.findall(r"pixel(?:Width|Height):\s*(\d+)", out)]
    return max(nums) if nums else 2000


def convert_to_jpeg(src, dst):
    """macOS only: first page of a PDF / any image -> JPEG under CONVERTED_MAX_BYTES."""
    private_dir(dst.parent)
    with tempfile.TemporaryDirectory() as tmp:
        image = src
        if src.suffix.lower() == ".pdf" and shutil.which("qlmanage"):
            # Quick Look renders the first page sharply; sips alone only manages 72 dpi
            subprocess.run(["qlmanage", "-t", "-s", "2000", "-o", tmp, str(src)],
                           capture_output=True, timeout=120)
            pngs = sorted(Path(tmp).glob("*.png"))
            if pngs:
                image = pngs[0]
        largest = image_size(image)
        for max_px, quality in [(2000, 85), (1800, 75), (1600, 70), (1400, 60), (1200, 55), (1000, 50), (800, 45)]:
            cmd = ["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(quality)]
            if largest > max_px:
                cmd += ["-Z", str(max_px)]
            result = subprocess.run(cmd + [str(image), "--out", str(dst)], capture_output=True, text=True, timeout=120)
            if result.returncode != 0 or not dst.exists():
                raise ConfigError(f"could not convert {src.name}: {(result.stderr or result.stdout).strip()[:200]}")
            if dst.stat().st_size <= CONVERTED_MAX_BYTES:
                return dst
    raise ConfigError(f"could not get {src.name} under 500 KB - please shrink it by hand")


def prepare_documents(settings, out_dir):
    """Fill settings.docs with upload-ready files. Returns (problems, notes)."""
    problems, notes = [], []
    settings.docs = {}
    preparation_id = uuid.uuid4().hex
    for key, _, _, required in DOCUMENTS:
        applies = any(document_applies(key, target, settings.dob) for target in settings.targets)
        if key in ("parentSignature", "skiingCertificate") and not applies:
            continue
        src = settings.doc_sources.get(key)
        if src is None:
            if key in settings.doc_placeholders:  # reported already
                continue
            if required is True:
                problems.append(f"documents.{key} is not set (the form requires it)")
            elif required == "minor" and any(settings.dob and age_on(settings.dob, t.start) < 18
                                             for t in settings.targets):
                problems.append(f"documents.{key} is not set (needed because the applicant is under 18)")
            continue
        if not src.is_file():
            problems.append(f"documents.{key}: file not found: {src}")
            continue
        size = src.stat().st_size
        if src.suffix.lower() in UPLOAD_EXTS and size <= MAX_UPLOAD_BYTES:
            settings.docs[key] = src
            notes.append(f"{key}: OK - {src.name} ({size // 1000} KB)")
            continue
        why = "is a PDF" if src.suffix.lower() == ".pdf" else (
            f"is {size // 1000} KB" if src.suffix.lower() in UPLOAD_EXTS else f"is a {src.suffix} file")
        if not can_convert():
            problems.append(f"documents.{key}: {src.name} {why}; the site needs JPG/PNG under 500 KB - "
                            "convert it (e.g. export as JPG) and point the config at the new file")
            continue
        dst = out_dir / "upload-ready" / f"{key}-{preparation_id}.jpg"
        pending = out_dir / "upload-ready" / f".{key}-{preparation_id}.jpg.tmp"
        try:
            private_dir(out_dir)
            private_dir(dst.parent)
            convert_to_jpeg(src, pending)
            private_file(pending)
            os.replace(pending, dst)
            private_file(dst)
        except ConfigError as e:
            problems.append(f"documents.{key}: {e}")
            continue
        except (OSError, subprocess.SubprocessError) as e:
            dst.unlink(missing_ok=True)
            problems.append(f"documents.{key}: converting {src.name} failed: {e}")
            continue
        finally:
            try:
                pending.unlink()
            except FileNotFoundError:
                pass
        settings.docs[key] = dst
        page_note = " (first page)" if src.suffix.lower() == ".pdf" else ""
        notes.append(f"{key}: {src.name} {why} -> converted{page_note} to {dst} ({dst.stat().st_size // 1000} KB)")
    prepared = {}
    for key, path in settings.docs.items():
        try:
            upload = Upload(path)
            is_png = upload.content.startswith(b"\x89PNG\r\n\x1a\n")
            is_jpeg = upload.content.startswith(b"\xff\xd8\xff")
            if not upload.content or len(upload.content) > MAX_UPLOAD_BYTES:
                problems.append(f"documents.{key}: upload must be nonempty and at most 500 KB")
            elif (upload.mime == "image/png" and not is_png) or (upload.mime == "image/jpeg" and not is_jpeg):
                problems.append(f"documents.{key}: file content does not match its JPG/PNG extension")
            else:
                prepared[key] = upload
        except OSError as exc:
            problems.append(f"documents.{key}: cannot read configured file: {exc}")
    settings.docs = prepared
    return problems, notes


# ---------------------------------------------------------------------------
# Logging, notifications, keeping the Mac awake
# ---------------------------------------------------------------------------

class Log:
    def __init__(self, echo=True):
        self.echo = echo
        self.file = None
        self.lock = threading.Lock()

    def open(self, path):
        private_dir(path.parent)
        descriptor = private_descriptor(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        self.file = os.fdopen(descriptor, "a", encoding="utf-8")

    def write(self, level, msg):
        stamp = datetime.now()
        line = f"{stamp:%H:%M:%S} {level:<5} {msg}"
        with self.lock:
            if self.echo:
                print(line, flush=True)
            if self.file:
                self.file.write(f"{stamp:%Y-%m-%d} {line}\n")
                self.file.flush()

    def info(self, msg):
        self.write("INFO", msg)

    def warn(self, msg):
        self.write("WARN", msg)

    def error(self, msg):
        self.write("ERROR", msg)

    def banner(self, lines, level="INFO"):
        bar = "=" * 72
        for line in [bar] + list(lines) + [bar]:
            self.write(level, line)


def notify(title, message, sound="Glass"):
    sys.stdout.write("\a")
    sys.stdout.flush()
    if sys.platform == "darwin" and shutil.which("osascript"):
        script = (f"display notification {json.dumps(message, ensure_ascii=False)} "
                  f"with title {json.dumps(title, ensure_ascii=False)} sound name {json.dumps(sound)}")
        try:
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass


def keep_awake():
    """Stop the Mac from idling to sleep while this process runs."""
    if sys.platform == "darwin" and shutil.which("caffeinate"):
        try:
            return subprocess.Popen(["caffeinate", "-ims", "-w", str(os.getpid())],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
    return None


# ---------------------------------------------------------------------------
# HTML forms
# ---------------------------------------------------------------------------

class FormParser(HTMLParser):
    """Reads the controls of the page's first <form> the way a browser sees them."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.action = None
        self.enctype = ""
        self.controls = []
        self._in_form = False
        self._select = self._option = self._textarea = None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): ("" if v is None else v) for k, v in attrs}
        if tag == "form":
            if self.action is None:
                self.action, self.enctype, self._in_form = a.get("action", ""), a.get("enctype", ""), True
            return
        if not self._in_form:
            return
        if tag == "input":
            self.controls.append({"tag": "input", "type": a.get("type", "text").lower(), "name": a.get("name", ""),
                                  "value": a.get("value", ""), "checked": "checked" in a, "disabled": "disabled" in a})
        elif tag == "select":
            self._close_option()
            self._select = {"tag": "select", "name": a.get("name", ""), "options": [], "disabled": "disabled" in a}
            self.controls.append(self._select)
        elif tag == "option" and self._select is not None:
            self._close_option()
            self._option = {"value": a.get("value"), "text": "", "selected": "selected" in a}
            self._select["options"].append(self._option)
        elif tag == "textarea":
            self._textarea = {"tag": "textarea", "name": a.get("name", ""), "value": "", "disabled": "disabled" in a}
            self.controls.append(self._textarea)

    def handle_endtag(self, tag):
        if tag == "form":
            self._in_form = False
        elif tag == "select":
            self._close_option()
            self._select = None
        elif tag == "option":
            self._close_option()
        elif tag == "textarea":
            self._textarea = None

    def handle_data(self, data):
        if self._option is not None:
            self._option["text"] += data
        elif self._textarea is not None:
            self._textarea["value"] += data

    def _close_option(self):
        if self._option is not None:
            self._option["text"] = " ".join(self._option["text"].split())
            if self._option["value"] is None:
                self._option["value"] = self._option["text"]
            self._option = None


class Page:
    """A fetched page plus the controls of its <form>."""

    def __init__(self, url, status, body, fresh=True, headers=None, redirects=(), complete=True):
        self.url, self.status, self.body = url, status, body
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}
        self.redirects = tuple(redirects)
        self.complete = complete
        self.fresh = fresh  # loaded by GET or redirect, i.e. not the echo of our own postback
        parser = FormParser()
        try:
            parser.feed(body)
            parser.close()
        except Exception:
            pass  # broken HTML: keep whatever was parsed
        self.controls = parser.controls
        self.multipart = "multipart" in parser.enctype.lower()
        self.post_url = urllib.parse.urljoin(url, parser.action) if parser.action is not None else url
        self.name = urllib.parse.urlparse(self.post_url).path.rsplit("/", 1)[-1].lower()
        self.is_aspx = self.value("__VIEWSTATE") is not None

    def _find(self, name):
        return next((c for c in self.controls if c["name"] == name), None)

    @staticmethod
    def _selected(control):
        opts = control["options"]
        chosen = next((o for o in opts if o["selected"]), opts[0] if opts else None)
        return chosen["value"] if chosen else ""

    def value(self, name):
        c = self._find(name)
        if c is None:
            return None
        return self._selected(c) if c["tag"] == "select" else c["value"]

    def options(self, name):
        c = self._find(name)
        if c is None or c["tag"] != "select":
            return None
        return [(o["value"], o["text"]) for o in c["options"]]

    def fields(self):
        """(name, value) pairs a browser would post for this form, minus buttons and files."""
        out = []
        for c in self.controls:
            if not c["name"] or c["disabled"]:
                continue
            if c["tag"] == "select":
                out.append((c["name"], self._selected(c)))
            elif c["tag"] == "textarea":
                out.append((c["name"], c["value"]))
            elif c["type"] in ("checkbox", "radio"):
                if c["checked"]:
                    out.append((c["name"], c["value"] or "on"))
            elif c["type"] not in ("submit", "button", "image", "reset", "file"):
                out.append((c["name"], c["value"]))
        return out

    def file_inputs(self):
        return [c["name"] for c in self.controls
                if c["tag"] == "input" and c["type"] == "file" and c["name"] and not c["disabled"]]

    def missing_enabled(self, names):
        return [name for name in names if self._find(name) is None or self._find(name)["disabled"]]

    def submit_button(self):
        control = self._find("btnSubmit")
        if (control is None or control["disabled"] or control["tag"] != "input"
                or control["type"] != "submit"):
            return None
        return control["name"], control["value"]

    def messages(self):
        return server_messages(self.body)

    def text(self):
        return visible_text(self.body)

    def title(self):
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", self.body)
        return clean_html(m.group(1)) if m else ""


class Upload:
    """A startup snapshot; workers never reopen a user's mutable file."""

    def __init__(self, path):
        path = Path(path)
        self.name = path.name
        self.content = path.read_bytes()
        self.mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"


class Multipart:
    def __init__(self):
        self.boundary = "----WebKitFormBoundary" + uuid.uuid4().hex[:16]
        self.parts = []

    def _head(self, name, filename=None):
        disposition = f'form-data; name="{name}"' + ("" if filename is None else f'; filename="{filename}"')
        return f"--{self.boundary}\r\nContent-Disposition: {disposition}\r\n".encode("utf-8")

    def field(self, name, value):
        self.parts.append(self._head(name) + b"\r\n" + str(value).encode("utf-8") + b"\r\n")

    def file(self, name, upload=None):
        if upload is not None:
            data, filename, ctype = upload.content, upload.name.replace('"', ""), upload.mime
        else:
            data, filename, ctype = b"", "", "application/octet-stream"
        self.parts.append(self._head(name, filename) + f"Content-Type: {ctype}\r\n\r\n".encode() + data + b"\r\n")

    def body(self):
        return b"".join(self.parts) + f"--{self.boundary}--\r\n".encode()

    def content_type(self):
        return "multipart/form-data; boundary=" + self.boundary


def origin(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ConfigError("The request URL has an invalid port or host.") from exc
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ConfigError("The request URL has an invalid origin.")
    return parsed.scheme.lower(), parsed.hostname.lower(), port


class OriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, expected, stop, drain):
        self.expected, self.stop, self.drain = expected, stop, drain
        self.history = []

    def redirect_request(self, request, response, code, message, headers, new_url):
        if self.stop is not None and self.stop.is_set():
            raise Cancelled()
        self.history.append((code, request.get_method(), request.full_url, new_url))
        self.check_redirect(new_url)
        return super().redirect_request(request, response, code, message, headers, new_url)

    def check_redirect(self, new_url):
        try:
            redirected_origin = origin(new_url)
        except ConfigError as exc:
            raise RedirectBlocked("Blocked an invalid redirect outside the configured admission site.") from exc
        if redirected_origin != self.expected:
            raise RedirectBlocked("Blocked a redirect outside the configured admission site.")

    def http_error_302(self, request, response, code, message, headers):
        location = headers.get("location") or headers.get("uri")
        if location:
            try:
                new_url = urllib.parse.urljoin(request.full_url, location)
            except ValueError as exc:
                raise RedirectBlocked("Blocked an invalid redirect outside the configured admission site.") from exc
            self.check_redirect(new_url)
        wrapped = RedirectResponse(response, self.drain)
        return super().http_error_302(request, wrapped, code, message, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


class RedirectResponse:
    def __init__(self, response, drain):
        self.response, self.drain = response, drain
        self.body = None

    def read(self, *args, **kwargs):
        if args or kwargs:
            return self.response.read(*args, **kwargs)
        if self.body is None:
            try:
                self.body = self.drain(self.response)
            except BaseException:
                self.response.close()
                raise
        return self.body

    def close(self):
        return self.response.close()

    def __getattr__(self, name):
        return getattr(self.response, name)


class TransportStartMixin:
    def __init__(self, *args, on_write, deadline, **kwargs):
        self._on_write = on_write
        self._deadline = deadline
        self._write_started = False
        self._in_tunnel = False
        super().__init__(*args, **kwargs)

    def _tunnel(self):
        self._in_tunnel = True
        try:
            return super()._tunnel()
        finally:
            self._in_tunnel = False

    def send(self, data):
        if self.sock is None:
            self.connect()
        if not self._in_tunnel and not self._write_started:
            self._on_write()
            self._write_started = True
        return super().send(data)

    def getresponse(self):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("response header deadline exceeded")
        expired = threading.Event()
        response_socket = self.sock

        def expire():
            expired.set()
            try:
                response_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        timer = threading.Timer(remaining, expire)
        timer.daemon = True
        timer.start()
        try:
            response = super().getresponse()
        except BaseException as exc:
            timer.cancel()
            if expired.is_set() and isinstance(exc, Exception):
                raise TimeoutError("response header deadline exceeded") from exc
            raise
        timer.cancel()
        if expired.is_set() or time.monotonic() >= self._deadline:
            response.close()
            raise TimeoutError("response header deadline exceeded")
        return response


class TrackedHTTPConnection(TransportStartMixin, http.client.HTTPConnection):
    pass


class TrackedHTTPSConnection(TransportStartMixin, http.client.HTTPSConnection):
    pass


class TrackedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, on_write, deadline):
        super().__init__()
        self.on_write = on_write
        self.deadline = deadline

    def http_open(self, request):
        def factory(host, **kwargs):
            return TrackedHTTPConnection(host, on_write=self.on_write, deadline=self.deadline(), **kwargs)
        return self.do_open(factory, request)


class TrackedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, on_write, deadline, context):
        super().__init__(context=context)
        self.on_write = on_write
        self.deadline = deadline

    def https_open(self, request):
        def factory(host, **kwargs):
            return TrackedHTTPSConnection(host, on_write=self.on_write, deadline=self.deadline(), **kwargs)
        return self.do_open(factory, request, context=self._context)


def response_retry_after(page):
    value = page.headers.get("retry-after", "")
    try:
        seconds = float(value)
    except ValueError:
        try:
            moment = parsedate_to_datetime(value)
            seconds = (moment - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def normalize_outcome_text(text):
    normalized = str(text or "").lower().replace("’", "'").replace("‘", "'")
    for contraction, expanded in OUTCOME_CONTRACTIONS:
        normalized = re.sub(rf"\b{re.escape(contraction)}\b", expanded, normalized)
    return " ".join(normalized.split())


def outcome_envelopes(text):
    return re.split(r"\s+\|\s+", normalize_outcome_text(text))


def outcome_negative(text):
    for envelope in outcome_envelopes(text):
        if has_any(envelope, OUTCOME_NEGATIVE_PHRASES):
            return True
        if re.search(r"\b(?:no|not|never|nothing|none|neither|cannot)\b[^.!?;|]*"
                     r"\b(?:submit|accept|register|save|receiv|confirm|verif)\w*\b", envelope):
            return True
    return False


def affirmative_success(text):
    envelopes = outcome_envelopes(text)
    for envelope in envelopes:
        if outcome_negative(envelope):
            continue
        for pattern in AFFIRMATIVE_OUTCOME_PATTERNS:
            match = re.fullmatch(rf"(?:{pattern}){AFFIRMATIVE_RECEIPT_SUFFIX}", envelope.strip())
            if match is not None:
                return match.group(0)
    return None


def affirmative_evidence(text):
    for envelope in outcome_envelopes(text):
        claim_start = (
            r"(?:(?:(?:the|your)\s+)?(?:admission\s+form|application|form|registration)\b|"
            r"(?:submitted\s+successfully|successfully\s+submitted|registered\s+successfully|"
            r"successfully\s+registered|saved\s+successfully|successfully\s+saved)\b)"
        )
        boundary = (
            r"(?:[.!?;]+|\balthough\b[^,;.!?]*,\s*|"
            r",\s*(?=(?:(?:the|your)\s+)?(?:admission\s+form|application|form|registration)\b)|"
            rf"\b(?:but|yet|however)\b(?=\s*,?\s*{claim_start})|"
            rf"\band\b(?=\s*{claim_start}))"
        )
        for pattern in AFFIRMATIVE_OUTCOME_PATTERNS:
            for match in re.finditer(rf"(?<!\w)(?:{pattern})(?!\w)", envelope):
                prefix = re.split(boundary, envelope[:match.start()])[-1]
                suffix = envelope[match.end():]
                if (outcome_negative(prefix)
                        or re.search(r"\b(?:no|not|never|nothing|none|neither|cannot)\b", prefix)
                        or re.search(r"\b(?:if|unless|when|whenever|once|after|before|until|will|shall|"
                                     r"would|should|could|may|might|can|verify|check)\b", prefix)):
                    continue
                if re.match(r"\s*\?", suffix):
                    continue
                if re.match(r"\s*(?:(?:[,;:]\s*)|(?:\b(?:but|yet|however)\b\s*))?"
                            r"(?:only\s+if|provided(?:\s+that)?|unless|if|when|whenever|once|after|"
                            r"before|until|subject\s+to|pending)\b", suffix):
                    continue
                return True
    return False


class Http:
    """Cookie-keeping HTTP client; every network-level failure becomes Transient."""

    def __init__(self, base_url, timeout, log, stop=None, verify=True):
        self.base_url = base_url.rstrip("/") + "/"
        self.origin = origin(base_url)
        self.stop = stop
        self.timeout = timeout
        self.log = log
        self.jar = http.cookiejar.CookieJar()
        self._on_write = None
        self._deadline = None
        self._build(verify)

    def _build(self, verify):
        ctx = ssl.create_default_context()
        if not verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self.verified = verify
        self.redirect_handler = OriginRedirectHandler(self.origin, self.stop, self._drain_redirect)
        self.opener = urllib.request.build_opener(TrackedHTTPHandler(self._before_write, self._request_deadline),
                                                  TrackedHTTPSHandler(self._before_write, self._request_deadline, ctx),
                                                  urllib.request.HTTPCookieProcessor(self.jar),
                                                  self.redirect_handler)

    def _before_write(self):
        if self._on_write is not None:
            self._on_write()

    def _request_deadline(self):
        if self._deadline is None:
            raise RuntimeError("HTTP request has no active deadline")
        return self._deadline

    def _drain_redirect(self, response):
        return self._read_response(response, self._request_deadline())

    def check_destination(self, url):
        if self.stop is not None and self.stop.is_set():
            raise Cancelled()
        if origin(url) != self.origin:
            raise ConfigError("Blocked a form/request outside the configured admission site.")

    def get(self, path):
        return self.send("GET", urllib.parse.urljoin(self.base_url, path))

    @staticmethod
    def _set_read_timeout(response, timeout):
        if not isinstance(response, http.client.HTTPResponse):
            raise Transient(f"unsupported stdlib HTTP response type: {type(response).__name__}")
        try:
            response_socket = response.fp.raw._sock
        except AttributeError as exc:
            raise Transient("stdlib HTTP response is missing its required socket stream") from exc
        if not isinstance(response_socket, socket.socket):
            raise Transient("stdlib HTTP response has an invalid socket stream")
        response_socket.settimeout(timeout)

    def _read_response(self, response, deadline):
        if not isinstance(response, http.client.HTTPResponse):
            raise Transient(f"unsupported stdlib HTTP response type: {type(response).__name__}")
        if response.length == 0:
            return b""
        chunks = []

        def finished():
            body = b"".join(chunks)
            if response.length not in (None, 0):
                raise http.client.IncompleteRead(body, response.length)
            return body

        if response.isclosed():
            return finished()
        while True:
            if response.length == 0 or response.isclosed():
                return finished()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("response body deadline exceeded")
            self._set_read_timeout(response, remaining)
            chunk = response.read1(64 * 1024)
            if not chunk:
                return finished()
            chunks.append(chunk)

    def _network_error(self, message, retry_after=None):
        kind = RedirectFailure if self.redirect_handler.history else Transient
        return kind(message, retry_after)

    def send(self, method, url, data=None, content_type=None, referer=None, timeout=None, on_start=None):
        self.check_destination(url)
        timeout = timeout or self.timeout
        deadline = time.monotonic() + timeout
        self.redirect_handler.history = []
        headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                   "Accept-Language": "en-IN,en;q=0.9", "Cache-Control": "no-cache"}
        if content_type:
            headers["Content-Type"] = content_type
        if referer:
            headers["Referer"] = referer
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        previous_on_write, self._on_write = self._on_write, on_start
        previous_deadline, self._deadline = self._deadline, deadline
        complete = True
        try:
            with self.opener.open(request, timeout=timeout) as resp:
                raw, status, final = self._read_response(resp, deadline), resp.status, resp.geturl()
                response_headers = dict(resp.headers)
        except urllib.error.HTTPError as e:
            try:
                raw = self._read_response(e.fp, deadline)
            except (http.client.HTTPException, OSError, TimeoutError):
                raw = b""
                complete = False
            finally:
                e.close()
            status, final, response_headers = e.code, e.geturl() or url, dict(e.headers)
        except urllib.error.URLError as e:
            if isinstance(e.reason, ssl.SSLCertVerificationError) and self.verified:
                self.log.warn("The site's HTTPS certificate did not verify - continuing without verification")
                redirected = bool(self.redirect_handler.history)
                self._build(verify=False)
                if method == "GET" and not redirected:
                    return self.send(method, url, data, content_type, referer, timeout)
                kind = RedirectFailure if redirected else Transient
                raise kind(f"network error: {e.reason}")
            raise self._network_error(f"network error: {e.reason}")
        except (socket.timeout, TimeoutError):
            raise self._network_error(f"no answer within {timeout:g}s")
        except (http.client.HTTPException, OSError) as e:
            raise self._network_error(f"connection problem: {type(e).__name__} {e}".strip())
        finally:
            self._on_write = previous_on_write
            self._deadline = previous_deadline
        redirects = tuple(self.redirect_handler.history)
        fresh = method == "GET" or bool(redirects)
        self.check_destination(final)
        return Page(final, status, raw.decode("utf-8", errors="replace"), fresh=fresh,
                    headers=response_headers, redirects=redirects, complete=complete)


# ---------------------------------------------------------------------------
# One browser-like visit with one phone number
# ---------------------------------------------------------------------------

class Selection:
    def __init__(self, course, level, batch, state, seats, fee, page, server_age):
        self.course, self.level, self.batch, self.state = course, level, batch, state
        self.seats, self.fee, self.page = seats, fee, page
        self.server_age = server_age

    def __str__(self):
        parts = [f"course '{self.course[1]}'"]
        if self.level:
            parts.append(f"level '{self.level[1]}'")
        parts.append(f"batch '{self.batch[1]}'")
        parts.append(f"{self.seats if self.seats is not None else '?'} seats")
        if self.fee:
            parts.append(f"fee {self.fee}")
        return ", ".join(parts)


class Session:
    def __init__(self, bot, phone, who):
        self.bot, self.phone, self.who = bot, phone, who
        self.http = None
        self.restart()

    def restart(self):
        verify = self.http.verified if self.http is not None else True
        self.http = Http(self.bot.base_url, self.bot.opt["requestTimeoutSec"], self.bot.log,
                         self.bot.stop, verify=verify)
        self.static = {}

    # -- plumbing ------------------------------------------------------------

    def checked(self, page):
        """Raise for error pages; otherwise count the success and hand the page back."""
        if page.status >= 400 or not page.is_aspx:
            if has_any(page.body.lower(), VIEWSTATE_ERRORS):
                raise SessionLost("the site rejected our page state")
            what = f"HTTP {page.status}" if page.status >= 400 else "error page"
            title = page.title()
            raise Transient(what + (f" ({title[:80]})" if title else ""), response_retry_after(page))
        return page

    def get(self, path):
        return self.checked(self.http.get(path))

    def postback(self, page, event, values, files=None, timeout=None, on_start=None):
        self.http.check_destination(page.post_url)
        source_page = urllib.parse.urlparse(page.url).path.rsplit("/", 1)[-1].lower()
        if (page.name == FORM_PAGE or source_page == FORM_PAGE) and page.value("txtMobileNo") != self.phone:
            raise SessionLost("the admission form no longer echoes the assigned mobile number")
        data = dict(page.fields())
        data.update({"__EVENTTARGET": event, "__EVENTARGUMENT": ""})
        data.update(values)
        if page.multipart or files:
            mp = Multipart()
            for k, v in data.items():
                mp.field(k, v)
            names = page.file_inputs()
            names += [n for n in (files or {}) if n not in names]
            for name in names:
                mp.file(name, (files or {}).get(name))
            body, ctype = mp.body(), mp.content_type()
        else:
            body = urllib.parse.urlencode(list(data.items())).encode()
            ctype = "application/x-www-form-urlencoded"
        return self.http.send("POST", page.post_url, body, ctype, referer=page.url, timeout=timeout,
                              on_start=on_start)

    def msgs(self, page):
        """Server messages minus the ones that are part of the page itself."""
        found = page.messages()
        if page.fresh and page.name not in self.static:
            verdicts = (PHONE_USED_PHRASES + FULL_PHRASES + CLOSED_PHRASES + SUCCESS_PHRASES
                        + OUTCOME_NEGATIVE_PHRASES)
            self.static[page.name] = {m for m in found
                                      if not has_any(m.lower(), verdicts) and not outcome_negative(m)}
        return [m for m in found if m not in self.static.get(page.name, ())]

    def verdict(self, page, deep=False, full_ok=True):
        """Raise if the site says this number is used, the choice is full, or booking is closed."""
        msgs = self.msgs(page)
        said = " | ".join(msgs).lower()
        if deep:
            said += " | " + page.text().lower()
        reason = "; ".join(msgs[:3]) or page.title()
        if has_any(said, PHONE_USED_PHRASES):
            raise PhoneUsed(reason or "the site says this number is already used")
        if full_ok and has_any(said, FULL_PHRASES):
            raise TargetFull(f"site says: {reason}")
        if has_any(said, CLOSED_PHRASES) or (not full_ok and has_any(said, FULL_PHRASES)):
            raise NotOpen(f"site says: {reason}")
        if msgs:
            self.bot.note(self.who, f"site says: {reason}")
        return msgs

    # -- flow ----------------------------------------------------------------

    def open_form(self):
        """Mobile verification + criteria; returns the admission form."""
        page = self.get("Admission_instructions.aspx")
        for _ in range(4):
            if page.name == FORM_PAGE:
                self.verdict(page, full_ok=False)
                self.bot.number_accepted(self.phone)
                return page
            if page.name == INSTRUCTIONS_PAGE:
                nxt = self.checked(self.postback(page, "lnkbtnSubmit", {"txtMobileNo": self.phone}))
                said = self.verdict(nxt, deep=nxt.name == INSTRUCTIONS_PAGE, full_ok=False)
                if nxt.name == INSTRUCTIONS_PAGE:
                    if said:
                        reason = "; ".join(said)
                        if has_any(reason.lower(), PERMANENT_MOBILE_REFUSAL_PHRASES):
                            raise Refused(f"the site refused mobile number {mask(self.phone)}: {reason}", "number")
                        raise Transient(f"mobile verification did not go through: {reason}")
                    raise Transient("mobile verification did not go through")
                page = nxt
            elif page.name == CRITERIA_PAGE:
                nxt = self.checked(self.postback(page, "lnkbtnNext", {"chkAccept": "on"}))
                said = self.verdict(nxt, deep=nxt.name == CRITERIA_PAGE, full_ok=False)
                if nxt.name == CRITERIA_PAGE:
                    if said:
                        raise Refused(f"the terms page did not move on: {'; '.join(said)}", "criteria")
                    raise Transient("the criteria page did not move on")
                page = nxt
            else:
                raise Transient(f"unexpected page '{page.name}'")
        raise Transient("could not get through to the admission form")

    def select(self, page, event, values, classify_full=False):
        """Postback after changing a dropdown/date; returns the refreshed form page."""
        values = dict(values)
        nxt = self.checked(self.postback(page, event, values))
        if nxt.name != FORM_PAGE:
            if nxt.name in (INSTRUCTIONS_PAGE, CRITERIA_PAGE):
                raise SessionLost(f"the site sent us back to {nxt.name}")
            raise Transient(f"unexpected page '{nxt.name}' after choosing {event}")
        try:
            self.verdict(nxt)
        except TargetFull:
            if classify_full:
                return nxt, True
            raise
        return (nxt, False) if classify_full else nxt

    def cascade(self, target):
        """Choose course, level, batch, state and date of birth for this target.

        Each attempt starts from scratch because the site only recovers reliably after a new
        cookie session repeats mobile verification and every dependent postback."""
        bot = self.bot
        excluded_courses = set()
        excluded_levels = set()
        excluded_batches = set()
        found = None
        while found is None:
            self.restart()
            page = self.open_form()
            course_options = page.options("ddlCourse")
            restart = False
            for value, text in course_candidates(course_options, target, bot.course_hint.get(target.index)):
                if value in excluded_courses:
                    continue
                page, full = self.select(
                    page, "ddlCourse",
                    dict(first_values(page, "ddlCourseLevel", "ddlScheduleCourse"), ddlCourse=value),
                    classify_full=True)
                if full:
                    excluded_courses.add(value)
                    restart = True
                    break
                level_options = page.options("ddlCourseLevel")
                levels = level_candidates(level_options, target, loose=kind_score(text, target.kind) > 0)
                if real_options(level_options) and not levels:
                    continue
                for level in levels or [None]:
                    level_key = (value, level[0] if level else "")
                    if level_key in excluded_levels:
                        continue
                    if level:
                        page, full = self.select(
                            page, "ddlCourseLevel",
                            dict(first_values(page, "ddlScheduleCourse"), ddlCourseLevel=level[0]),
                            classify_full=True)
                        if full:
                            excluded_levels.add(level_key)
                            restart = True
                            break
                    batches = page.options("ddlScheduleCourse")
                    if any(is_season_batch(t, bot.s.batches) for _, t in real_options(batches)):
                        bot.saw_season()
                    for batch in batch_candidates(batches, target):
                        batch_key = level_key + (batch[0],)
                        if batch_key in excluded_batches or selection_conflicts(
                                (value, text), level, batch, target):
                            continue
                        values = {"ddlScheduleCourse": batch[0]}
                        if level:
                            values["ddlCourseLevel"] = level[0]
                        candidate_page, full = self.select(page, "ddlScheduleCourse", values, classify_full=True)
                        if full:
                            excluded_batches.add(batch_key)
                            restart = True
                            break
                        seats = parse_server_number(candidate_page.value("txtAvailableSeat"))
                        if seats is None:
                            raise Transient("the selected schedule reported an invalid seat count")
                        if seats == 0:
                            excluded_batches.add(batch_key)
                            restart = True
                            break
                        found = ((value, text), level, batch, candidate_page, seats)
                        break
                    if found or restart:
                        break
                if found or restart:
                    break
            if restart:
                continue
            break
        if not found:
            if bot.season_seen is None:
                raise NotOpen("the snow courses are not listed yet" + listing(page, "ddlCourse"))
            raise TargetFull(f"{target.when()} is not listed" + listing(page, "ddlScheduleCourse"))
        course, level, batch, page, seats = found
        if not has_semantic_evidence(course, level, batch, target):
            bot.note(self.who, f"{target.label}: only the configured dates support this generic course/level match; "
                     "proceeding with the approved permissive attempt", warning=True)
        bot.course_hint[target.index] = course[0]
        if seats:
            bot.saw_seats(target)

        state_options = page.options("ddlState")
        if page.missing_enabled(("ddlState",)) or not real_options(state_options):
            raise Transient("the required state list is missing or empty")
        state = match_choice(state_options, bot.s.state, state_aliases(bot.s.state))
        if state is None:
            raise Rejected(f"address.state \"{bot.s.state}\" cannot be reasonably resolved to one site option"
                           + listing(page, "ddlState"))
        page = self.select(page, "ddlState", {"ddlState": state[0]})
        dob = bot.s.applicant.get("dateOfBirth", "")
        page = self.select(page, "txtDateOfBirth", {"txtDateOfBirth": dob})
        # outside the age limit the site says "Your Age N Years , Age Allow X To Y Years." and clears the date
        said = self.msgs(page)
        echoed = page.value("txtDateOfBirth")
        if has_any(" | ".join(said).lower(), AGE_PHRASES) or (echoed is not None and not echoed.strip()):
            raise Rejected(f"date of birth not accepted for {target.label}: {'; '.join(said) or 'date cleared'}")
        server_age = observed_server_age(page, bot.s.dob) if bot.s.dob else None
        return Selection(course, level, batch, state, seats, page.value("txtCourseFee"), page, server_age)

    def prepare_submit(self, target, sel):
        bot = self.bot
        page = sel.page
        minor = sel.server_age is not None and sel.server_age < 18
        if minor and not bot.s.docs.get("parentSignature"):
            raise Rejected(f"the site classifies the applicant as age {sel.server_age} for {target.label}; "
                           "documents.parentSignature must contain the real guardian signature")
        self.http.check_destination(page.post_url)
        if page.value("txtMobileNo") != self.phone:
            raise SessionLost("the latest form no longer echoes the assigned mobile number")
        required_paths = set(REQUIRED_FIELDS)
        required_text = {field for field, section, key in TEXT_FIELDS if (section, key) in required_paths}
        required_controls = required_text | {
            "__EVENTTARGET", "__EVENTARGUMENT", "__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION",
            "ddlCourse", "ddlCourseLevel", "ddlScheduleCourse", "ddlState", "txtAvailableSeat", "txtCourseFee",
            "hdfTypeOfCapacitySheet", "hdfCapacitySheetAllocationId", "txtMobileNo",
            "ddlEduationQualification", "ddlVegetarianOrNon", "chkIAgree", "btnSubmit",
        }
        files = {}
        document_flags = {}
        missing_files = []
        enabled_files = set(page.file_inputs())
        for key, input_name, flag, requirement in DOCUMENTS:
            path = bot.s.docs.get(key)
            if not document_applies(key, target, bot.s.dob):
                path = None
            if (key == "parentSignature" and path is None and bot.parent_fallback
                    and sel.server_age is not None and sel.server_age >= 18):
                path = bot.s.docs.get("signature")
            required_now = requirement is True or (key == "parentSignature" and (
                minor or bot.parent_fallback or target.index in bot.guardian_required))
            flag_ready = not page.missing_enabled((flag,))
            input_ready = input_name in enabled_files
            if required_now and path is None:
                raise ConfigError(f"prepared required document {key} is unavailable")
            if required_now:
                required_controls.add(flag)
                if not input_ready:
                    missing_files.append(input_name)
            if path is not None and input_ready and flag_ready:
                files[input_name] = path
                document_flags[flag] = "Yes"
            elif path is not None and not required_now:
                bot.note(self.who, f"{target.label}: configured optional documents.{key} control is unavailable; "
                         "omitting that upload", warning=True)
        missing = page.missing_enabled(required_controls) + missing_files
        if missing:
            raise Transient("required final form controls are missing or disabled: " + ", ".join(sorted(missing)))
        required_tokens = ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION",
                           "hdfTypeOfCapacitySheet", "hdfCapacitySheetAllocationId")
        empty_tokens = [name for name in required_tokens if not str(page.value(name) or "").strip()]
        if str(page.value("hdfCapacitySheetAllocationId") or "").strip() == "0":
            empty_tokens.append("hdfCapacitySheetAllocationId")
        if empty_tokens:
            raise Transient("required final form values are empty or reset: " + ", ".join(sorted(set(empty_tokens))))
        if sel.level is None:
            raise Transient("the latest form has no selected course level")
        expected = {"ddlCourse": sel.course[0], "ddlCourseLevel": sel.level[0],
                    "ddlScheduleCourse": sel.batch[0], "ddlState": sel.state[0]}
        current = {name: next((item for item in real_options(page.options(name)) if item[0] == value), None)
                   for name, value in expected.items()}
        stale = [name for name, value in expected.items() if current[name] is None or page.value(name) != value]
        if stale:
            raise Transient("the latest form no longer contains and selects: " + ", ".join(stale))
        if (selection_conflicts(current["ddlCourse"], current["ddlCourseLevel"],
                                current["ddlScheduleCourse"], target)
                or match_batch([current["ddlScheduleCourse"]], target) is None):
            raise Transient("the latest course, level or batch meaning no longer matches the configured preference")
        current_state = match_choice(page.options("ddlState"), bot.s.state, state_aliases(bot.s.state))
        if current_state is None or current_state[0] != sel.state[0]:
            raise Transient("the latest state option no longer matches the resolved website choice")
        echoed_dob = page.value("txtDateOfBirth")
        if parse_day(echoed_dob) != bot.s.dob:
            raise Transient("the latest form no longer echoes the configured date of birth")
        seats = parse_server_number(page.value("txtAvailableSeat"))
        if seats is None:
            raise Transient("the latest form does not report a valid seat count")
        if seats <= 0:
            raise TargetFull("0 seats left on the latest form")
        current_fee = parse_server_number(page.value("txtCourseFee"), allow_decimal=True)
        selected_fee = parse_server_number(sel.fee, allow_decimal=True)
        if current_fee is None or current_fee <= 0:
            raise Transient("the latest form does not report a valid course fee")
        if selected_fee is None or selected_fee <= 0:
            raise Transient("the selected form did not report a valid course fee")
        if current_fee != selected_fee:
            raise Transient("the latest form reports a different course fee")
        values = {}
        for field, section, key in TEXT_FIELDS:
            configured = bot.s.form_values[field]
            if field in required_text:
                values[field] = configured
            elif configured:
                if not page.missing_enabled((field,)):
                    values[field] = configured
                else:
                    bot.note(self.who, f"{target.label}: configured optional {section}.{key} control is unavailable; "
                             "omitting that value", warning=True)
        values.update({"ddlCourse": sel.course[0], "ddlScheduleCourse": sel.batch[0],
                       "ddlState": sel.state[0], "chkIAgree": "on"})
        values["ddlCourseLevel"] = sel.level[0]
        for field, key, wanted in (("ddlEduationQualification", "applicant.education", bot.s.education),
                                   ("ddlVegetarianOrNon", "applicant.diet", bot.s.diet)):
            options = page.options(field)
            if not real_options(options):
                raise Transient(f"the required {key} list is missing or empty")
            pick = match_choice(options, wanted)
            if pick is None:
                raise Rejected(f"{key} \"{wanted}\" cannot be reasonably resolved to one site option"
                               + listing(page, field))
            values[field] = pick[0]
        values.update(document_flags)
        button = page.submit_button()
        if button is None:
            raise Transient("the required final submit control is missing or disabled")
        values[button[0]] = button[1]
        return values, files

    def submit(self, target, sel, values, files):
        bot = self.bot
        page = sel.page

        request_started = False

        def note_request_started():
            nonlocal request_started
            if bot.stop.is_set():
                raise Cancelled()
            request_started = True

        bot.mark_submitting(self.phone, target)
        try:
            bot.log.info(f"{self.who}SUBMITTING {target.label}: {sel}")
            resp = self.postback(page, "", values, files=files, timeout=bot.opt["submitTimeoutSec"],
                                 on_start=note_request_started)
            saved = bot.save_page(self.who, f"submit-{target.label}", resp)
            result = self.judge_submit(resp, target, sel, saved, set(files))
        except (RateLimited, Resend, Rejected, TargetFull, NotOpen, PhoneUsed):
            bot.clear_submitting(self.phone)
            raise
        except RedirectBlocked as exc:
            if not request_started:
                bot.clear_submitting(self.phone)
                raise
            raise Uncertain(str(exc)) from exc
        except Uncertain as exc:
            if not request_started:
                bot.clear_submitting(self.phone)
                raise Transient(f"final request did not start: {exc}") from exc
            raise
        except (Cancelled, ConfigError):
            if not request_started:
                bot.clear_submitting(self.phone)
            raise
        except Exception as exc:
            if not request_started:
                bot.clear_submitting(self.phone)
                raise Transient(f"final request did not start: {exc}") from exc
            raise Uncertain(str(exc)) from exc
        return result

    def judge_submit(self, resp, target, sel, saved, sent_files):
        self.http.check_destination(resp.url)
        if not resp.complete:
            raise Uncertain("the final HTTP error response ended before its body could be verified")
        parsed = urllib.parse.urlparse(resp.url)
        query = {k.lower(): v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        msgs = self.msgs(resp) if resp.is_aspx else server_messages(resp.body)
        said = " | ".join(msgs).lower()
        own_page = resp.name in (INSTRUCTIONS_PAGE, CRITERIA_PAGE, FORM_PAGE)
        text = "" if own_page else resp.text().lower()
        path = parsed.path.lower()
        actual_page = path.rsplit("/", 1)[-1]
        hidden_id = resp.value("hdfAdmissionFormId") or ""
        payment_receipt = (actual_page == "admission_formpayment.aspx" and query.get("rid")
                           and hidden_id == query["rid"] and hidden_id != "0")
        negative = outcome_negative(said + " | " + text)
        message_success = next((affirmative_success(message.lower()) for message in msgs
                                if affirmative_success(message.lower())), None)
        page_success = affirmative_success(text) if actual_page == "admissionformsuccess.aspx" else None
        explicit_success = bool(message_success or page_success)
        positive_evidence = (any(affirmative_evidence(message) for message in msgs)
                             or (actual_page == "admissionformsuccess.aspx" and affirmative_evidence(text)))
        phone_used = has_any(said, PHONE_USED_PHRASES)
        full = has_any(said, FULL_PHRASES)
        closed = has_any(said, CLOSED_PHRASES)
        failed_validators = failed_validator_messages(resp.body) if resp.name == FORM_PAGE else []
        confirmed_evidence = explicit_success or payment_receipt
        if (positive_evidence or payment_receipt) and (
                negative or phone_used or full or closed or failed_validators or resp.status == 429):
            raise Uncertain("the final response contains conflicting success and rejection evidence")
        if 200 <= resp.status < 300 and confirmed_evidence:
            message = next((m for m in msgs if affirmative_success(m.lower())), "") or query.get("resp", "")
            if not message:
                hit = page_success
                if hit:
                    i = text.find(hit)
                    message = resp.text()[max(0, i - 80):i + 120]
            app_id = next((query[k] for k in ("rid", "id", "appid", "applicationid", "admissionformid", "regno")
                           if query.get(k)), "")
            if not app_id:
                hidden = resp.value("hdfAdmissionFormId") or ""
                app_id = hidden if hidden not in ("", "0") else ""
            return {"message": message.strip(), "applicationId": app_id, "url": resp.url, "response": str(saved or "")}
        if resp.redirects:
            raise Uncertain(f"the final POST redirected, but its receipt did not confirm the application "
                            f"(HTTP {resp.status}, page '{resp.name}')")
        reason = "; ".join(msgs[:4])
        if phone_used:
            raise PhoneUsed(reason)
        if full:
            raise TargetFull(f"site says: {reason}")
        if closed:
            raise NotOpen(f"site says: {reason}")
        if failed_validators:
            bot = self.bot
            wants_parent = has_any(" ".join(failed_validators).lower(), ("parent", "guardian"))
            if wants_parent:
                if sel.server_age is None:
                    raise Rejected("the site requested a guardian signature, but its age response did not "
                                   "establish that the applicant is an adult")
                guardian_sent = "FupParentGuardianSignature" in sent_files
                if sel.server_age >= 18 and bot.s.docs.get("parentSignature") and not guardian_sent:
                    bot.require_guardian(target)
                    raise Resend("the site requires the configured guardian signature; retrying from a fresh form")
                if (not guardian_sent and not bot.s.docs.get("parentSignature") and not bot.parent_fallback
                        and sel.server_age >= 18 and bot.s.docs.get("signature")
                        and bot.opt.get("parentSignatureFallback")):
                    bot.use_parent_fallback(reason)
                    raise Resend("the site wants a parent/guardian signature - resending with the applicant's signature")
            raise Rejected(f"{reason} (page saved: {saved})")
        if resp.status == 429:
            raise RateLimited("submit was rate limited: HTTP 429", response_retry_after(resp))
        if resp.status >= 400 or not resp.is_aspx:
            if has_any(resp.body.lower(), VIEWSTATE_ERRORS):
                raise SessionLost("the site rejected our page state on submit")
            title = resp.title()
            raise Transient(f"submit failed: HTTP {resp.status}" + (f" ({title[:80]})" if title else ""))
        if resp.name in (INSTRUCTIONS_PAGE, CRITERIA_PAGE):
            raise Transient("the site sent us back to the start after submitting; outcome unknown")
        if resp.name == FORM_PAGE:
            meaningful = [m for m in msgs if not re.fullmatch(r"Your Age \d+ Years\.?", m, re.I)]
            if meaningful:
                raise Uncertain(f"the final form returned an unverified message and may have accepted the application: "
                                f"{reason}")
            raise Transient("the form came back without any message")
        raise Transient(f"unexpected page '{resp.name}' after submitting (saved: {saved})")

    def book(self, target):
        sel = self.cascade(target)
        if self.bot.stop.is_set():
            raise Cancelled()
        values, files = self.prepare_submit(target, sel)
        if self.bot.stop.is_set():
            raise Cancelled()
        if self.bot.dry_run:
            self.bot.log.info(f"{self.who}DRY RUN - would submit {target.label} now: {sel}")
            return {"message": f"dry run: {sel}", "applicationId": "", "dryRun": True}
        return self.submit(target, sel, values, files)


# ---------------------------------------------------------------------------
# Coordinating numbers and choices
# ---------------------------------------------------------------------------

STATE_VERSION = 2
TARGET_KEY_VERSION = 1
TARGET_IDENTITY_FIELDS = {"location", "from", "to", "kind", "level"}
STATE_FIELDS = {"version", "targetKeyVersion", "identity", "bookings", "phones", "assignments"}
BOOKING_REQUIRED_FIELDS = {"target", "targetKey", "targetIdentity", "batch", "phone", "status", "at"}
BOOKING_OPTIONAL_FIELDS = {"message", "applicationId", "url", "response"}


def target_identity(target):
    return {"location": norm(target.location), "from": target.start.isoformat(),
            "to": target.end.isoformat() if target.end else "", "kind": target.kind,
            "level": target.level or ""}


def expected_state_identity(settings, base_url):
    values = [settings.applicant.get(key, "") for key in ("name", "fatherName", "dateOfBirth")]
    values += [settings.season, origin(base_url)]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def target_key_from_identity(identity):
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def target_key(target):
    return target_key_from_identity(target_identity(target))


def valid_target_key(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def canonical_phone(value):
    return isinstance(value, str) and parse_phone(value) == value


def valid_timestamp(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def valid_target_identity(identity):
    if not isinstance(identity, dict) or set(identity) != TARGET_IDENTITY_FIELDS:
        return False
    if any(not isinstance(value, str) for value in identity.values()):
        return False
    if identity["location"] != norm(identity["location"]):
        return False
    try:
        start = date.fromisoformat(identity["from"])
        end = date.fromisoformat(identity["to"]) if identity["to"] else None
    except ValueError:
        return False
    if identity["from"] != start.isoformat() or (end is not None and identity["to"] != end.isoformat()):
        return False
    if end is not None and end < start:
        return False
    if identity["kind"] == "Snow Skiing":
        return identity["level"] in ("Basic", "Intermediate", "Advance")
    return identity["kind"] in ("Snow Boarding", "MOI") and identity["level"] == ""


def valid_booking_record(item):
    if not isinstance(item, dict):
        return False
    fields = set(item)
    if not BOOKING_REQUIRED_FIELDS <= fields or not fields <= BOOKING_REQUIRED_FIELDS | BOOKING_OPTIONAL_FIELDS:
        return False
    if (not isinstance(item["target"], str) or not item["target"]
            or not isinstance(item["batch"], str) or not item["batch"]
            or item["status"] not in ("confirmed", "user-confirmed")
            or not valid_timestamp(item["at"])
            or not canonical_phone(item["phone"])
            or not valid_target_key(item["targetKey"])
            or not valid_target_identity(item["targetIdentity"])
            or target_key_from_identity(item["targetIdentity"]) != item["targetKey"]):
        return False
    return all(isinstance(item[key], str) for key in BOOKING_OPTIONAL_FIELDS if key in item)


def valid_phone_record(item):
    if not isinstance(item, dict) or item.get("status") not in ("used", "submitting", "booked"):
        return False
    if item["status"] == "used":
        return set(item) == {"status", "note"} and isinstance(item["note"], str)
    expected = {"status", "targetKey", "target"}
    if item["status"] == "submitting":
        expected.add("at")
    if (set(item) != expected or not valid_target_key(item["targetKey"])
            or not isinstance(item["target"], str) or not item["target"]):
        return False
    return item["status"] != "submitting" or valid_timestamp(item["at"])


class State:
    """Versioned progress, locked for a run and atomically made durable before submit."""

    def __init__(self, path, enabled):
        self.path, self.enabled = Path(path), enabled
        self.lease = None
        self.identity = None
        self.data = {"version": STATE_VERSION, "targetKeyVersion": TARGET_KEY_VERSION,
                     "identity": None, "bookings": [], "phones": {}, "assignments": {}}
        self.reload()

    def reload(self):
        if not self.enabled or not self.path.exists():
            return
        try:
            value = strict_json_loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("invalid progress root")
            if (type(value.get("version")) is not int or value["version"] != STATE_VERSION
                    or type(value.get("targetKeyVersion")) is not int
                    or value["targetKeyVersion"] != TARGET_KEY_VERSION):
                raise ValueError("unsupported progress schema or target-key version")
            if set(value) != STATE_FIELDS:
                raise ValueError("invalid progress root")
            if (not isinstance(value.get("identity"), str)
                    or not isinstance(value.get("bookings"), list)
                    or not isinstance(value.get("phones"), dict)
                    or not isinstance(value.get("assignments"), dict)):
                raise ValueError("invalid or outdated progress schema")
            booked_phones, booked_targets = set(), set()
            for item in value["bookings"]:
                if not valid_booking_record(item):
                    raise ValueError("invalid booking record")
                if item["phone"] in booked_phones or item["targetKey"] in booked_targets:
                    raise ValueError("duplicate booking records")
                booked_phones.add(item["phone"])
                booked_targets.add(item["targetKey"])
                phone_record = value["phones"].get(item["phone"], {})
                if (phone_record.get("status") != "booked" or phone_record.get("targetKey") != item["targetKey"]
                        or phone_record.get("target") != item["target"]):
                    raise ValueError("inconsistent booked phone")
            for phone, item in value["phones"].items():
                if not canonical_phone(phone) or not valid_phone_record(item):
                    raise ValueError("invalid phone record")
                if item["status"] == "booked" and phone not in booked_phones:
                    raise ValueError("booked phone has no booking record")
            if any(not valid_target_key(k) or not canonical_phone(v) for k, v in value["assignments"].items()):
                raise ValueError("invalid phone assignments")
            assigned_phones = list(value["assignments"].values())
            if len(assigned_phones) != len(set(assigned_phones)):
                raise ValueError("one phone is assigned to multiple preferences")
            for target_key_value, phone in value["assignments"].items():
                if target_key_value in booked_targets:
                    raise ValueError("booked target still has a pending phone assignment")
                record = value["phones"].get(phone)
                if record and record["status"] in ("used", "booked"):
                    raise ValueError("unavailable phone still has a preference assignment")
                if (record and record["status"] in ("submitting", "booked")
                        and record.get("targetKey") != target_key_value):
                    raise ValueError("phone assignment conflicts with its application record")
            for phone, item in value["phones"].items():
                if item["status"] == "submitting" and value["assignments"].get(item["targetKey"]) != phone:
                    raise ValueError("pending submission conflicts with its phone assignment")
            if self.identity is not None and value.get("identity") != self.identity:
                raise ValueError("progress belongs to a different applicant, season or site")
            self.data = value
        except (OSError, ValueError, TypeError) as exc:
            raise ConfigError(f"Cannot safely read progress {self.path}: {exc}. "
                              "Keep this file and reconcile existing applications before resetting it.") from exc

    def bind(self, identity):
        self.validate_identity(identity)
        self.identity = self.data["identity"] = identity

    def validate_identity(self, identity):
        if self.data["identity"] not in (None, identity):
            raise ConfigError("Saved progress belongs to a different applicant, season or site; use a separate config.")

    def acquire(self):
        if not self.enabled or self.lease is not None:
            return
        private_dir(self.path.parent)
        lock_path = self.path.with_suffix(".lock")
        descriptor = private_descriptor(lock_path, os.O_RDWR | os.O_CREAT | os.O_APPEND)
        handle = os.fdopen(descriptor, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if handle.seek(0, os.SEEK_END) == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise ConfigError("Another booking process holds this config's progress lock, or the lock is not writable.") from exc
        self.lease = handle
        try:
            self.reload()
            if self.path.exists():
                private_file(self.path)
            if self.identity is not None:
                self.bind(self.identity)
        except Exception:
            self.release()
            raise

    def release(self):
        if self.lease is not None:
            self.lease.close()
            self.lease = None

    def save(self):
        if not self.enabled:
            return
        if self.lease is None:
            raise ConfigError("Progress must be locked before it can be changed.")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".state-", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(self.data, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            private_file(self.path)
            if os.name != "nt":
                fd = os.open(str(self.path.parent), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except OSError as exc:
            raise ConfigError(f"Cannot durably save progress: {exc}. No further submissions will be sent.") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class Bot:
    """One persistent worker per preference; primary numbers and a shared reserve pool."""

    def __init__(self, settings, out_dir, base_url=DEFAULT_BASE_URL, dry_run=False, log=None,
                 confirmation_handler=None):
        self.s, self.opt = settings, settings.opt
        self.out_dir, self.base_url, self.dry_run = Path(out_dir), base_url, dry_run
        self.log = log or Log()
        self.targets, self.phones = settings.targets, settings.phones
        self.opens_at = settings.opens_at or now_ist()
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.state = State(self.out_dir / "state.json", enabled=not dry_run)
        self.state.bind(expected_state_identity(settings, base_url))
        self.confirmation_handler = confirmation_handler or self.prompt_confirmation
        self.questions = queue.Queue()
        self.pending_questions = {}
        self.failures = []
        self.season_seen = None
        self.attention_seen = {}
        self.refusals = {}
        self.parent_fallback = False
        self.guardian_required = set()
        self.course_hint = {}
        self.status = {}
        self.notes = {}
        self.saved_pages = 0
        self.blocked = {}
        self._restore()

    def _restore(self):
        self.bookings, self.booked, self.pending, self.assignments = [], {}, {}, {}
        self.phone_state = {phone: "free" for phone in self.phones}
        self.owners = {}
        lookup = {target_key(t): t for t in self.targets}
        for booking in self.state.data["bookings"]:
            self.bookings.append(booking)
            target = lookup.get(booking["targetKey"])
            if target is not None:
                self.booked[target.index] = booking
        for phone, item in self.state.data["phones"].items():
            if phone in self.phone_state:
                self.phone_state[phone] = item["status"]
            if item["status"] == "submitting":
                if item["targetKey"] not in lookup:
                    raise ConfigError("An unresolved submission belongs to a removed/changed preference. "
                                      "Restore that preference and reconcile it before booking.")
                if phone not in self.phone_state:
                    raise ConfigError("An unresolved submission's phone was removed. Restore it before continuing.")
                self.pending[phone] = item["targetKey"]
        previous = dict(self.state.data["assignments"])
        reserve_phones = set(self.phones[len(self.targets):])
        for phone, key in self.pending.items():
            target = lookup[key]
            primary = self.phones[target.index]
            if previous.get(key) != phone:
                raise ConfigError("An unresolved submission conflicts with its saved phone assignment.")
            if phone != primary and phone not in reserve_phones:
                raise ConfigError("An unresolved submission conflicts with the current preference/phone order. "
                                  "Restore the prior ordering and reconcile it before booking.")
            self.assignments[key] = phone
            self.owners[phone] = key
        pending_targets = set(self.pending.values())
        for target in self.targets:
            key = target_key(target)
            if target.index in self.booked or key in pending_targets:
                continue
            primary = self.phones[target.index]
            if self.phone_state[primary] == "free" and primary not in self.owners:
                self.assignments[key] = primary
                self.owners[primary] = key
                continue
            prior_phone = previous.get(key)
            if (prior_phone in reserve_phones and self.phone_state[prior_phone] == "free"
                    and prior_phone not in self.owners):
                self.assignments[key] = prior_phone
                self.owners[prior_phone] = key
        if previous != self.assignments:
            self.state.data["assignments"] = dict(self.assignments)
            if self.state.lease is not None:
                self.state.save()

    def sleep(self, seconds):
        self.stop.wait(max(0.0, seconds))

    def backoff(self, failures):
        base = self.opt["retryDelaySec"]
        cap = min(10.0, self.opt["maxRetryDelaySec"])
        exponent = max(0, failures - 1)
        limit = max(0.0, (math.log(cap) - math.log(base)) / math.log(1.6))
        delay = cap if exponent >= limit else math.exp(math.log(base) + exponent * math.log(1.6))
        return min(cap, delay * random.uniform(0.7, 1.3))

    def retry_delay(self, failures, error):
        delay = self.backoff(failures)
        if error.retry_after is not None:
            self.note("", f"Server requested Retry-After {error.retry_after:g}s; retry waits are capped at 10s.")
            delay = max(delay, min(10.0, error.retry_after))
        return min(10.0, delay)

    def poll_delay(self):
        return min(10.0, self.opt["pollSec"])

    def note(self, who, message, every=60, warning=False):
        with self.lock:
            key, now = (who, message), time.monotonic()
            if now - self.notes.get(key, -1e9) < every:
                return
            self.notes[key] = now
        (self.log.warn if warning else self.log.info)(f"{who}{message}")

    def save_page(self, who, tag, page):
        with self.lock:
            if self.saved_pages >= 500:
                return None
            self.saved_pages += 1
        folder = self.out_dir / "responses"
        name = re.sub(r"[^A-Za-z0-9.-]+", "_", f"{datetime.now():%Y%m%d-%H%M%S}-{who}-{tag}")
        path = folder / f"{name}-{uuid.uuid4().hex}.html"
        try:
            private_dir(folder)
            descriptor = private_descriptor(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(f"<!-- {page.status} {page.url} -->\n{page.body}")
        except OSError:
            return None
        return path

    def attention(self, key, title, lines, fix):
        with self.lock:
            now = time.monotonic()
            if now - self.attention_seen.get(key, -1e9) < 600:
                return
            self.attention_seen[key] = now
        self.log.banner([f"NEEDS YOUR ATTENTION: {title}"] + list(lines) + [f"To fix: {fix}"], "ERROR")
        if self.opt["notify"]:
            threading.Thread(target=notify, args=("IISM needs your attention", title, "Basso"), daemon=True).start()

    def set_status(self, worker, text):
        with self.lock:
            if text is None:
                self.status.pop(worker, None)
            else:
                self.status[worker] = text

    def status_line(self):
        with self.lock:
            return " | ".join([f"booked {len(self.booked)}/{len(self.targets)}"] +
                              [f"{key}: {value}" for key, value in sorted(self.status.items())])

    def finished(self):
        with self.lock:
            return all(target.index in self.booked for target in self.targets)

    def assign_phone(self, target):
        with self.lock:
            key = target_key(target)
            assigned = self.assignments.get(key)
            if assigned is not None:
                return assigned
            candidates = self.phones[target.index:target.index + 1] + self.phones[len(self.targets):]
            for phone in candidates:
                if self.phone_state[phone] == "free" and phone not in self.owners:
                    self.assignments[key] = phone
                    self.owners[phone] = key
                    self.state.data["assignments"][key] = phone
                    self.state.save()
                    return phone
        return None

    def mark_submitting(self, phone, target):
        with self.lock:
            if self.stop.is_set():
                raise Cancelled()
            key = target_key(target)
            if self.assignments.get(key) != phone or self.owners.get(phone) != key:
                raise ConfigError("Cannot submit without an exclusive saved phone assignment.")
            self.state.data["phones"][phone] = {"status": "submitting", "targetKey": key,
                                                 "target": target.label, "at": now_ist().isoformat()}
            try:
                self.state.save()
            except Exception:
                del self.state.data["phones"][phone]
                raise
            self.pending[phone] = key
            self.phone_state[phone] = "submitting"

    def clear_submitting(self, phone):
        with self.lock:
            self.pending.pop(phone, None)
            if self.state.data["phones"].get(phone, {}).get("status") == "submitting":
                del self.state.data["phones"][phone]
                self.phone_state[phone] = "free"
                self.state.save()

    def phone_used(self, phone, target, why):
        with self.lock:
            if phone in self.pending:
                raise Uncertain(why)
            key = target_key(target)
            self.phone_state[phone] = "used"
            self.assignments.pop(key, None)
            self.owners.pop(phone, None)
            self.state.data["assignments"].pop(key, None)
            self.state.data["phones"][phone] = {"status": "used", "note": why}
            self.state.save()
        self.attention(("used", phone), f"number {mask(phone)} is already used/refused",
                       [why, "This preference will use an available reserve number."],
                       "check the configured number or add reserve numbers, then restart if needed")

    def number_accepted(self, phone):
        with self.lock:
            self.refusals.pop(phone, None)

    def number_refused(self, phone, what):
        with self.lock:
            if what != "number":
                return False
            self.refusals[phone] = self.refusals.get(phone, 0) + 1
            return self.refusals[phone] >= self.opt["maxRejections"]

    def use_parent_fallback(self, why):
        with self.lock:
            self.parent_fallback = True
        self.log.warn("The site insists on an adult's guardian-signature upload; using the applicant's "
                      f"configured signature as authorized. Site says: {why}")

    def require_guardian(self, target):
        with self.lock:
            self.guardian_required.add(target.index)

    def saw_season(self):
        with self.lock:
            if self.season_seen is None:
                self.season_seen = time.monotonic()

    def saw_seats(self, target):
        with self.lock:
            self.blocked.pop(target.index, None)

    def prompt_confirmation(self, phone, target, reason):
        while not self.stop.is_set():
            try:
                answer = input(f"\n{target.label}, {mask(phone)}: reply was uncertain ({reason}). "
                               "Check your application/email. Was it booked? [booked/not booked]: ").strip().lower()
            except EOFError:
                self.log.warn("No interactive input is available. This batch stays pending; restart interactively "
                              "to confirm its outcome. Other batches continue.")
                return None
            if answer in ("b", "booked", "yes", "y"):
                return "booked"
            if answer in ("r", "retry", "not booked", "not-booked", "no", "n"):
                return "not-booked"
            self.log.warn("Please answer 'booked' or 'not booked'.")
        return None

    def confirm_uncertain(self, phone, target, reason):
        question = {"phone": phone, "target": target, "reason": reason,
                    "event": threading.Event(), "answer": None}
        with self.lock:
            self.pending_questions[target.index] = question
        self.attention(("uncertain", target.index), f"confirm whether {target.label} was booked",
                       [reason, "This batch is held. Other batches continue; no reserve number is consumed."],
                       "answer 'booked' or 'not booked' in the terminal after checking the actual outcome")
        self.questions.put(question)
        while not self.stop.is_set() and not question["event"].wait(0.1):
            pass
        with self.lock:
            self.pending_questions.pop(target.index, None)
        if self.stop.is_set():
            raise Cancelled()
        return question["answer"]

    def record_booking(self, phone, target, info):
        with self.lock:
            key = target_key(target)
            if target.index in self.booked:
                return
            status = "dry-run" if info.get("dryRun") else (
                "user-confirmed" if info.get("userConfirmed") else "confirmed")
            booking = {"target": target.label, "targetKey": key, "targetIdentity": target_identity(target),
                       "batch": target.when(), "phone": phone,
                       "status": status, "at": now_ist().isoformat(timespec="seconds")}
            booking.update({k: v for k, v in info.items() if k not in ("dryRun", "userConfirmed")})
            if not self.dry_run:
                self.state.data["bookings"].append(booking)
                self.state.data["phones"][phone] = {"status": "booked", "targetKey": key, "target": target.label}
                self.state.data["assignments"].pop(key, None)
                self.state.save()
            self.bookings.append(booking)
            self.booked[target.index] = booking
            self.phone_state[phone] = "booked"
            self.assignments.pop(key, None)
            self.owners.pop(phone, None)
            self.pending.pop(phone, None)
            self.blocked.pop(target.index, None)
        headline = "DRY RUN OK" if self.dry_run else ("BOOKED (confirmed by user)" if status == "user-confirmed" else "BOOKED")
        self.log.banner([f"{headline}: {target.label} ({target.when()}) with {mask(phone)}",
                         f"Completed {len(self.booked)} of {len(self.targets)} configured preferences.",
                         "Watch the applicant's email for the payment link." if not self.dry_run else "Nothing submitted."])
        if not self.dry_run and self.opt["notify"]:
            threading.Thread(target=notify, args=("IISM booking", target.label), daemon=True).start()

    def run(self):
        workers = []
        confirmation_task = None
        confirmation_results = queue.Queue()

        def ask_for_confirmation(question):
            try:
                answer = self.confirmation_handler(question["phone"], question["target"], question["reason"])
                confirmation_results.put((question, answer, None, ""))
            except BaseException as exc:
                confirmation_results.put((question, None, exc, traceback.format_exc()))

        try:
            self.state.acquire()
            self._restore()
            todo = [t for t in self.targets if t.index not in self.booked]
            waiting = list(todo)
            if self.opt["parallel"]:
                workers = [Worker(self, target) for target in waiting]
                waiting = []
            elif waiting:
                workers = [Worker(self, waiting.pop(0))]
            for worker in workers:
                worker.start()
            last_status = time.monotonic()
            while workers or waiting:
                if not self.opt["parallel"] and not any(w.is_alive() for w in workers) and waiting and not self.stop.is_set():
                    worker = Worker(self, waiting.pop(0))
                    workers.append(worker)
                    worker.start()
                if confirmation_task is None and not self.stop.is_set():
                    try:
                        question = self.questions.get_nowait()
                    except queue.Empty:
                        question = None
                    if question is not None:
                        confirmation_task = threading.Thread(target=ask_for_confirmation, args=(question,),
                                                             name="confirmation-input", daemon=True)
                        confirmation_task.start()
                try:
                    question, answer, error, failure_trace = confirmation_results.get_nowait()
                except queue.Empty:
                    pass
                else:
                    confirmation_task = None
                    if error is not None:
                        self.failures.append(f"confirmation handler failed: {error}")
                        self.log.error(f"confirmation handler failed:\n{failure_trace}")
                        self.stop.set()
                    elif answer in ("booked", "not-booked") and not self.stop.is_set():
                        with self.lock:
                            if self.pending_questions.get(question["target"].index) is question:
                                question["answer"] = answer
                                question["event"].set()
                for worker in workers:
                    worker.join(0.1)
                workers = [w for w in workers if w.is_alive()]
                if self.stop.is_set():
                    waiting.clear()
                if time.monotonic() - last_status >= self.opt["statusEverySec"]:
                    last_status = time.monotonic()
                    self.log.info("status: " + self.status_line())
        except KeyboardInterrupt:
            self.log.warn("Stopping; pending submissions remain saved for confirmation on restart.")
            self.stop.set()
        except ConfigError as exc:
            self.failures.append(str(exc))
            self.log.error(str(exc))
            self.stop.set()
        finally:
            self.stop.set()
            for worker in workers:
                worker.join()
            self.state.release()
        return self.bookings


class Worker(threading.Thread):
    def __init__(self, bot, target):
        super().__init__(name=f"W{target.index + 1}", daemon=True)
        self.bot, self.target = bot, target

    def run(self):
        bot, target = self.bot, self.target
        phone = None
        session = None
        failures = 0
        try:
            while not bot.stop.is_set() and target.index not in bot.booked:
                if phone is None:
                    phone = bot.assign_phone(target)
                    if phone is None:
                        bot.set_status(self.name, f"{target.label}: waiting for a usable reserve number")
                        bot.attention(("exhausted", target.index), f"no usable number for {target.label}", [],
                                      "add a reserve phone number to the config and restart")
                        bot.sleep(10)
                        continue
                    session = Session(bot, phone, f"[{self.name} {mask(phone)}] ")
                if phone in bot.pending:
                    answer = bot.confirm_uncertain(phone, target, "a previous final submit has no confirmed reply")
                    if answer == "booked":
                        bot.record_booking(phone, target, {"userConfirmed": True, "message": "User verified the booking."})
                        return
                    bot.clear_submitting(phone)
                    failures = 0
                bot.set_status(self.name, f"{target.label} with {mask(phone)}")
                try:
                    info = session.book(target)
                except Uncertain as exc:
                    bot.note(session.who, f"{target.label}: final outcome needs your confirmation: {exc}")
                    continue
                except PhoneUsed as exc:
                    bot.phone_used(phone, target, str(exc))
                    phone, session = None, None
                    failures = 0
                except (TargetFull, NotOpen) as exc:
                    failures = 0
                    with bot.lock:
                        bot.blocked[target.index] = {"why": str(exc), "kind": "pending"}
                    bot.note(session.who, f"{target.label}: {exc}; keeping this preference pending")
                    bot.sleep(bot.poll_delay())
                except Refused as exc:
                    failures += 1
                    if bot.number_refused(phone, exc.what):
                        bot.phone_used(phone, target, str(exc))
                        phone, session = None, None
                        failures = 0
                    else:
                        if exc.what == "criteria":
                            bot.note(session.who, f"{target.label}: {exc}; keeping this preference and number pending",
                                     warning=True)
                            bot.attention(("criteria", target.index, str(exc)),
                                          f"the site stopped {target.label} at its criteria page",
                                          [str(exc), "The same phone and preference will keep retrying."],
                                          "review the site's current declaration/criteria and stop the run if action is needed")
                        delay = max(min(10, bot.opt["rejectedRetrySec"]), bot.retry_delay(failures, exc))
                        bot.sleep(delay)
                except Rejected as exc:
                    with bot.lock:
                        bot.blocked[target.index] = {"why": str(exc), "kind": "rejected"}
                    bot.attention(("rejected", target.index, str(exc)), f"the site refused {target.label}",
                                  [str(exc)], fix_hint(str(exc)))
                    return
                except Transient as exc:
                    failures += 1
                    bot.note(session.who, f"{target.label}: {exc}; retrying with capped backoff")
                    bot.sleep(bot.retry_delay(failures, exc))
                else:
                    bot.record_booking(phone, target, info)
                    return
        except Cancelled:
            pass
        except ConfigError as exc:
            with bot.lock:
                bot.failures.append(str(exc))
            bot.stop.set()
            try:
                bot.log.error(str(exc))
            except Exception:
                pass
        except Exception:
            with bot.lock:
                bot.failures.append(f"{target.label}: unexpected worker failure")
            bot.stop.set()
            try:
                bot.log.error(f"[{self.name}] failed:\n{traceback.format_exc()}")
            except Exception:
                pass
        finally:
            bot.set_status(self.name, None)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def describe_plan(settings, log, dry_run=False):
    log.info(f"Applicant: {settings.applicant.get('name', '?')}  (email {settings.applicant.get('email', '?')})")
    log.info(f"Website choices: state '{settings.state}', education '{settings.education}', diet '{settings.diet}'")
    log.info(f"Numbers:   {', '.join(mask(p) for p in settings.phones)}")
    log.info("Preferences and their primary numbers:")
    for t in settings.targets:
        number = mask(settings.phones[t.index]) if t.index < len(settings.phones) else "MISSING"
        log.info(f"  {t.index + 1}. {t.label:<20} {t.when()}  primary {number}")
    mode = "parallel" if settings.opt["parallel"] else "one number at a time"
    reserves = settings.phones[len(settings.targets):]
    log.info(f"Reserve numbers: {', '.join(mask(p) for p in reserves) or 'none'}")
    log.info(f"Goal: every configured preference ({mode})" + (" - DRY RUN" if dry_run else ""))


def wait_for_opening(settings, log, stop):
    """Sleep until startEarlySec before the announced opening (wall clock, survives Mac sleep).
    From then on the bot looks every pollSec and books whenever the courses actually appear."""
    if settings.opens_at is None:
        return
    start_at = settings.opens_at - timedelta(seconds=settings.opt["startEarlySec"])
    last_said = 0.0
    while True:
        left = (start_at - now_ist()).total_seconds()
        if left <= 0:
            return
        every = 60 if left < 900 else 900
        if time.monotonic() - last_said >= every or last_said == 0:
            last_said = time.monotonic()
            log.info(f"Booking is announced for {fmt_when(settings.opens_at)}. Looking for the snow courses from "
                     f"{start_at.astimezone(IST):%H:%M} (in {fmt_duration(left)}) - keep this window open and the "
                     "laptop awake.")
        if stop.wait(min(30, left)):
            return


def print_summary(bot):
    log = bot.log
    lines = []
    for b in bot.bookings:
        status = {"confirmed": "BOOKED", "user-confirmed": "USER CONFIRMED", "dry-run": "DRY RUN OK"}.get(b["status"], b["status"])
        extra = f"  application {b['applicationId']}" if b.get("applicationId") else ""
        lines.append(f"{status:<16} {b['target']:<16} {b.get('batch', ''):<28} number {mask(b['phone'])}{extra}")
    for t in bot.targets:
        if t.index not in bot.booked:
            why = bot.blocked.get(t.index, {}).get("why", "")
            lines.append(f"{'not booked':<16} {t.label:<16} {t.when():<28} {why[:60]}")
    used = [mask(p) for p, st in bot.phone_state.items() if st == "used"]
    unused = [mask(p) for p, st in bot.phone_state.items() if st in ("free", "busy")]
    if used:
        lines.append("Numbers already used on the site: " + ", ".join(used))
    if unused:
        lines.append("Numbers not used: " + ", ".join(unused))
    if bot.pending:
        lines.append("Unresolved submissions are saved: confirm their outcome before booking those batches again.")
    if any(b["status"] in ("confirmed", "user-confirmed") for b in bot.bookings):
        lines.append("Pay through the link sent to the applicant's email before it expires.")
    lines.append(f"Log and saved pages: {bot.out_dir}")
    log.banner(["RESULT"] + lines)


def run_check(settings, errors, warnings, doc_problems, doc_notes, base_url, log):
    log.info(f"Config: {settings.config_path}")
    if settings.targets:
        describe_plan(settings, log)
    if settings.opens_at:
        left = (settings.opens_at - now_ist()).total_seconds()
        log.info(f"Booking opens {fmt_when(settings.opens_at)}" + (f" (in {fmt_duration(left)})" if left > 0 else " (already open)"))
    if settings.dob and settings.targets:
        age = age_on(settings.dob, min(t.start for t in settings.targets))
        log.info(f"Age at the first chosen batch: {age} (allowed 10-40; Gulmarg fee Rs "
                 f"{'10,000' if age <= 25 else '20,000'} at this age, Kargil Rs 5,000 local / 10,000 others)")
        if not 10 <= age <= 40:
            warnings.append(f"age {age} is outside the 10-40 limit")
    log.info("Documents:")
    for note in doc_notes:
        log.info(f"  {note}")
    if not errors and not doc_problems:
        started = time.monotonic()
        try:
            page = Http(base_url, 30, log).get("Admission_instructions.aspx")
            took = time.monotonic() - started
            if page.status == 200 and page.name == INSTRUCTIONS_PAGE and page.value("txtMobileNo") is not None:
                log.info(f"Site: OK - mobile verification page answered in {took:.1f}s")
            else:
                warnings.append(f"site answered HTTP {page.status} with page '{page.name}' - it may be down right now")
        except Transient as e:
            warnings.append(f"could not reach the site right now: {e}")
    for w in warnings:
        log.warn(w)
    for p in errors + doc_problems:
        log.error(p)
    if errors or doc_problems:
        log.error(f"{len(errors) + len(doc_problems)} thing(s) to fix before booking day.")
        return 1
    log.info("All good. When you intend to proceed, rerun the same invocation with the same --config and "
             "--base-url values; remove only --check and preserve any rehearsal safety flags.")
    return 0


def with_retries(bot, action, tries=15):
    for n in range(1, tries + 1):
        try:
            return action()
        except Transient as e:
            if n == tries:
                raise
            bot.log.info(f"  ({e} - retrying)")
            bot.sleep(bot.backoff(n))


def _run_discover(bot, phone):
    """Walk the live form with one number and show what the site offers. Never submits."""
    log = bot.log
    session = Session(bot, phone, f"[discover {mask(phone)}] ")
    def fresh(action):
        session.restart()
        return action()
    try:
        page = with_retries(bot, lambda: fresh(session.open_form))
    except PhoneUsed as e:
        log.error(f"Number {mask(phone)} is already used on the site: {e}")
        return 1
    except NotOpen as e:
        log.error(f"The site says booking is not open: {e}")
        return 1
    courses = real_options(page.options("ddlCourse"))
    log.info(f"Courses on the form: {len(courses)}")
    for value, text in courses:
        def choose(v=value):
            p = session.open_form()
            return session.select(p, "ddlCourse",
                                  dict(first_values(p, "ddlCourseLevel", "ddlScheduleCourse"), ddlCourse=v))
        try:
            p = with_retries(bot, lambda: fresh(choose))
        except (TargetFull, NotOpen) as e:
            log.info(f"  [{value}] {text}: {e}")
            continue
        log.info(f"  [{value}] {text}")
        log.info(f"      levels:  {' | '.join(t for _, t in real_options(p.options('ddlCourseLevel'))) or '-'}")
        for v, t in real_options(p.options("ddlScheduleCourse")):
            log.info(f"      batch:   {t}")
    log.info("Your choices on today's site:")
    for target in bot.targets:
        try:
            sel = with_retries(bot, lambda: session.cascade(target))
            log.info(f"  {target.label:<18} -> {sel}")
        except (TargetFull, NotOpen, Rejected) as e:
            log.info(f"  {target.label:<18} -> {e}")
        except PhoneUsed as e:
            log.error(f"  number {mask(phone)} is already used: {e}")
            return 1
    log.info("Nothing was submitted.")
    return 0


def run_discover(bot, phone):
    try:
        return _run_discover(bot, phone)
    except Transient as exc:
        bot.log.error(f"Discovery stopped after fresh-session retries: {exc}")
        bot.log.error("Rerun the same --discover invocation, preserving --config, --base-url and --phone.")
        return 1


def find_config(arg):
    if arg:
        path = Path(os.path.expanduser(arg))
        if not path.exists():
            raise ConfigError(f"config file not found: {path}")
        return path.resolve()
    if (SCRIPT_DIR / "config.jsonc").exists():
        return SCRIPT_DIR / "config.jsonc"
    raise ConfigError("no config found - copy config.example.jsonc to config.jsonc and fill it in")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Book every configured IISM course preference.")
    ap.add_argument("--config", help="config file (default: config.jsonc next to this script)")
    ap.add_argument("--check", action="store_true",
                    help="startup preflight and, if valid, one read-only site check")
    ap.add_argument("--discover", action="store_true", help="inspect course/level/batch postbacks without final submission")
    ap.add_argument("--dry-run", action="store_true", help="run preference workers without final submissions")
    ap.add_argument("--now", action="store_true", help="start immediately")
    ap.add_argument("--phone", help="configured number to use with --discover")
    ap.add_argument("--reset-state", action="store_true", help="reset only progress without existing or pending applications")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    log, bot, awake = Log(), None, None
    try:
        config_path = find_config(args.config)
        settings, errors, warnings = parse_settings(load_jsonc(config_path), config_path)
        identity = json.dumps([str(config_path.resolve()), origin(args.base_url), settings.season])
        digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
        out_dir = SCRIPT_DIR / "runs" / f"{config_path.stem}-{digest}"
        problems, notes = prepare_documents(settings, out_dir)
        if args.check:
            try:
                progress = State(out_dir / "state.json", True)
                progress.validate_identity(expected_state_identity(settings, args.base_url))
            except ConfigError as exc:
                problems.append(str(exc))
            return run_check(settings, errors, warnings, problems, notes, args.base_url, log)
        log.open(out_dir / "bot.log")
        log.info("Startup preflight: configuration, form rules and documents")
        for note in notes:
            log.info(note)
        for warning in warnings:
            log.warn(warning)
        if args.discover and settings.phones:
            errors, problems = [], []
        if errors or problems:
            for problem in errors + problems:
                log.error(problem)
            log.error("Fix these startup problems before booking; --check lists them without submitting.")
            return 2
        log.info("Startup preflight passed; all workers share the prepared upload snapshots.")
        if args.reset_state:
            old = State(out_dir / "state.json", True)
            try:
                old.acquire()
                if old.data["bookings"] or any(i["status"] == "submitting" for i in old.data["phones"].values()):
                    raise ConfigError("Cannot reset existing or unresolved applications. Reconcile them first; "
                                      "use a distinct season/config for a new booking plan.")
                if old.path.exists():
                    backup = old.path.with_name(f"state.json.bak-{uuid.uuid4().hex}")
                    old.path.rename(backup)
                    log.info(f"Progress without applications moved to {backup}")
            finally:
                old.release()
        bot = Bot(settings, out_dir, base_url=args.base_url, dry_run=args.dry_run or args.discover, log=log)
        if args.discover:
            phone = parse_phone(args.phone) if args.phone else settings.phones[0]
            if phone not in settings.phones:
                raise ConfigError("--phone must be one of this config's phoneNumbers")
            return run_discover(bot, phone)
        if settings.opt["preventSleep"]:
            awake = keep_awake()
        describe_plan(settings, log, args.dry_run)
        if not bot.finished() and not args.now:
            wait_for_opening(settings, log, bot.stop)
        if not bot.finished():
            bot.run()
        print_summary(bot)
        return 0 if bot.finished() and not bot.failures else 1
    except KeyboardInterrupt:
        if bot is not None:
            bot.stop.set()
        log.warn("Stopped. Any unresolved final requests remain saved for confirmation on restart.")
        return 1
    except (ConfigError, OSError, ValueError) as exc:
        log.error(str(exc))
        return 2
    finally:
        if awake is not None:
            awake.terminate()
        if log.file is not None:
            log.file.close()


if __name__ == "__main__":
    sys.exit(main())
