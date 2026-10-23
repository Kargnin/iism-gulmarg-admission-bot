#!/usr/bin/env python3
"""
IISM Gulmarg admission bot (Python 3.8+, standard library only).

Books seats on https://admission.iismgulmarg.in by driving its ASP.NET
WebForms pages over plain HTTP - no browser needed. Built for booking day,
when the site is overloaded and keeps falling over:

  * Course priorities - keeps trying your 1st choice until the site shows it
    is full, then moves on to the 2nd, and so on.
  * Several phone numbers - the site allows one booking per number, so once a
    number has booked, the next number goes after your next choice. You end
    up with one booking per number, best choices first.
  * Never gives up - timeouts, 5xx errors, ASP.NET error pages and lost
    sessions are retried with backoff until everything you asked for is
    booked or you press Ctrl+C.
  * Resumable - progress is saved under runs/<config name>/, so a restart
    skips numbers and batches that are already booked.

Usage:
  python3 iism-bot.py --check        check config + documents and reach the site (sends no form data)
  python3 iism-bot.py --discover     list the live courses/batches using your first number (never submits)
  python3 iism-bot.py --dry-run      full run that stops right before each final submit
  python3 iism-bot.py                book (waits until shortly before the opening time)
  python3 iism-bot.py --now          book, starting immediately
  python3 iism-bot.py --config friend.jsonc   use someone else's config
"""

import argparse
import html
import http.client
import http.cookiejar
import json
import os
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
from difflib import get_close_matches
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
    "pollSec": 2,                # until the snow courses are listed, look this often (one number at a time)
    "requestTimeoutSec": 45,
    "submitTimeoutSec": 120,
    "retryDelaySec": 1.0,        # first retry delay; grows with repeated failures ...
    "maxRetryDelaySec": 10.0,    # ... up to this
    "fullConfirmations": 2,      # a choice must look full this many times before moving on
    "unlistedGraceSec": 300,     # once courses are listed, a batch that never showed up gets this long
    "seatGraceSec": 120,         # "0 seats" while no batch shows seats counts as full only after this long
    "recheckBlockedSec": 60,     # when every choice is full, look at them again this often
    "maxRejections": 3,          # site rejected the form this often for a choice -> move on
    "rejectedRetrySec": 5,       # pause after the site rejected the form
    # The form's own check asks for a parent/guardian signature even from adults. If the site
    # refuses an adult's form for that reason, resend with the applicant's signature in that slot.
    "parentSignatureFallback": True,
    "statusEverySec": 30,
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
SUCCESS_PHRASES = [
    "submitted successfully", "successfully submitted", "has been submitted", "application submitted",
    "form submitted", "registered successfully", "successfully registered", "saved successfully",
    "successfully saved", "application received",
]
AGE_PHRASES = ["age allow", "not eligible", "age should", "age must", "age limit", "invalid date"]
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
LEVEL_WORDS = [("Basic", ("basic", "beginner")), ("Intermediate", ("inter",)), ("Advance", ("adv",))]  # your text
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


class Resend(Transient):
    """The site refused the form for something we can fix ourselves: send it again."""


class SessionLost(Exception):
    """The site dropped our session or page state: verify the number again."""

    def __init__(self, message, resume=False):
        super().__init__(message)
        self.resume = resume  # True: we already hold the page to continue from


class PhoneUsed(Exception):
    """The site says this number has already been used."""


class NotOpen(Exception):
    """Booking is not open yet, or nothing is listed yet."""


class TargetFull(Exception):
    """This choice shows no seats, or is not on the list."""

    def __init__(self, message, kind="shown"):
        super().__init__(message)
        # "shown": 0 seats shown; "gone": was listed, now isn't; "unlisted": not listed yet;
        # "submit": the site refused the submit itself, whatever the seat count says
        self.kind = kind


class Rejected(Exception):
    """The site refused the filled-in form."""

    def __init__(self, message, final=False):
        super().__init__(message)
        self.final = final  # retrying this choice cannot help (e.g. outside its age limit)


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


def to_int(value):
    m = re.search(r"\d+", str(value or "").replace(",", ""))
    return int(m.group()) if m else None


def now_ist():
    return datetime.now(IST)


def age_on(born, day):
    return day.year - born.year - ((day.month, day.day) < (born.month, born.day))


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
    for vid in re.findall(r'(\w+)\.isvalid\s*=\s*"False"', body):
        m = re.search(re.escape(vid) + r'\.errormessage\s*=\s*"((?:[^"\\]|\\.)*)"', body)
        found.append(js_text(m.group(1)) if m else vid + " failed")
    out = []
    for text in found:
        if text and text not in out:
            out.append(text)
    return out


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

def dates_in(text):
    """Dates in a dropdown text as (year or None, month, day, reading).
    Numeric dates are read both day-first and month-first; matching prefers day-first."""
    t = str(text or "").lower()
    out = []

    def add(y, m, d, how):
        if y is not None and y < 100:
            y += 2000
        if 1 <= m <= 12 and 1 <= d <= 31:
            out.append((y, m, d, how))

    for a, b, c in re.findall(r"(\d{1,4})[/.\-](\d{1,2})[/.\-](\d{2,4})", t):
        if len(a) == 4:
            add(int(a), int(b), int(c), "iso")
        else:
            add(int(c), int(b), int(a), "dmy")
            add(int(c), int(a), int(b), "mdy")
    # "20 Dec 2026", "20-Dec-26", "20th December"; a 2-digit year only when tightly attached,
    # so "20 Dec - 02 Jan" is not read as the year 2002
    for d, mon, y4, y2 in re.findall(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s\-/.,]*([a-z]{3,9})\.?"
                                     r"(?:[\s\-/.,']*(\d{4})\b|[\-/.'](\d{2})\b)?", t):
        if mon[:3] in MONTHS:
            add(int(y4 or y2) if (y4 or y2) else None, MONTHS[mon[:3]], int(d), "text")
    for mon, d, y in re.findall(r"\b([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b,?(?:\s*(\d{4}))?", t):
        if mon[:3] in MONTHS:
            add(int(y) if y else None, MONTHS[mon[:3]], int(d), "text")
    return out


def date_match(found, day):
    """2 = same date read day-first (or unambiguous), 1 = only month-first, 0 = no."""
    if day is None:
        return 0
    best = 0
    for y, m, d, how in found:
        if (m, d) == (day.month, day.day) and (y is None or y == day.year):
            best = max(best, 1 if how == "mdy" else 2)
    return best


def parse_day(value):
    """'2027-01-03', '03/01/2027' or '3 Jan 2027' -> date (day-first for numbers)."""
    if not value:
        return None
    if isinstance(value, date):
        return value
    for y, m, d, how in dates_in(value):
        if how != "mdy" and y:
            try:
                return date(y, m, d)
            except ValueError:
                pass
    return None


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
    if target.kind != "Snow Skiing" or not target.level or target.level_text:
        return 0
    named = [level for level, pattern in LEVEL_PATTERNS.items() if re.search(pattern, norm(text))]
    if not named:
        return 0
    return 6 if target.level in named else -100


