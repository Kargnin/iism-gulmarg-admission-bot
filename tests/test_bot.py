"""
Tests for iism-bot.py. The end-to-end tests run the bot against tests/mock_site.py,
a copy of the real site's pages and flow.

  python3 -m unittest discover -s tests -v
  VERBOSE=1 python3 -m unittest tests.test_bot.EndToEnd.test_survives_a_crashing_site   # show the bot's log
"""

import contextlib
import copy
import hashlib
import importlib.util
import io
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

    def test_non_skiing_targets_reject_known_competitors_but_keep_generic_paths(self):
        moi, board = target("G5 MOI"), target("G4 Snowboarding")
        basic = ("21", "Snow Skiing Basic Course")
        moi_course = ("25", "MOI Course")
        board_course = ("24", "Snow Boarding Course")
        self.assertEqual(bot.course_candidates([basic], moi), [])
        self.assertEqual(bot.course_candidates([basic], board), [])
        self.assertEqual(bot.course_candidates([board_course], moi), [])
        self.assertEqual(bot.course_candidates([moi_course], board), [])

        parent = ("10", "Snow Skiing Courses at Gulmarg")
        board_level = ("17", "Snow Boarding")
        board_batch = ("74", "Course from 31 Jan 2027 to 13 Feb 2027")
        self.assertIn(parent, bot.course_candidates([parent], board))
        self.assertFalse(bot.selection_conflicts(parent, board_level, board_batch, board))

        generic_course = ("9", "Gulmarg Winter Training")
        generic_level = ("16", "None")
        moi_batch = ("75", "Course from 14 Feb 2027 to 27 Feb 2027")
        self.assertFalse(bot.selection_conflicts(generic_course, generic_level, moi_batch, moi))
        self.assertTrue(bot.selection_conflicts(parent, generic_level, moi_batch, moi))

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

    def test_course_hints_only_break_ties_within_semantic_rank(self):
        batches = bot.load_season({}, Path("."))[0]
        configured = bot.parse_priority({"batch": "G2", "course": "Snow Skiing", "level": "Basic",
                                         "courseText": "Generic Adventure"}, 0, batches)
        options = [("9", "Generic Adventure"), ("10", "Snow Skiing Course at Gulmarg")]
        self.assertEqual(bot.course_candidates(options, configured)[0][0], "10")
        self.assertEqual(bot.course_candidates(options, configured, hint="9")[0][0], "10")

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
        self.assertEqual(bot.match_choice(mock_site.DIET, "Non-Vegetarian")[0], "Non-Vegetarian")
        self.assertEqual(bot.match_choice(mock_site.EDUCATION, "Post graduation")[0], "Post graduation")
        self.assertEqual(bot.match_choice(states, "Andhra Pradesh N")[0], "37")
        self.assertEqual(bot.match_choice(states, "Mahar astra")[0], "27")
        self.assertEqual(bot.match_choice(states, "Maharastra")[0], "27")
        self.assertEqual(bot.match_choice(mock_site.DIET, "non veg")[0], "Non-Vegetarian")
        self.assertEqual(bot.match_choice(mock_site.DIET, "nonveg")[0], "Non-Vegetarian")
        self.assertEqual(bot.match_choice(mock_site.EDUCATION, "Post Graduate")[0], "Post graduation")
        self.assertEqual(bot.match_choice(mock_site.EDUCATION, "post grad")[0], "Post graduation")
        self.assertEqual(bot.match_choice(mock_site.EDUCATION, "grad")[0], "Graduation")
        self.assertEqual(bot.match_choice(states, "UP")[0], "9")
        self.assertEqual(bot.match_choice(mock_site.EDUCATION, "PG")[0], "Post graduation")
        self.assertEqual(bot.match_choice(mock_site.EDUCATION, "HS")[0], "High School")
        self.assertIsNone(bot.match_choice(states, "AP"))
        self.assertEqual(bot.choice_suggestions(states, "AP"), ["Andhra Pradesh", "ARUNACHAL PRADESH"])
        self.assertIsNone(bot.match_choice(states, "totally unknown place"))
        self.assertIsNone(bot.match_choice([("1", "Same"), ("2", "same")], "same"))

    def test_applicant_choices_are_frozen_and_unresolvable_values_list_options(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = make_config(Path(folder), PHONES[:1], ["G2 Basic"])
            cfg["address"]["state"] = "Andhra Pradesh N"
            cfg["applicant"]["education"] = "post grad"
            cfg["applicant"]["diet"] = "nonveg"
            settings, errors, _ = bot.parse_settings(cfg, None)
            self.assertEqual(errors, [])
            self.assertEqual((settings.state, settings.education, settings.diet),
                             ("ANDHRA PRADESH (NEW)", "Post graduation", "Non-Vegetarian"))
            log_path = Path(folder) / "plan.log"
            log = bot.Log(echo=False)
            log.open(log_path)
            try:
                bot.describe_plan(settings, log)
            finally:
                log.file.close()
            plan = log_path.read_text()
            for resolved in (settings.state, settings.education, settings.diet):
                self.assertIn(resolved, plan)

            abbreviated = make_config(Path(folder), PHONES[:1], ["G2 Basic"])
            abbreviated["address"]["state"] = "UP"
            abbreviated["applicant"]["education"] = "PG"
            resolved, errors, _ = bot.parse_settings(abbreviated, None)
            self.assertEqual(errors, [])
            self.assertEqual((resolved.state, resolved.education), ("UTTAR PRADESH", "Post graduation"))

            high_school = make_config(Path(folder), PHONES[:1], ["G2 Basic"])
            high_school["applicant"]["education"] = "HS"
            resolved, errors, _ = bot.parse_settings(high_school, None)
            self.assertEqual(errors, [])
            self.assertEqual(resolved.education, "High School")

            ambiguous = make_config(Path(folder), PHONES[:1], ["G2 Basic"])
            ambiguous["address"]["state"] = "AP"
            error = next(error for error in bot.parse_settings(ambiguous, None)[1]
                         if "address.state" in error)
            self.assertIn("possible matches: Andhra Pradesh | ARUNACHAL PRADESH", error)

            cases = [
                (("address", "state"), "totally unknown place", "MAHARASHTRA"),
                (("applicant", "education"), "unrelated qualification", "Post graduation"),
                (("applicant", "diet"), "anything is fine", "Non-Vegetarian"),
            ]
            for path, value, accepted in cases:
                with self.subTest(path=".".join(path)):
                    invalid = make_config(Path(folder), PHONES[:1], ["G2 Basic"])
                    invalid[path[0]][path[1]] = value
                    matching = [error for error in bot.parse_settings(invalid, None)[1]
                                if ".".join(path) in error]
                    self.assertEqual(len(matching), 1)
                    self.assertIn("accepted choices:", matching[0])
                    self.assertIn(accepted, matching[0])

    def test_operational_guidance_preserves_active_invocation(self):
        hint = bot.fix_hint("Please Enter Name")
        self.assertIn("same invocation", hint)
        for flag in ("--config", "--base-url", "--dry-run"):
            self.assertIn(flag, hint)
        self.assertNotIn("python3 iism-bot.py", hint)

        with tempfile.TemporaryDirectory() as folder:
            cfg = make_config(Path(folder), PHONES[:1], ["G2 Basic"])
            settings = bot.parse_settings(cfg, Path(folder) / "person config.jsonc")[0]
            page = bot.Page("http://127.0.0.1:8765/Admission_instructions.aspx", 200,
                            '<form action="Admission_instructions.aspx"><input name="__VIEWSTATE" value="x">'
                            '<input name="txtMobileNo"></form>')
            output = io.StringIO()
            with mock.patch.object(bot, "Http") as http, contextlib.redirect_stdout(output):
                http.return_value.get.return_value = page
                self.assertEqual(bot.run_check(settings, [], [], [], [], "http://127.0.0.1:8765", bot.Log()), 0)
            guidance = output.getvalue()
            self.assertIn("same --config and --base-url values", guidance)
            self.assertNotIn("On booking day run:  python3 iism-bot.py", guidance)

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

    def test_batch_codes_are_unique_per_source_but_inline_overrides_the_schedule(self):
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder)
            record = {"from": "2027-01-03", "to": "2027-01-16", "location": "Gulmarg"}
            cfg = make_config(tmp, PHONES[:1], ["G2 Basic"],
                              batches={"G2": record, " g2 ": dict(record)})
            path = tmp / "duplicate-inline.json"
            path.write_text(json.dumps(cfg))
            with mock.patch.object(bot, "Http") as http:
                self.assertEqual(bot.main(["--check", "--config", str(path)]), 2)
            http.assert_not_called()

            duplicate_schedule = tmp / "duplicate-schedule.json"
            duplicate_schedule.write_text(json.dumps({"batches": {"G2": record, " g2 ": dict(record)}}))
            cfg = make_config(tmp, PHONES[:1], ["G2 Basic"], scheduleFile=str(duplicate_schedule))
            with self.assertRaisesRegex(bot.ConfigError, "defines batch G2 more than once"):
                bot.parse_settings(cfg, None)

            base_schedule = tmp / "base-schedule.json"
            base_schedule.write_text(json.dumps({"batches": {"G2": record}}))
            override = {"from": "2028-01-04", "to": "2028-01-17", "location": "Kargil"}
            cfg = make_config(tmp, PHONES[:1], ["G2 Basic"], scheduleFile=str(base_schedule),
                              batches={" g2 ": override})
            settings, errors, _ = bot.parse_settings(cfg, None)
            self.assertEqual(errors, [])
            self.assertEqual(settings.batches["G2"]["from"], date(2028, 1, 4))
            self.assertEqual(settings.batches["G2"]["location"], "Kargil")
            self.assertEqual(settings.targets[0].start, date(2028, 1, 4))

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

            cfg = make_config(tmp, PHONES[:1],
                              [{"batch": "G2", "from": "2027-01-16", "to": "2027-01-03"}])
            self.assertTrue(any("ends before it starts" in error for error in bot.parse_settings(cfg, None)[1]))

            cfg = make_config(tmp, PHONES[:1], ["G2 Basic"],
                              batches={"G2": {"from": "2027-01-16", "to": "2027-01-03"}})
            with self.assertRaisesRegex(bot.ConfigError, "ends before it starts"):
                bot.parse_settings(cfg, None)

            schedule = tmp / "reversed-schedule.json"
            schedule.write_text(json.dumps({"batches": {
                "G2": {"from": "2027-01-16", "to": "2027-01-03", "location": "Gulmarg"}}}))
            cfg = make_config(tmp, PHONES[:1], ["G2 Basic"], scheduleFile=str(schedule))
            with self.assertRaisesRegex(bot.ConfigError, "ends before it starts"):
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
                            f'<input name="txtMobileNo" value="{PHONES[0]}" />'
                            '<input type="file" name="FupPhototOfApplicant" /></form>')
            session = object.__new__(bot.Session)
            session.http = mock.Mock()
            session.phone = PHONES[0]
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

    def test_full_hinted_course_continues_to_later_compatible_course(self):
        original_id = self.key("G2")[0]
        original_course = self.site.courses[original_id]
        full_id = "98"
        full_course = dict(original_course)
        full_course["name"] = original_course["name"] + " Preferred"
        full_course["levels"] = list(original_course["levels"])
        full_course["batches"] = list(original_course["batches"])
        self.site.courses[full_id] = full_course
        for level, _ in full_course["levels"]:
            for batch, *_ in full_course["batches"]:
                self.site.seats[(full_id, level, batch)] = 0

        priorities = ({"batch": "G2", "course": "Snow Skiing", "level": "Basic",
                       "courseText": "Preferred"}, "G2 Basic")
        for mode, priority in zip(("configured", "runtime"), priorities):
            with self.subTest(mode=mode):
                self.site.used.clear()
                self.site.applications.clear()
                self.site.sessions.clear()
                self.site.viewstates.clear()
                self.site.request_log.clear()
                attempted = []
                original_post = mock_site.Handler.post_form

                def record_courses(handler, session, query, values, files):
                    if values.get("__EVENTTARGET") == "ddlCourse":
                        attempted.append((values.get("ddlCourse"), handler.sid))
                    return original_post(handler, session, query, values, files)

                cfg = make_config(self.tmp, PHONES[:1], [priority])
                out = self.tmp / f"full-hint-{mode}"
                with mock.patch.object(mock_site.Handler, "post_form", record_courses):
                    if mode == "configured":
                        instance = run_bot(cfg, self.url, out, timeout=4)
                    else:
                        settings = bot.parse_settings(cfg, None)[0]
                        self.assertEqual(bot.prepare_documents(settings, out)[0], [])
                        instance = bot.Bot(settings, out, base_url=self.url, log=bot.Log(echo=False))
                        instance.course_hint[0] = full_id
                        guard = threading.Timer(4, instance.stop.set)
                        guard.start()
                        try:
                            instance.run()
                        finally:
                            guard.cancel()
                self.assertTrue(instance.finished())
                full_sessions = {sid for course, sid in attempted if course == full_id}
                available_sessions = {sid for course, sid in attempted if course == original_id}
                first_full_session = next(sid for course, sid in attempted if course == full_id)
                self.assertGreaterEqual(len(full_sessions), 2)
                self.assertTrue(any(sid != first_full_session for sid in available_sessions))
                self.assertEqual(len(self.site.applications), 1)
                self.assertEqual(self.site.applications[0]["course"], original_id)
                self.assertEqual(self.site.applications[0]["mobile"], PHONES[0])
                self.assertFalse(any(application["course"] == full_id for application in self.site.applications))

    def test_full_first_compatible_level_continues_to_later_level(self):
        if self.layout != "levels":
            self.skipTest("multi-level layout only")
        course_id, first_level, batch_id = self.key("G2")
        course = self.site.courses[course_id]
        course["levels"] = list(course["levels"]) + [("19", "Basic alternate")]
        for batch, *_ in course["batches"]:
            self.site.seats[(course_id, "19", batch)] = 3
        self.site.seats[(course_id, first_level, batch_id)] = 0
        instance = self.run_with(PHONES[:1], ["G2 Basic"])
        self.assertTrue(instance.finished())
        self.assertEqual(self.site.applications[0]["level"], "19")

    def _book_after_full_message(self, stage):
        original_id, original_level, original_batch = self.key("G2")
        expected_course = original_id
        expected_level = original_level
        expected_batch = original_batch
        if stage == "level":
            if self.layout != "levels":
                self.skipTest("multi-level layout only")
            course = self.site.courses[original_id]
            course["levels"].append(("19", "Basic alternate"))
            for batch, *_ in course["batches"]:
                self.site.seats[(original_id, "19", batch)] = 3
            full_course, full_level = original_id, original_level
            full_batch = None
            expected_level = "19"
            priority = "G2 Basic"
        elif stage == "course":
            full_course = "98"
            course = copy.deepcopy(self.site.courses[original_id])
            course["name"] += " Preferred"
            self.site.courses[full_course] = course
            for level, _ in course["levels"]:
                for batch, *_ in course["batches"]:
                    self.site.seats[(full_course, level, batch)] = 3
            full_level = course["levels"][0][0]
            full_batch = None
            priority = {"batch": "G2", "course": "Snow Skiing", "level": "Basic",
                        "courseText": "Preferred"}
        else:
            full_course, full_level, full_batch = original_id, original_level, "99"
            course = self.site.courses[original_id]
            batch = next(item for item in course["batches"] if item[0] == original_batch)
            course["batches"].append((full_batch, batch[1] + " Preferred", *batch[2:]))
            self.site.seats[(full_course, full_level, full_batch)] = 3
            priority = {"batch": "G2", "course": "Snow Skiing", "level": "Basic",
                        "batchText": "Preferred"}

        original_form = mock_site.Handler.form
        original_post = mock_site.Handler.post_form
        event = {"course": "ddlCourse", "level": "ddlCourseLevel", "schedule": "ddlScheduleCourse"}[stage]

        def inject_full(handler, mobile, values, listed, invalid=(), alert=None):
            body = original_form(handler, mobile, values, listed, invalid=invalid, alert=alert)
            matches = (values.get("__EVENTTARGET") == event and values.get("ddlCourse") == full_course
                       and (stage == "course" or values.get("ddlCourseLevel") == full_level)
                       and (stage != "schedule" or values.get("ddlScheduleCourse") == full_batch))
            if matches:
                handler.site.sessions[handler.sid]["poisoned-full"] = True
                body = body.replace("</form>", '<span id="lblCheckError">Seat not available.</span></form>')
            return body

        def reject_poisoned(handler, session, query, values, files):
            if session.get("poisoned-full"):
                return handler.send(500, mock_site.YSOD)
            return original_post(handler, session, query, values, files)

        with mock.patch.object(mock_site.Handler, "form", inject_full), \
                mock.patch.object(mock_site.Handler, "post_form", reject_poisoned):
            instance = self.run_with(PHONES[:1], [priority], timeout=4)
        self.assertTrue(instance.finished())
        self.assertEqual(self.site.applications[0]["course"], expected_course)
        self.assertEqual(self.site.applications[0]["level"], expected_level)
        self.assertEqual(self.site.applications[0]["schedule"], expected_batch)

    def test_full_course_message_restarts_before_later_candidate(self):
        self._book_after_full_message("course")

    def test_full_level_message_restarts_before_later_candidate(self):
        self._book_after_full_message("level")

    def test_full_schedule_message_restarts_before_later_candidate(self):
        self._book_after_full_message("schedule")

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

    def test_latest_final_page_revalidates_selections_dob_seats_and_fee(self):
        original = mock_site.Handler.form
        batch_key = self.key("G2")
        cases = {
            "course": ("ddlCourse", "0"),
            "level": ("ddlCourseLevel", "0"),
            "batch": ("ddlScheduleCourse", "0"),
            "state": ("ddlState", "0"),
            "dob": ("txtDateOfBirth", "01/01/1990"),
            "seats": ("txtAvailableSeat", "0"),
            "fee": ("txtCourseFee", "0"),
            "dob-format": ("txtDateOfBirth", "15-05-1998"),
        }
        for name, (field, replacement) in cases.items():
            with self.subTest(name=name):
                changed_sid = []

                def changed_final_page(handler, mobile, values, listed, invalid=(), alert=None):
                    rendered = dict(values)
                    if values.get("__EVENTTARGET") == "txtDateOfBirth" and not changed_sid:
                        changed_sid.append(handler.sid)
                        rendered[field] = replacement
                    return original(handler, mobile, rendered, listed, invalid=invalid, alert=alert)

                self.site.used.clear()
                self.site.applications.clear()
                self.site.seats[batch_key] = 3
                self.site.request_log.clear()
                self.site.sessions.clear()
                self.site.viewstates.clear()
                out = self.tmp / f"latest-{name}"
                with mock.patch.object(mock_site.Handler, "form", changed_final_page):
                    instance = run_bot(make_config(self.tmp, PHONES[:1], ["G2 Basic"]), self.url, out,
                                       timeout=3, confirmation_handler=lambda *args: None)
                self.assertTrue(instance.finished())
                self.assertEqual(self.site.applications[0]["mobile"], PHONES[0])
                submit = next(item for item in self.site.request_log if item["submit"])
                if name == "dob-format":
                    self.assertEqual(submit["sid"], changed_sid[0])
                else:
                    self.assertNotEqual(submit["sid"], changed_sid[0])

    def test_latest_final_page_revalidates_same_id_meanings(self):
        original = mock_site.Handler.form
        batch_key = self.key("G2")
        cases = {
            "course-kind": ("ddlCourse", "Snow Boarding Course", False),
            "level": ("ddlCourseLevel", "Advance SSC", False),
            "batch-dates": ("ddlScheduleCourse", "Course from 17 Jan 2027 to 30 Jan 2027", False),
            "state": ("ddlState", "KARNATAKA", False),
            "benign-course": ("ddlCourse", "Gulmarg Winter Training", True),
        }
        for name, (field, text, same_session) in cases.items():
            with self.subTest(name=name):
                changed_sid = []

                def rebound_final_page(handler, mobile, values, listed, invalid=(), alert=None):
                    body = original(handler, mobile, values, listed, invalid=invalid, alert=alert)
                    if values.get("__EVENTTARGET") == "txtDateOfBirth" and not changed_sid:
                        changed_sid.append(handler.sid)
                        value = values[field]
                        pattern = rf'(<option selected="selected" value="{re.escape(value)}">)[^<]*(</option>)'
                        body = re.sub(pattern, rf'\1{text}\2', body, count=1)
                    return body

                self.site.used.clear()
                self.site.applications.clear()
                self.site.seats[batch_key] = 3
                self.site.request_log.clear()
                self.site.sessions.clear()
                self.site.viewstates.clear()
                out = self.tmp / f"rebound-{name}"
                with mock.patch.object(mock_site.Handler, "form", rebound_final_page):
                    instance = run_bot(make_config(self.tmp, PHONES[:1], ["G2 Basic"]), self.url, out,
                                       timeout=5, confirmation_handler=lambda *args: None)
                self.assertTrue(instance.finished())
                submit = next(item for item in self.site.request_log if item["submit"])
                self.assertEqual(submit["sid"] == changed_sid[0], same_session)

    def test_disabled_optional_upload_controls_are_omitted(self):
        original = mock_site.Handler.form
        batch_key = self.key("G2")
        cases = (
            ("adult-guardian", ("FupParentGuardianSignature",), None, ("FupParentGuardianSignature",)),
            ("basic-extras", ("FupAnyAdventureCoursePhoto", "FupCurrentEmployment"), None,
             ("FupAnyAdventureCoursePhoto", "FupCurrentEmployment")),
            ("configured-employment", ("FupCurrentEmployment",), ("employmentProof", "sign.jpg"),
             ("FupCurrentEmployment",)),
        )
        for name, disabled, configured, omitted in cases:
            with self.subTest(name=name):
                def optional_disabled(handler, mobile, values, listed, invalid=(), alert=None):
                    body = original(handler, mobile, values, listed, invalid=invalid, alert=alert)
                    if values.get("txtDateOfBirth"):
                        for control in disabled:
                            body = body.replace(f'type="file" name="{control}"',
                                                f'type="file" disabled="disabled" name="{control}"')
                    return body

                self.site.used.clear()
                self.site.applications.clear()
                self.site.seats[batch_key] = 3
                self.site.sessions.clear()
                self.site.viewstates.clear()
                cfg = make_config(self.tmp, PHONES[:1], ["G2 Basic"])
                if configured:
                    cfg["documents"][configured[0]] = configured[1]
                out = self.tmp / f"optional-{name}"
                with mock.patch.object(mock_site.Handler, "form", optional_disabled):
                    instance = run_bot(cfg, self.url, out, timeout=3)
                self.assertTrue(instance.finished())
                for control in omitted:
                    self.assertNotIn(control, self.site.applications[0]["files"])
                if configured:
                    self.assertIn("configured optional documents.employmentProof control is unavailable",
                                  (out / "bot.log").read_text())

    def test_disabled_configured_optional_text_is_omitted(self):
        original = mock_site.Handler.form

        def domicile_disabled(handler, mobile, values, listed, invalid=(), alert=None):
            body = original(handler, mobile, values, listed, invalid=invalid, alert=alert)
            if values.get("txtDateOfBirth"):
                body = body.replace('name="txtDomicile"', 'name="txtDomicile" disabled="disabled"')
            return body

        cfg = make_config(self.tmp, PHONES[:1], ["G2 Basic"], applicant={"domicile": "Maharashtra"})
        out = self.tmp / "optional-domicile"
        with mock.patch.object(mock_site.Handler, "form", domicile_disabled):
            instance = run_bot(cfg, self.url, out, timeout=3)
        self.assertTrue(instance.finished())
        self.assertIsNone(self.site.applications[0]["domicile"])
        self.assertIn("configured optional applicant.domicile control is unavailable", (out / "bot.log").read_text())

    def test_disabled_required_upload_controls_never_submit(self):
        original = mock_site.Handler.form
        batch_key = self.key("G2")
        cases = (("photo", "FupPhototOfApplicant", {}),
                 ("minor-guardian", "FupParentGuardianSignature", {"dateOfBirth": "15/05/2010"}))
        for name, disabled, applicant in cases:
            with self.subTest(name=name):
                def required_disabled(handler, mobile, values, listed, invalid=(), alert=None):
                    body = original(handler, mobile, values, listed, invalid=invalid, alert=alert)
                    if values.get("txtDateOfBirth"):
                        body = body.replace(f'type="file" name="{disabled}"',
                                            f'type="file" disabled="disabled" name="{disabled}"')
                    return body

                self.site.used.clear()
                self.site.applications.clear()
                self.site.request_log.clear()
                self.site.seats[batch_key] = 3
                self.site.sessions.clear()
                self.site.viewstates.clear()
                cfg = make_config(self.tmp, PHONES[:1], ["G2 Basic"], applicant=applicant)
                if name == "minor-guardian":
                    cfg["documents"]["parentSignature"] = "sign.jpg"
                out = self.tmp / f"required-{name}"
                with mock.patch.object(mock_site.Handler, "form", required_disabled):
                    instance = run_bot(cfg, self.url, out, timeout=0.4,
                                       confirmation_handler=lambda *args: None)
                self.assertFalse(any(item["submit"] for item in self.site.request_log))
                self.assertEqual(self.site.applications, [])
                self.assertEqual(instance.pending, {})

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

    def test_mixed_success_and_rejection_evidence_requires_feedback(self):
        cases = {
            "used": (200, "This Mobile No. is already registered."),
            "full": (200, "Seat not available."),
            "closed": (200, "Registration is closed."),
            "validator": (200, None),
            "http-500-full": (500, "Seat not available."),
            "http-429-validator": (429, None),
        }
        for name, (status, negative) in cases.items():
            with self.subTest(name=name):
                feedback = []
                self.site.used.clear()
                self.site.applications.clear()
                self.site.request_log.clear()
                shutil.rmtree(self.out, ignore_errors=True)

                def mixed(handler, mobile, values, files, listed, course, level, batch):
                    success = "Admission Form submitted successfully. Payment link has been sent."
                    values["lblMsg"] = (success + ((" " + negative) if negative else ""), "Green")
                    invalid = ["rfvtxtName"] if "validator" in name else []
                    return handler.send(status, handler.form(mobile, values, listed, invalid=invalid))

                def confirm(phone, target, reason):
                    feedback.append((phone, target.label, reason))
                    return "booked"

                with mock.patch.object(mock_site.Handler, "submit", mixed):
                    instance = self.run_with(PHONES[:2], ["G2 Basic"], confirmation_handler=confirm)
                self.assertEqual(self.booked(instance), [("G2 Basic", PHONES[0], "user-confirmed")])
                self.assertEqual(len(feedback), 1)
                self.assertNotIn(PHONES[1], self.site.used)

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
        self.site.slow_seconds = 0.3
        original = mock_site.Handler.misbehave
        injected = []
        delayed_started = threading.Event()
        feedback_waiting = threading.Event()
        release_delayed = threading.Event()
        forced_phone = []
        forced_feedback = []
        feedback_answers = []
        coordination = []

        def finite_fault_burst(handler):
            if handler.request_submit and not forced_phone:
                forced_phone.append(handler.request_mobile)
                delayed_started.set()
                release_delayed.wait(5)
                raise ConnectionResetError("injected final-request disconnect")
            if handler.site.requests > 40:
                handler.site.chaos = handler.site.session_loss = handler.site.restarts = 0
            result = original(handler)
            if result:
                injected.append(True)
            return result

        def settled_feedback(phone, target, reason):
            is_forced = bool(forced_phone) and phone == forced_phone[0] and not release_delayed.is_set()
            active_before = self.site.active_request_count(phone)
            if is_forced:
                feedback_waiting.set()
            settled = self.site.wait_for_requests(phone, 5)
            answer = self.feedback_from_receipts(phone, target, reason)
            feedback_answers.append((phone, answer))
            if is_forced:
                forced_feedback.append((active_before, settled, answer))
            return answer

        def release_after_feedback_waits():
            ready = delayed_started.wait(5) and feedback_waiting.wait(5)
            coordination.append(ready)
            release_delayed.set()

        coordinator = threading.Thread(target=release_after_feedback_waits, daemon=True)
        coordinator.start()

        try:
            with mock.patch.object(mock_site.Handler, "misbehave", finite_fault_burst):
                instance = self.run_with(PHONES, ["G2 Basic", "G3 Basic"], timeout=15,
                                         bot={"requestTimeoutSec": 0.2, "submitTimeoutSec": 0.2},
                                         confirmation_handler=settled_feedback)
        finally:
            release_delayed.set()
            coordinator.join(1)
        self.assertTrue(injected)
        self.assertEqual(coordination, [True])
        self.assertEqual(len(forced_feedback), 1)
        self.assertGreater(forced_feedback[0][0], 0)
        self.assertEqual(forced_feedback[0][1:], (True, "not-booked"))
        self.assertIn((forced_phone[0], "not-booked"), feedback_answers)
        self.assertTrue(instance.finished())
        self.assertEqual(self.booked(instance), [("G2 Basic", PHONES[0], "confirmed"),
                                                ("G3 Basic", PHONES[1], "confirmed")])
        applications = self.site.snapshot()["applications"]
        self.assertEqual(len(applications), 2)
        self.assertEqual({(app["course"], app["level"], app["schedule"]): app["mobile"]
                          for app in applications},
                         {self.key("G2"): PHONES[0], self.key("G3"): PHONES[1]})
        self.assertEqual(self.site.used, set(PHONES[:2]))
        self.assertNotIn(PHONES[2], self.site.used)

    def test_persistent_outage_keeps_retrying_until_stopped(self):
        def unavailable(handler):
            handler.send(503, mock_site.IIS_503)
            return True
        with mock.patch.object(mock_site.Handler, "misbehave", unavailable):
            instance = self.run_with(PHONES[:2], ["G2 Basic"], timeout=0.6)
        self.assertFalse(instance.finished())
        self.assertTrue(instance.stop.is_set())
        self.assertGreaterEqual(self.site.requests, 3)
        self.assertEqual(instance.failures, [])
        self.assertEqual(instance.pending, {})
        self.assertEqual(self.site.applications, [])
        self.assertNotIn(PHONES[1], self.site.used)

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

    def test_resolved_applicant_choices_match_reasonable_live_variations(self):
        states = [(value, "MAHAR ASTRA" if value == "27" else text)
                  for value, text in mock_site.STATES]
        education = [("Graduate", "Graduate") if value == "Graduation" else (value, text)
                     for value, text in mock_site.EDUCATION]
        diet = [("Non Veg", "Non Veg") if value == "Non-Vegetarian" else (value, text)
                for value, text in mock_site.DIET]
        with mock.patch.object(mock_site, "STATES", states), \
                mock.patch.object(mock_site, "EDUCATION", education), \
                mock.patch.object(mock_site, "DIET", diet):
            instance = self.run_with(PHONES[:1], ["G2 Basic"],
                                     applicant={"education": "grad", "diet": "nonveg"},
                                     address={"houseNo": "1", "street": "MG Road", "district": "Pune",
                                              "state": "Mahar astra", "pincode": "411001"})
        self.assertTrue(instance.finished())
        application = self.site.applications[0]
        self.assertEqual((application["state"], application["education"], application["diet"]),
                         ("27", "Graduate", "Non Veg"))

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

    def test_date_only_generic_match_warns_but_still_attempts(self):
        course_id, _, batch_id = self.key("G2")
        course = dict(self.site.courses[course_id])
        course["name"] = "Generic Adventure Courses"
        course["levels"] = [("16", "None")]
        self.site.courses[course_id] = course
        self.site.seats[(course_id, "16", batch_id)] = 3
        instance = self.run_with(PHONES[:1], ["G2 Basic"])
        self.assertTrue(instance.finished())
        self.assertEqual(self.site.applications[0]["schedule"], batch_id)
        self.assertIn("only the configured dates support this generic course/level match",
                      (self.out / "bot.log").read_text())

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

    def test_current_state_rejects_malformed_fields_and_noncanonical_phones(self):
        self.run_with(PHONES[:2], ["G2 Basic"])
        state_path = self.out / "state.json"
        baseline = json.loads(state_path.read_text())
        settings = bot.parse_settings(make_config(self.tmp, PHONES[:2], ["G2 Basic"]), None)[0]
        formatted = "+91 " + PHONES[0]
        cases = ("missing-target", "batch-type", "status", "timestamp", "message-type",
                 "application-id-type", "url-type", "response-type", "booking-phone",
                 "phone-map-key", "assignment-phone", "phone-record-target",
                 "identity-from-date", "identity-to-date", "booked-pending-target")
        for case in cases:
            with self.subTest(case=case):
                state = json.loads(json.dumps(baseline))
                booking = state["bookings"][0]
                if case == "missing-target":
                    del booking["target"]
                elif case == "batch-type":
                    booking["batch"] = []
                elif case == "status":
                    booking["status"] = "maybe"
                elif case == "timestamp":
                    booking["at"] = "yesterday"
                elif case == "message-type":
                    booking["message"] = []
                elif case == "application-id-type":
                    booking["applicationId"] = 1
                elif case == "url-type":
                    booking["url"] = []
                elif case == "response-type":
                    booking["response"] = {}
                elif case == "booking-phone":
                    booking["phone"] = formatted
                elif case == "phone-map-key":
                    state["bookings"] = []
                    state["phones"] = {formatted: {"status": "used", "note": "used"}}
                elif case == "assignment-phone":
                    state["assignments"]["f" * 64] = formatted
                elif case == "phone-record-target":
                    del state["phones"][PHONES[0]]["target"]
                elif case in ("identity-from-date", "identity-to-date"):
                    field = "from" if case == "identity-from-date" else "to"
                    booking["targetIdentity"][field] = booking["targetIdentity"][field].replace("-", "")
                    changed_key = bot.target_key_from_identity(booking["targetIdentity"])
                    booking["targetKey"] = changed_key
                    state["phones"][PHONES[0]]["targetKey"] = changed_key
                elif case == "booked-pending-target":
                    state["phones"][PHONES[1]] = {
                        "status": "submitting", "targetKey": booking["targetKey"],
                        "target": booking["target"], "at": booking["at"],
                    }
                    state["assignments"][booking["targetKey"]] = PHONES[1]
                state_path.write_text(json.dumps(state))
                preserved = state_path.read_bytes()
                requests = self.site.requests
                with self.assertRaises(bot.ConfigError):
                    bot.Bot(settings, self.out, base_url=self.url, log=bot.Log(echo=False))
                self.assertEqual(state_path.read_bytes(), preserved)
                self.assertEqual(self.site.requests, requests)
        state_path.write_text(json.dumps(baseline))

    def test_reversed_persisted_identity_is_preserved_and_rejected(self):
        self.run_with(PHONES[:1], ["G2 Basic"])
        state_path = self.out / "state.json"
        state = json.loads(state_path.read_text())
        identity = state["bookings"][0]["targetIdentity"]
        identity["from"], identity["to"] = identity["to"], identity["from"]
        changed_key = bot.target_key_from_identity(identity)
        state["bookings"][0]["targetKey"] = changed_key
        state["phones"][PHONES[0]]["targetKey"] = changed_key
        state_path.write_text(json.dumps(state))
        preserved = state_path.read_bytes()
        requests = self.site.requests
        settings = bot.parse_settings(make_config(self.tmp, PHONES[:1], ["G2 Basic"]), None)[0]
        with self.assertRaises(bot.ConfigError):
            bot.Bot(settings, self.out, base_url=self.url, log=bot.Log(echo=False))
        self.assertEqual(state_path.read_bytes(), preserved)
        self.assertEqual(self.site.requests, requests)

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

    def test_check_uses_booking_identity_without_writing_or_contacting_site(self):
        config_path = self.tmp / "identity-check.json"
        cfg = make_config(self.tmp, PHONES[:1], ["G2 Basic"])
        config_path.write_text(json.dumps(cfg))
        settings = bot.parse_settings(cfg, config_path)[0]
        run_identity = json.dumps([str(config_path.resolve()), bot.origin(self.url), settings.season])
        digest = hashlib.sha256(run_identity.encode()).hexdigest()[:16]
        out_dir = bot.SCRIPT_DIR / "runs" / f"{config_path.stem}-{digest}"
        shutil.rmtree(out_dir, ignore_errors=True)
        try:
            progress = bot.State(out_dir / "state.json", True)
            progress.bind(bot.expected_state_identity(settings, self.url))
            progress.acquire()
            try:
                progress.save()
            finally:
                progress.release()
            preserved = progress.path.read_bytes()
            cfg["applicant"]["name"] = "Changed Applicant"
            config_path.write_text(json.dumps(cfg))
            requests = self.site.requests
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = bot.main(["--check", "--config", str(config_path), "--base-url", self.url])
            self.assertEqual(result, 1)
            self.assertIn("different applicant, season or site", output.getvalue())
            self.assertNotIn("All good", output.getvalue())
            self.assertEqual(self.site.requests, requests)
            self.assertEqual(progress.path.read_bytes(), preserved)
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

    def test_old_booking_does_not_complete_a_changed_date(self):
        self.run_with(PHONES[:1], ["G2 Basic"])
        changed = {"batch": "G2", "from": "2028-01-03", "to": "2028-01-16", "label": "G2 Basic"}
        instance = self.run_with(PHONES[:1], [changed], timeout=0.3)
        self.assertFalse(instance.finished())
        self.assertEqual(len(instance.bookings), 1)
        self.assertEqual(instance.bookings[0]["phone"], PHONES[0])
        self.assertEqual(instance.phone_state[PHONES[0]], "booked")
        self.assertEqual(len(self.site.applications), 1)

    def test_missing_or_empty_required_controls_never_start_final_submission(self):
        original = mock_site.Handler.form
        cases = ("ddlState", "ddlState-empty", "ddlEduationQualification",
                 "ddlVegetarianOrNon", "btnSubmit")
        for control in cases:
            with self.subTest(control=control):
                def incomplete_form(handler, mobile, values, listed, invalid=(), alert=None):
                    body = original(handler, mobile, values, listed, invalid=invalid, alert=alert)
                    if control == "btnSubmit":
                        return re.sub(r'<input\b(?=[^>]*\bid="btnSubmit")[^>]*>\s*', "", body,
                                      count=1, flags=re.I)
                    name = control[:-6] if control.endswith("-empty") else control
                    pattern = rf'(<select\b[^>]*\bid="{name}"[^>]*>).*?(</select>\s*)'
                    if control.endswith("-empty"):
                        return re.sub(pattern, r'\1<option value="0">Select</option>\2', body,
                                      count=1, flags=re.I | re.S)
                    return re.sub(pattern, "", body, count=1, flags=re.I | re.S)

                start = len(self.site.request_log)
                out = self.tmp / f"missing-{control}"
                with mock.patch.object(mock_site.Handler, "form", incomplete_form):
                    instance = run_bot(make_config(self.tmp, PHONES[:1], ["G2 Basic"]), self.url, out,
                                       timeout=0.4, confirmation_handler=lambda *args: None)
                requests = self.site.request_log[start:]
                self.assertFalse(any(item["method"] == "POST" and item["page"] == "admission_form.aspx"
                                     and not item["event"] for item in requests))
                self.assertEqual(instance.pending, {})
                self.assertEqual(instance.failures, [])

    def test_empty_or_reset_required_hidden_values_never_start_final_submission(self):
        original = mock_site.Handler.form
        cases = (
            ("viewstate", "__VIEWSTATE", "   "),
            ("viewstate-generator", "__VIEWSTATEGENERATOR", "   "),
            ("event-validation", "__EVENTVALIDATION", "   "),
            ("capacity-type", "hdfTypeOfCapacitySheet", "   "),
            ("capacity-allocation-empty", "hdfCapacitySheetAllocationId", "   "),
            ("capacity-allocation-zero", "hdfCapacitySheetAllocationId", "0"),
        )
        for dry_run in (False, True):
            for label, name, replacement in cases:
                with self.subTest(dry_run=dry_run, field=label):
                    def stale_hidden(handler, mobile, values, listed, invalid=(), alert=None):
                        body = original(handler, mobile, values, listed, invalid=invalid, alert=alert)
                        if values.get("__EVENTTARGET") != "txtDateOfBirth":
                            return body
                        pattern = rf'(<input\b[^>]*\bname="{re.escape(name)}"[^>]*\bvalue=")[^"]*(")'
                        return re.sub(pattern, rf'\g<1>{replacement}\2', body, count=1, flags=re.I)

                    start = len(self.site.request_log)
                    out = self.tmp / f"hidden-{dry_run}-{label}"
                    with mock.patch.object(mock_site.Handler, "form", stale_hidden):
                        instance = run_bot(make_config(self.tmp, PHONES[:1], ["G2 Basic"]), self.url, out,
                                           dry_run=dry_run, timeout=0.3,
                                           confirmation_handler=lambda *args: None)
                    requests = self.site.request_log[start:]
                    self.assertFalse(any(item["submit"] for item in requests))
                    self.assertEqual(instance.pending, {})
                    self.assertEqual(instance.bookings, [])
                    self.assertEqual(instance.failures, [])

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

    def test_dry_run_requires_a_ready_final_form(self):
        original = mock_site.Handler.form
        batch_key = self.key("G2")
        cases = ("submit", "course-meaning", "dob", "seats", "fee", "photo", "minor-guardian")
        for case in cases:
            with self.subTest(case=case):
                def unready_form(handler, mobile, values, listed, invalid=(), alert=None):
                    rendered = dict(values)
                    if values.get("__EVENTTARGET") == "txtDateOfBirth":
                        if case == "dob":
                            rendered["txtDateOfBirth"] = "01/01/1990"
                        elif case == "seats":
                            rendered["txtAvailableSeat"] = "0"
                        elif case == "fee":
                            rendered["txtCourseFee"] = "0"
                    body = original(handler, mobile, rendered, listed, invalid=invalid, alert=alert)
                    if values.get("__EVENTTARGET") != "txtDateOfBirth":
                        return body
                    if case == "submit":
                        return re.sub(r'<input\b(?=[^>]*\bid="btnSubmit")[^>]*>\s*', "", body,
                                      count=1, flags=re.I)
                    if case == "course-meaning":
                        value = values["ddlCourse"]
                        pattern = rf'(<option selected="selected" value="{re.escape(value)}">)[^<]*(</option>)'
                        return re.sub(pattern, r'\1Snow Boarding Course\2', body, count=1)
                    control = {"photo": "FupPhototOfApplicant",
                               "minor-guardian": "FupParentGuardianSignature"}.get(case)
                    if control:
                        return body.replace(f'type="file" name="{control}"',
                                            f'type="file" disabled="disabled" name="{control}"')
                    return body

                self.site.used.clear()
                self.site.applications.clear()
                self.site.request_log.clear()
                self.site.seats[batch_key] = 3
                self.site.sessions.clear()
                self.site.viewstates.clear()
                applicant = {"dateOfBirth": "15/05/2010"} if case == "minor-guardian" else {}
                cfg = make_config(self.tmp, PHONES[:1], ["G2 Basic"], applicant=applicant)
                if case == "minor-guardian":
                    cfg["documents"]["parentSignature"] = "sign.jpg"
                out = self.tmp / f"dry-unready-{case}"
                with mock.patch.object(mock_site.Handler, "form", unready_form):
                    instance = run_bot(cfg, self.url, out, dry_run=True, timeout=0.4)
                self.assertEqual(instance.bookings, [])
                self.assertEqual(self.site.applications, [])
                self.assertFalse((out / "state.json").exists())

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

    def test_omitted_configured_guardian_retries_with_real_file_when_control_returns(self):
        self.site.parent_signature_required = True
        cfg = make_config(self.tmp, PHONES[:1], ["G2 Basic"])
        guardian = self.tmp / "docs" / "guardian.jpg"
        content = b"\xff\xd8\xffconfigured adult guardian"
        guardian.write_bytes(content)
        cfg["documents"]["parentSignature"] = guardian.name
        original = mock_site.Handler.form
        omitted = []

        def first_guardian_control_missing(handler, mobile, values, listed, invalid=(), alert=None):
            body = original(handler, mobile, values, listed, invalid=invalid, alert=alert)
            if values.get("__EVENTTARGET") == "txtDateOfBirth" and not omitted:
                omitted.append(handler.sid)
                body = body.replace('type="file" name="FupParentGuardianSignature"',
                                    'type="file" disabled="disabled" name="FupParentGuardianSignature"')
            return body

        with mock.patch.object(mock_site.Handler, "form", first_guardian_control_missing):
            instance = run_bot(cfg, self.url, self.out, timeout=4)
        self.assertTrue(instance.finished())
        self.assertFalse(instance.parent_fallback)
        self.assertEqual(self.site.applications[0]["fileHashes"]["FupParentGuardianSignature"],
                         hashlib.sha256(content).hexdigest())
        submits = [item for item in self.site.request_log if item["submit"]]
        self.assertEqual(len(submits), 2)
        self.assertEqual(submits[0]["sid"], omitted[0])
        self.assertNotEqual(submits[1]["sid"], omitted[0])

    def test_adult_signature_is_not_sent_in_guardian_slot_unnecessarily(self):
        instance = self.run_with(PHONES[:1], ["G2 Basic"])
        self.assertTrue(instance.finished())
        self.assertNotIn("FupParentGuardianSignature", self.site.applications[0]["files"])


class EndToEndOtherLayout(EndToEnd):
    layout = "courses"


class EndToEndLevelCourses(EndToEnd):
    layout = "level-courses"

    def test_missing_nonski_course_never_falls_back_to_basic_skiing(self):
        for label, kind in (("G4 Snowboarding", "board"), ("G5 MOI", "moi")):
            with self.subTest(label=label):
                course_id = next(value for value, course in self.site.courses.items() if course["kind"] == kind)
                course = self.site.courses.pop(course_id)
                try:
                    self.site.used.clear()
                    self.site.applications.clear()
                    self.site.sessions.clear()
                    self.site.viewstates.clear()
                    instance = run_bot(make_config(self.tmp, PHONES[:1], [label]), self.url,
                                       self.tmp / ("missing-" + kind), timeout=0.4)
                    self.assertFalse(instance.finished())
                    self.assertEqual(self.site.applications, [])
                    self.assertNotIn(PHONES[0], self.site.used)
                finally:
                    self.site.courses[course_id] = course


if __name__ == "__main__":
    unittest.main()
