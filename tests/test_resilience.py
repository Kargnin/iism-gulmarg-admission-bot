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

    @unittest.skipIf(os.name == "nt", "POSIX permission contract")
    def test_generated_private_artifacts_do_not_change_source_document_modes(self):
        site, url = self.local_site()
        cfg = make_config(self.root, PHONES[:1], ["G2 Basic"])
        sources = list((self.root / "docs").iterdir())
        for source in sources:
            source.chmod(0o644)
        previous_umask = os.umask(0o022)
        try:
            out = self.root / "private-run"
            instance = run_bot(cfg, url, out, timeout=4)
        finally:
            os.umask(previous_umask)
        self.assertTrue(instance.finished())
        self.assertEqual(out.stat().st_mode & 0o777, 0o700)
        self.assertEqual((out / "bot.log").stat().st_mode & 0o777, 0o600)
        self.assertEqual((out / "state.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((out / "state.lock").stat().st_mode & 0o777, 0o600)
        self.assertEqual((out / "responses").stat().st_mode & 0o777, 0o700)
        for response in (out / "responses").iterdir():
            self.assertEqual(response.stat().st_mode & 0o777, 0o600)
        self.assertTrue(all(source.stat().st_mode & 0o777 == 0o644 for source in sources))

        settings = bot.parse_settings(cfg, None)[0]
        source = self.root / "docs" / "convert.pdf"
        source.write_bytes(b"source document")
        source.chmod(0o644)
        settings.doc_sources["photo"] = source

        def converted(src, dst):
            dst.write_bytes(b"\xff\xd8\xff" + b"converted")
            return dst

        previous_umask = os.umask(0o022)
        try:
            with mock.patch.object(bot, "can_convert", return_value=True), \
                    mock.patch.object(bot, "convert_to_jpeg", side_effect=converted):
                problems, _ = bot.prepare_documents(settings, self.root / "converted-run")
        finally:
            os.umask(previous_umask)
        self.assertEqual(problems, [])
        converted_path = next((self.root / "converted-run" / "upload-ready").glob("photo-*.jpg"))
        self.assertEqual((self.root / "converted-run").stat().st_mode & 0o777, 0o700)
        self.assertEqual(converted_path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(converted_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(source.stat().st_mode & 0o777, 0o644)

    def test_corrupt_progress_is_preserved_and_rejected(self):
        path = self.root / "state.json"
        path.write_text("{broken")
        with self.assertRaises(bot.ConfigError):
            bot.State(path, True)
        self.assertEqual(path.read_text(), "{broken")

    def test_duplicate_json_keys_fail_closed_without_changing_progress(self):
        config = self.root / "duplicate.jsonc"
        config.write_text('{"applicant": {"name": "First", "name": "Second"}}')
        with self.assertRaisesRegex(bot.ConfigError, "duplicate JSON object key 'name'"):
            bot.load_jsonc(config)

        state = self.root / "duplicate-state.json"
        state.write_text(
            '{"version":2,"targetKeyVersion":1,"identity":"test","bookings":[],'
            '"phones":{"9000000011":{"status":"used","note":"used"}},"phones":{},"assignments":{}}')
        preserved = state.read_bytes()
        with self.assertRaisesRegex(bot.ConfigError, "duplicate JSON object key 'phones'"):
            bot.State(state, True)
        self.assertEqual(state.read_bytes(), preserved)

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
            first.state.data["assignments"]["f" * 64] = PHONES[2]
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
        duplicate.write_text(json.dumps({"version": bot.STATE_VERSION,
                                         "targetKeyVersion": bot.TARGET_KEY_VERSION,
                                         "identity": "test", "bookings": [], "phones": {},
                                         "assignments": {"a" * 64: PHONES[0], "b" * 64: PHONES[0]}}))
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

    def test_mark_submitting_updates_phone_status_only_after_durable_save(self):
        settings = bot.parse_settings(make_config(self.root, PHONES[:1], ["G2 Basic"]), None)[0]
        instance = bot.Bot(settings, self.root / "mark-state", base_url="http://127.0.0.1:9",
                           log=bot.Log(echo=False))
        instance.state.acquire()
        try:
            instance._restore()
            phone = instance.assign_phone(instance.targets[0])
            with mock.patch.object(instance.state, "save", side_effect=bot.ConfigError("disk full")):
                with self.assertRaises(bot.ConfigError):
                    instance.mark_submitting(phone, instance.targets[0])
            self.assertEqual(instance.phone_state[phone], "free")
            self.assertNotIn(phone, instance.pending)
            self.assertNotIn(phone, instance.state.data["phones"])
            instance.mark_submitting(phone, instance.targets[0])
            self.assertEqual(instance.phone_state[phone], "submitting")
            self.assertEqual(instance.pending[phone], bot.target_key(instance.targets[0]))
        finally:
            instance.state.release()

    def test_unexpected_worker_failure_stops_polling_siblings(self):
        settings = bot.parse_settings(
            make_config(self.root, PHONES[:2], ["G2 Basic", "G3 Basic"]), None)[0]
        instance = bot.Bot(settings, self.root / "fatal-worker", base_url="http://127.0.0.1:9",
                           dry_run=True, log=bot.Log(echo=False))
        sibling_started = threading.Event()
        guard_fired = threading.Event()

        def fail_or_poll(session, target):
            if target.index == 1:
                sibling_started.set()
                raise bot.TargetFull("still unavailable")
            sibling_started.wait(1)
            raise RuntimeError("unexpected worker bug")

        guard = threading.Timer(1, lambda: (guard_fired.set(), instance.stop.set()))
        guard.start()
        try:
            with mock.patch.object(bot.Session, "book", fail_or_poll):
                instance.run()
        finally:
            guard.cancel()
        self.assertFalse(guard_fired.is_set())
        self.assertTrue(instance.stop.is_set())
        self.assertFalse(instance.finished())
        self.assertTrue(any("unexpected worker failure" in failure for failure in instance.failures))

    def test_blocking_feedback_cannot_hide_fatal_worker_failure(self):
        settings = bot.parse_settings(
            make_config(self.root, PHONES[:2], ["G2 Basic", "G3 Basic"]), None)[0]
        instance = bot.Bot(settings, self.root / "blocking-feedback", base_url="http://127.0.0.1:9",
                           log=bot.Log(echo=False))
        handler_started = threading.Event()
        handler_release = threading.Event()
        handler_finished = threading.Event()
        guard_fired = threading.Event()

        def blocking_feedback(phone, target, reason):
            handler_started.set()
            handler_release.wait(2)
            handler_finished.set()
            return "not-booked"

        instance.confirmation_handler = blocking_feedback

        def uncertain_or_fatal(session, target):
            if target.index == 0:
                if session.phone not in instance.pending:
                    instance.mark_submitting(session.phone, target)
                raise bot.Uncertain("lost final reply")
            handler_started.wait(1)
            raise RuntimeError("unexpected sibling failure")

        guard = threading.Timer(1.5, lambda: (guard_fired.set(), instance.stop.set()))
        guard.start()
        try:
            with mock.patch.object(bot.Session, "book", uncertain_or_fatal):
                instance.run()
        finally:
            guard.cancel()
        self.assertFalse(guard_fired.is_set())
        self.assertTrue(handler_started.is_set())
        self.assertTrue(any("unexpected worker failure" in failure for failure in instance.failures))
        self.assertEqual(instance.pending, {PHONES[0]: bot.target_key(instance.targets[0])})
        self.assertEqual(instance.pending_questions, {})
        preserved = instance.state.path.read_bytes()
        handler_release.set()
        self.assertTrue(handler_finished.wait(1))
        time.sleep(0.05)
        self.assertEqual(instance.state.path.read_bytes(), preserved)
        self.assertEqual(instance.bookings, [])

    def test_fatal_sibling_before_final_send_clears_unused_pending_marker(self):
        site, url = self.local_site()
        settings = bot.parse_settings(
            make_config(self.root, PHONES[:2], ["G2 Basic", "G3 Basic"]), None)[0]
        out = self.root / "pre-send-cancel"
        self.assertEqual(bot.prepare_documents(settings, out)[0], [])
        instance = bot.Bot(settings, out, base_url=url, log=bot.Log(echo=False))
        marked = threading.Event()
        guard_fired = threading.Event()
        original_mark = instance.mark_submitting
        original_book = bot.Session.book

        def mark_and_wait(phone, target):
            original_mark(phone, target)
            if target.index == 0:
                marked.set()
                instance.stop.wait(1)

        def book_or_fail(session, target):
            if target.index == 1:
                marked.wait(1)
                raise RuntimeError("fatal sibling after pending save")
            return original_book(session, target)

        guard = threading.Timer(2, lambda: (guard_fired.set(), instance.stop.set()))
        guard.start()
        try:
            with mock.patch.object(instance, "mark_submitting", mark_and_wait), \
                    mock.patch.object(bot.Session, "book", book_or_fail):
                instance.run()
        finally:
            guard.cancel()
        self.assertFalse(guard_fired.is_set())
        self.assertTrue(any("unexpected worker failure" in failure for failure in instance.failures))
        self.assertEqual(instance.pending, {})
        self.assertFalse(any(item.get("status") == "submitting" for item in instance.state.data["phones"].values()))
        self.assertFalse(any(item["submit"] for item in site.request_log))
        self.assertEqual(site.applications, [])

    def test_cancellation_at_final_start_callback_clears_pending_without_send(self):
        site, url = self.local_site()
        settings = bot.parse_settings(make_config(self.root, PHONES[:1], ["G2 Basic"]), None)[0]
        out = self.root / "start-callback-cancel"
        self.assertEqual(bot.prepare_documents(settings, out)[0], [])
        instance = bot.Bot(settings, out, base_url=url, log=bot.Log(echo=False))
        original_postback = bot.Session.postback
        callback_reached = []

        def cancel_at_start(session, page, event, values, files=None, timeout=None, on_start=None):
            if values.get("btnSubmit"):
                callback_reached.append(True)
                session.bot.stop.set()
                on_start()
            return original_postback(session, page, event, values, files=files, timeout=timeout,
                                     on_start=on_start)

        with mock.patch.object(bot.Session, "postback", cancel_at_start):
            instance.run()
        self.assertEqual(callback_reached, [True])
        self.assertEqual(instance.pending, {})
        self.assertFalse(any(item.get("status") == "submitting" for item in instance.state.data["phones"].values()))
        self.assertFalse(any(item["submit"] for item in site.request_log))
        self.assertEqual(site.applications, [])

    def test_submit_log_failure_clears_prestart_pending_marker(self):
        site, url = self.local_site()
        settings = bot.parse_settings(make_config(self.root, PHONES[:1], ["G2 Basic"]), None)[0]
        out = self.root / "submit-log-failure"
        self.assertEqual(bot.prepare_documents(settings, out)[0], [])
        instance = bot.Bot(settings, out, base_url=url, log=bot.Log(echo=False))
        original_info = instance.log.info
        failed = []

        def fail_once(message):
            if "SUBMITTING" in message and not failed:
                failed.append(True)
                instance.stop.set()
                raise OSError("broken log stream")
            return original_info(message)

        with mock.patch.object(instance.log, "info", fail_once):
            instance.run()
        self.assertEqual(failed, [True])
        self.assertEqual(instance.pending, {})
        self.assertFalse(any(item.get("status") == "submitting" for item in instance.state.data["phones"].values()))
        self.assertFalse(any(item["submit"] for item in site.request_log))
        self.assertEqual(site.applications, [])

    def test_inflight_cancellation_preserves_pending_for_feedback(self):
        site, url = self.local_site()
        settings = bot.parse_settings(make_config(self.root, PHONES[:1], ["G2 Basic"]), None)[0]
        out = self.root / "inflight-cancel"
        self.assertEqual(bot.prepare_documents(settings, out)[0], [])
        instance = bot.Bot(settings, out, base_url=url, log=bot.Log(echo=False))
        entered = threading.Event()
        release = threading.Event()
        original_submit = mock_site.Handler.submit

        def slow_submit(handler, *args, **kwargs):
            entered.set()
            release.wait(1)
            return original_submit(handler, *args, **kwargs)

        def cancel_inflight():
            if entered.wait(1):
                instance.stop.set()
                release.set()

        coordinator = threading.Thread(target=cancel_inflight, daemon=True)
        coordinator.start()
        with mock.patch.object(mock_site.Handler, "submit", slow_submit):
            instance.run()
        coordinator.join(1)
        self.assertTrue(entered.is_set())
        self.assertEqual(len(site.applications), 1)
        self.assertEqual(instance.bookings, [])
        self.assertEqual(instance.pending, {PHONES[0]: bot.target_key(instance.targets[0])})
        self.assertEqual(instance.state.data["phones"][PHONES[0]]["status"], "submitting")

    def test_transport_provenance_starts_only_at_first_write(self):
        started = []

        class FakeSocket:
            def __init__(self, fail=False):
                self.fail = fail
                self.writes = []
            def sendall(self, data):
                self.writes.append(data)
                if self.fail:
                    raise OSError("partial write")
            def close(self):
                pass

        refused = bot.Http("http://127.0.0.1:9", 0.2, bot.Log(echo=False))
        with mock.patch.object(bot.TrackedHTTPConnection, "connect",
                               side_effect=ConnectionRefusedError("refused")):
            with self.assertRaises(bot.Transient):
                refused.send("POST", "http://127.0.0.1:9/admission_form.aspx", b"body",
                             on_start=lambda: started.append("refused"))
        self.assertEqual(started, [])

        tls = bot.Http("https://example.test", 0.2, bot.Log(echo=False))
        with mock.patch.object(bot.TrackedHTTPSConnection, "connect",
                               side_effect=ssl.SSLCertVerificationError(1, "certificate verify failed")):
            with self.assertRaises(bot.Transient):
                tls.send("POST", "https://example.test/admission_form.aspx", b"body",
                         on_start=lambda: started.append("tls"))
        self.assertEqual(started, [])
        self.assertFalse(tls.verified)

        stop = threading.Event()
        cancelled_socket = FakeSocket()
        cancelled = bot.Http("http://example.test", 1, bot.Log(echo=False), stop)

        def connect_then_cancel(connection):
            connection.sock = cancelled_socket
            stop.set()

        def cancellation_gate():
            if stop.is_set():
                raise bot.Cancelled()
            started.append("cancelled")

        with mock.patch.object(bot.TrackedHTTPConnection, "connect", connect_then_cancel):
            with self.assertRaises(bot.Cancelled):
                cancelled.send("POST", "http://example.test/admission_form.aspx", b"body",
                               on_start=cancellation_gate)
        self.assertEqual(cancelled_socket.writes, [])
        self.assertNotIn("cancelled", started)

        partial_socket = FakeSocket(fail=True)
        partial = bot.Http("http://example.test", 1, bot.Log(echo=False))
        with mock.patch.object(bot.TrackedHTTPConnection, "connect",
                               lambda connection: setattr(connection, "sock", partial_socket)):
            with self.assertRaises(bot.Transient):
                partial.send("POST", "http://example.test/admission_form.aspx", b"body",
                             on_start=lambda: started.append("partial"))
        self.assertIn("partial", started)
        self.assertTrue(partial_socket.writes)

    def test_final_connect_failure_clears_pending_before_feedback(self):
        site, url = self.local_site()
        settings = bot.parse_settings(make_config(self.root, PHONES[:1], ["G2 Basic"]), None)[0]
        out = self.root / "connect-failure"
        self.assertEqual(bot.prepare_documents(settings, out)[0], [])
        instance = bot.Bot(settings, out, base_url=url, log=bot.Log(echo=False))
        instance.state.acquire()
        try:
            instance._restore()
            phone = instance.assign_phone(instance.targets[0])
            session = bot.Session(instance, phone, "")
            selection = session.cascade(instance.targets[0])
            values, files = session.prepare_submit(instance.targets[0], selection)
            with mock.patch.object(bot.TrackedHTTPConnection, "connect",
                                   side_effect=ConnectionRefusedError("refused")):
                with self.assertRaises(bot.Transient):
                    session.submit(instance.targets[0], selection, values, files)
            self.assertEqual(instance.pending, {})
            self.assertFalse(any(item.get("status") == "submitting"
                                 for item in instance.state.data["phones"].values()))
            self.assertEqual(site.applications, [])
        finally:
            instance.state.release()

    def test_fatal_worker_logging_failure_still_stops_siblings(self):
        for error in (bot.ConfigError("fatal config"), RuntimeError("fatal bug")):
            with self.subTest(error=type(error).__name__):
                settings = bot.parse_settings(
                    make_config(self.root, PHONES[:2], ["G2 Basic", "G3 Basic"]), None)[0]
                instance = bot.Bot(settings, self.root / ("log-failure-" + type(error).__name__),
                                   base_url="http://127.0.0.1:9", dry_run=True, log=bot.Log(echo=False))
                guard_fired = threading.Event()

                def fail_or_poll(session, target):
                    if target.index == 0:
                        raise error
                    raise bot.TargetFull("still unavailable")

                guard = threading.Timer(1, lambda: (guard_fired.set(), instance.stop.set()))
                guard.start()
                try:
                    with mock.patch.object(bot.Session, "book", fail_or_poll), \
                            mock.patch.object(instance.log, "error", side_effect=OSError("broken log")):
                        instance.run()
                finally:
                    guard.cancel()
                self.assertFalse(guard_fired.is_set())
                self.assertTrue(instance.stop.is_set())
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

    def test_429_already_used_reply_retires_primary_for_reserve(self):
        site, url = self.local_site()
        original_submit = mock_site.Handler.submit

        def used_primary(handler, mobile, values, files, listed, course, level, batch):
            if mobile == PHONES[0]:
                values["lblMsg"] = ("This Mobile No. is already registered.", "Red")
                return handler.send(429, handler.form(mobile, values, listed))
            return original_submit(handler, mobile, values, files, listed, course, level, batch)

        with mock.patch.object(mock_site.Handler, "submit", used_primary):
            instance = run_bot(make_config(self.root, PHONES[:2], ["G2 Basic"]), url,
                               self.root / "used-429", timeout=5)
        self.assertTrue(instance.finished())
        self.assertEqual(instance.bookings[0]["phone"], PHONES[1])
        self.assertEqual(site.applications[0]["mobile"], PHONES[1])

    def test_truncated_http_error_body_is_not_a_safe_rate_limit(self):
        partial = (b'<form action="admission_form.aspx"><input name="__VIEWSTATE" value="a">'
                   b'<span id="lblMsg">Admission Form submitted successfully.</span>')

        class Truncated429(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(429)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(partial) + 200))
                self.end_headers()
                self.wfile.write(partial)
                self.wfile.flush()
                time.sleep(0.5)
            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Truncated429)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}"
        client = bot.Http(url, 0.15, bot.Log(echo=False))
        page = client.send("POST", url + "/admission_form.aspx", b"x")
        self.assertEqual(page.status, 429)
        self.assertFalse(page.complete)
        instance = offline_bot()
        session = bot.Session(instance, PHONES[0], "")
        session.http = client
        with self.assertRaises(bot.Uncertain):
            session.judge_submit(page, instance.targets[0], None, None, set())

        settings = bot.parse_settings(make_config(self.root, PHONES[:1], ["G2 Basic"]), None)[0]
        settings.opt["submitTimeoutSec"] = 0.15
        owner = bot.Bot(settings, self.root / "truncated-submit", base_url=url, log=bot.Log(echo=False))
        owner.state.acquire()
        try:
            owner._restore()
            phone = owner.assign_phone(owner.targets[0])
            request_page = bot.Page(
                url + "/admission_form.aspx", 200,
                '<form action="/admission_form.aspx"><input name="__VIEWSTATE" value="a"></form>')
            selection = type("PreparedSelection", (), {"page": request_page})()
            final_session = bot.Session(owner, phone, "")
            with self.assertRaises(bot.Uncertain):
                final_session.submit(owner.targets[0], selection, {}, {})
            self.assertEqual(owner.phone_state[phone], "submitting")
            self.assertEqual(owner.pending[phone], bot.target_key(owner.targets[0]))
            saved = bot.strict_json_loads(owner.state.path.read_text())
            self.assertEqual(saved["phones"][phone]["status"], "submitting")
        finally:
            owner.state.release()

    def assert_early_eof_final_reply_requires_feedback(self, status):
        class EarlyEOF(mock_site.Handler):
            def redirect(self, location):
                if location.lower().startswith("/admissionformsuccess.aspx"):
                    self.send_response(status)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", "99999")
                    self.end_headers()
                    try:
                        self.wfile.write(
                            b'<form><input name="__VIEWSTATE" value="test">'
                            b'<span id="lblMsg">No seats left.</span>')
                        self.wfile.flush()
                    except OSError:
                        pass
                    self.close_connection = True
                    return
                return super().redirect(location)

        site, url = self.local_site(EarlyEOF)
        answers = []
        cfg = make_config(self.root, PHONES[:2], ["G2 Basic"], bot={"submitTimeoutSec": 0.2})
        instance = run_bot(cfg, url, self.root / f"early-eof-{status}", timeout=3,
                           confirmation_handler=lambda *args: answers.append("booked") or "booked")
        self.assertTrue(instance.finished())
        self.assertEqual(answers, ["booked"])
        self.assertEqual(len(site.applications), 1)
        self.assertEqual(site.applications[0]["mobile"], PHONES[0])
        self.assertNotIn(PHONES[1], site.used)

    def test_early_eof_200_after_commit_requires_feedback_without_reserve(self):
        self.assert_early_eof_final_reply_requires_feedback(200)

    def test_early_eof_429_after_commit_requires_feedback_without_reserve(self):
        self.assert_early_eof_final_reply_requires_feedback(429)

    def test_trickled_redirect_bodies_obey_one_absolute_deadline(self):
        class TrickledRedirect(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/done":
                    self.send_response(200)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"ok")
                    return
                self.send_response(int(self.path.strip("/")))
                self.send_header("Location", "/done")
                self.send_header("Content-Length", "100")
                self.end_headers()
                try:
                    for value in b"x" * 100:
                        self.wfile.write(bytes([value]))
                        self.wfile.flush()
                        time.sleep(0.03)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), TrickledRedirect)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}"
        client = bot.Http(url, 0.15, bot.Log(echo=False))
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                started = time.monotonic()
                with self.assertRaises(bot.Transient):
                    client.get(f"/{status}")
                self.assertLess(time.monotonic() - started, 0.7)

    def test_trickled_final_redirect_body_requires_feedback_without_reserve(self):
        class TrickledReceipt(mock_site.Handler):
            def redirect(self, location):
                if location.lower().startswith("/admissionformsuccess.aspx"):
                    self.send_response(302)
                    self.send_header("Location", location)
                    self.send_header("Content-Length", "100")
                    self.end_headers()
                    try:
                        for value in b"x" * 100:
                            self.wfile.write(bytes([value]))
                            self.wfile.flush()
                            time.sleep(0.03)
                    except OSError:
                        pass
                    return
                return super().redirect(location)

        site, url = self.local_site(TrickledReceipt)
        answers = []
        cfg = make_config(self.root, PHONES[:2], ["G2 Basic"], bot={"submitTimeoutSec": 0.15})
        instance = run_bot(cfg, url, self.root / "trickled-redirect", timeout=3,
                           confirmation_handler=lambda *args: answers.append("booked") or "booked")
        self.assertTrue(instance.finished())
        self.assertEqual(answers, ["booked"])
        self.assertEqual(len(site.applications), 1)
        self.assertEqual(site.applications[0]["mobile"], PHONES[0])
        self.assertNotIn(PHONES[1], site.used)

    def test_criteria_refusal_is_reported_without_retiring_phone(self):
        site, url = self.local_site()
        reason = "The revised declaration must be reviewed before continuing."

        def refuse_criteria(handler, session, query, values):
            return handler.send(200, handler.criteria(session["mobile"], reason))

        out = self.root / "criteria-refusal"
        with mock.patch.object(mock_site.Handler, "post_criteria", refuse_criteria):
            instance = run_bot(make_config(self.root, PHONES[:2], ["G2 Basic"]), url, out, timeout=0.5)
        log = (out / "bot.log").read_text()
        criteria_sessions = {item["sid"] for item in site.request_log
                             if item["page"] == "admission_criteria.aspx" and item["method"] == "POST"}
        self.assertFalse(instance.finished())
        self.assertEqual(site.applications, [])
        self.assertEqual(instance.assignments[bot.target_key(instance.targets[0])], PHONES[0])
        self.assertEqual(instance.refusals, {})
        self.assertNotIn(PHONES[0], site.used)
        self.assertNotIn(PHONES[1], site.used)
        self.assertGreaterEqual(len(criteria_sessions), 2)
        self.assertIn(reason, log)
        self.assertIn("NEEDS YOUR ATTENTION", log)

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

    def test_trickled_response_headers_obey_total_request_deadline(self):
        class TrickleHeaders(BaseHTTPRequestHandler):
            def do_GET(self):
                response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nX-Slow: header\r\n\r\nOK"
                try:
                    for value in response:
                        self.connection.sendall(bytes([value]))
                        time.sleep(0.03)
                except OSError:
                    pass
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), TrickleHeaders)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = bot.Http(f"http://127.0.0.1:{server.server_port}", 0.2, bot.Log(echo=False))
        started = time.monotonic()
        with self.assertRaises(bot.Transient):
            client.get("/")
        self.assertLess(time.monotonic() - started, 0.8)

    def test_complete_zero_length_eof_framed_and_chunked_responses_remain_valid(self):
        class Framing(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_HEAD(self):
                self.send_response(200)
                self.send_header("Content-Length", "99")
                self.end_headers()

            def do_GET(self):
                if self.path == "/zero":
                    self.send_response(204)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif self.path == "/eof":
                    self.send_response(200)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(b"eof body")
                    self.close_connection = True
                else:
                    self.send_response(200)
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    self.wfile.write(b"4\r\ntest\r\n0\r\n\r\n")
                    self.wfile.flush()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Framing)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}"
        client = bot.Http(url, 1, bot.Log(echo=False))
        self.assertEqual(client.send("HEAD", url + "/head").body, "")
        self.assertEqual(client.get("/zero").body, "")
        self.assertEqual(client.get("/eof").body, "eof body")
        self.assertEqual(client.get("/chunked").body, "test")

    def test_stdlib_empty_and_http_error_bodies_use_one_response_contract(self):
        class EmptyBodies(BaseHTTPRequestHandler):
            def do_GET(self):
                status = 429 if self.path == "/error" else 204
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), EmptyBodies)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = bot.Http(f"http://127.0.0.1:{server.server_port}", 1, bot.Log(echo=False))
        empty = client.get("/empty")
        error = client.get("/error")
        self.assertEqual((empty.status, empty.body), (204, ""))
        self.assertEqual((error.status, error.body), (429, ""))

    def test_unsupported_response_interface_fails_clearly(self):
        class UnsupportedResponse:
            status = 200
            headers = {}
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def geturl(self):
                return "http://127.0.0.1:9/"
        class UnsupportedOpener:
            def open(self, request, timeout):
                return UnsupportedResponse()
        client = bot.Http("http://127.0.0.1:9", 1, bot.Log(echo=False))
        client.opener = UnsupportedOpener()
        with self.assertRaisesRegex(bot.Transient, "unsupported stdlib HTTP response type"):
            client.get("/")

    def test_error_or_negative_success_page_is_not_a_confirmation(self):
        instance = offline_bot()
        session = bot.Session(instance, PHONES[0], "")
        for path, status, body in (
                ("payment-error.aspx", 503, "payment failed"),
                ("Unsuccessful.aspx", 200, "application failed"),
                ("AdmissionFormSuccess.aspx", 200, "Application not successfully submitted"),
                ("AdmissionFormSuccess.aspx", 200, "Application not submitted successfully"),
                ("AdmissionFormSuccess.aspx", 200, "Application unsuccessfully submitted"),
                ("AdmissionFormSuccess.aspx", 200,
                 "Application was not in fact yet been submitted successfully"),
                ("AdmissionFormSuccess.aspx", 200, "Application received invalid data"),
                ("AdmissionFormSuccess.aspx", 200, "AdmissionFormSuccess")):
            with self.subTest(path=path):
                page = bot.Page(f"http://127.0.0.1:9/{path}", status, body)
                with self.assertRaises(bot.Transient):
                    session.judge_submit(page, instance.targets[0], None, None, set())

    def test_success_mixed_with_decisive_negative_evidence_is_uncertain(self):
        instance = offline_bot()
        session = bot.Session(instance, PHONES[0], "")
        success = "Admission Form submitted successfully. Payment link has been sent."
        cases = {
            "used": (200, '<span id="lblerrorMsg">This Mobile No. is already registered.</span>'),
            "full": (200, '<span id="lblerrorMsg">Seat not available.</span>'),
            "closed": (200, '<span id="lblerrorMsg">Registration is closed.</span>'),
            "validator": (200, '<script>rfvtxtName.errormessage = "Please Enter Name.";'
                               'rfvtxtName.isvalid = "False";</script>'),
            "http-500-full": (500, '<span id="lblerrorMsg">Seat not available.</span>'),
            "http-429-validator": (429, '<script>rfvtxtName.errormessage = "Please Enter Name.";'
                                         'rfvtxtName.isvalid = "False";</script>'),
            "http-429": (429, ""),
            "rejected-clause": (200, '<span id="lblerrorMsg">The application was rejected.</span>'),
            "invalid-clause": (200, '<span id="lblerrorMsg">The application contains invalid data.</span>'),
            "not-accepted-clause": (200, '<span id="lblerrorMsg">The application was not accepted.</span>'),
        }
        for name, (status, evidence) in cases.items():
            with self.subTest(name=name):
                session.static = {}
                body = ('<form action="admission_form.aspx"><input name="__VIEWSTATE" value="a">'
                        f'<span id="lblMsg">{success}</span>{evidence}</form>')
                page = bot.Page("http://127.0.0.1:9/admission_form.aspx", status, body)
                with self.assertRaises(bot.Uncertain):
                    session.judge_submit(page, instance.targets[0], None, None, set())

        same_clause = bot.Page(
            "http://127.0.0.1:9/admission_form.aspx", 200,
            '<form action="admission_form.aspx"><input name="__VIEWSTATE" value="a">'
            '<span id="lblMsg">Application submitted successfully but the request was rejected.</span></form>')
        with self.assertRaises(bot.Uncertain):
            session.judge_submit(same_clause, instance.targets[0], None, None, set())

        page = bot.Page("http://127.0.0.1:9/admission_form.aspx", 429,
                        '<form action="admission_form.aspx"><input name="__VIEWSTATE" value="a"></form>')
        with self.assertRaises(bot.RateLimited):
            session.judge_submit(page, instance.targets[0], None, None, set())

        pure = {
            "used": ("This Mobile No. is already registered.", bot.PhoneUsed),
            "full": ("Seat not available.", bot.TargetFull),
            "closed": ("Registration is closed.", bot.NotOpen),
        }
        for name, (message, error) in pure.items():
            with self.subTest(pure=name):
                session.static = {}
                body = ('<form action="admission_form.aspx"><input name="__VIEWSTATE" value="a">'
                        f'<span id="lblMsg">{message}</span></form>')
                page = bot.Page("http://127.0.0.1:9/admission_form.aspx", 200, body)
                with self.assertRaises(error):
                    session.judge_submit(page, instance.targets[0], None, None, set())

        decisive = {
            "429-used": (429, '<span id="lblMsg">This Mobile No. is already registered.</span>', bot.PhoneUsed),
            "429-full": (429, '<span id="lblMsg">Seat not available.</span>', bot.TargetFull),
            "429-closed": (429, '<span id="lblMsg">Registration is closed.</span>', bot.NotOpen),
            "429-validator": (429, '<script>rfvtxtName.errormessage = "Please Enter Name.";'
                                     'rfvtxtName.isvalid = "False";</script>', bot.Rejected),
            "500-validator": (500, '<script>rfvtxtName.errormessage = "Please Enter Name.";'
                                     'rfvtxtName.isvalid = "False";</script>', bot.Rejected),
        }
        for name, (status, evidence, error) in decisive.items():
            with self.subTest(decisive=name):
                session.static = {}
                body = ('<form action="admission_form.aspx"><input name="__VIEWSTATE" value="a">'
                        f'{evidence}</form>')
                page = bot.Page("http://127.0.0.1:9/admission_form.aspx", status, body)
                with self.assertRaises(error):
                    session.judge_submit(page, instance.targets[0], None, None, set())

    def test_explicit_historical_success_does_not_require_an_id(self):
        instance = offline_bot()
        session = bot.Session(instance, PHONES[0], "")
        page = bot.Page("http://127.0.0.1:9/AdmissionFormSuccess.aspx", 200,
                        '<form><input name="__VIEWSTATE" value="a"><span id="lblMsg">'
                        'Admission Form submitted successfully, Payment details have been sent on your registered email.'
                        '</span></form>')
        result = session.judge_submit(page, instance.targets[0], None, None, set())
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