def course_candidates(options, target, hint=None):
    """Course dropdown entries that could hold this choice, most likely first."""
    real = real_options(options)
    if target.course_text:
        picks = [o for o in real if norm(target.course_text) in norm(o[1])]
    else:
        fits = lambda text: location_score(text, target.location) >= 0 and level_score(text, target) >= 0
        scored = []
        for n, (value, text) in enumerate(real):
            s = kind_score(text, target.kind)
            if s <= 0:
                continue
            s += location_score(text, target.location) + level_score(text, target)
            if date_match(dates_in(text), target.start):
                s += 8
            if s > 0:
                scored.append((-s, n, (value, text)))
        picks = [o for _, _, o in sorted(scored)]
        if target.kind != "Snow Skiing":
            # snowboarding / MOI may be offered as a level of the skiing course
            picks += [o for o in real if o not in picks and kind_score(o[1], "Snow Skiing") > 0 and fits(o[1])]
        # then anything not clearly another kind of course, in case the site names it unexpectedly
        # (today the real form lists just "Adventure Courses"); the batch dates decide
        exclude = KIND_PATTERNS[target.kind][1]
        picks += [o for o in real if o not in picks and not re.search(exclude, norm(o[1])) and fits(o[1])]
    if hint:
        picks.sort(key=lambda o: o[0] != hint)
    return picks


def match_level(options, target, loose):
    real = real_options(options)
    if target.level_text:
        pick = next((o for o in real if norm(target.level_text) in norm(o[1])), None)
    elif target.level:
        pick = next((o for o in real if re.search(LEVEL_PATTERNS[target.level], norm(o[1]))), None)
    else:
        pick = next((o for o in real if kind_score(o[1], target.kind) > 0), None)
        if pick is None and loose and real:
            pick = real[0]  # e.g. the only level of the snowboarding course
    if pick is None and real and all(norm(t) in GENERIC_LEVELS for _, t in real):
        pick = real[0]  # the course has no real levels, just "None"
    return pick


def state_aliases(name):
    n = norm(name)
    return next(([a for a in group if a != n] for group in STATE_ALIASES if n in group), [])


def match_batch(options, target):
    best, best_score = None, 0
    for value, text in real_options(options):
        if target.batch_text:
            score = 100 if norm(target.batch_text) in norm(text) else 0
        else:
            found = dates_in(text)
            start = date_match(found, target.start)
            if not start:
                continue
            score = (10 * start + 5 * date_match(found, target.end) + location_score(text, target.location)
                     + level_score(text, target))
        if score > best_score:
            best, best_score = (value, text), score
    return best


def is_season_batch(text, batches):
    """Whether a dropdown text is one of this season's batches (by its start date)."""
    found = dates_in(text)
    return any(date_match(found, b["from"]) == 2 for b in batches.values())


def match_choice(options, wanted, aliases=()):
    """Pick a dropdown entry by its text or value: exact, then prefix, then contains, then fuzzy."""
    real = real_options(options)
    keys = [k for k in [norm(wanted)] + [norm(a) for a in aliases] if k]
    if not real or not keys:
        return None
    for k in keys:
        for v, t in real:
            if k in (norm(t), norm(v)):
                return v, t
    for k in keys:
        for v, t in real:
            if norm(t).startswith(k) or k.startswith(norm(t)):
                return v, t
    for k in keys:
        for v, t in real:
            if k in norm(t):
                return v, t
    names = [norm(t) for _, t in real]
    close = get_close_matches(keys[0], names, n=1, cutoff=0.75)
    return real[names.index(close[0])] if close else None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

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
        return json.loads("".join(result))
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path}: {e.msg} at line {e.lineno}, column {e.colno}")


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
    if "board" in t:
        return "Snow Boarding"
    if re.search(KIND_PATTERNS["MOI"][0], t):
        return "MOI"
    return "Snow Skiing"


def course_level(text):
    t = norm(text)
    return next((level for level, words in LEVEL_WORDS if has_any(t, words)), None)


def parse_priority(item, index, batches):
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
    start = parse_day(spec.get("from")) or batch.get("from")
    if start is None:
        raise ConfigError(f"coursePriorities: {item!r} needs a batch code (G1..G6, K1, K2) or a \"from\" date")
    kind = course_kind(spec.get("course", ""))
    level = course_level(spec.get("level", ""))
    if kind == "Snow Skiing" and level is None and not spec.get("levelText"):
        level = "Basic"
    name = bid or start.strftime("%d%b")
    label = spec.get("label") or f"{name} {level if kind == 'Snow Skiing' else kind}"
    return Target(index, label, bid, spec.get("location") or batch.get("location", ""), start,
                  parse_day(spec.get("to")) or batch.get("to"), kind, level,
                  spec.get("courseText", ""), spec.get("levelText", ""), spec.get("batchText", ""))


