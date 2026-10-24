"""
Tests for iism-bot.py. The end-to-end tests run the bot against tests/mock_site.py,
a copy of the real site's pages and flow.

  python3 -m unittest discover -s tests -v
  VERBOSE=1 python3 -m unittest tests.test_bot.EndToEnd.test_survives_a_crashing_site   # show the bot's log
"""

import importlib.util
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mock_site  # noqa: E402

spec = importlib.util.spec_from_file_location("iism_bot", HERE.parent / "iism-bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

FAST = {"parallel": True, "notify": False, "preventSleep": False, "startEarlySec": 0, "pollSec": 0.1,
        "requestTimeoutSec": 2, "submitTimeoutSec": 2, "retryDelaySec": 0.01, "maxRetryDelaySec": 0.05,
        "fullConfirmations": 2, "unlistedGraceSec": 1, "seatGraceSec": 1, "recheckBlockedSec": 0.3,
        "maxRejections": 3, "rejectedRetrySec": 0.05, "statusEverySec": 999}
PHONES = ["9000000011", "9000000012", "9000000013"]


def make_config(tmp, phones, priorities, **extra):
    docs = tmp / "docs"
    docs.mkdir(exist_ok=True)
    for name in ("photo.jpg", "sign.jpg", "dob.jpg", "edu.jpg", "parent.png"):
        (docs / name).write_bytes(b"\xff\xd8\xff\xe0" + os.urandom(4000))
    cfg = {
        "applicant": {"name": "Test Person", "fatherName": "Test Father", "dateOfBirth": "15/05/1998",
                      "nationality": "Indian", "domicile": "Maharashtra", "email": "test@example.com",
                      "bloodGroup": "B+", "height": "175", "weight": "70", "waist": "32", "shoeSize": "9",
                      "diet": "Vegetarian", "education": "Graduation", "emergencyContactName": "Mom",
                      "emergencyContactPhone": "9000000001"},
        "address": {"houseNo": "1", "street": "MG Road", "district": "Pune", "state": "Maharashtra",
                    "pincode": "411001"},
        "phoneNumbers": phones, "coursePriorities": priorities,
        "bookingOpensAt": "2020-01-01T10:00:00+05:30",
        "documentsDir": str(docs),
        "documents": {"photo": "photo.jpg", "signature": "sign.jpg", "dobProof": "dob.jpg",
                      "educationProof": "edu.jpg"},  # adults: no parent/guardian signature
        "bot": dict(FAST),
    }
    for key, value in extra.items():
        if key == "bot":
            cfg["bot"].update(value)
        elif key == "applicant":
            cfg["applicant"].update(value)
        else:
            cfg[key] = value
    return cfg


def run_bot(cfg, base_url, out_dir, dry_run=False, timeout=60):
    settings, errors, _ = bot.parse_settings(cfg, None)
    assert not errors, errors
    problems, _ = bot.prepare_documents(settings, out_dir)
    assert not problems, problems
    log = bot.Log(echo=bool(os.environ.get("VERBOSE")))
    log.open(out_dir / "bot.log")
    b = bot.Bot(settings, out_dir, base_url=base_url, dry_run=dry_run, log=log)
    guard = threading.Timer(timeout, b.stop.set)
    guard.start()
    try:
        b.run()
    finally:
        guard.cancel()
        log.file.close()
    return b


def target(label):
    return bot.parse_priority(label, 0, bot.load_season({}, Path("."))[0])


class Matching(unittest.TestCase):
    def test_reads_dates_in_many_formats(self):
        g1 = target("G1 Basic")
        for text in ["Course from 20 Dec to 02 Jan 2027", "20/12/2026 To 02/01/2027", "20-Dec-2026 to 02-Jan-2027",
                     "Dec 20, 2026 - Jan 2, 2027", "12/20/2026 - 1/2/2027", "2026-12-20 / 2027-01-02",
                     "20th December 2026 to 2nd January 2027", "1st Course (20.12.2026 - 02.01.2027)", "20 DEC - 02 JAN"]:
            self.assertEqual(bot.match_batch([("0", "Select Schedule Course"), ("7", text)], g1), ("7", text), text)

    def test_prefers_day_first_reading(self):
        options = [("1", "01/03/2027 To 14/03/2027"), ("2", "03/01/2027 To 16/01/2027")]
        self.assertEqual(bot.match_batch(options, target("G2 Basic"))[0], "2")

    def test_does_not_match_other_batches(self):
        options = [("70", "Course from 13 Sep to 26 Sep 2026"), ("71", "Course from 20 Dec to 02 Jan 2027"),
                   ("73", "Course from 17 Jan to 30 Jan 2027")]
        self.assertIsNone(bot.match_batch(options, target("G2 Basic")))
        self.assertEqual(bot.match_batch(options, target("G3 Basic"))[0], "73")

    def test_priorities(self):
        t = target("G2 Basic")
        self.assertEqual((t.kind, t.level, t.location, t.start), ("Snow Skiing", "Basic", "Gulmarg", date(2027, 1, 3)))
        self.assertEqual(target("g4 snowboarding").kind, "Snow Boarding")
        self.assertEqual(target("G5 MOI").kind, "MOI")
        self.assertEqual(target("K1 inter").level, "Intermediate")
        self.assertEqual(target("G6").level, "Basic")
        with self.assertRaises(bot.ConfigError):
            target("G9 Basic")

    def test_course_candidates(self):
        options = [("0", "Select Course"), ("9", "Adventure Courses"), ("1", "SNOW SKIING COURSE (GULMARG)"),
                   ("2", "SNOW SKIING COURSE (KARGIL)"), ("3", "SNOW BOARDING"), ("4", "M.O.I. COURSE"),
                   ("5", "WATER SKIING COURSE")]
        first = lambda label: bot.course_candidates(options, target(label))[0][0]
        self.assertEqual(first("G2 Basic"), "1")
        self.assertEqual(first("K1 Basic"), "2")
        self.assertEqual(first("G4 Snowboarding"), "3")
        self.assertEqual(first("G5 MOI"), "4")
        everything = [v for v, _ in bot.course_candidates(options, target("G2 Basic"))]
        self.assertIn("9", everything)       # unknown names are still tried, last
        self.assertNotIn("5", everything)    # water skiing is not snow skiing
        self.assertNotIn("2", everything)    # Kargil course is not tried for a Gulmarg batch
        self.assertEqual(bot.course_candidates([("0", "Select Course"), ("9", "Adventure Courses")],
                                               target("K2 Basic")), [("9", "Adventure Courses")])

    def test_level_named_courses(self):
        options = [("0", "Select Course"), ("9", "Adventure Courses"), ("21", "Snow Skiing Basic Course"),
                   ("22", "Snow Skiing Intermediate Course"), ("23", "Snow Skiing Advance Course")]
        picks = lambda label: [v for v, _ in bot.course_candidates(options, target(label))]
        self.assertEqual(picks("G2 Intermediate"), ["22", "9"])  # never the Basic or Advance course
        self.assertEqual(picks("G2 Advance"), ["23", "9"])      # "Adventure" is not "Advance"
        self.assertEqual(picks("G2 Basic"), ["21", "9"])

    def test_levels(self):
        ski = [("0", "Select Course Level"), ("13", "Basic"), ("14", "INTERMIDIATE"), ("15", "Advance")]
        self.assertEqual(bot.match_level(ski, target("G2 Intermediate"), loose=True)[0], "14")
        self.assertIsNone(bot.match_level(ski, target("G4 Snowboarding"), loose=False))
        none_only = [("0", "Select Course Level"), ("16", "None")]
        self.assertEqual(bot.match_level(none_only, target("G2 Basic"), loose=True)[0], "16")

    def test_choices(self):
        states = mock_site.STATES
        self.assertEqual(bot.match_choice(states, "Maharashtra")[0], "27")
        self.assertEqual(bot.match_choice(states, "Uttar Pradesh")[0], "9")
        self.assertEqual(bot.match_choice(states, "Chhattisgarh", bot.state_aliases("Chhattisgarh"))[0], "22")
        self.assertEqual(bot.match_choice(states, "Jammu & Kashmir")[0], "1")
        self.assertEqual(bot.match_choice(states, "Orissa", bot.state_aliases("Orissa"))[0], "21")
        self.assertEqual(bot.match_choice(states, "Ladakh")[0], "39")
        self.assertEqual(bot.match_choice(mock_site.DIET, "Vegetarian")[0], "Vegetarian")
        self.assertEqual(bot.match_choice(mock_site.DIET, "non veg")[0], "Non-Vegetarian")
        self.assertEqual(bot.match_choice(mock_site.EDUCATION, "Post Graduate")[0], "Post graduation")

    def test_server_messages(self):
        # the real page: checkFileSize() alert is code, the startup block mixes functions and a server alert
        body = ("<script type=\"text/javascript\">function checkFileSize(inputFile) { if (inputFile.files[0].size > "
                "1048576) { alert(\"File Size Too Large.Only File Upload Lessthan 1MB\"); // Do your thing\n } }</script>"
                "<span id=\"lblmsg\" style=\"color:Red;\">Mobile No. already registered</span><span id=\"lblName\">x</span>"
                "<script>rfvtxtName.errormessage = \"Please Enter Name.\";\nrfvtxtName.isvalid = \"False\";</script>"
                "<script type=\"text/javascript\">//<![CDATA[\nalert('Seat not available.');var Page_ValidationActive = "
                "false;\nfunction ValidatorOnSubmit() { if (Page_ValidationActive) { return ValidatorCommonOnSubmit(); } }"
                "\nSys.Application.add_init(function() { $create(X, {}, null, null, $get(\"y\")); });\n//]]>\n</script>")
        self.assertEqual(bot.server_messages(body),
                         ["Seat not available.", "Mobile No. already registered", "Please Enter Name."])

    def test_instructions_page_text_is_not_a_verdict(self):
        session = bot.Session(offline_bot(), PHONES[0], "")
        page = bot.Page("https://x/Admission_instructions.aspx", 200,
                        "<form action='./Admission_instructions.aspx'><input name='__VIEWSTATE' value='a'/>"
                        + mock_site.INSTRUCTIONS_TEXT + "</form>")
        self.assertEqual(session.verdict(page, deep=True, full_ok=False), [])

    def test_jsonc(self):
        path = Path(tempfile.mkdtemp()) / "c.jsonc"
        path.write_text('// top\n{\n  "url": "https://x//y", /* block */ "list": [1, 2,],\n  "a": "b", // c\n}\n')
        self.assertEqual(bot.load_jsonc(path), {"url": "https://x//y", "list": [1, 2], "a": "b"})

    def test_phones(self):
        self.assertEqual(bot.parse_phone("+91 98765 43210"), "9876543210")
        self.assertEqual(bot.parse_phone("098765-43210"), "9876543210")
        self.assertIsNone(bot.parse_phone("12345"))


class OpenAndFullRules(unittest.TestCase):
    def test_rules(self):
        b = offline_bot()
        b.opt.update(fullConfirmations=1, unlistedGraceSec=60, seatGraceSec=60)
        t = b.targets[0]
        # before any snow course is listed nothing counts - the site may just be late
        self.assertEqual([b.full_allowed(k) for k in ("shown", "gone", "unlisted")], [False, False, False])
        self.assertTrue(b.full_allowed("submit"))  # the site refusing a submit always counts
        b.saw_season()
        self.assertTrue(b.full_allowed("gone"))        # was listed, now taken off: full
        self.assertFalse(b.full_allowed("unlisted"))   # never listed: courses come one by one
        self.assertFalse(b.full_allowed("shown"))      # 0 seats, but no seats seen anywhere yet
        b.saw_seats(None)
        self.assertTrue(b.full_allowed("shown"))       # other batches have seats: 0 means full
        self.assertEqual(b.report_full(t, "0 seats", "shown"), "full")
        b.saw_seats(t)                                 # seats again: back in play
        self.assertNotIn(t.index, b.blocked)
        b.season_seen -= 61
        self.assertTrue(b.full_allowed("unlisted"))


class MockMatchesRealSite(unittest.TestCase):
    """What the real form answered on 23 Oct 2026 (course "Adventure Courses", the only one listed then):
    the mock must answer the same postbacks the same way."""

    RECORDED = [
        ("ddlCourse", {"ddlCourse": "9"},
         {"levels": [("16", "None")], "batches": [("70", "Course from 13 Sep to 26 Sep 2026")],
          "txtAvailableSeat": "0", "txtCourseFee": "0", "hdfMinimumAge": "0", "hdfMaximumAge": "0",
          "hdfAgeCriteria": "12", "lblDOBMsg": ""}),
        ("ddlScheduleCourse", {"ddlCourseLevel": "16", "ddlScheduleCourse": "70"},
         {"txtAvailableSeat": "0", "txtCourseFee": "0", "hdfMinimumAge": "12", "hdfMaximumAge": "45",
          "hdfAgeCountDate": "10/13/2026 12:00:00 AM", "lblDOBMsg": ""}),
        ("ddlState", {"ddlState": "27"}, {"lblDOBMsg": "Please Enter Date Of Birth.", "txtCourseFee": "0"}),
        ("txtDateOfBirth", {"txtDateOfBirth": "15/05/1998"},
         {"lblDOBMsg": "Your Age 28 Years.", "txtCourseFee": "8000", "txtDateOfBirth": "15/05/1998"}),
        ("txtDateOfBirth", {"txtDateOfBirth": "15/05/1970"},
         {"lblDOBMsg": "Your Age 56 Years , Age Allow 12 To 45 Years.", "txtCourseFee": "0", "txtDateOfBirth": ""}),
    ]

    def test_replay(self):
        site = mock_site.Site()
        server, url = mock_site.start(site)
        try:
            session = bot.Session(offline_bot(), PHONES[0], "")
            session.bot.base_url = url
            session.restart()
            page = session.open_form()
            self.assertEqual(page.value("txtAvailableSeat"), "0")  # before any batch is chosen, like the real form
            for event, values, expected in self.RECORDED:
                page = session.checked(session.postback(page, event, values))
                got = {"levels": bot.real_options(page.options("ddlCourseLevel")),
                       "batches": bot.real_options(page.options("ddlScheduleCourse")),
                       "lblDOBMsg": " ".join(re.findall(r'id="lblDOBMsg"[^>]*>([^<]*)<', page.body))}
                for key, want in expected.items():
                    self.assertEqual(got[key] if key in got else page.value(key), want, f"{event}: {key}")
        finally:
            server.shutdown()
            server.server_close()


def offline_bot():
    cfg = make_config(Path(tempfile.mkdtemp()), PHONES[:1], ["G2 Basic"])
    settings, _, _ = bot.parse_settings(cfg, None)
    return bot.Bot(settings, Path(tempfile.mkdtemp()), base_url="http://127.0.0.1:9", dry_run=True,
                   log=bot.Log(echo=False))


class EndToEnd(unittest.TestCase):
    layout = "levels"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="iism-test-"))
        self.out = self.tmp / "out"
        self.site = mock_site.Site(layout=self.layout, seed=7)
        self.server, self.url = mock_site.start(self.site)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_with(self, phones, priorities, timeout=60, **extra):
        return run_bot(make_config(self.tmp, phones, priorities, **extra), self.url, self.out, timeout=timeout)

    def key(self, batch, level="Basic", kind=None):
        return self.site.batch_key((batch, level), kind)

    def booked(self, b):
        return sorted((x["target"], x["phone"], x["status"]) for x in b.bookings)

    def applied(self):
        return sorted((a["course"], a["level"], a["schedule"]) for a in self.site.snapshot()["applications"])

    def test_one_booking_per_number_best_choices_first(self):
        b = self.run_with(PHONES[:2], ["G2 Basic", "G3 Basic", "G6 Basic"])
        self.assertEqual(sorted(x["target"] for x in b.bookings), ["G2 Basic", "G3 Basic"])
        self.assertEqual(len({x["phone"] for x in b.bookings}), 2)
        self.assertEqual(self.applied(), sorted([self.key("G2"), self.key("G3")]))
        apps = self.site.snapshot()["applications"]
        self.assertTrue(all(len(a["files"]) == 4 and a["fee"] == "20000" for a in apps))
        self.assertTrue(all(x["applicationId"] for x in b.bookings))

    def test_sequential_mode_goes_in_order(self):
        b = self.run_with(PHONES[:2], ["G2 Basic", "G3 Basic"], bot={"parallel": False})
        self.assertEqual(self.booked(b), [("G2 Basic", PHONES[0], "confirmed"), ("G3 Basic", PHONES[1], "confirmed")])

    def test_full_choice_is_skipped(self):
        self.site.seats[self.key("G2")] = 0
        b = self.run_with(PHONES[:2], ["G2 Basic", "G3 Basic", "G6 Basic"])
        self.assertEqual(sorted(x["target"] for x in b.bookings), ["G3 Basic", "G6 Basic"])
        self.assertTrue(b.blocked[0]["why"].startswith("full"))

    def test_seats_gone_by_submit_time(self):
        for style in ("label", "alert"):
            with self.subTest(style=style):
                self.site.full_style = style
                self.site.applications.clear()
                self.site.used.clear()
                shutil.rmtree(self.out, ignore_errors=True)
                self.site.seats[self.key("G2")] = 0
                self.site.shown_seats = {self.key("G2"): 4}  # the batch page still shows seats
                b = self.run_with(PHONES[:1], ["G2 Basic", "G3 Basic"])
                self.assertEqual([x["target"] for x in b.bookings], ["G3 Basic"])

    def test_used_number_is_skipped(self):
        for style in ("label", "alert"):
            with self.subTest(style=style):
                self.site.used_style = style
                self.site.used = {PHONES[0]}
                self.site.applications.clear()
                shutil.rmtree(self.out, ignore_errors=True)
                b = self.run_with(PHONES[:2], ["G2 Basic", "G3 Basic"], bot={"parallel": False})
                self.assertEqual(self.booked(b), [("G2 Basic", PHONES[1], "confirmed")])
                self.assertEqual(b.phone_state[PHONES[0]], "used")
                self.assertIn("NEEDS YOUR ATTENTION: number ..0011 is already registered", (self.out / "bot.log").read_text())

    def test_number_refused_at_submit_is_not_a_booking(self):
        self.site.used = {PHONES[0]}
        self.site.check_used_at_start = False
        b = self.run_with(PHONES[:2], ["G2 Basic", "G3 Basic"], bot={"parallel": False})
        self.assertEqual(self.booked(b), [("G2 Basic", PHONES[1], "confirmed")])
        self.assertEqual(b.phone_state[PHONES[0]], "used")

    def test_success_page_variants(self):
        for style in ("redirect", "alert", "payment"):
            with self.subTest(style=style):
                self.site.success_style = style
                self.site.used.clear()
                self.site.applications.clear()
                shutil.rmtree(self.out, ignore_errors=True)
                b = self.run_with(PHONES[:1], ["G2 Basic"])
                self.assertEqual(self.booked(b), [("G2 Basic", PHONES[0], "confirmed")])
                self.assertEqual(len(self.site.applications), 1)

    def test_success_page_that_fails_to_load_still_counts(self):
        self.site.fail_success_page = 1
        b = self.run_with(PHONES[:1], ["G2 Basic"])
        self.assertEqual(self.booked(b), [("G2 Basic", PHONES[0], "confirmed")])
        self.assertTrue(b.bookings[0]["applicationId"])
        self.assertEqual(len(self.site.applications), 1)

    def test_survives_a_crashing_site(self):
        self.site.chaos, self.site.session_loss, self.site.restarts = 0.4, 0.05, 0.02
        self.site.slow_seconds = 2.5
        b = self.run_with(PHONES[:2], ["G2 Basic", "G3 Basic"], timeout=150)
        self.assertEqual(sorted(x["target"] for x in b.bookings), ["G2 Basic", "G3 Basic"])
        self.assertNotIn("NEEDS YOUR ATTENTION", (self.out / "bot.log").read_text())  # server trouble: no alerts
        self.assertEqual(self.applied(), sorted([self.key("G2"), self.key("G3")]))

    def test_lost_reply_after_submit_counts_as_booked(self):
        self.site.crash_after_commit = 1
        b = self.run_with(PHONES[:2], ["G2 Basic", "G3 Basic"], bot={"parallel": False})
        self.assertEqual(self.booked(b), [("G2 Basic", PHONES[0], "likely"), ("G3 Basic", PHONES[1], "confirmed")])
        self.assertEqual(self.applied(), sorted([self.key("G2"), self.key("G3")]))  # G2 not booked twice

    def test_waits_for_opening(self):
        # like the real form today: only "Adventure Courses" until the snow courses are published
        opens = time.time() + 1.5
        self.site.opens_at = opens
        when = (bot.now_ist() + timedelta(seconds=1.5)).isoformat()
        b = self.run_with(PHONES[:1], ["G2 Basic"], bookingOpensAt=when)
        self.assertEqual([x["target"] for x in b.bookings], ["G2 Basic"])
        self.assertGreaterEqual(time.time(), opens)

    def test_courses_listed_earlier_than_announced(self):
        self.site.opens_at = time.time() + 0.5
        announced = (bot.now_ist() + timedelta(seconds=120)).isoformat()
        started = time.time()
        b = self.run_with(PHONES[:1], ["G2 Basic"], bookingOpensAt=announced, bot={"startEarlySec": 600})
        self.assertEqual([x["target"] for x in b.bookings], ["G2 Basic"])
        self.assertLess(time.time() - started, 30)  # did not wait for the announced time

    def test_courses_listed_later_than_announced(self):
        self.site.opens_at = time.time() + 2.5
        announced = (bot.now_ist() - timedelta(seconds=30)).isoformat()
        b = self.run_with(PHONES[:1], ["G2 Basic", "G3 Basic"], bookingOpensAt=announced)
        self.assertEqual([x["target"] for x in b.bookings], ["G2 Basic"])  # nothing was given up on meanwhile
        self.assertEqual(b.blocked, {})

    def test_courses_listed_one_by_one(self):
        later = self.key("K1")[0]
        self.site.course_opens[later] = time.time() + 2  # the other snow courses are listed right away
        b = self.run_with(PHONES[:1], ["K1 Basic", "G6 Basic"], bot={"unlistedGraceSec": 10})
        self.assertEqual([x["target"] for x in b.bookings], ["K1 Basic"])

    def test_new_course_brings_back_a_choice_given_up_on(self):
        later = self.key("K1")[0]
        self.site.course_opens[later] = time.time() + 2
        b = self.run_with(PHONES[:2], ["K1 Basic", "G6 Basic"],
                          bot={"unlistedGraceSec": 0.2, "recheckBlockedSec": 600})
        self.assertIn("K1 Basic", [x["target"] for x in b.bookings])

    def test_alert_when_a_batch_cannot_be_found(self):
        moved = {"from": "2027-01-05", "location": "Gulmarg", "level": "Basic", "label": "G2 moved"}  # not on the site
        b = self.run_with(PHONES[:1], [moved], timeout=3, bot={"missingAlertSec": 0.5})
        self.assertEqual(b.bookings, [])
        self.assertEqual(b.alerted, {0})
        log = (self.out / "bot.log").read_text()
        self.assertEqual(log.count("NEEDS YOUR ATTENTION: can't find G2 moved"), 1)  # once, not on every check
        self.assertIn("Course from 03 Jan to 16 Jan 2027", log)  # shows what the site lists

    def test_alert_when_a_level_cannot_be_found(self):
        b = self.run_with(PHONES[:1], [{"batch": "G2", "levelText": "Expert", "label": "G2 Expert"}],
                          timeout=3, bot={"missingAlertSec": 0.5})
        self.assertEqual(b.bookings, [])
        self.assertEqual(b.alerted, {0})
        self.assertIn("levels", (self.out / "bot.log").read_text())

    def test_refused_number_is_announced_then_skipped(self):
        self.site.refuse_numbers = {PHONES[0]: "Please Enter Valid Mobile No."}
        b = self.run_with(PHONES[:2], ["G2 Basic"], bot={"parallel": False})
        self.assertEqual(self.booked(b), [("G2 Basic", PHONES[1], "confirmed")])
        log = (self.out / "bot.log").read_text()
        self.assertEqual(log.count("NEEDS YOUR ATTENTION: the site refuses number ..0011"), 2)  # warning, then dropped
        self.assertIn("Number ..0011 is skipped for the rest of this run", log)
        self.assertIn("check phoneNumbers in your config", log)

    def test_full_batch_taken_off_the_list(self):
        self.site.hide_full = True
        g2 = self.key("G2")[2]
        for key in self.site.seats:
            if key[2] == g2:
                self.site.seats[key] = 0  # every level of G2 is full, so the batch is not shown
        b = self.run_with(PHONES[:1], ["G2 Basic", "G3 Basic"])
        self.assertEqual([x["target"] for x in b.bookings], ["G3 Basic"])
        self.assertTrue(b.blocked[0]["why"].startswith("not listed"))

    def test_full_choices_are_rechecked_until_seats_appear(self):
        self.site.seats[self.key("G2")] = 0
        self.site.seats[self.key("G3")] = 0
        threading.Timer(1.5, lambda: self.site.seats.__setitem__(self.key("G3"), 2)).start()
        b = self.run_with(PHONES[:1], ["G2 Basic", "G3 Basic"])
        self.assertEqual([x["target"] for x in b.bookings], ["G3 Basic"])

    def test_resume_skips_what_is_booked(self):
        b = self.run_with(PHONES[:2], ["G2 Basic", "G3 Basic"], maxBookings=1, bot={"parallel": False})
        self.assertEqual(self.booked(b), [("G2 Basic", PHONES[0], "confirmed")])
        b = self.run_with(PHONES[:2], ["G2 Basic", "G3 Basic"], bot={"parallel": False})
        self.assertEqual(self.booked(b), [("G2 Basic", PHONES[0], "confirmed"), ("G3 Basic", PHONES[1], "confirmed")])
        self.assertEqual(self.applied(), sorted([self.key("G2"), self.key("G3")]))

    def test_other_courses_and_kargil(self):
        b = self.run_with(PHONES, ["K1 Basic", "G4 Snowboarding", "G5 MOI"], applicant={"dateOfBirth": "15/05/2001"})
        self.assertEqual(len(b.bookings), 3)
        if self.layout == "levels":
            expected = [self.key("K1"), self.key("G4", "Snow Boarding"), self.key("G5", "MOI")]
        else:
            expected = [self.key("K1"), self.key("G4", "None", "board"), self.key("G5", "None", "moi")]
        self.assertEqual(self.applied(), sorted(expected))

    def test_age_limit_skips_that_choice(self):
        # 16 years old: too young for a separate MOI course (18-40), fine for skiing (10-40)
        cfg = make_config(self.tmp, PHONES[:1], ["G5 MOI", "G2 Basic"], applicant={"dateOfBirth": "15/05/2010"})
        cfg["documents"]["parentSignature"] = "parent.png"  # under 18
        b = run_bot(cfg, self.url, self.out)
        self.assertTrue(all("FupParentGuardianSignature" in a["files"] for a in self.site.applications))
        if self.layout != "levels":
            self.assertEqual([x["target"] for x in b.bookings], ["G2 Basic"])
            self.assertIn("Age Allow 18 To 40", b.blocked[0]["why"])
        else:
            self.assertEqual([x["target"] for x in b.bookings], ["G5 MOI"])

    def test_too_old_for_everything_gives_up_on_each_choice(self):
        b = self.run_with(PHONES[:1], ["G2 Basic", "G3 Basic"], applicant={"dateOfBirth": "15/05/1970"}, timeout=5)
        self.assertEqual(b.bookings, [])
        self.assertEqual(sorted(i for i, x in b.blocked.items() if "Age Allow 10 To 40" in x["why"]), [0, 1])
        self.assertIn("isn't possible for this applicant", (self.out / "bot.log").read_text())
        self.assertEqual(self.site.applications, [])

    def test_dry_run_submits_nothing(self):
        cfg = make_config(self.tmp, PHONES[:2], ["G2 Basic", "G3 Basic"])
        b = run_bot(cfg, self.url, self.out, dry_run=True)
        self.assertEqual(sorted(x["status"] for x in b.bookings), ["dry-run", "dry-run"])
        self.assertEqual(self.site.snapshot()["applications"], [])
        self.assertFalse((self.out / "state.json").exists())

    def test_rejected_form_moves_on_instead_of_looping(self):
        # passes our own email check, fails the site's validator pattern
        b = self.run_with(PHONES[:1], ["G2 Basic", "G3 Basic"], timeout=4, applicant={"email": "first..last@example.com"})
        self.assertEqual(b.bookings, [])
        self.assertTrue(b.blocked[0]["why"].startswith("rejected"))
        self.assertIn("Please Enter Valid Email ID", b.blocked[0]["why"])
        log = (self.out / "bot.log").read_text()
        self.assertEqual(log.count("NEEDS YOUR ATTENTION: the site refused the form"), 1)  # once, not every retry
        self.assertIn("check applicant.email in your config", log)

    def test_missing_required_document_is_caught_before_booking(self):
        cfg = make_config(self.tmp, PHONES[:1], ["G2 Basic"])
        cfg["documents"]["photo"] = ""
        settings, _, _ = bot.parse_settings(cfg, None)
        problems, _ = bot.prepare_documents(settings, self.out)
        self.assertEqual(problems, ["documents.photo is not set (the form requires it)"])

    def test_parent_signature_only_needed_under_18(self):
        for dob, needed in (("15/05/1998", False), ("15/05/2010", True)):
            cfg = make_config(self.tmp, PHONES[:1], ["G2 Basic"], applicant={"dateOfBirth": dob})
            settings, errors, _ = bot.parse_settings(cfg, None)
            problems, _ = bot.prepare_documents(settings, self.out)
            self.assertEqual(bool(problems), needed, dob)

    def test_parent_signature_fallback_when_the_site_insists(self):
        self.site.parent_signature_required = True  # the form's check is on for adults too
        b = self.run_with(PHONES[:1], ["G2 Basic"])
        self.assertEqual(self.booked(b), [("G2 Basic", PHONES[0], "confirmed")])
        self.assertTrue(b.parent_fallback)
        self.assertIn("FupParentGuardianSignature", self.site.applications[0]["files"])

    def test_parent_signature_not_sent_when_not_needed(self):
        b = self.run_with(PHONES[:1], ["G2 Basic"])
        self.assertEqual(self.booked(b), [("G2 Basic", PHONES[0], "confirmed")])
        self.assertFalse(b.parent_fallback)
        self.assertNotIn("FupParentGuardianSignature", self.site.applications[0]["files"])

    def test_parent_signature_fallback_can_be_turned_off(self):
        self.site.parent_signature_required = True
        b = self.run_with(PHONES[:1], ["G2 Basic"], timeout=4, bot={"parentSignatureFallback": False})
        self.assertEqual(b.bookings, [])
        self.assertIn("Parent/Guardian", b.blocked[0]["why"])


class EndToEndOtherLayout(EndToEnd):
    """Same tests with snowboarding/MOI as separate courses and Kargil inside the skiing course."""
    layout = "courses"


class EndToEndLevelCourses(EndToEnd):
    """Same tests with one course per level ("Snow Skiing Basic Course", ...), each listing every batch."""
    layout = "level-courses"


if __name__ == "__main__":
    unittest.main()
