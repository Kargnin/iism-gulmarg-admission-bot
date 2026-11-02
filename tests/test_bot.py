"""
Tests for iism-bot.py. The end-to-end tests run the bot against tests/mock_site.py,
a copy of the real site's pages and flow.

  python3 -m unittest discover -s tests -v
  VERBOSE=1 python3 -m unittest tests.test_bot.EndToEnd.test_survives_a_crashing_site   # show the bot's log
"""

import importlib.util
import json
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
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mock_site  # noqa: E402

spec = importlib.util.spec_from_file_location("iism_bot", HERE.parent / "iism-bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

FAST = {"parallel": True, "notify": False, "preventSleep": False, "startEarlySec": 0, "pollSec": 0.1,
        "requestTimeoutSec": 2, "submitTimeoutSec": 2, "retryDelaySec": 0.01, "maxRetryDelaySec": 0.05,
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


def run_bot(cfg, base_url, out_dir, dry_run=False, timeout=60, confirmation_handler=None):
    settings, errors, _ = bot.parse_settings(cfg, None)
    assert not errors, errors
    problems, _ = bot.prepare_documents(settings, out_dir)
    assert not problems, problems
    log = bot.Log(echo=bool(os.environ.get("VERBOSE")))
    log.open(out_dir / "bot.log")
    b = bot.Bot(settings, out_dir, base_url=base_url, dry_run=dry_run, log=log,
                confirmation_handler=confirmation_handler)
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

    def test_explicit_end_date_uses_parsed_written_date_spans(self):
        g1 = target("G1 Basic")
        self.assertIsNone(bot.match_batch(
            [("1", "Dec 20, 2026 - Jan 9, 2027")], g1))
        self.assertEqual(bot.match_batch(
            [("1", "Dec 20, 2026 - Jan 2, 2027")], g1)[0], "1")
        self.assertEqual(bot.match_batch(
            [("2", "03/01/2027")], target("G2 Basic"))[0], "2")
        self.assertEqual(bot.match_batch(
            [("3", "1st Course (20.12.2026 - 02.01.2027)")], g1)[0], "3")

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
        self.assertEqual(target("G2 Snow Skiing").level, "Basic")
        with self.assertRaises(bot.ConfigError):
            target("G9 Basic")

    def test_priority_semantics_reject_unknown_and_incompatible_levels(self):
        for label in ("G2 Adventure Courses", "G2 Winter Training"):
            with self.subTest(label=label), self.assertRaises(bot.ConfigError):
                target(label)
        batches = bot.load_season({}, Path("."))[0]
        for item in ({"batch": "G4", "course": "Snow Boarding", "level": "Basic"},
                     {"batch": "G5", "course": "MOI", "level": "Advance"}):
            with self.subTest(item=item), self.assertRaisesRegex(bot.ConfigError, "combines"):
                bot.parse_priority(item, 0, batches)
        self.assertEqual(bot.parse_priority({"batch": "G4", "course": "Snow Boarding"},
                                            0, batches).kind, "Snow Boarding")
        with tempfile.TemporaryDirectory() as folder:
            cfg = make_config(Path(folder), PHONES[:1],
                              [{"batch": "G4", "course": "Snow Boarding", "level": "Basic"}])
            self.assertTrue(any("combines" in error for error in bot.parse_settings(cfg, None)[1]))

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

    def test_hints_cannot_override_semantic_target_evidence(self):
        batches = bot.load_season({}, Path("."))[0]
        advanced = bot.parse_priority({"batch": "G2", "course": "Snow Skiing", "level": "Advance",
                                       "levelText": "Basic SSC", "batchText": "Course"}, 0, batches)
        self.assertEqual(bot.match_level([("13", "Basic SSC"), ("15", "Advance SSC")],
                                         advanced, loose=True)[0], "15")
        options = [("71", "Course from 20 Dec 2026 to 02 Jan 2027"),
                   ("72", "Course from 03 Jan 2027 to 16 Jan 2027")]
        self.assertEqual(bot.match_batch(options, advanced)[0], "72")
        self.assertIsNone(bot.match_batch([("1", "Course A"), ("2", "Course B")], advanced))
        self.assertIsNone(bot.match_batch(
            [("72", "Course at Kargil from 03 Jan 2027 to 16 Jan 2027")], advanced))

        wrong_course = bot.parse_priority({"batch": "G2", "course": "Snow Skiing", "level": "Advance",
                                           "courseText": "Basic Course"}, 0, batches)
        courses = [("1", "Snow Skiing Basic Course at Gulmarg"),
                   ("2", "Snow Skiing Advance Course at Kargil")]
        self.assertEqual(bot.course_candidates(courses, wrong_course), [])

    def test_hint_text_does_not_define_semantic_identity(self):
        batches = bot.load_season({}, Path("."))[0]
        plain = bot.parse_priority({"batch": "G2", "level": "Basic"}, 0, batches)
        hinted = bot.parse_priority({"batch": "g2", "levelText": "Basic SSC", "label": "hinted"}, 1, batches)
        self.assertEqual(plain.level, "Basic")
        self.assertEqual(hinted.level, "Basic")
        self.assertEqual(bot.target_key(plain), bot.target_key(hinted))
        board = bot.parse_priority({"batch": "G4", "course": "Snow Boarding"}, 0, batches)
        board_hint = bot.parse_priority({"batch": "G4", "course": "Snow Boarding",
                                         "levelText": "None", "label": "board hint"}, 1, batches)
        self.assertEqual(bot.target_key(board), bot.target_key(board_hint))
        with tempfile.TemporaryDirectory() as folder:
            cfg = make_config(Path(folder), PHONES[:2], [
                "G2 Basic", {"batch": "G2", "levelText": "Basic SSC", "label": "duplicate hint"}])
            self.assertTrue(any("duplicates" in error for error in bot.parse_settings(cfg, None)[1]))

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


class ConfigurationAndDocuments(unittest.TestCase):
    def errors_for(self, path, value):
        with tempfile.TemporaryDirectory() as folder:
            cfg = make_config(Path(folder), PHONES[:1], ["G2 Basic"])
            if path.startswith("bot."):
                cfg["bot"][path[4:]] = value
            else:
                cfg[path] = value
            try:
                return bot.parse_settings(cfg, None)[1]
            except bot.ConfigError as exc:
                return [str(exc)]

    def test_rejects_malformed_tuning_types_and_unknown_options(self):
        cases = [
            ("maxBookings", "1"),
            ("bot.parallel", "false"),
            ("bot.notify", 1),
            ("bot.preventSleep", 0),
            ("bot.parentSignatureFallback", None),
            ("bot.startEarlySec", "0"),
            ("bot.pollSec", "1"),
            ("bot.requestTimeoutSec", "1"),
            ("bot.submitTimeoutSec", "1"),
            ("bot.retryDelaySec", "1"),
            ("bot.maxRetryDelaySec", "1"),
            ("bot.fullConfirmations", 1.5),
            ("bot.unlistedGraceSec", "1"),
            ("bot.seatGraceSec", "1"),
            ("bot.recheckBlockedSec", "1"),
            ("bot.missingAlertSec", "1"),
            ("bot.maxRejections", 1.5),
            ("bot.rejectedRetrySec", "1"),
            ("bot.statusEverySec", "1"),
            ("bot.typoOption", 1),
        ]
        for path, value in cases:
            with self.subTest(path=path, value=value):
                errors = self.errors_for(path, value)
                self.assertTrue(any(path.split(".")[-1] in error for error in errors), errors)

    def test_rejects_unknown_root_option_before_network(self):
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder)
            cfg = make_config(tmp, PHONES[:2], ["G2 Basic", "G3 Basic"])
            cfg["maxBooking"] = 1
            path = tmp / "config.json"
            path.write_text(json.dumps(cfg))
            with mock.patch.object(bot, "Http") as http:
                self.assertEqual(bot.main(["--check", "--config", str(path)]), 2)
            http.assert_not_called()

    def test_rejects_malformed_config_containers_and_batch_records(self):
        cases = [
            ("applicant", []),
            ("address", []),
            ("phoneNumbers", {}),
            ("coursePriorities", {}),
            ("documents", []),
            ("bot", []),
            ("batches", []),
            ("batches", {"G2": []}),
            ("batches", {"G2": {"from": "2027-01-03", "offers": "Snow Skiing"}}),
        ]
        for key, value in cases:
            with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as folder:
                cfg = make_config(Path(folder), PHONES[:1], ["G2 Basic"])
                cfg[key] = value
                with self.assertRaises(bot.ConfigError):
                    bot.parse_settings(cfg, None)
        with self.assertRaises(bot.ConfigError):
            bot.parse_settings([], None)

    def test_rejects_unreadable_configured_dates_and_missing_schedule(self):
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder)
            for field in ("from", "to"):
                cfg = make_config(tmp, PHONES[:1], [{"batch": "G2", field: "not-a-date"}])
                errors = bot.parse_settings(cfg, None)[1]
                self.assertTrue(any(f"coursePriorities[0].{field}" in error for error in errors), errors)

            cfg = make_config(tmp, PHONES[:1], ["G2 Basic"],
                              batches={"G2": {"from": "2027-01-03", "to": "not-a-date"}})
            with self.assertRaisesRegex(bot.ConfigError, "batches.G2.to"):
                bot.parse_settings(cfg, None)

            cfg = make_config(tmp, PHONES[:1], ["G2 Basic"], scheduleFile=str(tmp / "missing.json"))
            with self.assertRaisesRegex(bot.ConfigError, "scheduleFile not found"):
                bot.parse_settings(cfg, None)

    def test_rejects_non_finite_and_out_of_range_tuning(self):
        cases = [
            ("maxBookings", -1),
            ("bot.startEarlySec", -1),
            ("bot.pollSec", 0),
            ("bot.requestTimeoutSec", float("nan")),
            ("bot.submitTimeoutSec", float("inf")),
            ("bot.retryDelaySec", 0),
            ("bot.maxRetryDelaySec", 0),
            ("bot.fullConfirmations", 0),
            ("bot.unlistedGraceSec", -1),
            ("bot.seatGraceSec", -1),
            ("bot.recheckBlockedSec", 0),
            ("bot.missingAlertSec", -1),
            ("bot.maxRejections", 0),
            ("bot.rejectedRetrySec", -1),
            ("bot.statusEverySec", 0),
        ]
        for path, value in cases:
            with self.subTest(path=path, value=value):
                errors = self.errors_for(path, value)
                self.assertTrue(any(path.split(".")[-1] in error for error in errors), errors)

        with tempfile.TemporaryDirectory() as folder:
            cfg = make_config(Path(folder), PHONES[:1], ["G2 Basic"],
                              bot={"retryDelaySec": 2, "maxRetryDelaySec": 1})
            errors = bot.parse_settings(cfg, None)[1]
            self.assertTrue(any("maxRetryDelaySec" in error for error in errors), errors)

    def test_converted_document_paths_remain_immutable_through_multipart(self):
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder)
            first = tmp / "docs" / "photo-a.pdf"
            second = tmp / "docs" / "photo-b.pdf"
            cfg = make_config(tmp, PHONES[:1], ["G2 Basic"])
            first.write_bytes(b"\xff\xd8\xfffirst applicant")
            second.write_bytes(b"\xff\xd8\xffsecond applicant")
            cfg["documents"]["photo"] = first.name
            first_settings, first_errors, _ = bot.parse_settings(cfg, None)
            cfg["documents"]["photo"] = second.name
            second_settings, second_errors, _ = bot.parse_settings(cfg, None)
            self.assertEqual(first_errors + second_errors, [])

            def convert(src, dst):
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
                return dst

            out = tmp / "run"
            with mock.patch.object(bot, "can_convert", return_value=True), \
                    mock.patch.object(bot, "convert_to_jpeg", side_effect=convert) as converter:
                first_problems, _ = bot.prepare_documents(first_settings, out)
                first_prepared = first_settings.docs["photo"]
                second_problems, _ = bot.prepare_documents(second_settings, out)
                second_prepared = second_settings.docs["photo"]

            self.assertEqual(first_problems + second_problems, [])
            self.assertNotEqual(first_prepared, second_prepared)
            self.assertEqual(first_prepared.content, first.read_bytes())
            self.assertEqual(second_prepared.content, second.read_bytes())
            self.assertEqual(converter.call_count, 2)
            self.assertFalse(any(path.name.startswith(".") for path in (out / "upload-ready").iterdir()))

            page = bot.Page("https://example.test/admission_form.aspx", 200,
                            '<form action="admission_form.aspx" enctype="multipart/form-data">'
                            '<input type="file" name="FupPhototOfApplicant" /></form>')
            session = object.__new__(bot.Session)
            session.http = mock.Mock()
            session.postback(page, "", {}, files={"FupPhototOfApplicant": first_prepared})
            multipart_body = session.http.send.call_args.args[2]
            self.assertIn(first.read_bytes(), multipart_body)
            self.assertNotIn(second.read_bytes(), multipart_body)

    def test_long_outage_backoff_stays_capped(self):
        instance = object.__new__(bot.Bot)
        instance.opt = {"retryDelaySec": 1.0, "maxRetryDelaySec": 10.0}
        with mock.patch.object(bot.random, "uniform", return_value=1.3):
            self.assertEqual(instance.backoff(2000), 10.0)