def parse_phone(raw):
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def load_season(cfg, config_dir):
    path = resolve_path(cfg["scheduleFile"], config_dir) if cfg.get("scheduleFile") else SCRIPT_DIR / "schedule.json"
    data = load_jsonc(path) if path.exists() else {}
    batches = {}
    merged = dict(data.get("batches") or {})
    merged.update(cfg.get("batches") or {})
    for bid, b in merged.items():
        start = parse_day(b.get("from"))
        if start is None:
            raise ConfigError(f"batch {bid}: \"from\" date is missing or unreadable")
        batches[bid.upper()] = {"location": b.get("location", ""), "from": start,
                                "to": parse_day(b.get("to")), "offers": b.get("offers") or [],
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


def parse_settings(cfg, config_path):
    errors, warnings = [], []
    s = Settings()
    s.config_path = Path(config_path) if config_path else None
    config_dir = s.config_path.parent if s.config_path else Path.cwd()
    s.batches, s.opens_at, s.season = load_season(cfg, config_dir)
    a = dict(cfg.get("applicant") or {})
    if "diet" not in a and "vegetarian" in a:  # the old bot's name for it
        a["diet"] = a["vegetarian"]
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
    email = s.applicant.get("email", "")
    if email and "FILL_ME" not in email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        errors.append(f"applicant.email \"{email}\" does not look like an email address")
    pin = str(addr.get("pincode") or "")
    if pin and "FILL_ME" not in pin and not re.fullmatch(r"\d{6}", pin.strip()):
        warnings.append(f"address.pincode \"{pin}\" is not 6 digits")

    raw_phones = cfg.get("phoneNumbers") or a.get("mobileNumbers") or ([a["mobile"]] if a.get("mobile") else [])
    s.phones = []
    for raw in raw_phones:
        if "FILL_ME" in str(raw):
            continue
        phone = parse_phone(raw)
        if phone is None:
            errors.append(f"phone number \"{raw}\" is not a 10-digit Indian mobile number")
        elif phone in s.phones:
            warnings.append(f"phone number {raw} is listed twice")
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
        if any(o.label == t.label for o in s.targets):
            warnings.append(f"coursePriorities lists {t.label} twice; keeping the first")
            continue
        offers = (s.batches.get(t.batch_id) or {}).get("offers") or []
        if offers and t.kind not in offers:
            warnings.append(f"{t.label}: the published schedule has no {t.kind} seats in batch {t.batch_id}")
        s.targets.append(t)
    if not s.targets and not any(p.startswith("coursePriorities") for p in placeholders):
        errors.append("coursePriorities is empty - list your choices, e.g. [\"G2 Basic\", \"G3 Basic\"]")
    if any(t.level in ("Intermediate", "Advance") for t in s.targets) and not (cfg.get("documents") or {}).get("skiingCertificate"):
        warnings.append("Intermediate/Advance choices need your previous course certificate in documents.skiingCertificate")

    # the parent/guardian signature is only for participants under 18 (by the first chosen batch)
    s.minor = bool(s.dob and s.targets and age_on(s.dob, min(t.start for t in s.targets)) < 18)

    wanted = int(cfg.get("maxBookings") or 0)
    s.max_bookings = min(wanted or len(s.phones), len(s.phones) or 1, len(s.targets) or 1)
    s.opt = dict(BOT_DEFAULTS)
    s.opt.update({k: v for k, v in (cfg.get("bot") or {}).items() if not str(k).startswith("_")})
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
    dst.parent.mkdir(parents=True, exist_ok=True)
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
    for key, _, _, required in DOCUMENTS:
        src = settings.doc_sources.get(key)
        if src is None:
            if key in settings.doc_placeholders:  # reported already
                continue
            if required is True:
                problems.append(f"documents.{key} is not set (the form requires it)")
            elif required == "minor" and settings.minor:
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
        dst = out_dir / "upload-ready" / f"{key}.jpg"
        fresh = dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime and dst.stat().st_size <= MAX_UPLOAD_BYTES
        try:
            if not fresh:
                convert_to_jpeg(src, dst)
        except ConfigError as e:
            problems.append(f"documents.{key}: {e}")
            continue
        except (OSError, subprocess.SubprocessError) as e:
            problems.append(f"documents.{key}: converting {src.name} failed: {e}")
            continue
        settings.docs[key] = dst
        page_note = " (first page)" if src.suffix.lower() == ".pdf" else ""
        notes.append(f"{key}: {src.name} {why} -> converted{page_note} to {dst} ({dst.stat().st_size // 1000} KB)")
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
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(path, "a", encoding="utf-8")

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


def notify(title, message):
    sys.stdout.write("\a")
    sys.stdout.flush()
    if sys.platform == "darwin" and shutil.which("osascript"):
        script = (f"display notification {json.dumps(message, ensure_ascii=False)} "
                  f"with title {json.dumps(title, ensure_ascii=False)} sound name \"Glass\"")
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

    def __init__(self, url, status, body, fresh=True):
        self.url, self.status, self.body = url, status, body
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
        return [c["name"] for c in self.controls if c["tag"] == "input" and c["type"] == "file" and c["name"]]

    def submit_button(self):
        buttons = [(c["name"], c["value"]) for c in self.controls
                   if c["tag"] == "input" and c["type"] == "submit" and c["name"]]
        return next((b for b in buttons if "submit" in b[0].lower()), buttons[0] if buttons else None)

    def messages(self):
        return server_messages(self.body)

    def text(self):
        return visible_text(self.body)

    def title(self):
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", self.body)
        return clean_html(m.group(1)) if m else ""


class Multipart:
    def __init__(self):
        self.boundary = "----WebKitFormBoundary" + uuid.uuid4().hex[:16]
        self.parts = []

    def _head(self, name, filename=None):
        disposition = f'form-data; name="{name}"' + ("" if filename is None else f'; filename="{filename}"')
        return f"--{self.boundary}\r\nContent-Disposition: {disposition}\r\n".encode("utf-8")

    def field(self, name, value):
        self.parts.append(self._head(name) + b"\r\n" + str(value).encode("utf-8") + b"\r\n")

    def file(self, name, path=None):
        if path:
            path = Path(path)
            data, filename = path.read_bytes(), path.name.replace('"', "")
            ctype = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        else:
            data, filename, ctype = b"", "", "application/octet-stream"
        self.parts.append(self._head(name, filename) + f"Content-Type: {ctype}\r\n\r\n".encode() + data + b"\r\n")

    def body(self):
        return b"".join(self.parts) + f"--{self.boundary}--\r\n".encode()

    def content_type(self):
        return "multipart/form-data; boundary=" + self.boundary


class Http:
    """Cookie-keeping HTTP client; every network-level failure becomes Transient."""

    def __init__(self, base_url, timeout, log):
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self.log = log
        self.jar = http.cookiejar.CookieJar()
        self._build(verify=True)

    def _build(self, verify):
        ctx = ssl.create_default_context()
        if not verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self.verified = verify
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                                                  urllib.request.HTTPCookieProcessor(self.jar))

    def get(self, path):
        return self.send("GET", urllib.parse.urljoin(self.base_url, path))

    def send(self, method, url, data=None, content_type=None, referer=None, timeout=None):
        timeout = timeout or self.timeout
        headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                   "Accept-Language": "en-IN,en;q=0.9", "Cache-Control": "no-cache"}
        if content_type:
            headers["Content-Type"] = content_type
        if referer:
            headers["Referer"] = referer
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=timeout) as resp:
                raw, status, final = resp.read(), resp.status, resp.geturl()
        except urllib.error.HTTPError as e:
            try:
                raw = e.read()
            except Exception:
                raw = b""
            status, final = e.code, e.geturl() or url
        except urllib.error.URLError as e:
            if isinstance(e.reason, ssl.SSLCertVerificationError) and self.verified:
                self.log.warn("The site's HTTPS certificate did not verify - continuing without verification")
                self._build(verify=False)
                return self.send(method, url, data, content_type, referer, timeout)
            raise Transient(f"network error: {e.reason}")
        except (socket.timeout, TimeoutError):
            raise Transient(f"no answer within {timeout:.0f}s")
        except (http.client.HTTPException, OSError) as e:
            raise Transient(f"connection problem: {type(e).__name__} {e}".strip())
        fresh = method == "GET" or final.split("#")[0] != url
        return Page(final, status, raw.decode("utf-8", errors="replace"), fresh=fresh)


# ---------------------------------------------------------------------------
# One browser-like visit with one phone number
# ---------------------------------------------------------------------------

