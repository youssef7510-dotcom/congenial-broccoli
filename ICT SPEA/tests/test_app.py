import base64
from concurrent.futures import ThreadPoolExecutor
import io
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import app as system
from openpyxl import load_workbook


class AttendanceSystemTests(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.TemporaryDirectory()
        system.DB_PATH = Path(self.data_dir.name) / "test.sqlite3"
        system.init_db()
        with system.app.app_context():
            system.set_settings(
                {
                    "attendance_start": "08:00",
                    "attendance_cutoff": "08:15",
                    "leaving_time": "15:00",
                    "flexibility_minutes": "5",
                    "minimum_early_leave_minutes": "30",
                }
            )
            db = system.get_db()
            roster_start = datetime(2026, 4, 14, 7, 0).astimezone().isoformat()
            db.execute(
                "UPDATE people SET created_at=?, confirmed_at=? WHERE confirmed=1",
                (roster_start, roster_start),
            )
            db.execute(
                "UPDATE settings SET value='2026-04-15' WHERE key='roster_history_start_date'"
            )
            db.commit()
        system.app.config["TESTING"] = True
        self.client = system.app.test_client()
        self.original_clock = system.now_local

    def tearDown(self):
        system.now_local = self.original_clock
        self.data_dir.cleanup()

    def csrf(self, path="/admin"):
        page = self.client.get(path).get_data(as_text=True)
        return re.search(r'data-csrf="([^"]+)"', page).group(1)

    def sign_in(self):
        token = self.csrf("/admin")
        response = self.client.post(
            "/api/admin/login",
            json={"username": "admin", "password": "admin123"},
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return self.csrf("/admin/dashboard")

    def test_schedule_arrivals_early_departure_and_finalize(self):
        csrf = self.sign_in()
        with system.app.app_context():
            system.set_settings(
                {
                    "attendance_cutoff": "08:20",
                    "flexibility_minutes": "5",
                    "leaving_time": "15:00",
                }
            )
            people = system.get_db().execute(
                "SELECT id, name FROM people WHERE kind='student' ORDER BY name"
            ).fetchall()
            person_ids = {row["name"]: row["id"] for row in people}
            today = "2026-04-15"

            system.now_local = lambda: datetime(2026, 4, 15, 8, 20)
            on_time = system.ensure_attendance(person_ids["Aisha Rahman"], "arrival")
            self.assertEqual(on_time["record"]["attendance_status"], "Present")
            duplicate = system.ensure_attendance(person_ids["Aisha Rahman"], "arrival")
            self.assertTrue(duplicate["duplicate"])

            system.now_local = lambda: datetime(2026, 4, 15, 8, 21)
            late = system.ensure_attendance(person_ids["Maya Chen"], "arrival")
            self.assertEqual(late["record"]["attendance_status"], "Late")

            early_code = system.get_db().execute(
                "SELECT code FROM early_codes WHERE used_at IS NULL LIMIT 1"
            ).fetchone()["code"]
            system.now_local = lambda: datetime(2026, 4, 15, 14, 30)
            early = system.ensure_attendance(
                person_ids["Aisha Rahman"], "early_departure", early_code, "Medical appointment"
            )
            self.assertEqual(early["record"]["departure_status"], "Left Early")
            self.assertEqual(early["record"]["early_leave_reason"], "Medical appointment")
            self.assertEqual(early["record"]["early_leave_code_result"], "valid")
            self.assertIsNotNone(early["record"]["early_leave_requested_at"])
            self.assertEqual(
                system.get_db().execute(
                    "SELECT COUNT(*) FROM early_codes WHERE used_at IS NULL"
                ).fetchone()[0],
                5,
            )

        system.now_local = lambda: datetime(2026, 4, 15, 15, 5)
        response = self.client.post(
            "/api/admin/end-day",
            json={"date": today},
            headers={"X-CSRFToken": csrf},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        rows = self.client.get(f"/api/admin/attendance?date={today}").get_json()["attendance"]
        statuses = {row["name"]: row for row in rows}
        self.assertEqual(statuses["Aisha Rahman"]["departure_status"], "Left Early")
        self.assertEqual(statuses["Maya Chen"]["attendance_status"], "Late")
        self.assertEqual(statuses["Omar Hassan"]["attendance_status"], "Absent")
        self.assertTrue(all(row["finalized"] for row in rows))
        with system.app.app_context():
            system.now_local = lambda: datetime(2026, 4, 15, 15, 10)
            aisha_id = system.get_db().execute(
                "SELECT id FROM people WHERE name='Aisha Rahman'"
            ).fetchone()["id"]
            finalized = system.ensure_attendance(aisha_id, "arrival")
            self.assertIn("finalized", finalized["error"])

    def test_end_to_end_arrival_early_leave_absence_and_excel_export(self):
        admin_csrf = self.sign_in()
        device_code = self.client.post(
            "/api/admin/devices/code",
            json={},
            headers={"X-CSRFToken": admin_csrf},
        ).get_json()["code"]
        station_csrf = self.csrf("/")
        paired = self.client.post(
            "/api/pair",
            json={"code": device_code, "device_name": "End-to-end test station"},
            headers={"X-CSRFToken": station_csrf},
        )
        self.assertEqual(paired.status_code, 200, paired.get_data(as_text=True))

        with system.app.app_context():
            system.set_settings(
                {
                    "attendance_start": "08:00",
                    "attendance_cutoff": "08:15",
                    "leaving_time": "15:00",
                    "flexibility_minutes": "5",
                    "minimum_early_leave_minutes": "0",
                }
            )
            db = system.get_db()
            ids = {
                row["name"]: row["id"]
                for row in db.execute(
                    "SELECT id, name FROM people WHERE kind='student' AND confirmed=1"
                ).fetchall()
            }
            unused_code = db.execute(
                "SELECT code FROM early_codes WHERE used_at IS NULL ORDER BY id LIMIT 1"
            ).fetchone()["code"]

        class FakeRecognizer:
            def predict(self, _face):
                return active_label[0], 10.0

        active_label = [0]
        labels = {0: ids["Aisha Rahman"], 1: ids["Maya Chen"]}
        fake_image = system.np.zeros((200, 200), dtype=system.np.uint8)
        current_time = [datetime(2026, 4, 15, 8, 0, 0)]
        with (
            patch.object(system, "now_local", side_effect=lambda: current_time[0]),
            patch.object(system, "parse_image_data", return_value=fake_image),
            patch.object(system, "extract_face", return_value=(fake_image, b"face")),
            patch.object(
                system,
                "recognition_model",
                return_value=(FakeRecognizer(), labels),
            ),
        ):
            on_time = self.client.post(
                "/api/recognize",
                json={"image": "test-frame"},
                headers={"X-CSRFToken": station_csrf},
            )
            self.assertEqual(on_time.status_code, 200, on_time.get_data(as_text=True))
            on_time_data = on_time.get_json()
            self.assertTrue(on_time_data["first_arrival"])
            self.assertEqual(on_time_data["current_status"]["attendance"], "Present")
            self.assertEqual(on_time_data["current_status"]["arrival_time"], "08:00:00")

            active_label[0] = 1
            current_time[0] = datetime(2026, 4, 15, 8, 20, 0)
            late = self.client.post(
                "/api/recognize",
                json={"image": "test-frame"},
                headers={"X-CSRFToken": station_csrf},
            )
            self.assertEqual(late.status_code, 200, late.get_data(as_text=True))
            self.assertEqual(late.get_json()["current_status"]["attendance"], "Late")
            self.assertEqual(late.get_json()["current_status"]["arrival_time"], "08:20:00")

            active_label[0] = 0
            current_time[0] = datetime(2026, 4, 15, 9, 0, 0)
            confirmation = self.client.post(
                "/api/recognize",
                json={"image": "test-frame", "confirm_early_departure": True},
                headers={"X-CSRFToken": station_csrf},
            )
            self.assertEqual(
                confirmation.status_code, 200, confirmation.get_data(as_text=True)
            )
            ticket = confirmation.get_json()["ticket"]
            self.assertTrue(ticket)

            invalid_code = self.client.post(
                "/api/attendance/early-leave",
                json={"ticket": ticket, "code": "INVALID!", "reason": "Medical appointment"},
                headers={"X-CSRFToken": station_csrf},
            )
            self.assertEqual(invalid_code.status_code, 400)
            self.assertIn("invalid", invalid_code.get_json()["error"].lower())

            early = self.client.post(
                "/api/attendance/early-leave",
                json={
                    "ticket": ticket,
                    "code": unused_code,
                    "reason": "Medical appointment",
                },
                headers={"X-CSRFToken": station_csrf},
            )
            self.assertEqual(early.status_code, 200, early.get_data(as_text=True))
            self.assertEqual(early.get_json()["record"]["departure_status"], "Left Early")
            self.assertEqual(
                early.get_json()["record"]["early_leave_reason"], "Medical appointment"
            )
            self.assertEqual(
                early.get_json()["record"]["early_leave_code_result"], "valid"
            )

        current_time[0] = datetime(2026, 4, 15, 15, 5, 0)
        ended = self.client.post(
            "/api/admin/end-day",
            json={"date": "2026-04-15"},
            headers={"X-CSRFToken": admin_csrf},
        )
        self.assertEqual(ended.status_code, 200, ended.get_data(as_text=True))

        report = self.client.get("/api/admin/report.xlsx?date=2026-04-15")
        self.assertEqual(report.status_code, 200, f"Excel export failed with HTTP {report.status_code}")
        self.assertIn("spreadsheetml", report.content_type)
        workbook = load_workbook(io.BytesIO(report.data), read_only=True)
        aisha = next(
            row for row in workbook["Grade 8"].iter_rows(min_row=2, values_only=True)
            if row[1] == "Aisha Rahman"
        )
        maya = next(
            row for row in workbook["Grade 9"].iter_rows(min_row=2, values_only=True)
            if row[1] == "Maya Chen"
        )
        omar = next(
            row for row in workbook["Grade 8"].iter_rows(min_row=2, values_only=True)
            if row[1] == "Omar Hassan"
        )
        self.assertEqual(aisha[4], "Present")
        self.assertEqual(aisha[6], "Left Early")
        self.assertEqual(aisha[7], "09:00:00")
        self.assertEqual(aisha[8], "Medical appointment")
        self.assertEqual(maya[4], "Late")
        self.assertEqual(maya[5], "08:20:00")
        self.assertEqual(omar[4], "Absent")
        self.assertIsNone(omar[5])
        workbook.close()

    def test_device_code_is_single_use_and_pairing_is_persistent(self):
        csrf = self.sign_in()
        response = self.client.post(
            "/api/admin/devices/code", json={}, headers={"X-CSRFToken": csrf}
        )
        self.assertEqual(response.status_code, 200)
        code = response.get_json()["code"]
        station_csrf = self.csrf("/")
        pair = self.client.post(
            "/api/pair",
            json={"code": code, "device_name": "Front door", "client_id": "a" * 32},
            headers={"X-CSRFToken": station_csrf},
        )
        self.assertEqual(pair.status_code, 200)
        self.assertTrue(self.client.get("/api/attendance/state").get_json()["paired"])
        reconnect_code = self.client.post(
            "/api/admin/devices/code", json={}, headers={"X-CSRFToken": csrf}
        ).get_json()["code"]
        reconnect = self.client.post(
            "/api/pair",
            json={
                "code": reconnect_code,
                "device_name": "Front door",
                "client_id": "a" * 32,
            },
            headers={"X-CSRFToken": station_csrf},
        )
        self.assertEqual(reconnect.status_code, 200)
        with system.app.app_context():
            self.assertEqual(
                system.get_db().execute("SELECT COUNT(*) FROM devices").fetchone()[0],
                1,
            )
        replay = self.client.post(
            "/api/pair",
            json={"code": reconnect_code, "device_name": "Second browser"},
            headers={"X-CSRFToken": station_csrf},
        )
        self.assertEqual(replay.status_code, 400)

    def test_admin_can_enable_no_code_device_connections(self):
        csrf = self.sign_in()
        enabled = self.client.post(
            "/api/admin/settings",
            json={"allow_unpaired_devices": True},
            headers={"X-CSRFToken": csrf},
        )
        self.assertEqual(enabled.status_code, 200, enabled.get_data(as_text=True))
        station = system.app.test_client()
        state = station.get("/api/attendance/state").get_json()
        self.assertTrue(state["paired"])
        self.assertTrue(state["allow_unpaired_devices"])
        device = station.get_cookie(system.DEVICE_COOKIE)
        self.assertIsNotNone(device)
        with system.app.app_context():
            saved = system.get_db().execute(
                "SELECT device_type FROM devices WHERE active=1"
            ).fetchone()
            self.assertEqual(saved["device_type"], "No-code browser")

        disabled = self.client.post(
            "/api/admin/settings",
            json={"allow_unpaired_devices": False},
            headers={"X-CSRFToken": csrf},
        )
        self.assertEqual(disabled.status_code, 200)
        new_browser = system.app.test_client()
        new_state = new_browser.get("/api/attendance/state").get_json()
        self.assertFalse(new_state["paired"])
        self.assertFalse(new_state["allow_unpaired_devices"])

    def test_schedule_and_enrollment_settings_are_in_the_correct_sections(self):
        self.sign_in()
        dashboard = self.client.get("/admin/dashboard").get_data(as_text=True)
        settings_form = re.search(
            r'<form id="settings-form".*?</form>',
            dashboard,
            flags=re.DOTALL,
        ).group(0)
        self.assertIn('name="attendance_start"', settings_form)
        self.assertIn('name="attendance_cutoff"', settings_form)
        self.assertIn('name="leaving_time"', settings_form)
        self.assertIn('name="flexibility_minutes"', settings_form)
        self.assertIn('name="minimum_early_leave_minutes"', settings_form)
        self.assertNotIn('name="self_enrollment"', settings_form)
        self.assertNotIn('name="school_day_start"', settings_form)
        self.assertNotIn('name="school_day_end"', settings_form)
        people_section = re.search(
            r'<section id="people".*</section>\s*<section id="devices"',
            dashboard,
            flags=re.DOTALL,
        ).group(0)
        self.assertIn('id="enrollment-settings-form"', people_section)
        self.assertIn('name="self_enrollment"', people_section)
        self.assertIn("active-schedule-status", dashboard)
        self.assertIn("pending-schedule-status", dashboard)
        self.assertIn("Departure Time", settings_form)

    def test_schedule_changes_update_active_day_immediately_without_rewriting_arrival(self):
        test_date = datetime(2026, 4, 15, 8, 0)
        with patch.object(system, "now_local", return_value=test_date):
            csrf = self.sign_in()
            original = self.client.get("/api/admin/dashboard").get_json()["schedule_state"]["active"]
            self.assertTrue(original["configured"])
            with system.app.app_context():
                person_id = system.get_db().execute("SELECT id FROM people WHERE kind='student' ORDER BY id LIMIT 1").fetchone()["id"]
                arrival = system.ensure_attendance(person_id, "arrival")
                self.assertEqual(arrival["record"]["arrival_time"], "08:00:00")
            changed = self.client.post("/api/admin/settings", json={"attendance_start":"07:30", "attendance_cutoff":"08:10", "leaving_time":"15:30", "flexibility_minutes":10, "minimum_early_leave_minutes":25}, headers={"X-CSRFToken": csrf})
            self.assertEqual(changed.status_code, 200, changed.get_data(as_text=True))
            payload = changed.get_json()
            state = payload["schedule_state"]
            self.assertTrue(payload["schedule_updated_today"])
            self.assertFalse(state["has_pending_changes"])
            self.assertEqual(state["active"]["attendance_start"], "07:30")
            self.assertEqual(state["active"]["attendance_cutoff"], "08:10")
            self.assertEqual(state["active"]["leaving_time"], "15:30")
            with system.app.app_context():
                self.assertEqual(system.get_active_schedule()["attendance_start"], "07:30")
                saved_arrival = system.get_db().execute("SELECT arrival_time, attendance_status FROM attendance WHERE person_id=? AND attendance_date='2026-04-15'", (person_id,)).fetchone()
                self.assertEqual(saved_arrival["arrival_time"], "08:00:00")
                self.assertEqual(saved_arrival["attendance_status"], "Present")
                with patch.object(system, "now_local", return_value=datetime(2026, 4, 15, 15, 30)):
                    regular_departure = system.ensure_attendance(person_id, "departure")
                self.assertEqual(regular_departure["record"]["departure_status"], "Left")
                self.assertEqual(regular_departure["record"]["departure_time"], "15:30:00")

    def test_schedule_changes_after_end_day_are_saved_for_next_day(self):
        test_date = datetime(2026, 4, 15, 8, 0)
        with patch.object(system, "now_local", return_value=test_date):
            csrf = self.sign_in()
            with system.app.app_context():
                system.get_db().execute("INSERT INTO attendance_days(attendance_date, finalized_at) VALUES (?, ?)", ("2026-04-15", system.stamp()))
                system.get_db().commit()
            changed = self.client.post("/api/admin/settings", json={"attendance_start":"07:30", "attendance_cutoff":"08:10", "leaving_time":"15:30", "flexibility_minutes":10, "minimum_early_leave_minutes":25}, headers={"X-CSRFToken": csrf})
            self.assertEqual(changed.status_code, 200, changed.get_data(as_text=True))
            payload = changed.get_json()
            self.assertFalse(payload["schedule_updated_today"])
            self.assertTrue(payload["schedule_state"]["has_pending_changes"])
            self.assertEqual(payload["schedule_state"]["pending_effective_date"], "2026-04-16")
            self.assertEqual(payload["schedule_state"]["active"]["attendance_start"], "08:00")

    def test_overnight_attendance_after_midnight_uses_shift_start_date(self):
        with system.app.app_context():
            system.set_settings(
                {
                    "attendance_start": "20:00",
                    "attendance_cutoff": "20:15",
                    "leaving_time": "02:00",
                    "flexibility_minutes": "5",
                }
            )
            person_id = system.get_db().execute(
                "SELECT id FROM people WHERE kind='student' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
            system.now_local = lambda: datetime(2026, 4, 15, 20, 0)
            arrival = system.ensure_attendance(person_id, "arrival")
            self.assertEqual(arrival["record"]["attendance_date"], "2026-04-15")
            system.now_local = lambda: datetime(2026, 4, 16, 2, 0)
            departure = system.ensure_attendance(person_id, "departure")
            self.assertEqual(departure["record"]["attendance_date"], "2026-04-15")
            self.assertEqual(departure["record"]["departure_time"], "02:00:00")
            self.assertIsNone(
                system.get_db().execute(
                    "SELECT id FROM attendance WHERE person_id=? AND attendance_date='2026-04-16'",
                    (person_id,),
                ).fetchone()
            )

    def test_first_complete_schedule_activates_today_and_unconfigured_days_block_attendance(self):
        test_date = datetime(2026, 4, 15, 8, 0)
        with system.app.app_context():
            db = system.get_db()
            db.execute("DELETE FROM schedule_snapshots")
            system.set_settings(
                {
                    "attendance_start": "",
                    "attendance_cutoff": "",
                    "leaving_time": "",
                }
            )
        with patch.object(system, "now_local", return_value=test_date):
            csrf = self.sign_in()
            state = self.client.get("/api/admin/dashboard").get_json()["schedule_state"]
            self.assertFalse(state["active"]["configured"])
            with system.app.app_context():
                person_id = system.get_db().execute(
                    "SELECT id FROM people WHERE kind='student' ORDER BY id LIMIT 1"
                ).fetchone()["id"]
                unavailable = system.ensure_attendance(person_id, "arrival")
                self.assertTrue(unavailable["schedule_not_configured"])
            saved = self.client.post(
                "/api/admin/settings",
                json={
                    "attendance_start": "07:45",
                    "attendance_cutoff": "08:15",
                    "leaving_time": "15:00",
                    "flexibility_minutes": 5,
                    "minimum_early_leave_minutes": 30,
                },
                headers={"X-CSRFToken": csrf},
            )
            self.assertEqual(saved.status_code, 200, saved.get_data(as_text=True))
            self.assertTrue(saved.get_json()["schedule_activated_today"])
            self.assertEqual(
                saved.get_json()["schedule_state"]["active"]["attendance_start"],
                "07:45",
            )

    def test_fresh_install_does_not_choose_fixed_schedule_times(self):
        self.assertEqual(system.DEFAULT_SETTINGS["attendance_start"], "")
        self.assertEqual(system.DEFAULT_SETTINGS["attendance_cutoff"], "")
        self.assertEqual(system.DEFAULT_SETTINGS["leaving_time"], "")

    def test_invalid_image_is_rejected_not_accepted_as_a_face(self):
        csrf = self.sign_in()
        with system.app.app_context():
            class_id = system.get_db().execute(
                "SELECT id FROM classes WHERE name='Grade 8'"
            ).fetchone()["id"]
        image = "data:image/jpeg;base64," + base64.b64encode(b"not an image").decode()
        response = self.client.post(
            "/api/admin/people",
            json={"kind": "student", "name": "Unverified", "student_number": "TEST-1", "class_id": class_id, "images": {"center": image}},
            headers={"X-CSRFToken": csrf},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("image", response.get_json()["error"].lower())

    def test_request_limit_accounts_for_three_base64_enrollment_photos(self):
        csrf = self.sign_in()
        encoded_photo_size = ((system.MAX_IMAGE_BYTES + 2) // 3) * 4
        request_size = encoded_photo_size * 3 + 4096
        body = b"{}" + b" " * request_size
        response = self.client.post(
            "/api/admin/settings",
            data=body,
            content_type="application/json",
            headers={"X-CSRFToken": csrf},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

    def test_enrollment_saves_center_left_and_right_face_profiles(self):
        csrf = self.sign_in()
        success, encoded = system.cv2.imencode(
            ".png",
            system.np.random.default_rng(42).integers(0, 256, (200, 200), dtype=system.np.uint8),
        )
        self.assertTrue(success)
        profile_image = encoded.tobytes()
        with patch.object(
            system,
            "parse_enrollment_profiles",
            return_value={pose: profile_image for pose in ("center", "left", "right")},
        ):
            class_response = self.client.post(
                "/api/admin/classes",
                json={"name": "Grade 10"},
                headers={"X-CSRFToken": csrf},
            )
            self.assertEqual(class_response.status_code, 200)
            response = self.client.post(
                "/api/admin/people",
                json={
                    "kind": "student",
                    "name": "Pose Test",
                    "student_number": "G10-001",
                    "class_id": class_response.get_json()["class_id"],
                    "images": {"center": "captured", "left": "captured", "right": "captured"},
                },
                headers={"X-CSRFToken": csrf},
            )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        with system.app.app_context():
            saved = system.get_db().execute(
                "SELECT pose FROM face_profiles WHERE person_id=? ORDER BY pose",
                (response.get_json()["person_id"],),
            ).fetchall()
            self.assertEqual([row["pose"] for row in saved], ["center", "left", "right"])
            model, labels = system.recognition_model()
            self.assertIsNotNone(model)
            self.assertIn(response.get_json()["person_id"], labels.values())

    def test_side_pose_detection_matches_the_requested_camera_direction(self):
        gray = system.np.tile(
            system.np.arange(200, dtype=system.np.uint8),
            (200, 1),
        )
        image = system.cv2.cvtColor(gray, system.cv2.COLOR_GRAY2BGR)
        detected_profile = system.np.array([[10, 20, 70, 80]], dtype=system.np.int32)

        class LeftProfileCascade:
            def __init__(self, _path):
                self.calls = 0

            def detectMultiScale(self, _image, **_kwargs):
                self.calls += 1
                return (
                    detected_profile
                    if self.calls == 2
                    else system.np.empty((0, 4), dtype=system.np.int32)
                )

        with patch.object(system.cv2, "CascadeClassifier", LeftProfileCascade):
            left_face, _ = system.extract_face(image, "left")
        self.assertAlmostEqual(
            float(left_face.mean()),
            float(system.cv2.equalizeHist(gray[20:100, 120:190]).mean()),
            delta=1,
        )

        class RightProfileCascade:
            def __init__(self, _path):
                self.calls = 0

            def detectMultiScale(self, _image, **_kwargs):
                self.calls += 1
                return (
                    detected_profile
                    if self.calls == 1
                    else system.np.empty((0, 4), dtype=system.np.int32)
                )

        with patch.object(system.cv2, "CascadeClassifier", RightProfileCascade):
            right_face, _ = system.extract_face(image, "right")
        self.assertAlmostEqual(
            float(right_face.mean()),
            float(system.cv2.equalizeHist(gray[20:100, 10:80]).mean()),
            delta=1,
        )

    def test_only_admins_manage_classes_and_assigned_class_is_archived(self):
        unauthenticated_csrf = self.csrf("/")
        response = self.client.post(
            "/api/admin/classes",
            json={"name": "Unauthorized"},
            headers={"X-CSRFToken": unauthenticated_csrf},
        )
        self.assertEqual(response.status_code, 401)
        csrf = self.sign_in()
        with system.app.app_context():
            grade_eight = system.get_db().execute(
                "SELECT id FROM classes WHERE name='Grade 8'"
            ).fetchone()["id"]
        response = self.client.delete(
            f"/api/admin/classes/{grade_eight}",
            headers={"X-CSRFToken": csrf},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["archived"])
        state = self.client.get("/api/admin/dashboard").get_json()
        self.assertFalse(any(item["name"] == "Grade 8" and item["active"] for item in state["classes"]))

    def test_students_must_use_admin_created_class_and_unique_student_number(self):
        csrf = self.sign_in()
        class_response = self.client.post(
            "/api/admin/classes",
            json={"name": "Grade 11"},
            headers={"X-CSRFToken": csrf},
        )
        self.assertEqual(class_response.status_code, 200)
        class_id = class_response.get_json()["class_id"]
        success, encoded = system.cv2.imencode(
            ".png",
            system.np.random.default_rng(42).integers(0, 256, (200, 200), dtype=system.np.uint8),
        )
        self.assertTrue(success)
        profile_image = encoded.tobytes()
        payload = {
            "kind": "student",
            "name": "Student Test",
            "student_number": "STU-001",
            "class_id": class_id,
            "images": {"center": "captured", "left": "captured", "right": "captured"},
        }
        with patch.object(
            system,
            "parse_enrollment_profiles",
            return_value={pose: profile_image for pose in ("center", "left", "right")},
        ):
            created = self.client.post(
                "/api/admin/people",
                json=payload,
                headers={"X-CSRFToken": csrf},
            )
            duplicate = self.client.post(
                "/api/admin/people",
                json={**payload, "name": "Another Student"},
                headers={"X-CSRFToken": csrf},
            )
            invented_class = self.client.post(
                "/api/admin/people",
                json={**payload, "name": "Other Student", "student_number": "STU-002", "class_id": 9999},
                headers={"X-CSRFToken": csrf},
            )
        self.assertEqual(created.status_code, 200, created.get_data(as_text=True))
        self.assertEqual(duplicate.status_code, 400)
        self.assertIn("already assigned", duplicate.get_json()["error"])
        self.assertEqual(invented_class.status_code, 400)
        self.assertIn("class", invented_class.get_json()["error"].lower())

    def test_teacher_creation_needs_only_name_and_face_profiles(self):
        csrf = self.sign_in()
        success, encoded = system.cv2.imencode(
            ".png",
            system.np.random.default_rng(45).integers(0, 256, (200, 200), dtype=system.np.uint8),
        )
        self.assertTrue(success)
        profile_image = encoded.tobytes()
        with patch.object(
            system,
            "parse_enrollment_profiles",
            return_value={pose: profile_image for pose in ("center", "left", "right")},
        ):
            response = self.client.post(
                "/api/admin/people",
                json={
                    "kind": "teacher",
                    "name": "Teacher Name Only",
                    "images": {"center": "captured", "left": "captured", "right": "captured"},
                },
                headers={"X-CSRFToken": csrf},
            )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        person = self.client.get("/api/admin/dashboard").get_json()["people"]
        saved = next(item for item in person if item["id"] == response.get_json()["person_id"])
        self.assertEqual(saved["group_name"], "Teachers")
        self.assertIsNone(saved["student_number"])
        self.assertIsNone(saved["class_id"])

    def test_end_day_export_contains_class_and_teacher_sheets(self):
        csrf = self.sign_in()
        system.now_local = lambda: datetime(2026, 4, 15, 16, 0)
        response = self.client.post(
            "/api/admin/end-day",
            json={"date": "2026-04-15"},
            headers={"X-CSRFToken": csrf},
        )
        self.assertEqual(response.status_code, 200)
        report = self.client.get("/api/admin/report.xlsx?date=2026-04-15")
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.data[:2], b"PK")
        self.assertIn(
            "spreadsheetml.sheet",
            report.headers["Content-Type"],
        )
        workbook = load_workbook(io.BytesIO(report.data))
        self.assertEqual(set(workbook.sheetnames), {"Grade 8", "Grade 9", "Teachers"})
        self.assertEqual(workbook["Grade 8"].auto_filter.ref, f"A1:I{workbook['Grade 8'].max_row}")

    def test_export_leaves_student_departure_blank_and_labels_teacher_not_departed(self):
        self.sign_in()
        report_date = "2026-04-15"
        with system.app.app_context():
            db = system.get_db()
            student = db.execute(
                "SELECT id, name, group_name FROM people WHERE kind='student' AND active=1 LIMIT 1"
            ).fetchone()
            teacher = db.execute(
                "SELECT id, name FROM people WHERE kind='teacher' AND active=1 AND confirmed=1 LIMIT 1"
            ).fetchone()
            for person in (student, teacher):
                db.execute(
                    """INSERT INTO attendance(
                           person_id, attendance_date, attendance_status, arrival_time, finalized
                       ) VALUES (?, ?, 'Present', '08:00:00', 1)""",
                    (person["id"], report_date),
                )
            db.commit()

        report = self.client.get(f"/api/admin/report.xlsx?date={report_date}")
        self.assertEqual(report.status_code, 200)
        workbook = load_workbook(io.BytesIO(report.data), read_only=True)
        student_sheet = workbook[student["group_name"]]
        student_row = next(
            row for row in student_sheet.iter_rows(min_row=2, values_only=True)
            if row[1] == student["name"]
        )
        self.assertIsNone(student_row[6])
        self.assertIsNone(student_row[7])
        teacher_row = next(
            row for row in workbook["Teachers"].iter_rows(min_row=2, values_only=True)
            if row[1] == teacher["name"]
        )
        self.assertEqual(teacher_row[6], "Did Not Depart")
        self.assertIsNone(teacher_row[7])

    def test_admin_can_delete_attendance_record(self):
        csrf = self.sign_in()
        with system.app.app_context():
            db = system.get_db()
            person_id = db.execute(
                "SELECT id FROM people WHERE kind='student' LIMIT 1"
            ).fetchone()["id"]
            cursor = db.execute(
                """INSERT INTO attendance(person_id, attendance_date, attendance_status, arrival_time)
                   VALUES (?, '2026-04-15', 'Present', '08:00:00')""",
                (person_id,),
            )
            record_id = cursor.lastrowid
            db.commit()
        response = self.client.delete(
            f"/api/admin/attendance/{record_id}",
            headers={"X-CSRFToken": csrf},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.get("/api/admin/attendance?date=2026-04-15").get_json()["attendance"],
            [],
        )

    def test_face_scan_records_arrival_and_enforces_original_30_minute_wait(self):
        admin_csrf = self.sign_in()
        code = self.client.post(
            "/api/admin/devices/code",
            json={},
            headers={"X-CSRFToken": admin_csrf},
        ).get_json()["code"]
        station_csrf = self.csrf("/")
        paired = self.client.post(
            "/api/pair",
            json={"code": code, "device_name": "Timing station"},
            headers={"X-CSRFToken": station_csrf},
        )
        self.assertEqual(paired.status_code, 200)
        with system.app.app_context():
            person_id = system.get_db().execute(
                "SELECT id FROM people WHERE kind='student' ORDER BY id LIMIT 1"
            ).fetchone()["id"]

        class FakeRecognizer:
            def predict(self, _face):
                return 0, 10.0

        image = system.np.zeros((200, 200), dtype=system.np.uint8)
        current_time = [datetime(2026, 4, 15, 8, 0, 0)]
        with (
            patch.object(system, "now_local", side_effect=lambda: current_time[0]),
            patch.object(system, "parse_image_data", return_value=image),
            patch.object(system, "extract_face", return_value=(image, b"face")),
            patch.object(system, "recognition_model", return_value=(FakeRecognizer(), {0: person_id})),
        ):
            scan = self.client.post(
                "/api/recognize",
                json={"image": "frame"},
                headers={"X-CSRFToken": station_csrf},
            )
            self.assertEqual(scan.status_code, 200, scan.get_data(as_text=True))
            self.assertTrue(scan.get_json()["first_arrival"])
            self.assertEqual(scan.get_json()["current_status"]["arrival_time"], "08:00:00")
            with system.app.app_context():
                self.assertEqual(
                    system.get_db().execute(
                        "SELECT COUNT(*) FROM attendance WHERE person_id=? AND attendance_date=?",
                        (person_id, system.current_day()),
                    ).fetchone()[0],
                    1,
                )

            current_time[0] = datetime(2026, 4, 15, 8, 29, 59)
            waiting_scan = self.client.post(
                "/api/recognize",
                json={"image": "frame"},
                headers={"X-CSRFToken": station_csrf},
            )
            self.assertEqual(waiting_scan.status_code, 200)
            self.assertFalse(waiting_scan.get_json()["early_available"])
            self.assertEqual(waiting_scan.get_json()["remaining_seconds"], 1)
            self.assertIsNone(waiting_scan.get_json()["ticket"])

            current_time[0] = datetime(2026, 4, 15, 8, 30, 0)
            eligible_scan = self.client.post(
                "/api/recognize",
                json={"image": "frame"},
                headers={"X-CSRFToken": station_csrf},
            )
            self.assertTrue(eligible_scan.get_json()["early_available"])
            self.assertIsNone(eligible_scan.get_json()["ticket"])

            confirmation_scan = self.client.post(
                "/api/recognize",
                json={"image": "frame", "confirm_early_departure": True},
                headers={"X-CSRFToken": station_csrf},
            )
            self.assertTrue(confirmation_scan.get_json()["early_available"])
            self.assertIsNotNone(confirmation_scan.get_json()["ticket"])
            ticket = confirmation_scan.get_json()["ticket"]

            missing_reason = self.client.post(
                "/api/attendance/early-leave",
                json={"ticket": ticket, "code": "ABCD1234", "reason": ""},
                headers={"X-CSRFToken": station_csrf},
            )
            self.assertEqual(missing_reason.status_code, 400)
            self.assertIn("reason", missing_reason.get_json()["error"].lower())

            rejected = self.client.post(
                "/api/attendance/early-leave",
                json={"ticket": ticket, "code": "INVALID!", "reason": "Appointment"},
                headers={"X-CSRFToken": station_csrf},
            )
            self.assertEqual(rejected.status_code, 400)
            self.assertIn("invalid", rejected.get_json()["error"].lower())
            token_hash = system.hashlib.sha256(ticket.encode()).hexdigest()
            with system.app.app_context():
                retained_ticket = system.get_db().execute(
                    "SELECT token_hash FROM face_tickets WHERE token_hash=?",
                    (token_hash,),
                ).fetchone()
                self.assertIsNotNone(retained_ticket)
                early_code = system.get_db().execute(
                    "SELECT code FROM early_codes WHERE used_at IS NULL LIMIT 1"
                ).fetchone()["code"]
            departure = self.client.post(
                "/api/attendance/early-leave",
                json={
                    "ticket": ticket,
                    "code": early_code,
                    "reason": "Appointment",
                },
                headers={"X-CSRFToken": station_csrf},
            )
            self.assertEqual(departure.status_code, 200, departure.get_data(as_text=True))
            self.assertEqual(departure.get_json()["record"]["arrival_time"], "08:00:00")
            self.assertEqual(departure.get_json()["record"]["departure_status"], "Left Early")
            saved_departure_time = departure.get_json()["record"]["departure_time"]
            with system.app.app_context():
                consumed_ticket = system.get_db().execute(
                    "SELECT token_hash FROM face_tickets WHERE token_hash=?",
                    (token_hash,),
                ).fetchone()
            self.assertIsNone(consumed_ticket)
            replay = self.client.post(
                "/api/attendance/early-leave",
                json={
                    "ticket": ticket,
                    "code": early_code,
                    "reason": "Duplicate submission",
                },
                headers={"X-CSRFToken": station_csrf},
            )
            self.assertEqual(replay.status_code, 400)
            self.assertIn("expired", replay.get_json()["error"].lower())

            with system.app.app_context():
                duplicate = system.ensure_attendance(person_id, "departure")
            self.assertTrue(duplicate["already_departed"])
            self.assertEqual(duplicate["record"]["arrival_time"], "08:00:00")
            self.assertEqual(duplicate["record"]["departure_time"], saved_departure_time)

            final_scan = self.client.post(
                "/api/recognize",
                json={"image": "frame"},
                headers={"X-CSRFToken": station_csrf},
            )
            self.assertTrue(final_scan.get_json()["already_departed"])
            self.assertEqual(final_scan.get_json()["final_status"], "Left Early")

    def test_early_ticket_cannot_fall_through_to_regular_checkout_at_boundary(self):
        system.now_local = lambda: datetime(2026, 4, 15, 7, 59, 0)
        with system.app.app_context():
            system.set_settings(
                {
                    "attendance_cutoff": "08:00",
                    "leaving_time": "08:01",
                    "minimum_early_leave_minutes": "0",
                }
            )
        admin_csrf = self.sign_in()
        code = self.client.post(
            "/api/admin/devices/code",
            json={},
            headers={"X-CSRFToken": admin_csrf},
        ).get_json()["code"]
        station_csrf = self.csrf("/")
        self.client.post(
            "/api/pair",
            json={"code": code, "device_name": "Checkout boundary station"},
            headers={"X-CSRFToken": station_csrf},
        )
        with system.app.app_context():
            system.set_settings(
                {
                    "attendance_cutoff": "08:00",
                    "leaving_time": "08:01",
                    "minimum_early_leave_minutes": "0",
                }
            )
            early_code = system.get_db().execute(
                "SELECT code FROM early_codes WHERE used_at IS NULL LIMIT 1"
            ).fetchone()["code"]
            person_id = system.get_db().execute(
                "SELECT id FROM people WHERE kind='student' ORDER BY id LIMIT 1"
            ).fetchone()["id"]

        class FakeRecognizer:
            def predict(self, _face):
                return 0, 10.0

        image = system.np.zeros((200, 200), dtype=system.np.uint8)
        current_time = [datetime(2026, 4, 15, 8, 0, 0)]
        with (
            patch.object(system, "now_local", side_effect=lambda: current_time[0]),
            patch.object(system, "parse_image_data", return_value=image),
            patch.object(system, "extract_face", return_value=(image, b"face")),
            patch.object(system, "recognition_model", return_value=(FakeRecognizer(), {0: person_id})),
        ):
            arrival_response = self.client.post(
                "/api/recognize",
                json={"image": "frame"},
                headers={"X-CSRFToken": station_csrf},
            )
            self.assertEqual(
                arrival_response.status_code,
                200,
                arrival_response.get_data(as_text=True),
            )
            arrival = arrival_response.get_json()
            self.assertTrue(arrival.get("first_arrival"), arrival)
            self.assertTrue(arrival["early_available"])
            self.assertEqual(arrival["early_leave_closes_in_seconds"], 60)
            self.assertIsNone(arrival["ticket"])

            confirmation = self.client.post(
                "/api/recognize",
                json={"image": "frame", "confirm_early_departure": True},
                headers={"X-CSRFToken": station_csrf},
            ).get_json()
            ticket = confirmation["ticket"]
            self.assertIsNotNone(ticket)

            current_time[0] = datetime(2026, 4, 15, 8, 1, 0)
            early_attempt = self.client.post(
                "/api/attendance/early-leave",
                json={"ticket": ticket, "code": early_code, "reason": "Appointment"},
                headers={"X-CSRFToken": station_csrf},
            )
            self.assertEqual(early_attempt.status_code, 409)
            self.assertIn("window closed", early_attempt.get_json()["error"].lower())
            with system.app.app_context():
                attendance = system.get_db().execute(
                    "SELECT departure_status, departure_time FROM attendance WHERE person_id=?",
                    (person_id,),
                ).fetchone()
                code_row = system.get_db().execute(
                    "SELECT used_at FROM early_codes WHERE code=?",
                    (early_code,),
                ).fetchone()
            self.assertIsNone(attendance["departure_status"])
            self.assertIsNone(code_row["used_at"])

            regular_scan = self.client.post(
                "/api/recognize",
                json={"image": "frame"},
                headers={"X-CSRFToken": station_csrf},
            ).get_json()
            self.assertFalse(regular_scan["already_departed"])
            self.assertIsNone(regular_scan["final_status"])
            with system.app.app_context():
                attendance = system.get_db().execute(
                    "SELECT departure_time, departure_status FROM attendance WHERE person_id=?",
                    (person_id,),
                ).fetchone()
            self.assertIsNone(attendance["departure_time"])
            self.assertIsNone(attendance["departure_status"])
            checkout = self.client.post(
                "/api/recognize",
                json={"image": "frame", "confirm_checkout": True},
                headers={"X-CSRFToken": station_csrf},
            ).get_json()
            self.assertTrue(checkout["already_departed"])
            self.assertEqual(checkout["final_status"], "Left")

    def test_simultaneous_early_leave_submissions_record_only_one_departure(self):
        admin_csrf = self.sign_in()
        device_code = self.client.post(
            "/api/admin/devices/code",
            json={},
            headers={"X-CSRFToken": admin_csrf},
        ).get_json()["code"]
        station_csrf = self.csrf("/")
        paired = self.client.post(
            "/api/pair",
            json={"code": device_code, "device_name": "Concurrent checkout"},
            headers={"X-CSRFToken": station_csrf},
        )
        self.assertEqual(paired.status_code, 200)
        with system.app.app_context():
            system.set_settings(
                {
                    "attendance_cutoff": "15:45",
                    "leaving_time": "16:00",
                    "minimum_early_leave_minutes": "0",
                }
            )
            person_id = system.get_db().execute(
                "SELECT id FROM people WHERE kind='student' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
            system.now_local = lambda: datetime(2026, 4, 15, 8, 0, 0)
            system.ensure_attendance(person_id, "arrival")
            code = system.get_db().execute(
                "SELECT code FROM early_codes WHERE used_at IS NULL LIMIT 1"
            ).fetchone()["code"]
            ticket = system.issue_ticket(person_id, mode="departure", face_verified=True)

        session_cookie = self.client.get_cookie("session").value
        device_cookie = self.client.get_cookie(system.DEVICE_COOKIE).value

        def submit_departure(_):
            with system.app.test_client() as client:
                client.set_cookie("session", session_cookie)
                client.set_cookie(system.DEVICE_COOKIE, device_cookie)
                return client.post(
                    "/api/attendance/early-leave",
                    json={"ticket": ticket, "code": code, "reason": "Concurrent test"},
                    headers={"X-CSRFToken": station_csrf},
                )

        with patch.object(system, "now_local", return_value=datetime(2026, 4, 15, 8, 0, 1)):
            with ThreadPoolExecutor(max_workers=2) as executor:
                responses = list(executor.map(submit_departure, range(2)))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 400])
        with system.app.app_context():
            record = system.get_db().execute(
                "SELECT departure_status, departure_time FROM attendance WHERE person_id=?",
                (person_id,),
            ).fetchone()
            self.assertEqual(record["departure_status"], "Left Early")
            self.assertEqual(record["departure_time"], "08:00:01")
            self.assertEqual(
                system.get_db().execute(
                    "SELECT COUNT(*) FROM early_codes WHERE used_at IS NOT NULL AND used_by=?",
                    (person_id,),
                ).fetchone()[0],
                1,
            )

    def test_late_arrival_time_is_preserved_on_repeated_scans(self):
        with system.app.app_context():
            system.set_settings({"attendance_cutoff": "08:15", "flexibility_minutes": "10"})
            person_id = system.get_db().execute(
                "SELECT id FROM people WHERE kind='student' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
        system.now_local = lambda: datetime(2026, 4, 15, 8, 21, 17)
        with system.app.app_context():
            first = system.ensure_attendance(person_id, "arrival")
        self.assertEqual(first["record"]["attendance_status"], "Late")
        self.assertEqual(first["record"]["arrival_time"], "08:21:17")
        system.now_local = lambda: datetime(2026, 4, 15, 9, 0, 0)
        with system.app.app_context():
            repeated = system.ensure_attendance(person_id, "arrival")
        self.assertTrue(repeated["duplicate"])
        self.assertEqual(repeated["record"]["arrival_time"], "08:21:17")

    def test_arrival_is_rejected_after_cutoff_and_flexibility_deadline(self):
        with system.app.app_context():
            system.set_settings(
                {"attendance_cutoff": "08:15", "flexibility_minutes": "5"}
            )
            person_id = system.get_db().execute(
                "SELECT id FROM people WHERE kind='student' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
            system.now_local = lambda: datetime(2026, 4, 15, 8, 20, 0)
            final_grace_minute = system.ensure_attendance(person_id, "arrival")
            self.assertEqual(final_grace_minute["record"]["attendance_status"], "Late")
            self.assertEqual(final_grace_minute["record"]["arrival_time"], "08:20:00")

            other_person_id = system.get_db().execute(
                "SELECT id FROM people WHERE kind='student' AND id<>? ORDER BY id LIMIT 1",
                (person_id,),
            ).fetchone()["id"]
            system.now_local = lambda: datetime(2026, 4, 15, 8, 20, 1)
            rejected = system.ensure_attendance(other_person_id, "arrival")
            self.assertTrue(rejected["attendance_closed"])
            self.assertIn("window has closed", rejected["error"])
            self.assertIsNone(
                system.get_db().execute(
                    "SELECT id FROM attendance WHERE person_id=? AND attendance_date='2026-04-15'",
                    (other_person_id,),
                ).fetchone()
            )

    def test_face_scan_reports_closed_attendance_without_recording_arrival(self):
        admin_csrf = self.sign_in()
        code = self.client.post(
            "/api/admin/devices/code",
            json={},
            headers={"X-CSRFToken": admin_csrf},
        ).get_json()["code"]
        station_csrf = self.csrf("/")
        self.client.post(
            "/api/pair",
            json={"code": code, "device_name": "Late arrival test"},
            headers={"X-CSRFToken": station_csrf},
        )
        with system.app.app_context():
            system.set_settings(
                {"attendance_cutoff": "08:15", "flexibility_minutes": "5"}
            )
            person_id = system.get_db().execute(
                "SELECT id FROM people WHERE kind='student' ORDER BY id LIMIT 1"
            ).fetchone()["id"]

        class FakeRecognizer:
            def predict(self, _face):
                return 0, 10.0

        image = system.np.zeros((200, 200), dtype=system.np.uint8)
        with (
            patch.object(system, "now_local", return_value=datetime(2026, 4, 15, 8, 21)),
            patch.object(system, "parse_image_data", return_value=image),
            patch.object(system, "extract_face", return_value=(image, b"face")),
            patch.object(system, "recognition_model", return_value=(FakeRecognizer(), {0: person_id})),
        ):
            response = self.client.post(
                "/api/recognize",
                json={"image": "frame"},
                headers={"X-CSRFToken": station_csrf},
            )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertTrue(payload["recognized"])
        self.assertTrue(payload["attendance_closed"])
        with system.app.app_context():
            self.assertIsNone(
                system.get_db().execute(
                    "SELECT id FROM attendance WHERE person_id=? AND attendance_date='2026-04-15'",
                    (person_id,),
                ).fetchone()
            )

    def test_departure_is_rejected_before_30_minutes_from_original_arrival(self):
        with system.app.app_context():
            person_id = system.get_db().execute(
                "SELECT id FROM people WHERE kind='student' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
            system.now_local = lambda: datetime(2026, 4, 15, 8, 0, 0)
            system.ensure_attendance(person_id, "arrival")
            system.now_local = lambda: datetime(2026, 4, 15, 8, 29, 59)
            early = system.ensure_attendance(person_id, "early_departure", "ABCD1234", "Test")
            self.assertEqual(early["remaining_seconds"], 1)
            self.assertIn("30 minutes", early["error"])
        with system.app.app_context():
            row = system.get_db().execute(
                "SELECT arrival_time, departure_time FROM attendance WHERE person_id=?",
                (person_id,),
            ).fetchone()
            self.assertEqual(row["arrival_time"], "08:00:00")
            self.assertIsNone(row["departure_time"])

    def test_configured_early_departure_minimum_is_enforced(self):
        with system.app.app_context():
            system.set_settings(
                {
                    "minimum_early_leave_minutes": "10",
                    "leaving_time": "16:00",
                    "attendance_cutoff": "14:45",
                }
            )
            person_id = system.get_db().execute(
                "SELECT id FROM people WHERE kind='student' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
            system.now_local = lambda: datetime(2026, 4, 15, 14, 30, 0)
            system.ensure_attendance(person_id, "arrival")
            system.now_local = lambda: datetime(2026, 4, 15, 14, 39, 59)
            too_early = system.ensure_attendance(person_id, "early_departure")
            self.assertEqual(too_early["remaining_seconds"], 1)
            self.assertIn("10 minutes", too_early["error"])
            system.now_local = lambda: datetime(2026, 4, 15, 14, 40, 0)
            code = system.get_db().execute(
                "SELECT code FROM early_codes WHERE used_at IS NULL LIMIT 1"
            ).fetchone()["code"]
            departure = system.ensure_attendance(
                person_id, "early_departure", code, "Configured minimum test"
            )
            self.assertEqual(departure["record"]["departure_status"], "Left Early")
            self.assertEqual(departure["record"]["arrival_time"], "14:30:00")

    def test_normal_departure_is_not_delayed_by_early_departure_setting(self):
        with system.app.app_context():
            system.set_settings(
                {
                    "minimum_early_leave_minutes": "30",
                    "attendance_cutoff": "14:00",
                    "leaving_time": "16:00",
                }
            )
            person_id = system.get_db().execute(
                "SELECT id FROM people WHERE kind='student' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
            system.now_local = lambda: datetime(2026, 4, 15, 13, 30, 0)
            system.ensure_attendance(person_id, "arrival")
            system.now_local = lambda: datetime(2026, 4, 15, 16, 0, 0)
            departure = system.ensure_attendance(person_id, "departure")
        self.assertEqual(departure["record"]["departure_status"], "Left")
        self.assertEqual(departure["record"]["arrival_time"], "13:30:00")
        self.assertEqual(departure["record"]["departure_time"], "16:00:00")

    def test_departure_flexibility_allows_normal_checkout_then_flags_late_departure(self):
        self.sign_in()
        with system.app.app_context():
            system.set_settings(
                {
                    "attendance_cutoff": "08:15",
                    "leaving_time": "15:00",
                    "flexibility_minutes": "5",
                }
            )
            people = system.get_db().execute(
                "SELECT id FROM people WHERE kind='student' ORDER BY id LIMIT 3"
            ).fetchall()
            person_ids = [row["id"] for row in people]
            now = [datetime(2026, 4, 15, 8, 0)]
            system.now_local = lambda: now[0]
            for person_id in person_ids:
                arrival = system.ensure_attendance(person_id, "arrival")
                self.assertEqual(arrival["record"]["attendance_status"], "Present")

            now[0] = datetime(2026, 4, 15, 15, 0)
            at_departure = system.ensure_attendance(person_ids[0], "departure")
            self.assertEqual(at_departure["record"]["departure_status"], "Left")

            now[0] = datetime(2026, 4, 15, 15, 5)
            at_grace_end = system.ensure_attendance(person_ids[1], "departure")
            self.assertEqual(at_grace_end["record"]["departure_status"], "Left")

            now[0] = datetime(2026, 4, 15, 15, 5, 1)
            after_grace = system.ensure_attendance(person_ids[2], "departure")
            self.assertEqual(after_grace["record"]["departure_status"], "Left Late")
            self.assertEqual(after_grace["record"]["departure_time"], "15:05:01")
            self.assertTrue(after_grace["late_departure"])
            self.assertIn("administrator review", after_grace["message"].lower())
        report = self.client.get("/api/admin/report.xlsx?date=2026-04-15")
        workbook = load_workbook(io.BytesIO(report.data), read_only=True)
        exported_statuses = [
            row[6]
            for sheet in workbook.worksheets
            for row in sheet.iter_rows(min_row=2, values_only=True)
        ]
        self.assertIn("Left Late", exported_statuses)

    def test_recognition_api_exposes_departure_grace_window_and_status(self):
        admin_csrf = self.sign_in()
        with system.app.app_context():
            now = [datetime(2026, 4, 15, 8, 0)]
            system.now_local = lambda: now[0]
            system.set_settings(
                {
                    "attendance_start": "08:00",
                    "attendance_cutoff": "08:15",
                    "leaving_time": "15:00",
                    "flexibility_minutes": "5",
                    "minimum_early_leave_minutes": "30",
                }
            )
            person_id = system.get_db().execute(
                "SELECT id FROM people WHERE kind='student' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
            system.ensure_attendance(person_id, "arrival")
            now[0] = datetime(2026, 4, 15, 15, 2)
        code = self.client.post(
            "/api/admin/devices/code",
            json={},
            headers={"X-CSRFToken": admin_csrf},
        ).get_json()["code"]
        station_csrf = self.csrf("/")
        self.client.post(
            "/api/pair",
            json={"code": code, "device_name": "Departure grace test"},
            headers={"X-CSRFToken": station_csrf},
        )

        class FakeRecognizer:
            def predict(self, _face):
                return 0, 10.0

        image = system.np.zeros((200, 200), dtype=system.np.uint8)
        with (
            patch.object(system, "parse_image_data", return_value=image),
            patch.object(system, "extract_face", return_value=(image, b"face")),
            patch.object(system, "recognition_model", return_value=(FakeRecognizer(), {0: person_id})),
        ):
            now[0] = datetime(2026, 4, 15, 15, 2)
            in_grace = self.client.post(
                "/api/recognize",
                json={"image": "frame"},
                headers={"X-CSRFToken": station_csrf},
            ).get_json()
            self.assertEqual(in_grace["departure_action"], "Check Out")
            self.assertFalse(in_grace["late_departure"])
            self.assertGreater(in_grace["late_departure_remaining_seconds"], 0)
            self.assertFalse(in_grace["already_departed"])
            self.assertIsNone(in_grace["final_status"])

        with system.app.app_context():
            late_person_id = system.get_db().execute(
                "SELECT id FROM people WHERE kind='student' ORDER BY id LIMIT 2 OFFSET 1"
            ).fetchone()["id"]
            system.now_local = lambda: datetime(2026, 4, 15, 8, 0)
            system.ensure_attendance(late_person_id, "arrival")
        with (
            patch.object(system, "now_local", return_value=datetime(2026, 4, 15, 15, 5, 1)),
            patch.object(system, "parse_image_data", return_value=image),
            patch.object(system, "extract_face", return_value=(image, b"face")),
            patch.object(system, "recognition_model", return_value=(FakeRecognizer(), {0: late_person_id})),
        ):
            late = self.client.post(
                "/api/recognize",
                json={"image": "frame"},
                headers={"X-CSRFToken": station_csrf},
            ).get_json()
        self.assertTrue(late["late_departure"])
        self.assertIsNone(late["final_status"])
        self.assertFalse(late["already_departed"])

    def test_early_leave_minimum_setting_is_validated_and_persisted(self):
        csrf = self.sign_in()
        saved = self.client.post(
            "/api/admin/settings",
            json={"minimum_early_leave_minutes": 12, "leaving_time": "16:30"},
            headers={"X-CSRFToken": csrf},
        )
        self.assertEqual(saved.status_code, 200, saved.get_data(as_text=True))
        self.assertEqual(saved.get_json()["settings"]["minimum_early_leave_minutes"], "12")
        self.assertEqual(saved.get_json()["settings"]["leaving_time"], "16:30")
        invalid = self.client.post(
            "/api/admin/settings",
            json={"minimum_early_leave_minutes": 1441},
            headers={"X-CSRFToken": csrf},
        )
        self.assertEqual(invalid.status_code, 400)
        invalid_schedule = self.client.post(
            "/api/admin/settings",
            json={"attendance_cutoff": "16:30", "leaving_time": "16:30"},
            headers={"X-CSRFToken": csrf},
        )
        self.assertEqual(invalid_schedule.status_code, 400)
        self.assertIn("departure must be after", invalid_schedule.get_json()["error"].lower())

    def test_admin_can_replace_demo_password_with_a_stronger_password(self):
        csrf = self.sign_in()
        rejected = self.client.post(
            "/api/admin/password",
            json={
                "current_password": "wrong",
                "new_password": "this-is-a-long-password",
                "confirmation": "this-is-a-long-password",
            },
            headers={"X-CSRFToken": csrf},
        )
        self.assertEqual(rejected.status_code, 400)
        changed = self.client.post(
            "/api/admin/password",
            json={
                "current_password": "admin123",
                "new_password": "this-is-a-long-password",
                "confirmation": "this-is-a-long-password",
            },
            headers={"X-CSRFToken": csrf},
        )
        self.assertEqual(changed.status_code, 200, changed.get_data(as_text=True))
        self.assertIsNone(self.client.get_cookie("session"))

        login_csrf = self.csrf("/admin")
        old_login = self.client.post(
            "/api/admin/login",
            json={"username": "admin", "password": "admin123"},
            headers={"X-CSRFToken": login_csrf},
        )
        self.assertEqual(old_login.status_code, 401)
        new_login = self.client.post(
            "/api/admin/login",
            json={"username": "admin", "password": "this-is-a-long-password"},
            headers={"X-CSRFToken": login_csrf},
        )
        self.assertEqual(new_login.status_code, 200)

    def test_login_attempts_are_rate_limited_until_lockout_expires(self):
        now = [datetime(2026, 4, 15, 8, 0)]
        system.now_local = lambda: now[0]
        csrf = self.csrf("/admin")
        for attempt in range(system.LOGIN_FAILURE_LIMIT):
            response = self.client.post(
                "/api/admin/login",
                json={"username": "admin", "password": f"wrong-{attempt}"},
                headers={"X-CSRFToken": csrf},
            )
        self.assertEqual(response.status_code, 429)
        self.assertGreater(int(response.headers["Retry-After"]), 0)
        blocked_valid_login = self.client.post(
            "/api/admin/login",
            json={"username": "admin", "password": "admin123"},
            headers={"X-CSRFToken": csrf},
        )
        self.assertEqual(blocked_valid_login.status_code, 429)
        now[0] += system.LOGIN_LOCKOUT_DURATION + timedelta(seconds=1)
        successful_login = self.client.post(
            "/api/admin/login",
            json={"username": "admin", "password": "admin123"},
            headers={"X-CSRFToken": csrf},
        )
        self.assertEqual(successful_login.status_code, 200)
        with system.app.app_context():
            self.assertEqual(
                system.get_db().execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0],
                0,
            )

    def test_network_bind_is_blocked_while_demo_password_is_active(self):
        with self.assertRaisesRegex(RuntimeError, "demo admin password"):
            system.validate_host_credentials("0.0.0.0")
        system.validate_host_credentials("127.0.0.1")
        with system.app.app_context():
            system.get_db().execute(
                "UPDATE admins SET password_hash=? WHERE username='admin'",
                (system.generate_password_hash("a-unique-secure-password"),),
            )
            system.get_db().commit()
        system.validate_host_credentials("0.0.0.0")

    def test_expired_early_leave_code_is_rejected_and_replaced(self):
        with system.app.app_context():
            system.set_settings(
                {"minimum_early_leave_minutes": "0", "leaving_time": "16:00"}
            )
            person_id = system.get_db().execute(
                "SELECT id FROM people WHERE kind='student' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
            system.now_local = lambda: datetime(2026, 4, 15, 8, 0, 0)
            system.ensure_attendance(person_id, "arrival")
            expired_code = system.get_db().execute(
                "SELECT code FROM early_codes WHERE used_at IS NULL ORDER BY id LIMIT 1"
            ).fetchone()["code"]
            system.get_db().execute(
                "UPDATE early_codes SET created_at=? WHERE code=?",
                ("2026-04-15T08:00:00", expired_code),
            )
            system.get_db().commit()
            system.now_local = lambda: datetime(2026, 4, 15, 8, 15, 1)
            result = system.ensure_attendance(
                person_id, "early_departure", expired_code, "Expired-code test"
            )
            self.assertIn("invalid", result["error"].lower())
            self.assertEqual(
                system.get_db().execute(
                    "SELECT COUNT(*) FROM early_codes WHERE used_at IS NULL"
                ).fetchone()[0],
                5,
            )
            self.assertIsNone(
                system.get_db().execute(
                    "SELECT id FROM early_codes WHERE code=? AND used_at IS NULL",
                    (expired_code,),
                ).fetchone()
            )
            attendance = system.get_db().execute(
                "SELECT departure_status, departure_time FROM attendance WHERE person_id=?",
                (person_id,),
            ).fetchone()
            self.assertIsNone(attendance["departure_status"])
            self.assertIsNone(attendance["departure_time"])

    def test_early_leave_code_can_only_be_used_once(self):
        with system.app.app_context():
            db = system.get_db()
            person_ids = [
                row["id"] for row in db.execute(
                    "SELECT id FROM people WHERE kind='student' ORDER BY id LIMIT 2"
                ).fetchall()
            ]
            system.set_settings({"leaving_time": "16:00", "minimum_early_leave_minutes": "0"})
            system.now_local = lambda: datetime(2026, 4, 15, 8, 0, 0)
            for person_id in person_ids:
                system.ensure_attendance(person_id, "arrival")
            code = db.execute(
                "SELECT code FROM early_codes WHERE used_at IS NULL ORDER BY id LIMIT 1"
            ).fetchone()["code"]
            first = system.ensure_attendance(person_ids[0], "early_departure", code, "First use")
            second = system.ensure_attendance(person_ids[1], "early_departure", code, "Replay")
        self.assertEqual(first["record"]["departure_status"], "Left Early")
        self.assertIn("invalid", second["error"].lower())

    def test_early_leave_codes_refresh_endpoint_requires_admin_and_rotates_pool(self):
        self.assertEqual(self.client.get("/api/admin/early-codes").status_code, 401)
        csrf = self.sign_in()
        with system.app.app_context():
            old_codes = {
                row["code"] for row in system.get_db().execute(
                    "SELECT code FROM early_codes WHERE used_at IS NULL"
                ).fetchall()
            }
            system.now_local = lambda: datetime(2026, 4, 15, 8, 16, 0)
            system.get_db().execute(
                "UPDATE early_codes SET created_at=? WHERE used_at IS NULL",
                ("2026-04-15T08:00:00",),
            )
            system.get_db().commit()
        response = self.client.get("/api/admin/early-codes")
        self.assertEqual(response.status_code, 200)
        new_codes = {item["code"] for item in response.get_json()["early_codes"]}
        self.assertEqual(len(new_codes), 5)
        self.assertTrue(old_codes.isdisjoint(new_codes))

    def test_code_rotation_worker_refreshes_pool_without_dashboard_requests(self):
        fixed_now = datetime(2026, 4, 15, 8, 16, 0)
        with system.app.app_context():
            db = system.get_db()
            old_codes = {
                row["code"] for row in db.execute(
                    "SELECT code FROM early_codes WHERE used_at IS NULL"
                ).fetchall()
            }
            db.execute(
                "UPDATE early_codes SET created_at=? WHERE used_at IS NULL",
                ("2026-04-15T08:00:00",),
            )
            db.commit()

        class StopAfterOneCycle:
            def __init__(self):
                self.wait_count = 0

            def wait(self, _seconds):
                self.wait_count += 1
                return self.wait_count > 1

        with patch.object(system, "now_local", return_value=fixed_now):
            system.early_code_rotation_worker(StopAfterOneCycle())
        with system.app.app_context():
            active_codes = {
                row["code"] for row in system.get_db().execute(
                    "SELECT code FROM early_codes WHERE used_at IS NULL"
                ).fetchall()
            }
        self.assertEqual(len(active_codes), 5)
        self.assertTrue(old_codes.isdisjoint(active_codes))

    def test_station_automatically_selects_departure_after_arrival(self):
        admin_csrf = self.sign_in()
        code = self.client.post(
            "/api/admin/devices/code",
            json={},
            headers={"X-CSRFToken": admin_csrf},
        ).get_json()["code"]
        station_csrf = self.csrf("/")
        self.client.post(
            "/api/pair",
            json={"code": code, "device_name": "Automatic mode station"},
            headers={"X-CSRFToken": station_csrf},
        )
        with system.app.app_context():
            db = system.get_db()
            person_id = db.execute(
                "SELECT id FROM people WHERE kind='student' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
            db.execute(
                """INSERT INTO attendance(person_id, attendance_date, attendance_status, arrival_time)
                   VALUES (?, ?, 'Present', '08:00:00')""",
                (person_id, system.current_day()),
            )
            db.commit()

        class FakeRecognizer:
            def predict(self, _face):
                return 0, 10.0

        image = system.np.zeros((200, 200), dtype=system.np.uint8)
        with (
            patch.object(system, "parse_image_data", return_value=image),
            patch.object(system, "extract_face", return_value=(image, b"face")),
            patch.object(system, "recognition_model", return_value=(FakeRecognizer(), {0: person_id})),
        ):
            response = self.client.post(
                "/api/recognize",
                json={"image": "frame"},
                headers={"X-CSRFToken": station_csrf},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["mode"], "departure")

    def test_arrival_is_rejected_before_configured_start(self):
        with system.app.app_context():
            people = system.get_db().execute(
                "SELECT id FROM people WHERE kind='student' ORDER BY id LIMIT 2"
            ).fetchall()
            person_id = people[0]["id"]
            system.now_local = lambda: datetime(2026, 4, 15, 7, 54, 59)
            early = system.ensure_attendance(person_id, "arrival")
            self.assertTrue(early["attendance_not_open"])
            self.assertIn("07:55", early["error"])
            self.assertIsNone(
                system.get_db().execute(
                    "SELECT id FROM attendance WHERE person_id=? AND attendance_date='2026-04-15'",
                    (person_id,),
                ).fetchone()
            )
            system.now_local = lambda: datetime(2026, 4, 15, 7, 55)
            opened = system.ensure_attendance(person_id, "arrival")
            self.assertEqual(opened["record"]["attendance_status"], "Present")
            system.now_local = lambda: datetime(2026, 4, 15, 8, 0)
            other_opened = system.ensure_attendance(people[1]["id"], "arrival")
            self.assertEqual(other_opened["record"]["attendance_status"], "Present")

    def test_admin_corrected_hhmm_time_is_usable_for_later_scan(self):
        csrf = self.sign_in()
        with system.app.app_context():
            system.now_local = lambda: datetime(2026, 4, 15, 8, 0)
            person_id = system.get_db().execute(
                "SELECT id FROM people WHERE kind='student' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
            record = system.ensure_attendance(person_id, "arrival")["record"]
        invalid = self.client.post(
            f"/api/admin/attendance/{record['id']}",
            json={"arrival_time": "99:99"},
            headers={"X-CSRFToken": csrf},
        )
        self.assertEqual(invalid.status_code, 400)
        corrected = self.client.post(
            f"/api/admin/attendance/{record['id']}",
            json={"arrival_time": "08:05"},
            headers={"X-CSRFToken": csrf},
        )
        self.assertEqual(corrected.status_code, 200, corrected.get_data(as_text=True))
        with system.app.app_context():
            system.now_local = lambda: datetime(2026, 4, 15, 15, 0)
            departure = system.ensure_attendance(person_id, "departure")
        self.assertEqual(departure["record"]["departure_status"], "Left")

    def test_attendance_correction_rejects_contradictory_statuses(self):
        csrf = self.sign_in()
        with system.app.app_context():
            system.now_local = lambda: datetime(2026, 4, 15, 8, 0)
            person_id = system.get_db().execute(
                "SELECT id FROM people WHERE kind='student' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
            record = system.ensure_attendance(person_id, "arrival")["record"]
        absent_with_arrival = self.client.post(
            f"/api/admin/attendance/{record['id']}",
            json={"attendance_status": "Absent"},
            headers={"X-CSRFToken": csrf},
        )
        self.assertEqual(absent_with_arrival.status_code, 400)
        no_departure_time = self.client.post(
            f"/api/admin/attendance/{record['id']}",
            json={"departure_status": "Left"},
            headers={"X-CSRFToken": csrf},
        )
        self.assertEqual(no_departure_time.status_code, 400)
        late_departure = self.client.post(
            f"/api/admin/attendance/{record['id']}",
            json={"departure_status": "Left Late", "departure_time": "15:06"},
            headers={"X-CSRFToken": csrf},
        )
        self.assertEqual(late_departure.status_code, 200, late_departure.get_data(as_text=True))

    def test_end_day_rejects_future_dates_and_closure_survives_record_deletion(self):
        csrf = self.sign_in()
        system.now_local = lambda: datetime(2026, 4, 15, 16, 0)
        future = self.client.post(
            "/api/admin/end-day",
            json={"date": "2026-04-16"},
            headers={"X-CSRFToken": csrf},
        )
        self.assertEqual(future.status_code, 400)
        ended = self.client.post(
            "/api/admin/end-day",
            json={"date": "2026-04-15"},
            headers={"X-CSRFToken": csrf},
        )
        self.assertEqual(ended.status_code, 200)
        with system.app.app_context():
            db = system.get_db()
            person_id = db.execute(
                "SELECT id FROM people WHERE kind='student' ORDER BY id LIMIT 1"
            ).fetchone()["id"]
            db.execute(
                "DELETE FROM attendance WHERE person_id=? AND attendance_date='2026-04-15'",
                (person_id,),
            )
            db.commit()
            reopened = system.ensure_attendance(person_id, "arrival")
        self.assertIn("finalized", reopened["error"])

    def test_end_day_rejects_dates_before_reliable_roster_history(self):
        csrf = self.sign_in()
        with system.app.app_context():
            system.get_db().execute(
                "UPDATE settings SET value='2026-04-16' WHERE key='roster_history_start_date'"
            )
            system.get_db().commit()
        system.now_local = lambda: datetime(2026, 4, 15, 16, 0)
        response = self.client.post(
            "/api/admin/end-day",
            json={"date": "2026-04-15"},
            headers={"X-CSRFToken": csrf},
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("predates reliable roster tracking", response.get_json()["error"])
        with system.app.app_context():
            self.assertIsNone(
                system.get_db().execute(
                    "SELECT 1 FROM attendance_days WHERE attendance_date='2026-04-15'"
                ).fetchone()
            )
            system.get_db().execute(
                "DELETE FROM settings WHERE key='roster_history_start_date'"
            )
            system.get_db().commit()
        missing_history = self.client.post(
            "/api/admin/end-day",
            json={"date": "2026-04-15"},
            headers={"X-CSRFToken": csrf},
        )
        self.assertEqual(missing_history.status_code, 409)
        self.assertIn("history is unavailable", missing_history.get_json()["error"].lower())

    def test_end_day_uses_roster_at_arrival_open_and_saved_schedule(self):
        csrf = self.sign_in()
        with system.app.app_context():
            db = system.get_db()
            students = db.execute(
                "SELECT id, name FROM people WHERE kind='student' ORDER BY name"
            ).fetchall()
            late_confirmed_id = students[0]["id"]
            already_deactivated_id = students[1]["id"]
            db.execute(
                "UPDATE people SET confirmed_at=? WHERE id=?",
                (
                    datetime(2026, 4, 15, 8, 0).astimezone().isoformat(),
                    late_confirmed_id,
                ),
            )
            db.execute(
                "UPDATE people SET active=0, deactivated_at=? WHERE id=?",
                (
                    datetime(2026, 4, 15, 7, 50).astimezone().isoformat(),
                    already_deactivated_id,
                ),
            )
            db.commit()
            system.get_active_schedule("2026-04-15")

        system.now_local = lambda: datetime(2026, 4, 15, 16, 0)
        response = self.client.post(
            "/api/admin/end-day",
            json={"date": "2026-04-15"},
            headers={"X-CSRFToken": csrf},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        with system.app.app_context():
            db = system.get_db()
            self.assertIsNone(
                db.execute(
                    "SELECT 1 FROM attendance WHERE person_id=? AND attendance_date='2026-04-15'",
                    (late_confirmed_id,),
                ).fetchone()
            )
            self.assertIsNone(
                db.execute(
                    "SELECT 1 FROM attendance WHERE person_id=? AND attendance_date='2026-04-15'",
                    (already_deactivated_id,),
                ).fetchone()
            )
            remaining_active = students[2]["id"]
            absent = db.execute(
                "SELECT attendance_status FROM attendance WHERE person_id=? AND attendance_date='2026-04-15'",
                (remaining_active,),
            ).fetchone()
            self.assertEqual(absent["attendance_status"], "Absent")

    def test_end_day_rejects_historical_date_without_schedule_snapshot(self):
        csrf = self.sign_in()
        system.now_local = lambda: datetime(2026, 4, 16, 16, 0)
        response = self.client.post(
            "/api/admin/end-day",
            json={"date": "2026-04-15"},
            headers={"X-CSRFToken": csrf},
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("No saved schedule", response.get_json()["error"])

    def test_self_enrollment_creates_pending_student_request(self):
        admin_csrf = self.sign_in()
        saved = self.client.post(
            "/api/admin/settings",
            json={"self_enrollment": True},
            headers={"X-CSRFToken": admin_csrf},
        )
        self.assertEqual(saved.status_code, 200)
        code = self.client.post(
            "/api/admin/devices/code",
            json={},
            headers={"X-CSRFToken": admin_csrf},
        ).get_json()["code"]
        station_csrf = self.csrf("/")
        self.client.post(
            "/api/pair",
            json={"code": code, "device_name": "Enrollment station"},
            headers={"X-CSRFToken": station_csrf},
        )
        with system.app.app_context():
            class_id = system.get_db().execute(
                "SELECT id FROM classes WHERE active=1 ORDER BY id LIMIT 1"
            ).fetchone()["id"]
        images = {"center": "center", "left": "left", "right": "right"}
        with patch.object(system, "parse_enrollment_profiles", return_value={"center": b"face", "left": b"face", "right": b"face"}):
            response = self.client.post(
                "/api/attendance/self-enroll",
                json={
                    "kind": "student",
                    "name": "Pending Student",
                    "student_number": "PENDING-1",
                    "class_id": class_id,
                    "images": images,
                },
                headers={"X-CSRFToken": station_csrf},
            )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        pending = self.client.get("/api/admin/dashboard").get_json()["pending_people"]
        person = next(item for item in pending if item["name"] == "Pending Student")
        with system.app.app_context():
            self.assertIsNone(system.get_person(person["id"]))
        confirmed = self.client.post(
            f"/api/admin/people/{person['id']}/confirm",
            json={},
            headers={"X-CSRFToken": admin_csrf},
        )
        self.assertEqual(confirmed.status_code, 200)
        with system.app.app_context():
            self.assertIsNotNone(system.get_person(person["id"]))

    def test_teacher_can_request_enrollment_when_student_self_enrollment_is_off(self):
        admin_csrf = self.sign_in()
        code = self.client.post(
            "/api/admin/devices/code",
            json={},
            headers={"X-CSRFToken": admin_csrf},
        ).get_json()["code"]
        station_csrf = self.csrf("/")
        self.client.post(
            "/api/pair",
            json={"code": code, "device_name": "Teacher request station"},
            headers={"X-CSRFToken": station_csrf},
        )
        with patch.object(
            system,
            "parse_enrollment_profiles",
            return_value={"center": b"face", "left": b"face", "right": b"face"},
        ):
            response = self.client.post(
                "/api/attendance/self-enroll",
                json={
                    "kind": "teacher",
                    "name": "Pending Teacher",
                    "images": {"center": "center", "left": "left", "right": "right"},
                },
                headers={"X-CSRFToken": station_csrf},
            )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        pending = self.client.get("/api/admin/dashboard").get_json()["pending_people"]
        self.assertTrue(any(item["name"] == "Pending Teacher" for item in pending))

    def test_export_preserves_original_person_details_after_deactivation(self):
        self.sign_in()
        with system.app.app_context():
            db = system.get_db()
            system.now_local = lambda: datetime(2026, 4, 15, 8, 0)
            person = db.execute(
                "SELECT id, name, group_name FROM people WHERE kind='student' ORDER BY id LIMIT 1"
            ).fetchone()
            original_name = person["name"]
            original_group = person["group_name"]
            system.ensure_attendance(person["id"], "arrival")
            db.execute(
                """UPDATE attendance SET departure_status='Left Early', departure_time='08:30:00',
                   early_leave_reason=? WHERE person_id=? AND attendance_date='2026-04-15'""",
                ("=HYPERLINK(\"https://example.invalid\")", person["id"]),
            )
            db.execute(
                "UPDATE people SET name='Updated Name', group_name='Updated Class', active=0 WHERE id=?",
                (person["id"],),
            )
            db.commit()
        report = self.client.get("/api/admin/report.xlsx?date=2026-04-15")
        workbook = load_workbook(io.BytesIO(report.data), read_only=True)
        row = next(
            row for row in workbook[original_group].iter_rows(min_row=2, values_only=True)
            if row[1] == original_name
        )
        self.assertEqual(row[3], original_group)
        workbook = load_workbook(io.BytesIO(report.data), read_only=False)
        reason = workbook[original_group]["I2"]
        self.assertEqual(reason.value, "'=HYPERLINK(\"https://example.invalid\")")
        self.assertEqual(reason.data_type, "s")

    def test_face_recognition_model_is_cached_and_invalidated(self):
        built = (object(), {1: 1})
        with patch.object(system, "_build_recognition_model", return_value=built) as build:
            self.assertIs(system.recognition_model(), built)
            self.assertIs(system.recognition_model(), built)
            self.assertEqual(build.call_count, 1)
            system.invalidate_recognition_model()
            self.assertIs(system.recognition_model(), built)
            self.assertEqual(build.call_count, 2)

    def test_database_migration_backfills_history_and_day_closures(self):
        legacy_schema = system.SCHEMA.replace(
            """    person_name_snapshot TEXT,
    person_kind_snapshot TEXT,
    group_name_snapshot TEXT,
    student_number_snapshot TEXT,
""",
            "",
        ).replace(
            """CREATE TABLE IF NOT EXISTS attendance_days (
    attendance_date TEXT PRIMARY KEY,
    finalized_at TEXT NOT NULL
);
""",
            "",
        )
        legacy_path = Path(self.data_dir.name) / "legacy.sqlite3"
        connection = sqlite3.connect(legacy_path)
        connection.executescript(legacy_schema)
        connection.execute(
            """INSERT INTO people(kind, name, group_name, created_at, updated_at)
               VALUES ('student', 'Legacy Student', 'Old Class', '2026-04-15', '2026-04-15')"""
        )
        person_id = connection.execute("SELECT id FROM people").fetchone()[0]
        connection.execute(
            """INSERT INTO attendance(
                   person_id, attendance_date, attendance_status, arrival_time, finalized
               ) VALUES (?, '2026-04-15', 'Present', '08:00:00', 1)""",
            (person_id,),
        )
        connection.commit()
        connection.close()

        system.DB_PATH = legacy_path
        system.init_db()
        with system.app.app_context():
            row = system.get_db().execute(
                "SELECT person_name_snapshot, group_name_snapshot FROM attendance"
            ).fetchone()
            closed = system.get_db().execute(
                "SELECT attendance_date FROM attendance_days"
            ).fetchone()
        self.assertEqual(row["person_name_snapshot"], "Legacy Student")
        self.assertEqual(row["group_name_snapshot"], "Old Class")
        self.assertEqual(closed["attendance_date"], "2026-04-15")


if __name__ == "__main__":
    unittest.main()
