"""Failure and boundary tests through runtime interfaces and real local HTTP."""
import json
import hashlib
import os
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from test_bot import FAST, PHONES, bot, make_config, offline_bot, run_bot
import mock_site


class Resilience(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="iism-resilience-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def local_site(self, handler=mock_site.Handler):
        site = mock_site.Site()
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.site, server.daemon_threads = site, True
        server.handle_error = lambda *args: None
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return site, f"http://127.0.0.1:{server.server_port}"

    def test_duplicate_identity_or_phone_fails_before_booking(self):
        cfg = make_config(self.root, PHONES[:2], ["G2 Basic", {"batch": "G2", "label": "duplicate"}])
        self.assertTrue(any("duplicates" in e for e in bot.parse_settings(cfg, None)[1]))
        cfg["coursePriorities"] = ["G2 Basic", "G3 Basic"]
        cfg["phoneNumbers"] = [PHONES[0], "+91 " + PHONES[0]]
        self.assertTrue(any("listed twice" in e for e in bot.parse_settings(cfg, None)[1]))

    def test_bad_nested_values_are_collected_without_network(self):
        cfg = make_config(self.root, PHONES[:1], ["G2 Basic"])
        cfg["applicant"]["name"] = ["Alice"]
        cfg["address"]["district"] = {"city": "Pune"}
        cfg["documents"]["photographTypo"] = "unused.png"
        with self.assertRaises(bot.ConfigError) as caught:
            bot.parse_settings(cfg, None)
        for field in ("applicant.name", "address.district", "documents.photographTypo"):
            self.assertIn(field, str(caught.exception))

    def test_invalid_dates_phone_and_field_lengths_fail_preflight(self):
        cfg = make_config(self.root, PHONES[:1], ["G2 Basic"],
                          applicant={"dateOfBirth": "31/02/2000", "name": "a" * 51,
                                     "emergencyContactPhone": "123"},
                          address={"houseNo": "1", "street": "Road", "district": "Pune",
                                   "state": "Maharashtra", "pincode": "not-a-pin"})
        errors = bot.parse_settings(cfg, None)[1]
        self.assertTrue(any("dateOfBirth" in e for e in errors))
        self.assertTrue(any("txtName" in e for e in errors))
        self.assertTrue(any("emergencyContactPhone" in e for e in errors))
        self.assertTrue(any("pincode" in e for e in errors))

    def test_max_bookings_is_obsolete_instead_of_silently_limiting_preferences(self):
        cfg = make_config(self.root, PHONES[:2], ["G2 Basic", "G3 Basic"], maxBookings=1)
        with self.assertRaisesRegex(bot.ConfigError, "maxBookings was removed"):
            bot.parse_settings(cfg, None)

    def test_intermediate_is_only_a_warning(self):
        cfg = make_config(self.root, PHONES[:1], ["G2 Intermediate"])
        settings, errors, warnings = bot.parse_settings(cfg, None)
        self.assertEqual(errors, [])
        self.assertTrue(any("not supported" in warning for warning in warnings))
        self.assertEqual(bot.prepare_documents(settings, self.root / "out")[0], [])

    def test_oversized_and_mislabelled_images_are_reported(self):
        cfg = make_config(self.root, PHONES[:1], ["G2 Basic"])
        settings = bot.parse_settings(cfg, None)[0]
        settings.doc_sources["photo"].write_bytes(b"\xff\xd8\xff" + b"x" * bot.MAX_UPLOAD_BYTES)
        settings.doc_sources["signature"].write_bytes(b"not an image")
        with mock.patch.object(bot, "can_convert", return_value=False):
            problems, _ = bot.prepare_documents(settings, self.root / "out")
        self.assertTrue(any("photo" in p and "500" in p for p in problems))
        self.assertTrue(any("signature" in p and "content" in p for p in problems))

    def test_prepared_upload_snapshot_is_independent_of_later_file_changes(self):
        cfg = make_config(self.root, PHONES[:1], ["G2 Basic"])
        settings = bot.parse_settings(cfg, None)[0]
        self.assertEqual(bot.prepare_documents(settings, self.root / "out")[0], [])
        expected = settings.docs["photo"].content
        settings.doc_sources["photo"].write_bytes(b"\xff\xd8\xffchanged source")
        payload = bot.Multipart()
        payload.file("FupPhototOfApplicant", settings.docs["photo"])
        _, files = mock_site.parse_multipart(payload.body(), payload.content_type())
        self.assertEqual(files["FupPhototOfApplicant"][1], expected)

    def test_corrupt_progress_is_preserved_and_rejected(self):
        path = self.root / "state.json"
        path.write_text("{broken")
        with self.assertRaises(bot.ConfigError):
            bot.State(path, True)
        self.assertEqual(path.read_text(), "{broken")

    def test_target_identity_is_canonical_for_harmless_casing(self):
        batches = bot.load_season({}, self.root)[0]
        normal = bot.parse_priority({"batch": "G2", "course": "Snow Skiing", "level": "Basic",
                                     "location": "Gulmarg"}, 0, batches)
        changed_case = bot.parse_priority({"batch": "g2", "course": "snow skiing", "level": "basic",
                                           "location": "  gULMARG  "}, 0, batches)
        self.assertEqual(bot.target_key(normal), bot.target_key(changed_case))

    def test_safe_assignments_follow_current_order_and_stale_ones_are_pruned(self):
        out = self.root / "reordered-state"
        first_settings = bot.parse_settings(
            make_config(self.root, PHONES, ["G2 Basic", "G3 Basic"]), None)[0]
        first = bot.Bot(first_settings, out, base_url="http://127.0.0.1:9", log=bot.Log(echo=False))
        first.state.acquire()
        try:
            first._restore()
            first.state.data["assignments"]["removed-target"] = PHONES[2]
            first.state.save()
        finally:
            first.state.release()

        reordered_settings = bot.parse_settings(
            make_config(self.root, PHONES, ["G3 Basic", "G2 Basic"]), None)[0]
        reordered = bot.Bot(reordered_settings, out, base_url="http://127.0.0.1:9", log=bot.Log(echo=False))
        reordered.state.acquire()
        try:
            reordered._restore()
            expected = {bot.target_key(reordered.targets[0]): PHONES[0],
                        bot.target_key(reordered.targets[1]): PHONES[1]}
            self.assertEqual(reordered.assignments, expected)
            self.assertEqual(reordered.state.data["assignments"], expected)
        finally:
            reordered.state.release()

    def test_duplicate_assignment_owners_and_reordered_pending_state_fail_closed(self):
        duplicate = self.root / "duplicate-state.json"
        duplicate.write_text(json.dumps({"version": 1, "identity": "test", "bookings": [], "phones": {},
                                         "assignments": {"first": PHONES[0], "second": PHONES[0]}}))
        with self.assertRaisesRegex(bot.ConfigError, "assigned to multiple"):
            bot.State(duplicate, True)

        out = self.root / "pending-state"
        settings = bot.parse_settings(make_config(self.root, PHONES[:2], ["G2 Basic", "G3 Basic"]), None)[0]
        first = bot.Bot(settings, out, base_url="http://127.0.0.1:9", log=bot.Log(echo=False))
        first.state.acquire()
        try:
            first._restore()
            phone = first.assign_phone(first.targets[0])
            first.mark_submitting(phone, first.targets[0])
        finally:
            first.state.release()

        reordered_settings = bot.parse_settings(
            make_config(self.root, PHONES[:2], ["G3 Basic", "G2 Basic"]), None)[0]
        with self.assertRaisesRegex(bot.ConfigError, "current preference/phone order"):
            bot.Bot(reordered_settings, out, base_url="http://127.0.0.1:9", log=bot.Log(echo=False))

    def test_explicit_guardian_file_is_used_for_minor_and_adult(self):
        site, url = self.local_site()
        for index, born in enumerate(("15/05/2010", "15/05/1998")):
            cfg = make_config(self.root, PHONES[index:index + 1], ["G2 Basic"],
                              applicant={"dateOfBirth": born})
            content = b"\xff\xd8\xffconfigured guardian signature " + str(index).encode()
            (self.root / "docs" / "guardian.jpg").write_bytes(content)
            cfg["documents"]["parentSignature"] = "guardian.jpg"
            instance = run_bot(cfg, url, self.root / f"run-{index}", timeout=4)
            self.assertTrue(instance.finished())
            self.assertEqual(site.applications[-1]["fileHashes"]["FupParentGuardianSignature"],
                             hashlib.sha256(content).hexdigest())

    def test_parallel_submissions_share_the_startup_snapshot(self):
        site, url = self.local_site()
        cfg = make_config(self.root, PHONES[:2], ["G2 Basic", "G3 Basic"])
        original_bytes = (self.root / "docs" / "photo.jpg").read_bytes()
        cascade = bot.Session.cascade
        def mutate_file(session, target):
            (self.root / "docs" / "photo.jpg").write_bytes(b"\xff\xd8\xfflater content")
            return cascade(session, target)
        with mock.patch.object(bot.Session, "cascade", mutate_file):
            instance = run_bot(cfg, url, self.root / "out", timeout=4)
        self.assertTrue(instance.finished())
        self.assertEqual([a["fileHashes"]["FupPhototOfApplicant"] for a in site.applications],
                         [hashlib.sha256(original_bytes).hexdigest()] * 2)

    def test_process_lock_cannot_be_taken_twice(self):
        first = bot.State(self.root / "state.json", True)
        second = bot.State(self.root / "state.json", True)
        first.bind("test-identity")
        second.bind("test-identity")
        try:
            first.acquire()
            with self.assertRaises(bot.ConfigError):
                second.acquire()
        finally:
            first.release()
        second.acquire()
        second.save()
        second.release()

    def test_disk_failure_before_submit_prevents_application(self):
        site, url = self.local_site()
        cfg = make_config(self.root, PHONES[:1], ["G2 Basic"])
        original = bot.State.save
        def fail_pending(state):
            if any(value["status"] == "submitting" for value in state.data["phones"].values()):
                raise bot.ConfigError("simulated disk-full failure before final request")
            return original(state)
        with mock.patch.object(bot.State, "save", fail_pending):
            instance = run_bot(cfg, url, self.root / "out", timeout=4)
        self.assertEqual(site.applications, [])
        self.assertTrue(instance.failures)

    def test_retry_after_delta_and_date_are_capped_and_cancel_aware(self):
        instance = offline_bot()
        for value in ("120", "Wed, 12 Nov 2036 10:00:00 GMT"):
            page = bot.Page("http://127.0.0.1:9/", 429, "busy", headers={"Retry-After": value})
            error = bot.Transient("HTTP 429", bot.response_retry_after(page))
            self.assertEqual(instance.retry_delay(2000, error), 10)
        instance.stop.set()
        started = time.monotonic()
        instance.sleep(10)
        self.assertLess(time.monotonic() - started, 0.1)

    def test_429_over_real_http_recovers_without_replacing_phone(self):
        class Throttled(mock_site.Handler):
            def misbehave(self):
                if self.site.throttled:
                    self.site.throttled = False
                    return self.send(429, "too many requests", {"Retry-After": "1"}) or True
                return False
        site, url = self.local_site(Throttled)
        site.throttled = True
        instance = run_bot(make_config(self.root, PHONES[:2], ["G2 Basic"]), url, self.root / "out", timeout=5)
        self.assertTrue(instance.finished())
        self.assertEqual(site.applications[0]["mobile"], PHONES[0])

    def test_foreign_form_destination_is_blocked_before_sending(self):
        instance = offline_bot()
        session = bot.Session(instance, PHONES[0], "")
        page = bot.Page("http://127.0.0.1:9/admission_form.aspx", 200,
                        '<form action="http://127.0.0.1:8/collect"><input name="__VIEWSTATE" value="a"></form>')
        with self.assertRaises(bot.ConfigError):
            session.postback(page, "", {"txtName": "Test"})

    def test_foreign_redirect_never_contacts_receiver(self):
        received = []
        class Receiver(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.path)
                self.send_response(200)
                self.end_headers()
            def log_message(self, *args):
                pass
        receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        threading.Thread(target=receiver.serve_forever, daemon=True).start()
        self.addCleanup(receiver.server_close)
        self.addCleanup(receiver.shutdown)
        class Redirect(Receiver):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{receiver.server_port}/collect")
                self.end_headers()
        sender = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        threading.Thread(target=sender.serve_forever, daemon=True).start()
        self.addCleanup(sender.server_close)
        self.addCleanup(sender.shutdown)
        http = bot.Http(f"http://127.0.0.1:{sender.server_port}", 2, bot.Log(echo=False))
        with self.assertRaises(bot.ConfigError):
            http.get("/")
        self.assertEqual(received, [])

    def test_foreign_receipt_redirect_is_uncertain_and_does_not_stop_siblings(self):
        received = []
        class Receiver(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.path)
                self.send_response(200)
                self.end_headers()
            def log_message(self, *args):
                pass
        receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        threading.Thread(target=receiver.serve_forever, daemon=True).start()
        self.addCleanup(receiver.server_close)
        self.addCleanup(receiver.shutdown)

        site, url = self.local_site()
        site.receipt_redirects[PHONES[0]] = f"http://127.0.0.1:{receiver.server_port}/collect"
        feedback = lambda phone, target, reason: (
            "booked" if any(item["mobile"] == phone for item in site.snapshot()["applications"]) else "not-booked")
        instance = run_bot(make_config(self.root, PHONES[:2], ["G2 Basic", "G3 Basic"]),
                           url, self.root / "foreign-receipt", timeout=5, confirmation_handler=feedback)
        self.assertTrue(instance.finished())
        self.assertEqual(len(site.applications), 2)
        self.assertEqual(received, [])
        self.assertEqual(instance.failures, [])

    def test_tls_failure_on_redirected_receipt_never_replays_original_post(self):
        client = bot.Http("https://example.test", 1, bot.Log(echo=False))
        calls = []

        class ReceiptTLSFailure:
            def open(self, request, timeout):
                calls.append((request.get_method(), request.full_url))
                client.redirect_handler.history.append(
                    (302, "POST", request.full_url, "https://example.test/receipt"))
                raise urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed"))

        client.opener = ReceiptTLSFailure()
        with self.assertRaises(bot.RedirectFailure):
            client.send("POST", "https://example.test/admission_form.aspx", b"application")
        self.assertEqual(calls, [("POST", "https://example.test/admission_form.aspx")])
        self.assertFalse(client.verified)

        instance = offline_bot()
        instance.base_url = "https://example.test"
        session = bot.Session(instance, PHONES[0], "")
        session.http = client
        old_jar = client.jar
        session.restart()
        self.assertFalse(session.http.verified)
        self.assertIsNot(session.http.jar, old_jar)

    def test_trickled_response_body_obeys_total_request_deadline(self):
        class Trickle(BaseHTTPRequestHandler):
            def do_GET(self):
                data = b"x" * 100
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    for value in data:
                        self.wfile.write(bytes([value]))
                        self.wfile.flush()
                        time.sleep(0.03)
                except OSError:
                    pass
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Trickle)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = bot.Http(f"http://127.0.0.1:{server.server_port}", 0.2, bot.Log(echo=False))
        started = time.monotonic()
        with self.assertRaises(bot.Transient):
            client.get("/")
        self.assertLess(time.monotonic() - started, 0.8)

    def test_error_or_negative_success_page_is_not_a_confirmation(self):
        instance = offline_bot()
        session = bot.Session(instance, PHONES[0], "")
        for path, status, body in (
                ("payment-error.aspx", 503, "payment failed"),
                ("Unsuccessful.aspx", 200, "application failed"),
                ("AdmissionFormSuccess.aspx", 200, "Application not successfully submitted")):
            with self.subTest(path=path):
                page = bot.Page(f"http://127.0.0.1:9/{path}", status, body)
                with self.assertRaises(bot.Transient):
                    session.judge_submit(page, instance.targets[0], None, None)

    def test_explicit_historical_success_does_not_require_an_id(self):
        instance = offline_bot()
        session = bot.Session(instance, PHONES[0], "")
        page = bot.Page("http://127.0.0.1:9/AdmissionFormSuccess.aspx", 200,
                        '<form><input name="__VIEWSTATE" value="a"><span id="lblMsg">'
                        'Admission Form submitted successfully, Payment details have been sent on your registered email.'
                        '</span></form>')
        result = session.judge_submit(page, instance.targets[0], None, None)
        self.assertEqual(result["applicationId"], "")
        self.assertIn("submitted successfully", result["message"])

    def test_cancelled_http_client_starts_no_request(self):
        event = threading.Event()
        event.set()
        client = bot.Http("http://127.0.0.1:9", 2, bot.Log(echo=False), event)
        with self.assertRaises(bot.Cancelled):
            client.get("/")

    @unittest.skipIf(os.name == "nt", "POSIX signal delivery rehearsal")
    def test_partial_cli_run_exits_unsuccessfully_after_interrupt(self):
        site, url = self.local_site()
        site.used.add(PHONES[0])
        cfg = make_config(self.root, PHONES[:2], ["G2 Basic", "G3 Basic"])
        path = self.root / "partial-run.json"
        path.write_text(json.dumps(cfg))
        process = subprocess.Popen([sys.executable, str(Path(bot.__file__)), "--config", str(path),
                                    "--base-url", url, "--now"], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            deadline = time.monotonic() + 5
            while not site.applications and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(len(site.applications), 1)
            process.send_signal(signal.SIGINT)
            output = process.communicate(timeout=5)[0]
            self.assertEqual(process.returncode, 1, output)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


if __name__ == "__main__":
    unittest.main()