class Selection:
    def __init__(self, course, level, batch, state, seats, fee, page):
        self.course, self.level, self.batch, self.state = course, level, batch, state
        self.seats, self.fee, self.page = seats, fee, page

    def is_basic(self):
        return self.level is not None and "basic" in norm(self.level[1])

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
        self.unconfirmed = None  # Target of a final submit whose answer never arrived
        self.progress = 0       # successful requests so far (resets the failure backoff)
        self.blank_returns = 0
        self.static = {}        # page name -> messages that are part of the page itself
        self.http = None
        self.fresh_needed = True  # the next attempt starts from scratch (see cascade)
        self.attempt_target = None
        self.restart()

    def restart(self, keep=False):
        """Start over from mobile verification (keep=True: continue from self.entry)."""
        if not keep or self.http is None:
            self.http = Http(self.bot.base_url, self.bot.opt["requestTimeoutSec"], self.bot.log)
            self.entry = None
        self.page = None        # latest admission form page
        self.form_url = None
        self.steps = []         # steps of the current attempt already done on self.page
        self.cursor = 0

    # -- plumbing ------------------------------------------------------------

    def checked(self, page):
        """Raise for error pages; otherwise count the success and hand the page back."""
        if page.status >= 400 or not page.is_aspx:
            if has_any(page.body.lower(), VIEWSTATE_ERRORS):
                raise SessionLost("the site rejected our page state")
            what = f"HTTP {page.status}" if page.status >= 400 else "error page"
            title = page.title()
            raise Transient(what + (f" ({title[:80]})" if title else ""))
        self.progress += 1
        return page

    def get(self, path):
        return self.checked(self.http.get(path))

    def postback(self, page, event, values, files=None, timeout=None):
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
        return self.http.send("POST", page.post_url, body, ctype, referer=page.url, timeout=timeout)

    def msgs(self, page):
        """Server messages minus the ones that are part of the page itself."""
        found = page.messages()
        if page.fresh and page.name not in self.static:
            verdicts = PHONE_USED_PHRASES + FULL_PHRASES + CLOSED_PHRASES + SUCCESS_PHRASES
            self.static[page.name] = {m for m in found if not has_any(m.lower(), verdicts)}
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

    # -- steps ---------------------------------------------------------------

    def open_form(self):
        """Mobile verification + criteria; leaves self.page on the admission form."""
        if self.page is not None:
            return self.page
        for _ in range(4):
            if self.entry is None:
                self.entry = self.get(self.form_url or "Admission_instructions.aspx")
            page = self.entry
            if page.name == FORM_PAGE:
                self.verdict(page, full_ok=False)
                self.entry = None
                self.page, self.form_url = page, page.url
                return page
            if page.name == INSTRUCTIONS_PAGE:
                self.form_url = None
                nxt = self.checked(self.postback(page, "lnkbtnSubmit", {"txtMobileNo": self.phone}))
                self.entry = nxt
                said = self.verdict(nxt, deep=nxt.name == INSTRUCTIONS_PAGE, full_ok=False)
                if nxt.name == INSTRUCTIONS_PAGE:
                    raise Transient("mobile verification did not go through" + (f": {'; '.join(said)}" if said else ""))
            elif page.name == CRITERIA_PAGE:
                nxt = self.checked(self.postback(page, "lnkbtnNext", {"chkAccept": "on"}))
                self.entry = nxt
                said = self.verdict(nxt, deep=nxt.name == CRITERIA_PAGE, full_ok=False)
                if nxt.name == CRITERIA_PAGE:
                    raise Transient("the criteria page did not move on" + (f": {'; '.join(said)}" if said else ""))
            else:
                self.entry = None
                raise Transient(f"unexpected page '{page.name}'")
        raise Transient("could not get through to the admission form")

    def select(self, page, event, values):
        """Postback after changing a dropdown/date; returns the refreshed form page."""
        values = dict(values)
        values.setdefault("txtMobileNo", self.phone)
        nxt = self.checked(self.postback(page, event, values))
        if nxt.name != FORM_PAGE:
            self.page, self.steps = None, []
            if nxt.name in (INSTRUCTIONS_PAGE, CRITERIA_PAGE):
                self.entry = nxt
                raise SessionLost(f"the site sent us back to {nxt.name}", resume=True)
            raise Transient(f"unexpected page '{nxt.name}' after choosing {event}")
        self.verdict(nxt)
        return nxt

    def step(self, key, action):
        """Run one step of this attempt, unless a retry of the same attempt already got through it
        (after a timeout or a 5xx we pick up at the step that failed, on the same session)."""
        i = self.cursor
        self.cursor += 1
        if self.steps[i:i + 1] == [key] and self.page is not None:
            return self.page
        del self.steps[i:]
        self.page = action(self.page)
        self.steps.append(key)
        return self.page

    def cascade(self, target):
        """Choose course, level, batch, state and date of birth for this target.

        Each new attempt starts from scratch - new session, mobile number, terms - because the
        site only shows newly listed courses to a fresh visit (reloading the form isn't enough),
        courses appear one by one, and they vanish from the list once full. Only a retry after a
        crash or timeout continues the attempt in progress."""
        bot = self.bot
        if self.fresh_needed or self.attempt_target is not target:
            self.restart()
            self.fresh_needed, self.attempt_target = False, target
        self.cursor = 0
        page = self.step(("form",), lambda p: self.open_form())
        course_options = page.options("ddlCourse")
        bot.saw_courses(course_options)
        if bot.season_seen is None and not bot.scan_due(course_options):
            raise NotOpen("the snow courses are not listed yet" + listing(page, "ddlCourse"))
        found = None
        for value, text in course_candidates(course_options, target, bot.course_hint.get(target.index)):
            self.cursor = 1
            page = self.step(("course", value), lambda p, v=value: self.select(
                p, "ddlCourse", dict(first_values(p, "ddlCourseLevel", "ddlScheduleCourse"), ddlCourse=v)))
            batches = page.options("ddlScheduleCourse")
            if any(is_season_batch(t, bot.s.batches) for _, t in real_options(batches)):
                bot.saw_season()
            batch = match_batch(batches, target)
            if batch:
                found = ((value, text), batch)
                break
        if not found:
            if bot.season_seen is None:
                raise NotOpen("the snow courses are not listed yet" + listing(page, "ddlCourse"))
            raise TargetFull(f"{target.when()} is not listed" + listing(page, "ddlScheduleCourse"),
                             kind=bot.unlisted_kind(target))
        course, batch = found
        bot.course_hint[target.index] = course[0]

        level = None
        level_options = page.options("ddlCourseLevel")
        if real_options(level_options):
            level = match_level(level_options, target, loose=kind_score(course[1], target.kind) > 0)
            if level is None:
                raise TargetFull(f"level {target.level or target.kind} is not listed" + listing(page, "ddlCourseLevel"),
                                 kind=bot.unlisted_kind(target))
        bot.saw_listed(target)
        values = {"ddlScheduleCourse": batch[0]}
        if level:
            values["ddlCourseLevel"] = level[0]
        page = self.step(("batch", values.get("ddlCourseLevel"), batch[0]),
                         lambda p: self.select(p, "ddlScheduleCourse", values))
        seats = to_int(page.value("txtAvailableSeat"))
        if seats == 0:
            if bot.needs_evidence():
                self.probe(page, batch[0], values)
            raise TargetFull("0 seats left", kind="shown")
        if seats:
            bot.saw_seats(target)

        state = None
        state_options = page.options("ddlState")
        if state_options is not None:
            state = match_choice(state_options, bot.s.state, state_aliases(bot.s.state))
            if state is None:
                raise Rejected(f"state \"{bot.s.state}\" is not in the site's list" + listing(page, "ddlState"))
            page = self.step(("state", state[0]), lambda p: self.select(p, "ddlState", {"ddlState": state[0]}))
        dob = bot.s.applicant.get("dateOfBirth", "")
        page = self.step(("dob", dob), lambda p: self.select(p, "txtDateOfBirth", {"txtDateOfBirth": dob}))
        # outside the age limit the site says "Your Age N Years , Age Allow X To Y Years." and clears the date
        said = self.msgs(page)
        echoed = page.value("txtDateOfBirth")
        if has_any(" | ".join(said).lower(), AGE_PHRASES) or (echoed is not None and not echoed.strip()):
            raise Rejected(f"date of birth not accepted for {target.label}: {'; '.join(said) or 'date cleared'}",
                           final=True)
        return Selection(course, level, batch, state, seats, page.value("txtCourseFee"), page)

    def probe(self, page, skip, values):
        """0 seats right after the courses appear can just mean seats aren't loaded yet:
        look at up to two other batches to learn whether anything is bookable."""
        others = [v for v, _ in real_options(page.options("ddlScheduleCourse")) if v != skip][:2]
        for value in others:
            page = self.select(page, "ddlScheduleCourse", dict(values, ddlScheduleCourse=value))
            if (to_int(page.value("txtAvailableSeat")) or 0) > 0:
                self.bot.saw_seats(None)
                return

    def submit(self, target, sel):
        bot = self.bot
        page = sel.page
        values = dict(bot.s.form_values)
        values.update({"ddlCourse": sel.course[0], "ddlScheduleCourse": sel.batch[0],
                       "txtMobileNo": self.phone, "chkIAgree": "on"})
        if sel.level:
            values["ddlCourseLevel"] = sel.level[0]
        if sel.state:
            values["ddlState"] = sel.state[0]
        for field, wanted in (("ddlEduationQualification", bot.s.education), ("ddlVegetarianOrNon", bot.s.diet)):
            options = page.options(field)
            if options is None:
                values[field] = wanted
                continue
            pick = match_choice(options, wanted)
            if pick is None:
                raise Rejected(f"\"{wanted}\" is not an option for {field}" + listing(page, field))
            values[field] = pick[0]
        files = {}
        for key, input_name, flag, _ in DOCUMENTS:
            path = bot.s.docs.get(key)
            if key == "skiingCertificate" and sel.is_basic():
                path = None
            if key == "parentSignature" and path is None and bot.parent_fallback:
                path = bot.s.docs.get("signature")
            files[input_name] = path
            values[flag] = "Yes" if path else "No"
        event = ""
        button = page.submit_button()
        if button:
            values[button[0]] = button[1]
        else:
            m = re.search(r"""(?:__doPostBack\(|PostBackOptions\()\s*['"](\w*submit\w*)['"]""", page.body, re.I)
            if m:
                event = m.group(1)
            else:
                values["btnSubmit"] = "Submit"

        bot.mark_submitting(self.phone, target)
        bot.log.info(f"{self.who}SUBMITTING {target.label}: {sel}")
        try:
            resp = self.postback(page, event, values, files=files, timeout=bot.opt["submitTimeoutSec"])
            saved = bot.save_page(self.who, f"submit-{target.label}", resp)
            result = self.judge_submit(resp, target, sel, saved)
        except Resend:
            if self.unconfirmed is None:
                bot.clear_submitting(self.phone)
            raise
        except (Transient, SessionLost):
            self.unconfirmed = target  # the site may have taken it; we never saw the answer
            raise
        except (Rejected, TargetFull, NotOpen):
            if self.unconfirmed is None:
                bot.clear_submitting(self.phone)
            raise
        self.unconfirmed = None
        return result

    def judge_submit(self, resp, target, sel, saved):
        parsed = urllib.parse.urlparse(resp.url)
        query = {k.lower(): v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        msgs = self.msgs(resp) if resp.is_aspx else server_messages(resp.body)
        said = " | ".join(msgs + list(query.values())).lower()
        own_page = resp.name in (INSTRUCTIONS_PAGE, CRITERIA_PAGE, FORM_PAGE)
        text = "" if own_page else resp.text().lower()
        path = parsed.path.lower()
        # being redirected to a success/payment address proves the site took the form, even when
        # that page itself then fails to load
        redirected = "success" in path or "payment" in path or has_any(" ".join(query.values()).lower(), SUCCESS_PHRASES)
        if redirected or (resp.status < 400 and (has_any(said, SUCCESS_PHRASES) or has_any(text, SUCCESS_PHRASES))):
            self.blank_returns = 0
            message = next((m for m in msgs if has_any(m.lower(), SUCCESS_PHRASES)), "") or query.get("resp", "")
            if not message:
                hit = next((p for p in SUCCESS_PHRASES if p in text), None)
                if hit:
                    i = text.find(hit)
                    message = resp.text()[max(0, i - 80):i + 120]
            app_id = next((query[k] for k in ("rid", "id", "appid", "applicationid", "admissionformid", "regno")
                           if query.get(k)), "")
            if not app_id:
                hidden = resp.value("hdfAdmissionFormId") or ""
                app_id = hidden if hidden not in ("", "0") else ""
            return {"message": message.strip(), "applicationId": app_id, "url": resp.url, "response": str(saved or "")}
        reason = "; ".join(msgs[:4])
        if has_any(said, PHONE_USED_PHRASES):
            raise PhoneUsed(reason)
        if has_any(said, FULL_PHRASES):
            raise TargetFull(f"site says: {reason}", kind="submit")
        if has_any(said, CLOSED_PHRASES):
            raise NotOpen(f"site says: {reason}")
        if resp.status >= 400 or not resp.is_aspx:
            if has_any(resp.body.lower(), VIEWSTATE_ERRORS):
                raise SessionLost("the site rejected our page state on submit")
            title = resp.title()
            raise Transient(f"submit failed: HTTP {resp.status}" + (f" ({title[:80]})" if title else ""))
        if resp.name in (INSTRUCTIONS_PAGE, CRITERIA_PAGE):
            self.entry, self.page = resp, None
            raise SessionLost("the site sent us back to the start after submitting", resume=True)
        if resp.name == FORM_PAGE:
            bot = self.bot
            if (has_any(" ".join(msgs).lower(), ("parent", "guardian")) and not bot.parent_fallback
                    and not bot.s.docs.get("parentSignature") and bot.s.docs.get("signature")
                    and bot.opt.get("parentSignatureFallback")):
                bot.use_parent_fallback(reason)
                raise Resend("the site wants a parent/guardian signature - resending with the applicant's signature")
            if msgs:
                raise Rejected(f"{reason} (page saved: {saved})")
            self.blank_returns += 1
            if self.blank_returns >= 4:
                self.blank_returns = 0
                raise Rejected(f"the form keeps coming back without any message (page saved: {saved})")
            raise Transient("the form came back without any message")
        raise Transient(f"unexpected page '{resp.name}' after submitting (saved: {saved})")

    def book(self, target):
        sel = self.cascade(target)
        if self.bot.dry_run:
            self.bot.log.info(f"{self.who}DRY RUN - would submit {target.label} now: {sel}")
            return {"message": f"dry run: {sel}", "applicationId": "", "dryRun": True}
        return self.submit(target, sel)


# ---------------------------------------------------------------------------
# Coordinating numbers and choices
# ---------------------------------------------------------------------------

class State:
    """Progress that survives restarts (bookings, used numbers)."""

    def __init__(self, path, enabled):
        self.path, self.enabled = path, enabled
        self.data = {"bookings": [], "phones": {}}
        if enabled and path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                self.data["bookings"] = loaded.get("bookings") or []
                self.data["phones"] = loaded.get("phones") or {}
            except (OSError, ValueError):
                path.rename(path.with_name(path.name + f".broken-{int(time.time())}"))

    def save(self):
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


class Bot:
    def __init__(self, settings, out_dir, base_url=DEFAULT_BASE_URL, dry_run=False, log=None):
        self.s, self.opt = settings, settings.opt
        self.out_dir, self.base_url, self.dry_run = Path(out_dir), base_url, dry_run
        self.log = log or Log()
        self.targets, self.phones = settings.targets, settings.phones
        self.wanted = settings.max_bookings
        self.opens_at = settings.opens_at or now_ist()
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.state = State(self.out_dir / "state.json", enabled=not dry_run)
        self.bookings = []
        self.booked = {}       # target index -> booking
        self.blocked = {}      # target index -> {"why", "kind", "last"}: full or rejected, re-checked slowly
        self.full_seen = {}
        self.rejections = {}
        self.claims = {}       # target index -> worker name
        self.phone_state = {p: "free" for p in self.phones}
        self.pending = {}      # phone -> label of a submit whose answer we never saw
        self.season_seen = None   # when the snow courses first showed up on the form (monotonic)
        self.known_courses = set()
        self.listed_once = set()  # targets whose batch has been on the list at some point
        self.open_evidence = False  # some batch has shown seats
        self.next_poll = 0.0      # see claim_poll
        self.last_scan = None     # see scan_due
        self.parent_fallback = False  # send the applicant's signature as the parent/guardian one
        self.course_hint = {}
        self.status = {}
        self.notes = {}
        self.saved_pages = 0
        self._restore()

    def _restore(self):
        by_label = {t.label: t for t in self.targets}
        for booking in self.state.data["bookings"]:
            self.bookings.append(booking)
            t = by_label.get(booking.get("target"))
            if t is not None:
                self.booked.setdefault(t.index, booking)
        for phone, info in self.state.data["phones"].items():
            if phone not in self.phone_state:
                continue
            status = info.get("status")
            if status in ("booked", "likely"):
                self.phone_state[phone] = "booked"
            elif status == "used":
                self.phone_state[phone] = "used"
            elif status == "submitting":
                self.pending[phone] = info.get("target")

    # -- helpers -------------------------------------------------------------

    def sleep(self, seconds):
        self.stop.wait(max(0.0, seconds))

    def backoff(self, failures):
        delay = min(self.opt["maxRetryDelaySec"], self.opt["retryDelaySec"] * (1.6 ** max(0, failures - 1)))
        return delay * random.uniform(0.7, 1.3)

    def poll_delay(self):
        """Wait before looking again while nothing is bookable: every pollSec - whatever the clock
        says, the site may open early or late - or every 30 s when started long before the
        published opening time (e.g. with --now)."""
        early = (self.opens_at - now_ist()).total_seconds() - self.opt["startEarlySec"]
        return 30.0 if early > 60 else self.opt["pollSec"]

    def note(self, who, message, every=60):
        """Log a message, but the same one at most once a minute per worker."""
        key = (who, message)
        now = time.monotonic()
        if now - self.notes.get(key, -1e9) >= every:
            self.notes[key] = now
            self.log.info(f"{who}{message}")

    def save_page(self, who, tag, page):
        if self.saved_pages >= 500:
            return None
        self.saved_pages += 1
        folder = self.out_dir / "responses"
        folder.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9.-]+", "_", f"{datetime.now():%Y%m%d-%H%M%S}-{who.strip('[] ')}-{tag}")
        path = folder / f"{name}-{uuid.uuid4().hex[:4]}.html"
        try:
            path.write_text(f"<!-- {page.status} {page.url} -->\n{page.body}", encoding="utf-8")
        except OSError:
            return None
        return path

    def set_status(self, worker, text):
        with self.lock:
            if text is None:
                self.status.pop(worker, None)
            else:
                self.status[worker] = text

    def status_line(self):
        with self.lock:
            parts = [f"booked {len(self.bookings)}/{self.wanted}"]
            if self.season_seen is None:
                parts.append(f"snow courses not listed yet (checking every {self.opt['pollSec']}s)")
            parts += [f"{w}: {text}" for w, text in sorted(self.status.items())]
            full = [self.targets[i].label for i in sorted(self.blocked)]
            if full:
                parts.append("full/blocked: " + ", ".join(full))
        return " | ".join(parts)

    # -- phones --------------------------------------------------------------

    def finished(self):
        with self.lock:
            return (len(self.bookings) >= self.wanted or len(self.booked) >= len(self.targets)
                    or not any(st in ("free", "busy") for st in self.phone_state.values()))

    def take_phone(self):
        with self.lock:
            if self.finished():
                return None
            for phone in self.phones:
                if self.phone_state[phone] == "free":
                    self.phone_state[phone] = "busy"
                    return phone
            return None

    def release_phone(self, phone):
        with self.lock:
            if self.phone_state.get(phone) == "busy":
                self.phone_state[phone] = "free"

    def pending_target(self, phone):
        label = self.pending.get(phone)
        return next((t for t in self.targets if t.label == label), None)

    def mark_submitting(self, phone, target):
        with self.lock:
            self.pending[phone] = target.label
            if not self.dry_run:
                self.state.data["phones"][phone] = {"status": "submitting", "target": target.label,
                                                    "at": now_ist().isoformat(timespec="seconds")}
                self.state.save()

    def use_parent_fallback(self, why):
        with self.lock:
            if self.parent_fallback:
                return
            self.parent_fallback = True
        self.log.warn(f"The site refused the form without a parent/guardian signature ({why}). From now on the "
                      "applicant's own signature goes in that slot (set bot.parentSignatureFallback to false to stop this).")

    def clear_submitting(self, phone):
        """The site answered that submit (with a refusal), so nothing is pending for this number."""
        with self.lock:
            self.pending.pop(phone, None)
            if not self.dry_run and self.state.data["phones"].get(phone, {}).get("status") == "submitting":
                del self.state.data["phones"][phone]
                self.state.save()

    def phone_used(self, phone, unconfirmed, why):
        """The site says the number is used. Right after a submit whose answer we never saw,
        that means our submit went through; otherwise the number was used before."""
        with self.lock:
            target = unconfirmed
            if target is not None:
                self._record(phone, target, "likely", {
                    "message": f"site now says this number is used ({why}) - our submit for {target.label} "
                               "most likely went through"})
                return
            self.phone_state[phone] = "used"
            if not self.dry_run:
                self.state.data["phones"][phone] = {"status": "used", "note": why,
                                                    "at": now_ist().isoformat(timespec="seconds")}
                self.state.save()
        self.log.warn(f"Number {mask(phone)} is already used on the site ({why}) - switching to the next number")

    # -- choices -------------------------------------------------------------

    def _claim(self, index, worker):
        for i, w in list(self.claims.items()):
            if w == worker:
                del self.claims[i]
        self.claims[index] = worker
        return self.targets[index]

    def pick_target(self, worker):
        """Best choice that is not booked, not full and not being worked on by another number."""
        with self.lock:
            free = [t for t in self.targets
                    if t.index not in self.booked and self.claims.get(t.index) in (None, worker)]
            for t in free:
                if t.index not in self.blocked:
                    return self._claim(t.index, worker)
            # everything left is full, not listed or keeps being rejected: look again now and
            # then - seats come back when bookings lapse, and a choice not listed may still be
            # published (those are looked at every pollSec, so a newly listed course is noticed)
            now = time.monotonic()
            for t in free:
                b = self.blocked[t.index]
                every = self.opt["pollSec"] if b.get("kind") == "unlisted" else self.opt["recheckBlockedSec"]
                if now - b["last"] >= every:
                    b["last"] = now
                    return self._claim(t.index, worker)
            self.release_claims(worker)
            return None

    def release_claims(self, worker):
        with self.lock:
            for i, w in list(self.claims.items()):
                if w == worker:
                    del self.claims[i]

    # -- when is the site open, and when is a choice full -------------------
    # Decided by what the site shows, not by the clock: the courses may be published early or
    # late, one after another, and a course or batch disappears from the list once it is full.

    def claim_poll(self):
        """Until the snow courses show up, only one number at a time does the full
        verify-and-look cycle (every pollSec); the rest wait for its news."""
        with self.lock:
            now = time.monotonic()
            if now < self.next_poll:
                return False
            self.next_poll = now + self.poll_delay()
            return True

    def scan_due(self, course_options):
        """Before the snow courses show up, look inside the listed courses only when the list
        changes (or every 30 s); otherwise the verify-and-look cycle alone is enough."""
        key = tuple(real_options(course_options))
        with self.lock:
            now = time.monotonic()
            if self.last_scan and self.last_scan[0] == key and now - self.last_scan[1] < 30:
                return False
            self.last_scan = (key, now)
            return True

    def saw_courses(self, options):
        """Note the course list of a freshly verified form. A snow course on it means the season is
        published. A course that wasn't there before may hold a batch we stopped looking for."""
        names = {t for _, t in real_options(options)}
        with self.lock:
            new = sorted(names - self.known_courses) if self.known_courses else []
            self.known_courses |= names
            reopened = [i for i, b in self.blocked.items() if b["kind"] == "unlisted"] if new else []
            for i in reopened:
                del self.blocked[i]
                self.full_seen.pop((i, "unlisted"), None)
        if new:
            self.log.info("New on the course list: " + ", ".join(new)
                          + (f" - looking again for {', '.join(self.targets[i].label for i in reopened)}" if reopened else ""))
        if any(kind_score(t, kind) > 0 for t in names for kind in KIND_PATTERNS):
            self.saw_season()

    def saw_season(self):
        with self.lock:
            if self.season_seen is not None:
                return
            self.season_seen = time.monotonic()
        self.log.banner(["The snow courses are on the list now - booking."])

    def saw_listed(self, target):
        with self.lock:
            self.listed_once.add(target.index)

    def unlisted_kind(self, target):
        return "gone" if target.index in self.listed_once else "unlisted"

    def full_allowed(self, kind):
        """Whether a "no seats" sighting of this kind counts yet."""
        if kind == "submit":
            return True  # the site refused the submit itself
        if self.season_seen is None:
            return False  # nothing is published yet - the site may just be late
        if kind == "gone":
            return True  # it was on the list and has been taken off: full
        listed_for = time.monotonic() - self.season_seen
        if kind == "shown":  # 0 seats: right away if other batches show seats, else after a grace
            return self.open_evidence or listed_for >= self.opt["seatGraceSec"]
        return listed_for >= self.opt["unlistedGraceSec"]  # never listed: courses come one by one

    def needs_evidence(self):
        return not self.open_evidence and self.season_seen is not None

    def saw_seats(self, target):
        with self.lock:
            self.open_evidence = True
            if target is not None:
                # seats on show cancel earlier "0 seats" / "not listed" sightings, but not the site
                # refusing a submit for lack of seats (the count shown can be stale)
                for kind in ("shown", "gone", "unlisted"):
                    self.full_seen.pop((target.index, kind), None)
                if self.blocked.get(target.index, {}).get("kind") in ("shown", "gone", "unlisted"):
                    del self.blocked[target.index]
                    self.log.info(f"{target.label} shows seats again")

    def report_full(self, target, why, kind="shown"):
        """Returns 'full' (move on), 'counted' (look again) or 'early' (doesn't count yet)."""
        with self.lock:
            if not self.full_allowed(kind):
                return "early"
            seen = self.full_seen[(target.index, kind)] = self.full_seen.get((target.index, kind), 0) + 1
            if seen < self.opt["fullConfirmations"] and target.index not in self.blocked:
                return "counted"
            what = "not on the list" if kind == "unlisted" else "FULL"
            if target.index not in self.blocked:
                self.log.warn(f"{target.label} ({target.when()}) is {what}: {why} - moving on to the next choice")
            self.blocked[target.index] = {"why": f"{'not listed' if kind == 'unlisted' else 'full'}: {why}",
                                          "kind": kind, "last": time.monotonic()}
            self.claims.pop(target.index, None)
            return "full"

    def report_rejected(self, target, why, final=False):
        with self.lock:
            count = self.rejections[target.index] = self.rejections.get(target.index, 0) + 1
            if final or count >= self.opt["maxRejections"]:
                self.log.error(f"{target.label}: " + ("not possible for this applicant" if final else
                               f"the site rejected the form {count} times") + " - moving on. "
                               "Fix the config and restart if this is about your details.")
                self.blocked[target.index] = {"why": f"rejected: {why}", "kind": "rejected",
                                              "last": time.monotonic()}
                self.claims.pop(target.index, None)
                self.rejections[target.index] = 0

    def record_booking(self, phone, target, info):
        with self.lock:
            self._record(phone, target, "dry-run" if info.get("dryRun") else "confirmed", info)

    def _record(self, phone, target, status, info):
        booking = {"target": target.label, "batch": target.when(), "phone": phone, "status": status,
                   "at": now_ist().isoformat(timespec="seconds")}
        booking.update({k: v for k, v in info.items() if k != "dryRun"})
        self.bookings.append(booking)
        self.booked.setdefault(target.index, booking)
        self.claims.pop(target.index, None)
        self.blocked.pop(target.index, None)
        self.phone_state[phone] = "booked"
        self.pending.pop(phone, None)
        if not self.dry_run:
            self.state.data["bookings"].append(booking)
            self.state.data["phones"][phone] = {"status": "booked" if status == "confirmed" else "likely",
                                                "target": target.label, "at": booking["at"]}
            self.state.save()
        headline = {"confirmed": "BOOKED", "likely": "PROBABLY BOOKED", "dry-run": "DRY RUN OK"}[status]
        lines = [f"{headline}: {target.label} ({target.when()}) with number {mask(phone)}"]
        if booking.get("message"):
            lines.append(f"Site: {booking['message'][:200]}")
        if booking.get("applicationId"):
            lines.append(f"Application id: {booking['applicationId']}")
        if status != "dry-run":
            lines.append("Watch the applicant's email for the payment link.")
        lines.append(f"Bookings so far: {len(self.bookings)} of {self.wanted}")
        self.log.banner(lines, "WARN" if status == "likely" else "INFO")
        if status != "dry-run" and self.opt.get("notify"):
            title = "IISM seat booked" if status == "confirmed" else "IISM seat probably booked"
            threading.Thread(target=notify, args=(title, f"{target.label} ({target.when()}) with {mask(phone)}"),
                             daemon=True).start()
        if self.finished():
            self.stop.set()

    # -- running -------------------------------------------------------------

    def run(self):
        with self.lock:
            free_phones = sum(1 for st in self.phone_state.values() if st == "free")
            count = min(free_phones, self.wanted - len(self.bookings), len(self.targets) - len(self.booked))
        workers = [Worker(self, n + 1) for n in range(max(1, count if self.opt["parallel"] else 1))]
        for w in workers:
            w.start()
        last_status = time.monotonic()
        try:
            while any(w.is_alive() for w in workers):
                for w in workers:
                    w.join(0.5)
                if time.monotonic() - last_status >= self.opt["statusEverySec"]:
                    last_status = time.monotonic()
                    self.log.info("status: " + self.status_line())
        except KeyboardInterrupt:
            self.log.warn("Stopping (Ctrl+C) - progress is saved, run again to continue")
            self.stop.set()
            for w in workers:
                w.join(3)
        return self.bookings