class MockMatchesRealSite(unittest.TestCase):
    """What the real form answered on 23 Oct 2026 (course "Adventure Courses", the only one listed then):
    the mock must answer the same postbacks the same way."""

    RECORDED = [
        ("ddlCourse", {"ddlCourse": "9"},
         {"levels": [("16", "None")], "batches": [],
          "txtAvailableSeat": "0", "txtCourseFee": "0", "hdfMinimumAge": "0", "hdfMaximumAge": "0",
          "hdfAgeCriteria": "12", "lblDOBMsg": ""}),
        ("ddlCourseLevel", {"ddlCourseLevel": "16"},
         {"batches": [("70", "Course from 13 Sep to 26 Sep 2026")]}),
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

    def run_with(self, phones, priorities, timeout=8, confirmation_handler=None, **extra):
        return run_bot(make_config(self.tmp, phones, priorities, **extra), self.url, self.out,
                       timeout=timeout, confirmation_handler=confirmation_handler)

    def key(self, batch, level="Basic", kind=None):
        return self.site.batch_key((batch, level), kind)

    def booked(self, instance):
        return sorted((x["target"], x["phone"], x["status"]) for x in instance.bookings)

    def applied(self):
        return sorted((a["course"], a["level"], a["schedule"]) for a in self.site.snapshot()["applications"])

    def feedback_from_receipts(self, phone, target, reason):
        return "booked" if any(a["mobile"] == phone for a in self.site.snapshot()["applications"]) else "not-booked"

    def test_every_preference_uses_its_primary_number(self):
        instance = self.run_with(PHONES[:2], ["G2 Basic", "G3 Basic"])
        self.assertEqual(self.booked(instance), [("G2 Basic", PHONES[0], "confirmed"),
                                                ("G3 Basic", PHONES[1], "confirmed")])
        self.assertTrue(instance.finished())
        self.assertGreater(self.site.level_postbacks, 0)
        self.assertEqual(self.applied(), sorted([self.key("G2"), self.key("G3")]))
        self.assertTrue(all(len(a["files"]) == 4 for a in self.site.applications))

    def test_sequential_option_keeps_the_same_assignments(self):
        instance = self.run_with(PHONES[:2], ["G2 Basic", "G3 Basic"], bot={"parallel": False})
        self.assertEqual(self.booked(instance), [("G2 Basic", PHONES[0], "confirmed"),
                                                ("G3 Basic", PHONES[1], "confirmed")])

    def test_full_slot_is_rechecked_without_stealing_other_primary(self):
        key = self.key("G2")
        self.site.seats[key] = 0
        timer = threading.Timer(0.6, lambda: self.site.seats.__setitem__(key, 2))
        timer.start()
        try:
            instance = self.run_with(PHONES, ["G2 Basic", "G3 Basic"])
        finally:
            timer.cancel()
        self.assertEqual({x["target"]: x["phone"] for x in instance.bookings},
                         {"G2 Basic": PHONES[0], "G3 Basic": PHONES[1]})
        self.assertNotIn(PHONES[2], self.site.used)

    def test_batch_appears_later_inside_an_already_visible_course(self):
        self.site.batch_opens[self.key("G2")[2]] = time.time() + 0.8
        instance = self.run_with(PHONES, ["G2 Basic", "G3 Basic"])
        self.assertEqual({x["target"]: x["phone"] for x in instance.bookings},
                         {"G2 Basic": PHONES[0], "G3 Basic": PHONES[1]})
        self.assertNotIn(PHONES[2], self.site.used)

    def test_disappearing_slot_returns_with_its_primary_while_sibling_books(self):
        g2 = self.key("G2")
        self.site.seats[g2] = 0
        self.site.shown_seats[g2] = 4
        original = mock_site.Handler.submit
        g2_attempts = []
        sibling_finished_before_return = []
        timer = None

        def submit_then_hide(handler, mobile, values, files, listed, course, level, batch):
            nonlocal timer
            if batch and batch[0] == g2[2]:
                g2_attempts.append(mobile)
                result = original(handler, mobile, values, files, listed, course, level, batch)
                if len(g2_attempts) == 1:
                    self.site.hide_full = True
                    for level_value, _ in self.site.courses[g2[0]]["levels"]:
                        self.site.seats[(g2[0], level_value, g2[2])] = 0

                    def restore():
                        sibling_finished_before_return.append(any(a["mobile"] == PHONES[1]
                                                                  for a in self.site.snapshot()["applications"]))
                        self.site.seats[g2] = 2
                        self.site.shown_seats.pop(g2, None)

                    timer = threading.Timer(0.8, restore)
                    timer.start()
                return result
            return original(handler, mobile, values, files, listed, course, level, batch)

        try:
            with mock.patch.object(mock_site.Handler, "submit", submit_then_hide):
                instance = self.run_with(PHONES, ["G2 Basic", "G3 Basic"])
        finally:
            if timer:
                timer.cancel()
        self.assertEqual({x["target"]: x["phone"] for x in instance.bookings},
                         {"G2 Basic": PHONES[0], "G3 Basic": PHONES[1]})
        self.assertEqual(g2_attempts, [PHONES[0], PHONES[0]])
        self.assertEqual(sibling_finished_before_return, [True])
        self.assertNotIn(PHONES[2], self.site.used)

    def test_seat_race_at_submit_keeps_retrying_same_number(self):
        key = self.key("G2")
        self.site.seats[key] = 0
        self.site.shown_seats[key] = 4
        timer = threading.Timer(0.6, lambda: self.site.seats.__setitem__(key, 2))
        timer.start()
        try:
            instance = self.run_with(PHONES[:2], ["G2 Basic"])
        finally:
            timer.cancel()
        self.assertEqual(self.booked(instance), [("G2 Basic", PHONES[0], "confirmed")])
        self.assertEqual(len(self.site.applications), 1)

    def test_final_submit_429_retries_same_phone_without_confirmation(self):
        original = mock_site.Handler.submit
        attempts = []

        def throttle_once(handler, *args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                return handler.send(429, "too many requests", {"Retry-After": "1"})
            return original(handler, *args, **kwargs)

        def no_confirmation(*args):
            self.fail("an explicit 429 must not request ambiguous-outcome feedback")

        with mock.patch.object(mock_site.Handler, "submit", throttle_once):
            instance = self.run_with(PHONES[:2], ["G2 Basic"], confirmation_handler=no_confirmation)
        self.assertEqual(self.booked(instance), [("G2 Basic", PHONES[0], "confirmed")])
        self.assertEqual(len(attempts), 2)
        self.assertNotIn(PHONES[1], self.site.used)
        submits = [(index, item) for index, item in enumerate(self.site.request_log) if item["submit"]]
        first_index, first_submit = submits[0]
        next_request = self.site.request_log[first_index + 1]
        self.assertEqual((next_request["method"], next_request["page"]),
                         ("GET", "admission_instructions.aspx"))
        self.assertNotEqual(next_request["sid"], first_submit["sid"])

    def test_used_primary_is_replaced_by_a_reserve(self):
        for style in ("label", "alert"):
            with self.subTest(style=style):
                self.site.used_style = style
                self.site.used = {PHONES[0]}
                self.site.applications.clear()
                shutil.rmtree(self.out, ignore_errors=True)
                instance = self.run_with(PHONES, ["G2 Basic", "G3 Basic"])
                self.assertEqual({x["target"]: x["phone"] for x in instance.bookings},
                                 {"G2 Basic": PHONES[2], "G3 Basic": PHONES[1]})

    def test_used_at_submit_is_not_treated_as_a_new_booking(self):
        self.site.used = {PHONES[0]}
        self.site.check_used_at_start = False
        instance = self.run_with(PHONES[:2], ["G2 Basic"])
        self.assertEqual(self.booked(instance), [("G2 Basic", PHONES[1], "confirmed")])

    def test_permanently_refused_primary_uses_a_reserve(self):
        self.site.refuse_numbers[PHONES[0]] = "Please Enter Valid Mobile No."
        instance = self.run_with(PHONES[:2], ["G2 Basic"])
        self.assertEqual(self.booked(instance), [("G2 Basic", PHONES[1], "confirmed")])

    def test_temporary_mobile_messages_never_consume_the_primary(self):
        original = mock_site.Handler.post_instructions
        refusals = []

        def temporarily_unavailable(handler, session, values):
            if values.get("txtMobileNo") == PHONES[0] and len(refusals) < 4:
                refusals.append(1)
                return handler.send(200, handler.instructions("Service temporarily unavailable. Please try again."))
            return original(handler, session, values)

        with mock.patch.object(mock_site.Handler, "post_instructions", temporarily_unavailable):
            instance = self.run_with(PHONES[:2], ["G2 Basic"])
        self.assertEqual(self.booked(instance), [("G2 Basic", PHONES[0], "confirmed")])
        self.assertEqual(len(refusals), 4)
        self.assertNotIn(PHONES[1], self.site.used)

    def test_trickled_worker_response_does_not_block_healthy_sibling(self):
        original = mock_site.Handler.post_instructions

        def trickle_first_phone(handler, session, values):
            if values.get("txtMobileNo") != PHONES[0]:
                return original(handler, session, values)
            data = handler.instructions().encode()
            handler.send_response(200)
            handler.send_header("Content-Type", "text/html; charset=utf-8")
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            try:
                for offset in range(0, len(data), 16):
                    handler.wfile.write(data[offset:offset + 16])
                    handler.wfile.flush()
                    time.sleep(0.03)
            except OSError:
                pass

        started = time.monotonic()
        with mock.patch.object(mock_site.Handler, "post_instructions", trickle_first_phone):
            instance = self.run_with(PHONES[:2], ["G2 Basic", "G3 Basic"], timeout=0.7,
                                     bot={"requestTimeoutSec": 0.15, "submitTimeoutSec": 0.5})
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(self.booked(instance), [("G3 Basic", PHONES[1], "confirmed")])
        self.assertFalse(instance.finished())

    def test_exhausted_numbers_do_not_stop_other_preferences(self):
        self.site.used = {PHONES[0]}
        instance = self.run_with(PHONES[:2], ["G2 Basic", "G3 Basic"], timeout=0.8)
        self.assertEqual(self.booked(instance), [("G3 Basic", PHONES[1], "confirmed")])
        self.assertFalse(instance.finished())

    def test_success_page_variants(self):
        for style in ("redirect", "alert", "payment"):
            with self.subTest(style=style):
                self.site.success_style = style
                self.site.used.clear()
                self.site.applications.clear()
                shutil.rmtree(self.out, ignore_errors=True)
                instance = self.run_with(PHONES[:1], ["G2 Basic"])
                self.assertEqual(self.booked(instance), [("G2 Basic", PHONES[0], "confirmed")])
                self.assertEqual(len(self.site.applications), 1)

    def test_failed_success_page_requires_feedback(self):
        self.site.fail_success_page = 1
        instance = self.run_with(PHONES[:1], ["G2 Basic"],
                                 confirmation_handler=self.feedback_from_receipts)
        self.assertEqual(self.booked(instance), [("G2 Basic", PHONES[0], "user-confirmed")])
        self.assertEqual(len(self.site.applications), 1)

    def test_redirected_receipt_errors_require_feedback_without_replay(self):
        cases = [
            (500, "<html><body>Validation of viewstate MAC failed.</body></html>", {}),
            (429, "too many requests", {"Retry-After": "1"}),
        ]
        for status, body, headers in cases:
            with self.subTest(status=status):
                self.site.used.clear()
                self.site.applications.clear()
                self.site.request_log.clear()
                self.site.success_receipt = (status, body, headers)
                shutil.rmtree(self.out, ignore_errors=True)
                instance = self.run_with(PHONES[:1], ["G2 Basic"],
                                         confirmation_handler=self.feedback_from_receipts)
                self.assertEqual(self.booked(instance), [("G2 Basic", PHONES[0], "user-confirmed")])
                self.assertEqual(len(self.site.applications), 1)
        self.site.success_receipt = None

    def test_same_url_receipt_redirect_is_uncertain_not_replayed(self):
        self.site.receipt_redirects[PHONES[0]] = f"/admission_form.aspx?mobile={PHONES[0]}"
        instance = self.run_with(PHONES[:1], ["G2 Basic"], confirmation_handler=self.feedback_from_receipts)
        self.assertEqual(self.booked(instance), [("G2 Basic", PHONES[0], "user-confirmed")])
        self.assertEqual(len(self.site.applications), 1)

    def test_uncertainty_holds_one_batch_and_survives_restart(self):
        self.site.crash_after_commit = 1
        instance = self.run_with(PHONES, ["G2 Basic", "G3 Basic"], timeout=0.8,
                                 confirmation_handler=lambda *args: None)
        self.assertEqual(len(instance.pending), 1)
        self.assertEqual(len(instance.bookings), 1)
        self.assertEqual(len(self.site.applications), 2)
        self.assertNotIn(PHONES[2], self.site.used)
        requests = self.site.requests
        resumed = self.run_with(PHONES, ["G2 Basic", "G3 Basic"],
                                confirmation_handler=lambda *args: "booked")
        self.assertTrue(resumed.finished())
        self.assertEqual(self.site.requests, requests, "Restored uncertainty must be resolved before any new request")
        self.assertEqual(len(self.site.applications), 2)

    def test_not_booked_feedback_retries_same_primary(self):
        original = mock_site.Handler.submit
        attempts = []
        def fail_once(handler, *args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                return handler.send(503, mock_site.IIS_503)
            return original(handler, *args, **kwargs)
        with mock.patch.object(mock_site.Handler, "submit", fail_once):
            instance = self.run_with(PHONES[:2], ["G2 Basic"],
                                     confirmation_handler=lambda *args: "not-booked")
        self.assertEqual(self.booked(instance), [("G2 Basic", PHONES[0], "confirmed")])
        self.assertEqual(len(attempts), 2)
        self.assertNotIn(PHONES[1], self.site.used)

    def test_known_validation_rejection_parks_once_while_sibling_finishes(self):
        original = mock_site.Handler.submit
        rejected_batch = self.key("G2")[2]
        rejections = []

        def reject_local_data(handler, mobile, values, files, listed, course, level, batch):
            if batch and batch[0] == rejected_batch:
                rejections.append(mobile)
                values["lblMsg"] = ("Please Enter Name.", "Red")
                return handler.send(200, handler.form(mobile, values, listed, invalid=["rfvtxtName"]))
            return original(handler, mobile, values, files, listed, course, level, batch)

        with mock.patch.object(mock_site.Handler, "submit", reject_local_data):
            instance = self.run_with(PHONES, ["G2 Basic", "G3 Basic"])
        self.assertEqual(self.booked(instance), [("G3 Basic", PHONES[1], "confirmed")])
        self.assertEqual(rejections, [PHONES[0]])
        self.assertEqual(instance.blocked[0]["kind"], "rejected")
        self.assertNotIn(PHONES[2], self.site.used)

    def test_temporary_submit_message_requires_feedback_without_replay(self):
        temporary = []

        def retry_later(handler, mobile, values, files, listed, course, level, batch):
            temporary.append(1)
            site = handler.site
            key = (values["ddlCourse"], values["ddlCourseLevel"], values["ddlScheduleCourse"])
            with site.lock:
                site.seats[key] -= 1
                site.used.add(mobile)
                site.applications.append({"mobile": mobile, "course": key[0], "level": key[1],
                                          "schedule": key[2], "files": [], "fileHashes": {}})
            values["lblMsg"] = ("Service temporarily unavailable. Please try again.", "Red")
            return handler.send(200, handler.form(mobile, values, listed))

        with mock.patch.object(mock_site.Handler, "submit", retry_later):
            instance = self.run_with(PHONES[:2], ["G2 Basic"],
                                     confirmation_handler=self.feedback_from_receipts)
        self.assertEqual(self.booked(instance), [("G2 Basic", PHONES[0], "user-confirmed")])
        self.assertEqual(len(temporary), 1)
        self.assertEqual(len(self.site.applications), 1)
        self.assertNotIn(PHONES[1], self.site.used)

    def test_unknown_error_form_stays_pending_across_restart(self):
        attempts = []

        def commit_then_error(handler, mobile, values, files, listed, course, level, batch):
            attempts.append(mobile)
            site = handler.site
            key = (values["ddlCourse"], values["ddlCourseLevel"], values["ddlScheduleCourse"])
            with site.lock:
                site.seats[key] -= 1
                site.used.add(mobile)
                site.applications.append({"mobile": mobile, "course": key[0], "level": key[1],
                                          "schedule": key[2], "files": [], "fileHashes": {}})
            values["lblMsg"] = ("Unexpected error while processing request", "Red")
            return handler.send(200, handler.form(mobile, values, listed))

        with mock.patch.object(mock_site.Handler, "submit", commit_then_error):
            first = self.run_with(PHONES[:2], ["G2 Basic"], timeout=0.6,
                                  confirmation_handler=lambda *args: None)
        self.assertEqual(first.bookings, [])
        self.assertEqual(first.pending, {PHONES[0]: bot.target_key(first.targets[0])})
        self.assertEqual(attempts, [PHONES[0]])
        self.assertEqual(len(self.site.applications), 1)
        self.assertNotIn(PHONES[1], self.site.used)

        requests = self.site.requests
        resumed = self.run_with(PHONES[:2], ["G2 Basic"], confirmation_handler=lambda *args: "booked")
        self.assertEqual(self.booked(resumed), [("G2 Basic", PHONES[0], "user-confirmed")])
        self.assertEqual(self.site.requests, requests)
        self.assertEqual(len(self.site.applications), 1)

    def test_direct_viewstate_error_after_commit_stays_pending_with_sibling_progress(self):
        original = mock_site.Handler.submit
        attempts = []

        def commit_then_viewstate_error(handler, mobile, values, files, listed, course, level, batch):
            if mobile != PHONES[0]:
                return original(handler, mobile, values, files, listed, course, level, batch)
            attempts.append(mobile)
            site = handler.site
            key = (values["ddlCourse"], values["ddlCourseLevel"], values["ddlScheduleCourse"])
            with site.lock:
                site.seats[key] -= 1
                site.used.add(mobile)
                site.applications.append({"mobile": mobile, "course": key[0], "level": key[1],
                                          "schedule": key[2], "files": [], "fileHashes": {}})
            return handler.ysod("Validation of viewstate MAC failed.")

        with mock.patch.object(mock_site.Handler, "submit", commit_then_viewstate_error):
            first = self.run_with(PHONES, ["G2 Basic", "G3 Basic"], timeout=0.8,
                                  confirmation_handler=lambda *args: None)
        self.assertEqual(self.booked(first), [("G3 Basic", PHONES[1], "confirmed")])
        self.assertEqual(first.pending, {PHONES[0]: bot.target_key(first.targets[0])})
        self.assertEqual(attempts, [PHONES[0]])
        self.assertEqual(len(self.site.applications), 2)
        self.assertNotIn(PHONES[2], self.site.used)

        requests = self.site.requests
        resumed = self.run_with(PHONES, ["G2 Basic", "G3 Basic"],
                                confirmation_handler=lambda *args: "booked")
        self.assertTrue(resumed.finished())
        self.assertEqual(self.site.requests, requests)
        self.assertEqual(len(self.site.applications), 2)

    def test_repeated_viewstate_failures_restart_from_mobile_and_grow_backoff(self):
        original = mock_site.Handler.post_form
        failed_sids = []
        delays = []

        def fail_state(handler, session, query, values, files):
            if values.get("__EVENTTARGET") == "ddlState" and len(failed_sids) < 3:
                failed_sids.append(handler.sid)
                return handler.ysod("Validation of viewstate MAC failed.")
            return original(handler, session, query, values, files)

        def record_delay(instance, failures, error):
            delays.append(failures)
            return 0

        with mock.patch.object(mock_site.Handler, "post_form", fail_state), \
                mock.patch.object(bot.Bot, "retry_delay", record_delay):
            instance = self.run_with(PHONES[:1], ["G2 Basic"])
        self.assertTrue(instance.finished())
        self.assertEqual(delays[:3], [1, 2, 3])
        cursor = 0
        for failed_sid in failed_sids:
            index = next(i for i in range(cursor, len(self.site.request_log))
                         if self.site.request_log[i]["event"] == "ddlState"
                         and self.site.request_log[i]["sid"] == failed_sid)
            next_request = self.site.request_log[index + 1]
            self.assertEqual((next_request["method"], next_request["page"]),
                             ("GET", "admission_instructions.aspx"))
            self.assertNotEqual(next_request["sid"], failed_sid)
            cursor = index + 1

    def test_discover_restarts_from_mobile_after_postback_failure(self):
        original = mock_site.Handler.post_form
        failed = []

        def fail_course_once(handler, session, query, values, files):
            if values.get("__EVENTTARGET") == "ddlCourse" and not failed:
                failed.append(handler.sid)
                return handler.send(500, mock_site.YSOD)
            return original(handler, session, query, values, files)

        settings = bot.parse_settings(make_config(self.tmp, PHONES[:1], ["G2 Basic"]), None)[0]
        instance = bot.Bot(settings, self.out, base_url=self.url, dry_run=True, log=bot.Log(echo=False))
        with mock.patch.object(mock_site.Handler, "post_form", fail_course_once):
            self.assertEqual(bot.run_discover(instance, PHONES[0]), 0)
        failed_index = next(i for i, item in enumerate(self.site.request_log)
                            if item["event"] == "ddlCourse" and item["sid"] == failed[0])
        next_request = self.site.request_log[failed_index + 1]
        self.assertEqual((next_request["method"], next_request["page"]),
                         ("GET", "admission_instructions.aspx"))
        self.assertNotEqual(next_request["sid"], failed[0])

    def test_survives_crashes_sessions_and_application_restarts(self):
        self.site.chaos, self.site.session_loss, self.site.restarts = 0.35, 0.04, 0.02
        self.site.slow_seconds = 2.5
        instance = self.run_with(PHONES[:2], ["G2 Basic", "G3 Basic"], timeout=90,
                                 confirmation_handler=self.feedback_from_receipts)
        self.assertTrue(instance.finished())
        self.assertEqual(self.applied(), sorted([self.key("G2"), self.key("G3")]))

    def test_courses_may_open_early_late_or_one_at_a_time(self):
        self.site.opens_at = time.time() + 0.5
        self.site.course_opens[self.key("K1")[0]] = time.time() + 0.9
        instance = self.run_with(PHONES[:2], ["G2 Basic", "K1 Basic"],
                                 bookingOpensAt=(bot.now_ist() + timedelta(seconds=120)).isoformat())
        self.assertTrue(instance.finished())
        self.assertEqual(len(self.site.applications), 2)

    def test_changed_course_wording_uses_real_basic_level_and_dates(self):
        course_id = self.key("G2")[0]
        self.site.courses[course_id] = dict(self.site.courses[course_id])
        self.site.courses[course_id]["name"] = "Gulmarg Winter Training"
        self.site.courses[course_id]["levels"] = [("13", "Basic SSC")]
        self.site.seats[(course_id, "13", self.key("G2")[2])] = 3
        instance = self.run_with(PHONES[:1], ["G2 Basic"])
        self.assertTrue(instance.finished())
        self.assertEqual(self.site.applications[0]["level"], "13")

    def test_stale_text_hints_rank_but_never_block_semantic_match(self):
        course_id, level_id, batch_id = self.key("G2")
        course = dict(self.site.courses[course_id])
        course["name"] = "Gulmarg Winter Training"
        course["levels"] = [(level_id, "Basic SSC")]
        course["batches"] = [
            (value, "Winter session 03 Jan 2027 through 16 Jan 2027", start, location)
            if value == batch_id else (value, text, start, location)
            for value, text, start, location in course["batches"]]
        self.site.courses[course_id] = course
        priority = {"batch": "G2", "course": "Snow Skiing", "level": "Basic",
                    "courseText": "Snow Skiing Courses at Gulmarg",
                    "levelText": "Beginner Level", "batchText": "Old G2 January Batch"}
        instance = self.run_with(PHONES[:1], [priority])
        self.assertTrue(instance.finished())
        self.assertEqual((self.site.applications[0]["course"], self.site.applications[0]["level"],
                          self.site.applications[0]["schedule"]), (course_id, level_id, batch_id))

    def test_resume_preserves_bookings_when_another_preference_is_added(self):
        self.run_with(PHONES[:2], ["G2 Basic"])
        resumed = self.run_with(PHONES[:2], ["G2 Basic", "G3 Basic"])
        self.assertTrue(resumed.finished())
        self.assertEqual(len(self.site.applications), 2)

    def test_hint_only_change_restores_the_same_accepted_preference(self):
        first = self.run_with(PHONES[:1], ["G2 Basic"])
        state = json.loads((self.out / "state.json").read_text())
        self.assertEqual((state["version"], state["targetKeyVersion"]),
                         (bot.STATE_VERSION, bot.TARGET_KEY_VERSION))
        self.assertEqual(state["bookings"][0]["targetIdentity"], bot.target_identity(first.targets[0]))
        hinted = {"batch": "g2", "levelText": "Basic SSC", "courseText": "Snow Skiing",
                  "batchText": "Course", "label": "same preference"}
        resumed = self.run_with(PHONES[:1], [hinted])
        self.assertTrue(resumed.finished())
        self.assertEqual(len(self.site.applications), 1)

    def test_removed_accepted_history_stays_visible_and_protects_its_phone(self):
        self.run_with(PHONES[:2], ["G2 Basic"])
        resumed = self.run_with(PHONES[:2], ["G3 Basic"])
        self.assertTrue(resumed.finished())
        self.assertEqual({item["target"]: item["phone"] for item in resumed.bookings},
                         {"G2 Basic": PHONES[0], "G3 Basic": PHONES[1]})
        self.assertEqual(len(self.site.applications), 2)

    def test_same_date_changed_kind_uses_reserve_without_hiding_history(self):
        self.run_with(PHONES[:2], ["G4 Basic"])
        resumed = self.run_with(PHONES[:2], ["G4 Snowboarding"])
        self.assertTrue(resumed.finished())
        self.assertEqual([item["phone"] for item in resumed.bookings], PHONES[:2])
        self.assertEqual(self.site.applications[-1]["mobile"], PHONES[1])

    def test_same_date_changed_level_uses_reserve_without_hiding_history(self):
        self.run_with(PHONES[:2], ["G4 Basic"])
        resumed = self.run_with(PHONES[:2], ["G4 Intermediate"])
        self.assertTrue(resumed.finished())
        self.assertEqual([item["phone"] for item in resumed.bookings], PHONES[:2])
        self.assertEqual(self.site.applications[-1]["mobile"], PHONES[1])

    def test_unsupported_old_progress_schema_is_preserved_and_fails_closed(self):
        self.run_with(PHONES[:1], ["G2 Basic"])
        state_path = self.out / "state.json"
        state = json.loads(state_path.read_text())
        state["version"] = 1
        state.pop("targetKeyVersion")
        state_path.write_text(json.dumps(state))
        preserved = state_path.read_bytes()
        settings = bot.parse_settings(
            make_config(self.tmp, PHONES[:1], [{"batch": "G2", "levelText": "Basic SSC"}]), None)[0]
        requests = self.site.requests
        with self.assertRaisesRegex(bot.ConfigError, "unsupported progress schema"):
            bot.Bot(settings, self.out, base_url=self.url, log=bot.Log(echo=False))
        self.assertEqual(state_path.read_bytes(), preserved)
        self.assertEqual(self.site.requests, requests)

    def test_old_booking_does_not_complete_a_changed_date(self):
        self.run_with(PHONES[:1], ["G2 Basic"])
        changed = {"batch": "G2", "from": "2028-01-03", "to": "2028-01-16", "label": "G2 Basic"}
        instance = self.run_with(PHONES[:1], [changed], timeout=0.3)
        self.assertFalse(instance.finished())
        self.assertEqual(len(instance.bookings), 1)
        self.assertEqual(instance.bookings[0]["phone"], PHONES[0])
        self.assertEqual(instance.phone_state[PHONES[0]], "booked")
        self.assertEqual(len(self.site.applications), 1)

    def test_cancellation_after_cascade_prevents_final_post(self):
        original = bot.Session.cascade
        def cancel(session, target):
            selection = original(session, target)
            session.bot.stop.set()
            return selection
        with mock.patch.object(bot.Session, "cascade", cancel):
            instance = self.run_with(PHONES[:1], ["G2 Basic"])
        self.assertEqual(instance.bookings, [])
        self.assertEqual(self.site.applications, [])

    def test_dry_run_sends_no_final_application_or_progress(self):
        instance = run_bot(make_config(self.tmp, PHONES[:2], ["G2 Basic", "G3 Basic"]),
                           self.url, self.out, dry_run=True)
        self.assertEqual(len(instance.bookings), 2)
        self.assertEqual(self.site.applications, [])
        self.assertFalse((self.out / "state.json").exists())

    def test_missing_required_document_fails_preflight(self):
        cfg = make_config(self.tmp, PHONES[:1], ["G2 Basic"])
        cfg["documents"]["photo"] = ""
        settings, _, _ = bot.parse_settings(cfg, None)
        self.assertTrue(any("photo" in problem for problem in bot.prepare_documents(settings, self.out)[0]))
        self.assertEqual(self.site.requests, 0)

    def test_invalid_email_fails_preflight_using_observed_validator(self):
        cfg = make_config(self.tmp, PHONES[:1], ["G2 Basic"], applicant={"email": "first..last@example.com"})
        _, errors, _ = bot.parse_settings(cfg, None)
        self.assertTrue(any("email" in error for error in errors))
        self.assertEqual(self.site.requests, 0)

    def test_parent_signature_is_required_only_for_minors(self):
        for born, required in (("15/05/1998", False), ("15/05/2010", True)):
            cfg = make_config(self.tmp, PHONES[:1], ["G2 Basic"], applicant={"dateOfBirth": born})
            settings, _, _ = bot.parse_settings(cfg, None)
            problems, _ = bot.prepare_documents(settings, self.out)
            self.assertEqual(any("parentSignature" in p for p in problems), required)

    def test_server_classified_minor_without_guardian_is_parked_before_submit(self):
        course_id = self.key("G2")[0]
        self.site.courses[course_id] = dict(self.site.courses[course_id])
        self.site.courses[course_id]["count_date"] = date(2026, 12, 1)
        instance = self.run_with(PHONES[:1], ["G2 Basic"], applicant={"dateOfBirth": "15/12/2008"})
        self.assertEqual(instance.bookings, [])
        self.assertEqual(self.site.applications, [])
        self.assertIn("real guardian signature", instance.blocked[0]["why"])
        self.assertFalse(instance.parent_fallback)

    def test_accepted_adult_signature_substitution_only_if_site_insists(self):
        self.site.parent_signature_required = True
        instance = self.run_with(PHONES[:1], ["G2 Basic"])
        self.assertTrue(instance.finished())
        self.assertTrue(instance.parent_fallback)
        self.assertIn("FupParentGuardianSignature", self.site.applications[0]["files"])
        submits = [(index, item) for index, item in enumerate(self.site.request_log) if item["submit"]]
        first_index, first_submit = submits[0]
        next_request = self.site.request_log[first_index + 1]
        self.assertEqual((next_request["method"], next_request["page"]),
                         ("GET", "admission_instructions.aspx"))
        self.assertNotEqual(next_request["sid"], first_submit["sid"])

    def test_adult_signature_is_not_sent_in_guardian_slot_unnecessarily(self):
        instance = self.run_with(PHONES[:1], ["G2 Basic"])
        self.assertTrue(instance.finished())
        self.assertNotIn("FupParentGuardianSignature", self.site.applications[0]["files"])


class EndToEndOtherLayout(EndToEnd):
    layout = "courses"


class EndToEndLevelCourses(EndToEnd):
    layout = "level-courses"


if __name__ == "__main__":
    unittest.main()