class Worker(threading.Thread):
    def __init__(self, bot, number):
        super().__init__(name=f"W{number}", daemon=True)
        self.bot = bot

    def run(self):
        bot = self.bot
        try:
            while not bot.stop.is_set():
                phone = bot.take_phone()
                if phone is None:
                    break
                try:
                    self.use(phone)
                finally:
                    bot.release_phone(phone)
                    bot.release_claims(self.name)
        except Exception:
            bot.log.error(f"[{self.name}] crashed:\n{traceback.format_exc()}")
        finally:
            bot.set_status(self.name, None)

    def use(self, phone):
        """Book one choice with this number (or find out the number is used)."""
        bot, log = self.bot, self.bot.log
        who = f"[{self.name} {mask(phone)}] "
        session = Session(bot, phone, who)
        session.unconfirmed = bot.pending_target(phone)  # a submit cut short by the last run
        fails, attempts, current = 0, 0, None
        while not bot.stop.is_set() and not bot.finished():
            target = bot.pick_target(self.name)
            if target is None:
                bot.set_status(self.name, f"{mask(phone)} waiting - every open choice is booked, full or taken")
                bot.sleep(min(5.0, bot.opt["recheckBlockedSec"]))
                continue
            if bot.season_seen is None and session.fresh_needed and not bot.claim_poll():  # retries skip the queue
                bot.set_status(self.name, f"{mask(phone)} -> {target.label}: waiting for the snow courses to be listed")
                bot.sleep(0.25)
                continue
            if target is not current:
                log.info(f"{who}going for {target.label} ({target.when()})")
                current, attempts = target, 0
            attempts += 1
            bot.set_status(self.name, f"{mask(phone)} -> {target.label} (try {attempts})")
            progress = session.progress
            try:
                info = session.book(target)
            except PhoneUsed as e:
                bot.phone_used(phone, session.unconfirmed, str(e))
                return
            except TargetFull as e:
                session.fresh_needed = True
                verdict = bot.report_full(target, str(e), e.kind)
                if verdict == "early":
                    bot.note(who, f"{target.label}: {e} - not counting it as full yet, checking again")
                elif verdict == "counted":
                    log.info(f"{who}{target.label}: {e} - checking again")
                bot.sleep(0 if verdict == "full" else bot.poll_delay())
            except NotOpen as e:
                session.fresh_needed = True
                bot.note(who, f"not open yet: {e}")
                if bot.season_seen is not None:  # before that, claim_poll paces the checks
                    bot.sleep(bot.poll_delay())
            except SessionLost as e:
                session.fresh_needed = True
                log.info(f"{who}{e} - starting over with the number")
                bot.sleep(bot.backoff(1))
            except Rejected as e:
                session.fresh_needed = True
                log.error(f"{who}{target.label}: site rejected the form: {e}")
                bot.report_rejected(target, str(e), e.final)
                bot.sleep(0 if e.final else bot.opt["rejectedRetrySec"])
            except Transient as e:
                # retry the step that failed; start over only if the same attempt keeps failing
                fails = 1 if session.progress > progress else fails + 1
                session.fresh_needed = session.fresh_needed or fails >= 6
                log.info(f"{who}{e} - retrying" + (f" ({fails} failures in a row)" if fails > 1 else ""))
                bot.sleep(bot.backoff(fails))
            except Exception:
                session.fresh_needed = True
                log.error(f"{who}unexpected problem, starting over:\n{traceback.format_exc(limit=6)}")
                bot.sleep(bot.backoff(5))
            else:
                bot.record_booking(phone, target, info)
                return


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def describe_plan(settings, log, dry_run=False):
    log.info(f"Applicant: {settings.applicant.get('name', '?')}  (email {settings.applicant.get('email', '?')})")
    log.info(f"Numbers:   {', '.join(mask(p) for p in settings.phones)}")
    log.info("Choices, best first:")
    for t in settings.targets:
        log.info(f"  {t.index + 1}. {t.label:<20} {t.when()}")
    mode = "parallel" if settings.opt["parallel"] else "one number at a time"
    log.info(f"Goal: {settings.max_bookings} booking(s), one per number ({mode})" + (" - DRY RUN" if dry_run else ""))


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
        status = {"confirmed": "BOOKED", "likely": "PROBABLY BOOKED", "dry-run": "DRY RUN OK"}.get(b["status"], b["status"])
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
    if any(b["status"] == "likely" for b in bot.bookings):
        lines.append("PROBABLY BOOKED = we never saw the reply, but the site then said the number was used.")
    if any(b["status"] in ("confirmed", "likely") for b in bot.bookings):
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
    log.info("All good. On booking day run:  python3 iism-bot.py" +
             (f" --config {settings.config_path}" if settings.config_path and settings.config_path.name != "config.jsonc" else ""))
    return 0


def with_retries(bot, action, tries=15):
    for n in range(1, tries + 1):
        try:
            return action()
        except (Transient, SessionLost) as e:
            if n == tries:
                raise
            bot.log.info(f"  ({e} - retrying)")
            time.sleep(bot.backoff(n))


def run_discover(bot, phone):
    """Walk the live form with one number and show what the site offers. Never submits."""
    log = bot.log
    session = Session(bot, phone, f"[discover {mask(phone)}] ")
    try:
        page = with_retries(bot, session.open_form)
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
            session.restart()
            p = session.open_form()
            return session.select(p, "ddlCourse",
                                  dict(first_values(p, "ddlCourseLevel", "ddlScheduleCourse"), ddlCourse=v))
        try:
            p = with_retries(bot, choose)
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


def find_config(arg):
    if arg:
        path = Path(os.path.expanduser(arg))
        if not path.exists():
            raise ConfigError(f"config file not found: {path}")
        return path.resolve()
    for name in ("config.jsonc", "config.json"):
        if (SCRIPT_DIR / name).exists():
            return SCRIPT_DIR / name
    raise ConfigError("no config found - copy config.example.jsonc to config.jsonc and fill it in")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Book IISM Gulmarg courses.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("Usage:")[1])
    ap.add_argument("--config", help="config file (default: config.jsonc next to this script)")
    ap.add_argument("--check", action="store_true", help="check config, documents and the site; send nothing")
    ap.add_argument("--discover", action="store_true", help="list the live courses/batches; never submits")
    ap.add_argument("--dry-run", action="store_true", help="do everything except the final submit")
    ap.add_argument("--now", action="store_true", help="start immediately instead of waiting for the opening time")
    ap.add_argument("--phone", help="number to use with --discover (default: the first one)")
    ap.add_argument("--reset-state", action="store_true", help="forget saved bookings/used numbers for this config")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    log = Log()
    try:
        config_path = find_config(args.config)
        cfg = load_jsonc(config_path)
        settings, errors, warnings = parse_settings(cfg, config_path)
    except ConfigError as e:
        log.error(str(e))
        return 2
    out_dir = SCRIPT_DIR / "runs" / config_path.stem
    if args.base_url != DEFAULT_BASE_URL:  # a rehearsal must not mark real numbers as booked
        site = urllib.parse.urlparse(args.base_url)
        out_dir = out_dir.with_name(f"{config_path.stem}@{site.hostname}_{site.port or ''}")
    doc_problems, doc_notes = prepare_documents(settings, out_dir)

    if args.check:
        return run_check(settings, errors, warnings, doc_problems, doc_notes, args.base_url, log)
    if args.discover and settings.phones:
        errors, doc_problems = [], []  # looking around needs only a number
    if errors or doc_problems:
        for p in errors + doc_problems:
            log.error(p)
        log.error("Fix the above (python3 iism-bot.py --check shows everything) and run again.")
        return 2
    for w in warnings:
        log.warn(w)

    log.open(out_dir / "bot.log")
    if args.reset_state and (out_dir / "state.json").exists():
        backup = out_dir / f"state.json.bak-{datetime.now():%Y%m%d-%H%M%S}"
        (out_dir / "state.json").rename(backup)
        log.info(f"Saved progress moved to {backup}")
    bot = Bot(settings, out_dir, base_url=args.base_url, dry_run=args.dry_run or args.discover, log=log)

    if args.discover:
        phone = parse_phone(args.phone) if args.phone else settings.phones[0]
        if not phone:
            log.error(f"--phone {args.phone} is not a 10-digit number")
            return 2
        return run_discover(bot, phone)

    if settings.opt.get("preventSleep"):
        keep_awake()
    describe_plan(settings, log, args.dry_run)
    if bot.bookings:
        log.info(f"Already booked earlier: {', '.join(b['target'] + ' (' + mask(b['phone']) + ')' for b in bot.bookings)}")
    if bot.finished():
        log.info("Nothing left to do for this config (use --reset-state to start over).")
        print_summary(bot)
        return 0
    try:
        if not args.now:
            wait_for_opening(settings, log, bot.stop)
        log.info(f"Logging to {out_dir / 'bot.log'}")
        bot.run()
    except KeyboardInterrupt:
        log.warn("Stopped.")
    print_summary(bot)
    return 0 if bot.bookings else 1


if __name__ == "__main__":
    sys.exit(main())
