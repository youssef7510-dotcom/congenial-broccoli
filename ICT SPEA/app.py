import base64
import hashlib
import io
import json
import math
import os
import re
import secrets
import sqlite3
import string
import threading
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

import cv2
import numpy as np
from flask import (
    Flask,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from werkzeug.security import check_password_hash, generate_password_hash


ROOT = Path(__file__).resolve().parent
INSTANCE = Path(os.environ.get("ATTENDANCE_DATA_DIR", ROOT / "instance"))
INSTANCE.mkdir(parents=True, exist_ok=True)
DB_PATH = INSTANCE / "attendance.sqlite3"
FACE_CASCADE_PATH = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
FACE_THRESHOLD = float(os.environ.get("FACE_CONFIDENCE_THRESHOLD", "65"))
MAX_IMAGE_BYTES = 5 * 1024 * 1024
SCHEDULE_KEYS = (
    "attendance_start",
    "attendance_cutoff",
    "leaving_time",
    "flexibility_minutes",
    "minimum_early_leave_minutes",
)
DEVICE_COOKIE = "attendance_device"
DEVICE_CODE_CHARS = string.ascii_uppercase + string.digits
EARLY_CODE_CHARS = string.ascii_uppercase + string.digits
EARLY_CODE_LIFETIME = timedelta(minutes=15)
EARLY_CODE_POOL_SIZE = 5
EARLY_CODE_REFRESH_INTERVAL_SECONDS = 30
FACE_MODEL_CACHE = None
FACE_MODEL_LOCK = threading.Lock()
LOGIN_FAILURE_LIMIT = 5
LOGIN_FAILURE_WINDOW = timedelta(minutes=15)
LOGIN_LOCKOUT_DURATION = timedelta(minutes=15)
DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_urlsafe(32))


def load_secret_key():
    configured = os.environ.get("FLASK_SECRET_KEY")
    if configured:
        return configured
    key_path = INSTANCE / "secret_key"
    if not key_path.exists():
        key_path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        try:
            key_path.chmod(0o600)
        except OSError:
            pass
    return key_path.read_text(encoding="utf-8").strip()


app = Flask(__name__, instance_path=str(INSTANCE))
app.secret_key = load_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
    MAX_CONTENT_LENGTH=MAX_IMAGE_BYTES * 4 + 1024 * 1024,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('student', 'teacher')),
    name TEXT NOT NULL,
    group_name TEXT NOT NULL,
    student_number TEXT,
    class_id INTEGER REFERENCES classes(id),
    face_image BLOB,
    active INTEGER NOT NULL DEFAULT 1,
    confirmed INTEGER NOT NULL DEFAULT 1,
    confirmed_at TEXT,
    deactivated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS classes (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS face_profiles (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES people(id),
    pose TEXT NOT NULL CHECK(pose IN ('center', 'left', 'right')),
    face_image BLOB NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(person_id, pose)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attendance (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES people(id),
    attendance_date TEXT NOT NULL,
    attendance_status TEXT NOT NULL,
    arrival_time TEXT,
    departure_status TEXT,
    departure_time TEXT,
    early_leave_requested_at TEXT,
    early_leave_reason TEXT,
    early_leave_code_result TEXT,
    finalized INTEGER NOT NULL DEFAULT 0,
    correction_note TEXT,
    person_name_snapshot TEXT,
    person_kind_snapshot TEXT,
    group_name_snapshot TEXT,
    student_number_snapshot TEXT,
    UNIQUE(person_id, attendance_date)
);
CREATE TABLE IF NOT EXISTS attendance_days (
    attendance_date TEXT PRIMARY KEY,
    finalized_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedule_snapshots (
    attendance_date TEXT PRIMARY KEY,
    schedule_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    device_type TEXT NOT NULL DEFAULT 'Browser',
    token_hash TEXT NOT NULL UNIQUE,
    client_id_hash TEXT,
    paired_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS device_codes (
    id INTEGER PRIMARY KEY,
    code_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
CREATE TABLE IF NOT EXISTS early_codes (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    used_at TEXT,
    used_by INTEGER REFERENCES people(id)
);
CREATE TABLE IF NOT EXISTS face_tickets (
    token_hash TEXT PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES people(id),
    expires_at TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'arrival',
    challenge_pose TEXT,
    face_verified INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS attendance_date_idx ON attendance(attendance_date);
CREATE INDEX IF NOT EXISTS people_active_idx ON people(active, confirmed, kind);
CREATE TABLE IF NOT EXISTS login_attempts (
    address_hash TEXT PRIMARY KEY,
    failed_attempts INTEGER NOT NULL,
    window_started_at REAL NOT NULL,
    locked_until REAL
);
"""

DEFAULT_SETTINGS = {
    "attendance_start": "",
    "attendance_cutoff": "",
    "flexibility_minutes": "5",
    "minimum_early_leave_minutes": "30",
    "leaving_time": "",
    "self_enrollment": "0",
    "allow_unpaired_devices": "0",
}


def now_local():
    return datetime.now().astimezone()


def stamp(value=None):
    return (value or now_local()).isoformat(timespec="seconds")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA journal_mode = WAL")
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    global FACE_MODEL_CACHE
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(SCHEMA)
    people_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(people)").fetchall()
    }
    if "student_number" not in people_columns:
        db.execute("ALTER TABLE people ADD COLUMN student_number TEXT")
    if "class_id" not in people_columns:
        db.execute("ALTER TABLE people ADD COLUMN class_id INTEGER REFERENCES classes(id)")
    for column in ("confirmed_at", "deactivated_at"):
        if column not in people_columns:
            db.execute(f"ALTER TABLE people ADD COLUMN {column} TEXT")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS active_student_number_idx "
        "ON people(student_number) WHERE kind='student' AND active=1 AND student_number IS NOT NULL"
    )
    device_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(devices)").fetchall()
    }
    if "client_id_hash" not in device_columns:
        db.execute("ALTER TABLE devices ADD COLUMN client_id_hash TEXT")
    db.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS devices_client_id_hash_idx
           ON devices(client_id_hash) WHERE client_id_hash IS NOT NULL"""
    )
    db.execute(
        """INSERT OR IGNORE INTO face_profiles(person_id, pose, face_image, created_at)
           SELECT id, 'center', face_image, updated_at FROM people WHERE face_image IS NOT NULL"""
    )
    attendance_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(attendance)").fetchall()
    }
    if "early_leave_requested_at" not in attendance_columns:
        db.execute("ALTER TABLE attendance ADD COLUMN early_leave_requested_at TEXT")
    for column in (
        "person_name_snapshot",
        "person_kind_snapshot",
        "group_name_snapshot",
        "student_number_snapshot",
    ):
        if column not in attendance_columns:
            db.execute(f"ALTER TABLE attendance ADD COLUMN {column} TEXT")
    db.execute(
        """UPDATE attendance SET
               person_name_snapshot=COALESCE(person_name_snapshot, (SELECT name FROM people WHERE id=attendance.person_id)),
               person_kind_snapshot=COALESCE(person_kind_snapshot, (SELECT kind FROM people WHERE id=attendance.person_id)),
               group_name_snapshot=COALESCE(group_name_snapshot, (SELECT group_name FROM people WHERE id=attendance.person_id)),
               student_number_snapshot=COALESCE(student_number_snapshot, (SELECT student_number FROM people WHERE id=attendance.person_id))
           WHERE person_name_snapshot IS NULL OR person_kind_snapshot IS NULL
              OR group_name_snapshot IS NULL OR student_number_snapshot IS NULL"""
    )
    db.execute(
        """INSERT OR IGNORE INTO attendance_days(attendance_date, finalized_at)
           SELECT attendance_date, ? FROM attendance WHERE finalized=1
           GROUP BY attendance_date""",
        (stamp(),),
    )
    ticket_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(face_tickets)").fetchall()
    }
    if "mode" not in ticket_columns:
        db.execute("ALTER TABLE face_tickets ADD COLUMN mode TEXT NOT NULL DEFAULT 'arrival'")
    if "challenge_pose" not in ticket_columns:
        db.execute("ALTER TABLE face_tickets ADD COLUMN challenge_pose TEXT")
    if "face_verified" not in ticket_columns:
        db.execute(
            "ALTER TABLE face_tickets ADD COLUMN face_verified INTEGER NOT NULL DEFAULT 0"
        )
    username = os.environ.get("ADMIN_USERNAME", "admin")
    password = os.environ.get("ADMIN_PASSWORD", "admin123")
    db.execute(
        "INSERT OR IGNORE INTO admins(username, password_hash) VALUES (?, ?)",
        (username, generate_password_hash(password)),
    )
    db.executemany(
        "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
        DEFAULT_SETTINGS.items(),
    )
    migration_day = (
        now_local().date() + timedelta(days=1)
    ).isoformat()
    db.execute(
        "INSERT OR IGNORE INTO settings(key, value) VALUES ('roster_history_start_date', ?)",
        (migration_day,),
    )
    db.execute(
        """UPDATE people SET confirmed_at=?
           WHERE confirmed=1 AND confirmed_at IS NULL""",
        (stamp(),),
    )
    db.execute(
        """UPDATE people SET deactivated_at=?
           WHERE active=0 AND deactivated_at IS NULL""",
        (stamp(),),
    )
    if db.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 0:
        now = stamp()
        db.executemany(
            """INSERT INTO people(
                   kind, name, group_name, confirmed_at, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            [
                ("student", "Aisha Rahman", "Grade 8", now, now, now),
                ("student", "Omar Hassan", "Grade 8", now, now, now),
                ("student", "Maya Chen", "Grade 9", now, now, now),
                ("teacher", "Mr. Daniel Lee", "Mathematics", now, now, now),
            ],
        )
    db.execute(
        """INSERT OR IGNORE INTO classes(name, created_at, updated_at)
           SELECT DISTINCT group_name, ?, ? FROM people WHERE kind='student' AND active=1""",
        (stamp(), stamp()),
    )
    db.execute(
        """UPDATE people SET class_id=(
               SELECT c.id FROM classes c WHERE c.name=people.group_name COLLATE NOCASE
           )
           WHERE kind='student' AND class_id IS NULL"""
    )
    db.execute(
        "DELETE FROM early_codes WHERE used_at IS NULL AND created_at <= ?",
        (stamp(now_local() - EARLY_CODE_LIFETIME),),
    )
    active_code_count = db.execute(
        "SELECT COUNT(*) FROM early_codes WHERE used_at IS NULL"
    ).fetchone()[0]
    while active_code_count < 5:
        db.execute(
            "INSERT INTO early_codes(code, created_at) VALUES (?, ?)",
            (new_early_code(), stamp()),
        )
        active_code_count += 1
    settings_values = DEFAULT_SETTINGS.copy()
    settings_values.update(
        {
            row["key"]: row["value"]
            for row in db.execute("SELECT key, value FROM settings").fetchall()
        }
    )
    schedule = {key: settings_values[key] for key in SCHEDULE_KEYS}
    if schedule_is_configured(schedule):
        db.execute(
            """INSERT OR IGNORE INTO schedule_snapshots(
                   attendance_date, schedule_json, created_at
               ) VALUES (?, ?, ?)""",
            (now_local().date().isoformat(), json.dumps(schedule), stamp()),
        )
    db.commit()
    db.close()
    with FACE_MODEL_LOCK:
        FACE_MODEL_CACHE = None
    try:
        DB_PATH.chmod(0o600)
    except OSError:
        pass


def get_settings():
    values = DEFAULT_SETTINGS.copy()
    rows = get_db().execute("SELECT key, value FROM settings").fetchall()
    values.update({row["key"]: row["value"] for row in rows})
    return values


def set_settings(values):
    db = get_db()
    db.executemany(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        [(key, str(value)) for key, value in values.items()],
    )
    db.commit()


def csrf_value():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


@app.before_request
def enforce_csrf():
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        supplied = request.headers.get("X-CSRFToken") or request.form.get("csrf_token")
        expected = session.get("csrf_token")
        if not expected or not supplied or not secrets.compare_digest(supplied, expected):
            return jsonify(error="Security token expired. Refresh the page and try again."), 400


def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("admin_id"):
            if request.path.startswith("/api/"):
                return jsonify(error="Administrator sign-in required."), 401
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)

    return wrapped


def device_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        token = request.cookies.get(DEVICE_COOKIE, "")
        if not token:
            return jsonify(error="This browser is not paired. Enter a device code to continue."), 403
        row = get_db().execute(
            "SELECT id FROM devices WHERE token_hash = ? AND active = 1",
            (hashlib.sha256(token.encode()).hexdigest(),),
        ).fetchone()
        if not row:
            return jsonify(error="This device has been disconnected. Pair it again to continue."), 403
        g.device_id = row["id"]
        return fn(*args, **kwargs)

    return wrapped


def api_error(message, status=400):
    return jsonify(error=message), status


def parse_image_data(data):
    if not isinstance(data, str):
        raise ValueError("A camera photo is required.")
    match = re.fullmatch(r"data:image/(?:jpeg|jpg|png);base64,([A-Za-z0-9+/=]+)", data)
    if not match:
        raise ValueError("The camera image format is invalid.")
    try:
        raw = base64.b64decode(match.group(1), validate=True)
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            raise ValueError
        image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError
        return image
    except (ValueError, cv2.error):
        raise ValueError("The camera image could not be read. Please try again.")


def extract_face(image, pose="center"):
    cascade = cv2.CascadeClassifier(FACE_CASCADE_PATH)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if pose == "center":
        faces = cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(80, 80)
        )
    elif pose in {"left", "right"}:
        profile_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_profileface.xml"
        )
        direct_faces = profile_cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=4, minSize=(70, 70)
        )
        mirrored_faces = profile_cascade.detectMultiScale(
            cv2.flip(gray, 1), scaleFactor=1.1, minNeighbors=4, minSize=(70, 70)
        )
        if len(direct_faces) + len(mirrored_faces) > 1:
            raise ValueError(
                "Could not verify a single turned face. Keep only one face in view and turn farther to the requested side."
            )
        expected_faces = mirrored_faces if pose == "left" else direct_faces
        if not len(expected_faces):
            expected_direction = "left (right side of the image)" if pose == "left" else "right (left side of the image)"
            raise ValueError(
                f"The face is not turned to the requested {pose}. Turn your head to your {expected_direction}, use good lighting, and try again."
            )
        if pose == "left":
            x, y, width, height = mirrored_faces[0]
            faces = np.asarray(
                [(gray.shape[1] - x - width, y, width, height)],
                dtype=np.int32,
            )
        else:
            faces = direct_faces
    else:
        raise ValueError("Choose a center, left, or right enrollment photo.")
    if len(faces) == 0:
        prompt = "Look straight at the camera" if pose == "center" else f"Turn your head to your {pose}"
        raise ValueError(f"No {pose} face was detected. {prompt}, use good lighting, and try again.")
    if len(faces) > 1:
        raise ValueError("More than one face is visible. Ensure only the person being enrolled is in view.")
    x, y, width, height = faces[0]
    crop = gray[y : y + height, x : x + width]
    crop = cv2.resize(crop, (200, 200), interpolation=cv2.INTER_AREA)
    crop = cv2.equalizeHist(crop)
    ok, encoded = cv2.imencode(".png", crop)
    if not ok:
        raise ValueError("Could not save the detected face. Please try again.")
    return crop, encoded.tobytes()


def parse_enrollment_profiles(images):
    required_poses = ("center", "left", "right")
    if not isinstance(images, dict) or any(not images.get(pose) for pose in required_poses):
        raise ValueError("Capture all three face images: center, left, and right.")
    profiles = {}
    for pose in required_poses:
        _face, profiles[pose] = extract_face(parse_image_data(images[pose]), pose)
    return profiles


def save_face_profiles(db, person_id, profiles):
    db.execute("DELETE FROM face_profiles WHERE person_id=?", (person_id,))
    db.executemany(
        """INSERT INTO face_profiles(person_id, pose, face_image, created_at)
           VALUES (?, ?, ?, ?)""",
        [(person_id, pose, image, stamp()) for pose, image in profiles.items()],
    )
    db.execute(
        "UPDATE people SET face_image=?, updated_at=? WHERE id=?",
        (profiles["center"], stamp(), person_id),
    )


def invalidate_recognition_model():
    global FACE_MODEL_CACHE
    with FACE_MODEL_LOCK:
        FACE_MODEL_CACHE = None


def recognition_model():
    global FACE_MODEL_CACHE
    with FACE_MODEL_LOCK:
        if FACE_MODEL_CACHE is None:
            FACE_MODEL_CACHE = _build_recognition_model()
        return FACE_MODEL_CACHE


def _build_recognition_model():
    if not hasattr(cv2, "face") or not hasattr(cv2.face, "LBPHFaceRecognizer_create"):
        raise RuntimeError(
            "OpenCV LBPH is unavailable. Install the opencv-contrib-python package "
            "(not opencv-python) in the active environment."
        )
    rows = get_db().execute(
        """SELECT p.id AS person_id, fp.face_image FROM face_profiles fp
           JOIN people p ON p.id=fp.person_id
           WHERE p.active=1 AND p.confirmed=1
           UNION ALL
           SELECT p.id AS person_id, p.face_image FROM people p
           WHERE p.active=1 AND p.confirmed=1 AND p.face_image IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM face_profiles fp WHERE fp.person_id=p.id)"""
    ).fetchall()
    if not rows:
        return None, {}
    faces, training_labels, label_to_person, person_to_label = [], [], {}, {}
    for row in rows:
        image = cv2.imdecode(np.frombuffer(row["face_image"], dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if image is not None:
            faces.append(cv2.resize(image, (200, 200)))
            person_id = row["person_id"]
            label = person_to_label.setdefault(person_id, len(person_to_label))
            label_to_person[label] = person_id
            training_labels.append(label)
    if not faces:
        return None, {}
    model = cv2.face.LBPHFaceRecognizer_create()
    model.train(faces, np.asarray(training_labels, dtype=np.int32))
    return model, label_to_person


def issue_ticket(person_id, mode="arrival", challenge_pose=None, face_verified=False):
    raw = secrets.token_urlsafe(32)
    db = get_db()
    db.execute("DELETE FROM face_tickets WHERE expires_at < ?", (stamp(),))
    db.execute(
        """INSERT INTO face_tickets(token_hash, person_id, expires_at, mode,
           challenge_pose, face_verified) VALUES (?, ?, ?, ?, ?, ?)""",
        (
            hashlib.sha256(raw.encode()).hexdigest(),
            person_id,
            stamp(now_local() + timedelta(minutes=2)),
            mode,
            challenge_pose,
            int(face_verified),
        ),
    )
    db.commit()
    return raw


def current_day():
    return now_local().date().isoformat()


def local_time():
    return now_local().strftime("%H:%M")


def parse_clock_time(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?",
        value,
    ):
        raise ValueError("Time must use HH:MM or HH:MM:SS.")
    formats = ("%H:%M", "%H:%M:%S") if len(value) == 5 else ("%H:%M:%S",)
    for time_format in formats:
        try:
            return datetime.strptime(value, time_format).time()
        except ValueError:
            continue
    raise ValueError("Time must be a valid HH:MM or HH:MM:SS value.")


def schedule_values(settings):
    return {key: settings.get(key, "") for key in SCHEDULE_KEYS}


def schedule_is_configured(schedule):
    return schedule_validation_error(schedule) is None


def schedule_validation_error(schedule):
    try:
        start = parse_clock_time(schedule["attendance_start"])
        cutoff = parse_clock_time(schedule["attendance_cutoff"])
        leaving = parse_clock_time(schedule["leaving_time"])
        flexibility = int(schedule["flexibility_minutes"])
        minimum = int(schedule["minimum_early_leave_minutes"])
        if not 0 <= flexibility <= 180:
            return "Flexibility must be between 0 and 180 minutes."
        if not 0 <= minimum <= 1440:
            return "Minimum time before early departure must be between 0 and 1440 minutes."
        start_minutes = start.hour * 60 + start.minute
        cutoff_minutes = cutoff.hour * 60 + cutoff.minute
        leaving_minutes = leaving.hour * 60 + leaving.minute
        if cutoff_minutes < start_minutes:
            cutoff_minutes += 24 * 60
        while leaving_minutes < cutoff_minutes:
            leaving_minutes += 24 * 60
        if cutoff_minutes < start_minutes or leaving_minutes <= cutoff_minutes:
            return "Departure must be after the attendance cut-off."
        if leaving_minutes - start_minutes >= 24 * 60:
            return "A school schedule must finish within 24 hours of its arrival time."
        return None
    except (KeyError, TypeError, ValueError):
        return "Enter valid arrival, cut-off, and departure times."


def get_active_schedule(attendance_date=None):
    day = attendance_date or current_day()
    db = get_db()
    row = db.execute(
        "SELECT schedule_json FROM schedule_snapshots WHERE attendance_date=?",
        (day,),
    ).fetchone()
    if row:
        schedule = json.loads(row["schedule_json"])
        return {
            "date": day,
            "configured": schedule_is_configured(schedule),
            **schedule,
        }

    pending = schedule_values(get_settings())
    if not schedule_is_configured(pending):
        return {"date": day, "configured": False, **pending}

    owns_transaction = not db.in_transaction
    if owns_transaction:
        db.execute("BEGIN IMMEDIATE")
    db.execute(
        """INSERT OR IGNORE INTO schedule_snapshots(
               attendance_date, schedule_json, created_at
           ) VALUES (?, ?, ?)""",
        (day, json.dumps(pending), stamp()),
    )
    if owns_transaction:
        db.commit()
    row = db.execute(
        "SELECT schedule_json FROM schedule_snapshots WHERE attendance_date=?",
        (day,),
    ).fetchone()
    schedule = json.loads(row["schedule_json"])
    return {
        "date": day,
        "configured": schedule_is_configured(schedule),
        **schedule,
    }


def schedule_snapshot(attendance_date):
    row = get_db().execute(
        "SELECT schedule_json FROM schedule_snapshots WHERE attendance_date=?",
        (attendance_date,),
    ).fetchone()
    if not row:
        return {"date": attendance_date, "configured": False, **schedule_values(get_settings())}
    schedule = json.loads(row["schedule_json"])
    return {
        "date": attendance_date,
        "configured": schedule_is_configured(schedule),
        **schedule,
    }


def schedule_offsets(schedule):
    start = parse_clock_time(schedule["attendance_start"])
    cutoff = parse_clock_time(schedule["attendance_cutoff"])
    leaving = parse_clock_time(schedule["leaving_time"])
    start_minutes = start.hour * 60 + start.minute
    cutoff_minutes = cutoff.hour * 60 + cutoff.minute
    leaving_minutes = leaving.hour * 60 + leaving.minute
    if cutoff_minutes < start_minutes:
        cutoff_minutes += 24 * 60
    while leaving_minutes < cutoff_minutes:
        leaving_minutes += 24 * 60
    return start_minutes, cutoff_minutes, leaving_minutes


def schedule_event_datetime(schedule, attendance_date, event, tzinfo=None):
    offsets = dict(
        zip(
            ("attendance_start", "attendance_cutoff", "leaving_time"),
            schedule_offsets(schedule),
        )
    )
    start = datetime.combine(
        date.fromisoformat(attendance_date),
        parse_clock_time(schedule["attendance_start"]),
    )
    return (
        start + timedelta(minutes=offsets[event] - offsets["attendance_start"])
    ).replace(tzinfo=tzinfo)


def attendance_schedule_context(now=None):
    now = now or now_local()
    today = now.date()
    today_text = today.isoformat()
    active = get_active_schedule(today_text)
    previous_text = (today - timedelta(days=1)).isoformat()
    previous = schedule_snapshot(previous_text)
    if previous["configured"]:
        _, _, previous_departure = schedule_offsets(previous)
        if previous_departure >= 24 * 60:
            next_schedule = active if active["configured"] else previous
            today_start_at = schedule_event_datetime(
                next_schedule, today_text, "attendance_start", now.tzinfo
            ) - timedelta(minutes=int(next_schedule["flexibility_minutes"]))
            if now < today_start_at:
                return previous_text, previous
    return today_text, active


def schedule_status():
    today = current_day()
    active = get_active_schedule(today)
    pending = schedule_values(get_settings())
    has_pending_changes = active["configured"] and any(
        str(active[key]) != str(pending[key]) for key in SCHEDULE_KEYS
    )
    return {
        "active": active,
        "pending": pending,
        "has_pending_changes": has_pending_changes,
        "pending_effective_date": (
            (now_local().date() + timedelta(days=1)).isoformat()
            if active["configured"]
            else today
        ),
    }


def parse_iso_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def timestamp_value(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.timestamp()


def ensure_attendance(person_id, event_mode, code=None, reason=None, transaction_open=False):
    if event_mode in {"departure", "early_departure"} and not transaction_open:
        refresh_early_codes()
    if event_mode not in {"arrival", "departure", "early_departure"}:
        return {"error": "Unsupported attendance event."}
    db = get_db()
    now = now_local()
    attendance_date, settings = attendance_schedule_context(now)
    if not settings["configured"]:
        return {
            "error": "The school schedule has not been configured. Please contact an administrator.",
            "schedule_not_configured": True,
        }
    clock = now.strftime("%H:%M:%S")
    if not transaction_open:
        db.execute("BEGIN IMMEDIATE")
    day = db.execute(
        "SELECT 1 FROM attendance_days WHERE attendance_date=?",
        (attendance_date,),
    ).fetchone()
    if day:
        db.rollback()
        return {"error": "The attendance day has been finalized. Ask an administrator to correct the record."}
    row = db.execute(
        "SELECT * FROM attendance WHERE person_id = ? AND attendance_date = ?",
        (person_id, attendance_date),
    ).fetchone()
    if row and row["finalized"]:
        db.rollback()
        return {"error": "The attendance day has been finalized. Ask an administrator to correct the record."}
    if event_mode == "arrival":
        if row and row["arrival_time"]:
            db.rollback()
            return {"message": "Arrival is already recorded for this school day.", "record": dict(row), "duplicate": True}
        start_at = schedule_event_datetime(
            settings, attendance_date, "attendance_start", now.tzinfo
        )
        arrival_opens_at = start_at - timedelta(minutes=int(settings["flexibility_minutes"]))
        if now < arrival_opens_at:
            db.rollback()
            return {
                "error": (
                    "Arrival opens at "
                    f"{arrival_opens_at.strftime('%H:%M')} including the flexibility period."
                ),
                "attendance_not_open": True,
            }
        grace = int(settings["flexibility_minutes"])
        cutoff_at = schedule_event_datetime(
            settings, attendance_date, "attendance_cutoff", now.tzinfo
        )
        final_arrival_deadline = cutoff_at + timedelta(minutes=grace)
        if now > final_arrival_deadline:
            db.rollback()
            return {
                "error": (
                    "The attendance window has closed. Arrivals are accepted until "
                    f"{final_arrival_deadline.strftime('%H:%M')} including the flexibility period."
                ),
                "attendance_closed": True,
            }
        status = "Late" if now > cutoff_at else "Present"
        if row:
            db.execute(
                """UPDATE attendance SET attendance_status=?, arrival_time=?
                   WHERE id=? AND arrival_time IS NULL""",
                (status, clock, row["id"]),
            )
        else:
            db.execute(
                """INSERT INTO attendance(
                       person_id, attendance_date, attendance_status, arrival_time,
                       person_name_snapshot, person_kind_snapshot, group_name_snapshot,
                       student_number_snapshot
                   )
                   SELECT id, ?, ?, ?, name, kind, group_name, student_number
                   FROM people WHERE id=?""",
                (attendance_date, status, clock, person_id),
            )
        db.commit()
        saved = db.execute(
            "SELECT * FROM attendance WHERE person_id = ? AND attendance_date = ?",
            (person_id, attendance_date),
        ).fetchone()
        return {"message": f"Arrival recorded: {status}.", "record": dict(saved), "duplicate": False}

    if not row or not row["arrival_time"]:
        db.rollback()
        db.rollback()
        return {"error": "No arrival is recorded for this school day. Scan again to check in first."}
    if row["departure_time"]:
        db.rollback()
        return {
            "message": f"Departure is already recorded: {row['departure_status'] or 'Left'}.",
            "record": dict(row),
            "duplicate": True,
            "already_departed": True,
        }
    arrival_clock = parse_clock_time(row["arrival_time"])
    arrival_datetime = datetime.combine(
        date.fromisoformat(attendance_date), arrival_clock
    ).replace(tzinfo=now.tzinfo)
    elapsed_seconds = max(0, int((now - arrival_datetime).total_seconds()))
    leaving_time = parse_clock_time(settings["leaving_time"])
    departure_at = schedule_event_datetime(
        settings, attendance_date, "leaving_time", now.tzinfo
    )
    if event_mode == "early_departure" and now >= departure_at:
        db.rollback()
        return {
            "error": (
                f"The early-leave window closed at {leaving_time.strftime('%H:%M')}. "
                "Scan again to check out normally."
            ),
            "early_departure_closed": True,
        }
    if event_mode == "early_departure":
        minimum_seconds = int(settings["minimum_early_leave_minutes"]) * 60
        remaining_seconds = max(0, minimum_seconds - elapsed_seconds)
        if remaining_seconds:
            db.rollback()
            return {
                "error": (
                    "At least "
                    f"{settings['minimum_early_leave_minutes']} minutes must pass after arrival "
                    "before early departure."
                ),
                "remaining_seconds": remaining_seconds,
                "record": dict(row),
            }
        if not code or not code.strip():
            db.rollback()
            return {"error": "Enter an active early-leave verification code."}
        reason = (reason or "").strip()[:500]
        if not reason:
            db.rollback()
            return {"error": "Enter a reason for leaving early."}
        early = db.execute(
            """SELECT id FROM early_codes
               WHERE code = ? AND used_at IS NULL AND created_at > ?""",
            (
                code.strip().upper(),
                stamp(now - EARLY_CODE_LIFETIME),
            ),
        ).fetchone()
        if not early:
            db.rollback()
            return {"error": "That early-leave code is invalid or has already been used."}
        db.execute(
            """UPDATE attendance SET departure_status='Left Early', departure_time=?,
               early_leave_requested_at=?, early_leave_reason=?, early_leave_code_result='valid'
               WHERE id=? AND departure_time IS NULL""",
            (clock, stamp(), reason, row["id"]),
        )
        if db.execute("SELECT changes()").fetchone()[0] != 1:
            db.rollback()
            return {"error": "A departure is already recorded for today."}
        db.execute(
            "UPDATE early_codes SET used_at = ?, used_by = ? WHERE id = ?",
            (stamp(now), person_id, early["id"]),
        )
        replenish_early_codes(db, now)
        if not transaction_open:
            db.commit()
            refresh_early_codes()
        saved = db.execute("SELECT * FROM attendance WHERE id = ?", (row["id"],)).fetchone()
        return {"message": "Early departure recorded.", "record": dict(saved), "duplicate": False}

    if now < departure_at:
        db.rollback()
        return {
            "error": (
                "Regular check-out opens at "
                f"{departure_at.strftime('%H:%M')}."
            ),
            "checkout_not_open": True,
        }
    grace_end = departure_at + timedelta(minutes=int(settings["flexibility_minutes"]))
    departure_status = "Left Late" if now > grace_end else "Left"
    db.execute(
        """UPDATE attendance SET departure_status=?, departure_time=?
           WHERE id=? AND departure_time IS NULL""",
        (departure_status, clock, row["id"]),
    )
    if db.execute("SELECT changes()").fetchone()[0] != 1:
        db.rollback()
        return {"error": "A departure is already recorded for today."}
    db.commit()
    saved = db.execute(
        "SELECT * FROM attendance WHERE person_id = ? AND attendance_date = ?",
        (person_id, attendance_date),
    ).fetchone()
    message = (
        "Late departure recorded for administrator review."
        if departure_status == "Left Late"
        else "Departure recorded: Left."
    )
    return {
        "message": message,
        "record": dict(saved),
        "duplicate": False,
        "late_departure": departure_status == "Left Late",
    }


def new_early_code():
    return "".join(secrets.choice(EARLY_CODE_CHARS) for _ in range(8))


def replenish_early_codes(db, now):
    expired_before = stamp(now - EARLY_CODE_LIFETIME)
    db.execute(
        "DELETE FROM early_codes WHERE used_at IS NULL AND created_at <= ?",
        (expired_before,),
    )
    count = db.execute(
        "SELECT COUNT(*) FROM early_codes WHERE used_at IS NULL"
    ).fetchone()[0]
    while count < EARLY_CODE_POOL_SIZE:
        db.execute(
            "INSERT INTO early_codes(code, created_at) VALUES (?, ?)",
            (new_early_code(), stamp(now)),
        )
        count += 1


def refresh_early_codes():
    db = get_db()
    now = now_local()
    db.execute("BEGIN IMMEDIATE")
    replenish_early_codes(db, now)
    db.commit()


def early_code_rotation_worker(stop_event):
    while not stop_event.wait(EARLY_CODE_REFRESH_INTERVAL_SECONDS):
        try:
            with app.app_context():
                refresh_early_codes()
        except sqlite3.Error:
            app.logger.exception("Early-leave code rotation failed")


def start_early_code_rotation():
    stop_event = threading.Event()
    worker = threading.Thread(
        target=early_code_rotation_worker,
        args=(stop_event,),
        name="early-leave-code-rotation",
        daemon=True,
    )
    worker.start()
    return stop_event, worker


def validate_host_credentials(host):
    if host in {"127.0.0.1", "localhost", "::1"}:
        return
    db = sqlite3.connect(DB_PATH)
    try:
        password_hashes = db.execute("SELECT password_hash FROM admins").fetchall()
    finally:
        db.close()
    if any(check_password_hash(row[0], "admin123") for row in password_hashes):
        raise RuntimeError(
            "Refusing to expose the server on a network with the demo admin password. "
            "Sign in locally, change the administrator password, then restart."
        )


def person_json(row):
    return {
        "id": row["id"],
        "kind": row["kind"],
        "name": row["name"],
        "group_name": row["group_name"],
        "student_number": row["student_number"],
        "class_id": row["class_id"],
        "active": bool(row["active"]),
        "confirmed": bool(row["confirmed"]),
        "has_face": row["face_image"] is not None,
    }


def get_person(person_id):
    return get_db().execute(
        "SELECT * FROM people WHERE id = ? AND active = 1 AND confirmed = 1",
        (person_id,),
    ).fetchone()


@app.get("/")
def attendance_page():
    return render_template("attendance.html", csrf_token=csrf_value())


@app.get("/admin")
def admin_login():
    if session.get("admin_id"):
        return redirect(url_for("admin_dashboard"))
    return render_template("login.html", csrf_token=csrf_value())


@app.get("/admin/dashboard")
@admin_required
def admin_dashboard():
    return render_template("admin.html", csrf_token=csrf_value())


def login_address_hash():
    address = request.remote_addr or "unknown"
    return hashlib.sha256(f"{app.secret_key}:{address}".encode()).hexdigest()


def login_lockout_remaining(address_hash, now=None):
    now = now or now_local().timestamp()
    row = get_db().execute(
        "SELECT locked_until FROM login_attempts WHERE address_hash=?",
        (address_hash,),
    ).fetchone()
    locked_until = float(row["locked_until"]) if row and row["locked_until"] is not None else None
    if locked_until is None or locked_until <= now:
        return 0
    return math.ceil(locked_until - now)


def record_login_failure(address_hash, now=None):
    now = now or now_local().timestamp()
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    row = db.execute(
        """SELECT failed_attempts, window_started_at, locked_until
           FROM login_attempts WHERE address_hash=?""",
        (address_hash,),
    ).fetchone()
    window_start = float(row["window_started_at"]) if row else None
    if (
        window_start is None
        or now - window_start >= LOGIN_FAILURE_WINDOW.total_seconds()
    ):
        failures = 1
        window_start = now
        locked_until = None
    else:
        existing_lockout = (
            float(row["locked_until"])
            if row["locked_until"] is not None
            else None
        )
        if existing_lockout is not None and existing_lockout > now:
            db.rollback()
            return math.ceil(existing_lockout - now)
        failures = row["failed_attempts"] + 1
        locked_until = (
            now + LOGIN_LOCKOUT_DURATION.total_seconds()
            if failures >= LOGIN_FAILURE_LIMIT
            else None
        )
    db.execute(
        """INSERT INTO login_attempts(
               address_hash, failed_attempts, window_started_at, locked_until
           ) VALUES (?, ?, ?, ?)
           ON CONFLICT(address_hash) DO UPDATE SET
               failed_attempts=excluded.failed_attempts,
               window_started_at=excluded.window_started_at,
               locked_until=excluded.locked_until""",
        (address_hash, failures, window_start, locked_until),
    )
    db.commit()
    return math.ceil(locked_until - now) if locked_until else 0


@app.post("/api/admin/login")
def login_api():
    address_hash = login_address_hash()
    retry_after = login_lockout_remaining(address_hash)
    if retry_after:
        response, status = api_error(
            "Too many sign-in attempts. Try again in 15 minutes.", 429
        )
        response.headers["Retry-After"] = str(retry_after)
        return response, status
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    admin = get_db().execute(
        "SELECT id, password_hash FROM admins WHERE username = ?", (username,)
    ).fetchone()
    password_valid = check_password_hash(
        admin["password_hash"] if admin else DUMMY_PASSWORD_HASH,
        password,
    )
    if not admin or not password_valid:
        retry_after = record_login_failure(address_hash)
        if retry_after:
            response, status = api_error(
                "Too many sign-in attempts. Try again in 15 minutes.", 429
            )
            response.headers["Retry-After"] = str(retry_after)
            return response, status
        return api_error("The username or password is incorrect.", 401)
    get_db().execute("DELETE FROM login_attempts WHERE address_hash=?", (address_hash,))
    get_db().commit()
    session.clear()
    session["admin_id"] = admin["id"]
    csrf_value()
    return jsonify(ok=True, redirect=url_for("admin_dashboard"))


@app.post("/api/admin/logout")
@admin_required
def logout_api():
    session.clear()
    return jsonify(ok=True)


@app.post("/api/admin/password")
@admin_required
def change_admin_password_api():
    data = request.get_json(silent=True) or {}
    current_password = str(data.get("current_password", ""))
    new_password = str(data.get("new_password", ""))
    confirmation = str(data.get("confirmation", ""))
    admin = get_db().execute(
        "SELECT id, password_hash FROM admins WHERE id=?",
        (session["admin_id"],),
    ).fetchone()
    if not admin or not check_password_hash(admin["password_hash"], current_password):
        return api_error("The current password is incorrect.", 400)
    if len(new_password) < 12:
        return api_error("Use a new password with at least 12 characters.", 400)
    if new_password != confirmation:
        return api_error("The new password and confirmation do not match.", 400)
    get_db().execute(
        "UPDATE admins SET password_hash=? WHERE id=?",
        (generate_password_hash(new_password), admin["id"]),
    )
    get_db().commit()
    session.clear()
    return jsonify(ok=True, message="Password changed. Sign in with your new password.")


@app.get("/api/admin/dashboard")
@admin_required
def dashboard_api():
    refresh_early_codes()
    db = get_db()
    rows = db.execute("SELECT * FROM people ORDER BY kind, group_name, name").fetchall()
    classes = db.execute(
        "SELECT id, name, active FROM classes ORDER BY active DESC, name COLLATE NOCASE"
    ).fetchall()
    attendance = db.execute(
        """SELECT a.*,
                  COALESCE(a.person_name_snapshot, p.name) AS name,
                  COALESCE(a.person_kind_snapshot, p.kind) AS kind,
                  COALESCE(a.group_name_snapshot, p.group_name) AS group_name,
                  COALESCE(a.student_number_snapshot, p.student_number) AS student_number
           FROM attendance a
           JOIN people p ON p.id = a.person_id
           WHERE a.attendance_date = ? ORDER BY p.kind, p.group_name, p.name""",
        (current_day(),),
    ).fetchall()
    devices = db.execute(
        "SELECT id, name, device_type, paired_at FROM devices WHERE active = 1 ORDER BY paired_at DESC"
    ).fetchall()
    codes = db.execute(
        "SELECT code, created_at FROM early_codes WHERE used_at IS NULL ORDER BY id"
    ).fetchall()
    return jsonify(
        date=current_day(),
        settings=get_settings(),
        schedule_state=schedule_status(),
        people=[person_json(row) for row in rows],
        classes=[dict(row) for row in classes],
        attendance=[dict(row) for row in attendance],
        devices=[dict(row) for row in devices],
        early_codes=[dict(row) for row in codes],
        pending_people=[dict(row) for row in db.execute(
            "SELECT id, kind, name, group_name FROM people WHERE confirmed=0 AND active=1"
        ).fetchall()],
    )


@app.get("/api/admin/schedule")
@admin_required
def admin_schedule_api():
    return jsonify(date=current_day(), schedule_state=schedule_status())


@app.post("/api/admin/classes")
@admin_required
def create_class_api():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    if not name or len(name) > 100:
        return api_error("Enter a class name of 1 to 100 characters.")
    db = get_db()
    try:
        cursor = db.execute(
            "INSERT INTO classes(name, created_at, updated_at) VALUES (?, ?, ?)",
            (name, stamp(), stamp()),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return api_error("A class with this name already exists.")
    return jsonify(ok=True, class_id=cursor.lastrowid, name=name)


@app.post("/api/admin/classes/<int:class_id>")
@admin_required
def update_class_api(class_id):
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    if not name or len(name) > 100:
        return api_error("Enter a class name of 1 to 100 characters.")
    db = get_db()
    existing = db.execute(
        "SELECT id FROM classes WHERE id=? AND active=1", (class_id,)
    ).fetchone()
    if not existing:
        return api_error("Class not found.", 404)
    try:
        db.execute(
            "UPDATE classes SET name=?, updated_at=? WHERE id=?",
            (name, stamp(), class_id),
        )
    except sqlite3.IntegrityError:
        return api_error("A class with this name already exists.")
    db.execute(
        "UPDATE people SET group_name=?, updated_at=? WHERE kind='student' AND class_id=?",
        (name, stamp(), class_id),
    )
    db.commit()
    return jsonify(ok=True)


@app.delete("/api/admin/classes/<int:class_id>")
@admin_required
def delete_class_api(class_id):
    db = get_db()
    existing = db.execute(
        "SELECT id FROM classes WHERE id=? AND active=1", (class_id,)
    ).fetchone()
    if not existing:
        return api_error("Class not found.", 404)
    assigned = db.execute(
        "SELECT COUNT(*) FROM people WHERE kind='student' AND class_id=?",
        (class_id,),
    ).fetchone()[0]
    if assigned:
        db.execute(
            "UPDATE classes SET active=0, updated_at=? WHERE id=?",
            (stamp(), class_id),
        )
        db.commit()
        return jsonify(
            ok=True,
            archived=True,
            message="Class archived because student records are assigned to it. Move those students before permanently deleting it.",
        )
    db.execute("DELETE FROM classes WHERE id=?", (class_id,))
    db.commit()
    return jsonify(ok=True, archived=False)


@app.post("/api/admin/classes/<int:class_id>/restore")
@admin_required
def restore_class_api(class_id):
    cursor = get_db().execute(
        "UPDATE classes SET active=1, updated_at=? WHERE id=?",
        (stamp(), class_id),
    )
    get_db().commit()
    if not cursor.rowcount:
        return api_error("Class not found.", 404)
    return jsonify(ok=True)


@app.get("/api/admin/early-codes")
@admin_required
def early_codes_api():
    refresh_early_codes()
    codes = get_db().execute(
        "SELECT code, created_at FROM early_codes WHERE used_at IS NULL ORDER BY id"
    ).fetchall()
    return jsonify(early_codes=[dict(row) for row in codes])


@app.post("/api/admin/settings")
@admin_required
def save_settings_api():
    data = request.get_json(silent=True) or {}
    allowed = {
        "attendance_start",
        "attendance_cutoff",
        "flexibility_minutes",
        "minimum_early_leave_minutes",
        "leaving_time",
        "self_enrollment",
        "allow_unpaired_devices",
    }
    values = {key: data[key] for key in allowed if key in data}
    for key in ("attendance_start", "attendance_cutoff", "leaving_time"):
        if key in values and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(values[key])):
            return api_error(f"{key.replace('_', ' ').title()} must be a valid 24-hour time.")
    if "flexibility_minutes" in values:
        try:
            grace = int(values["flexibility_minutes"])
            if not 0 <= grace <= 180:
                raise ValueError
        except (ValueError, TypeError):
            return api_error("Flexibility must be between 0 and 180 minutes.")
    if "minimum_early_leave_minutes" in values:
        try:
            minimum = int(values["minimum_early_leave_minutes"])
            if not 0 <= minimum <= 1440:
                raise ValueError
        except (ValueError, TypeError):
            return api_error("Minimum time before early departure must be between 0 and 1440 minutes.")
    schedule_changed = bool(set(values) & set(SCHEDULE_KEYS))
    active_before = get_active_schedule()
    effective_settings = get_settings()
    effective_settings.update({key: str(value) for key, value in values.items()})
    candidate_schedule = schedule_values(effective_settings)
    if schedule_changed and not schedule_is_configured(candidate_schedule):
        return api_error(schedule_validation_error(candidate_schedule))
    for key in ("self_enrollment", "allow_unpaired_devices"):
        if key in values:
            values[key] = "1" if values[key] in (True, 1, "1", "true") else "0"
    set_settings(values)
    if schedule_changed and not active_before["configured"]:
        get_active_schedule()
    return jsonify(
        ok=True,
        settings=get_settings(),
        schedule_state=schedule_status(),
        schedule_activated_today=schedule_changed and not active_before["configured"],
    )


@app.post("/api/admin/people")
@admin_required
def create_person_api():
    data = request.get_json(silent=True) or {}
    kind = str(data.get("kind", "")).lower()
    name = str(data.get("name", "")).strip()
    if kind not in {"student", "teacher"} or not name:
        return api_error("Choose Student or Teacher and enter the person's full name.")
    if len(name) > 100:
        return api_error("Name must be 100 characters or fewer.")
    student_number = None
    class_id = None
    group_name = "Teachers"
    if kind == "student":
        student_number = str(data.get("student_number", "")).strip()
        try:
            class_id = int(data.get("class_id"))
        except (TypeError, ValueError):
            return api_error("Select a class created by the administrator.")
        if not student_number or len(student_number) > 50:
            return api_error("Enter the student's ID or number (50 characters maximum).")
        selected_class = get_db().execute(
            "SELECT name FROM classes WHERE id=? AND active=1",
            (class_id,),
        ).fetchone()
        if not selected_class:
            return api_error("That class is unavailable. Select an active class created by the administrator.")
        group_name = selected_class["name"]
    try:
        profiles = parse_enrollment_profiles(data.get("images"))
    except ValueError as exc:
        return api_error(str(exc))
    db = get_db()
    if kind == "student":
        duplicate_number = db.execute(
            "SELECT id FROM people WHERE kind='student' AND active=1 AND student_number=?",
            (student_number,),
        ).fetchone()
        if duplicate_number:
            return api_error("That student ID/number is already assigned to an active student.")
        duplicate = db.execute(
            "SELECT id FROM people WHERE active=1 AND kind='student' AND lower(name)=lower(?) AND class_id=?",
            (name, class_id),
        ).fetchone()
        if duplicate:
            return api_error("A student with this name is already in that class.")
    else:
        duplicate = db.execute(
            """SELECT id FROM people WHERE active=1 AND kind='teacher'
               AND lower(name)=lower(?)""",
            (name,),
        ).fetchone()
        if duplicate:
            return api_error("A teacher with this name already exists.")
    now = stamp()
    cursor = db.execute(
        """INSERT INTO people(kind, name, group_name, student_number, class_id, face_image,
           confirmed_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (kind, name, group_name, student_number, class_id, profiles["center"], now, now, now),
    )
    save_face_profiles(db, cursor.lastrowid, profiles)
    db.commit()
    invalidate_recognition_model()
    return jsonify(ok=True, person_id=cursor.lastrowid)


@app.post("/api/admin/people/<int:person_id>")
@admin_required
def update_person_api(person_id):
    data = request.get_json(silent=True) or {}
    db = get_db()
    existing = db.execute("SELECT * FROM people WHERE id=? AND active=1", (person_id,)).fetchone()
    if not existing:
        return api_error("Person not found.", 404)
    name = str(data.get("name", existing["name"])).strip()
    if not name or len(name) > 100:
        return api_error("Enter a valid full name (100 characters maximum).")
    student_number = None
    class_id = None
    group_name = "Teachers"
    if existing["kind"] == "student":
        student_number = str(data.get("student_number", existing["student_number"] or "")).strip()
        try:
            class_id = int(data.get("class_id", existing["class_id"]))
        except (TypeError, ValueError):
            return api_error("Select a class created by the administrator.")
        if not student_number or len(student_number) > 50:
            return api_error("Enter the student's ID or number (50 characters maximum).")
        selected_class = db.execute(
            "SELECT name FROM classes WHERE id=? AND active=1",
            (class_id,),
        ).fetchone()
        if not selected_class:
            return api_error("That class is unavailable. Select an active class created by the administrator.")
        group_name = selected_class["name"]
        duplicate_number = db.execute(
            """SELECT id FROM people WHERE kind='student' AND active=1 AND id<>?
               AND student_number=?""",
            (person_id, student_number),
        ).fetchone()
        if duplicate_number:
            return api_error("That student ID/number is already assigned to another active student.")
        duplicate = db.execute(
            """SELECT id FROM people WHERE active=1 AND kind='student' AND id<>?
               AND lower(name)=lower(?) AND class_id=?""",
            (person_id, name, class_id),
        ).fetchone()
        if duplicate:
            return api_error("A student with this name is already in that class.")
    else:
        duplicate = db.execute(
            """SELECT id FROM people WHERE active=1 AND kind='teacher' AND id<>?
               AND lower(name)=lower(?)""",
            (person_id, name),
        ).fetchone()
        if duplicate:
            return api_error("A teacher with this name already exists.")
    profiles = None
    if "images" in data:
        try:
            profiles = parse_enrollment_profiles(data["images"])
        except ValueError as exc:
            return api_error(str(exc))
    db.execute(
        """UPDATE people SET name=?, group_name=?, student_number=?, class_id=?,
           updated_at=? WHERE id=?""",
        (name, group_name, student_number, class_id, stamp(), person_id),
    )
    if profiles:
        save_face_profiles(db, person_id, profiles)
    db.commit()
    if profiles:
        invalidate_recognition_model()
    return jsonify(ok=True)


@app.delete("/api/admin/people/<int:person_id>")
@admin_required
def remove_person_api(person_id):
    db = get_db()
    changed_at = stamp()
    cursor = db.execute(
        "UPDATE people SET active=0, deactivated_at=?, updated_at=? WHERE id=? AND active=1",
        (changed_at, changed_at, person_id),
    )
    db.commit()
    if not cursor.rowcount:
        return api_error("Person not found.", 404)
    invalidate_recognition_model()
    return jsonify(ok=True)


@app.post("/api/admin/people/<int:person_id>/confirm")
@admin_required
def confirm_teacher_api(person_id):
    db = get_db()
    cursor = db.execute(
        """UPDATE people SET confirmed=1, confirmed_at=?, updated_at=?
           WHERE id=? AND confirmed=0 AND active=1""",
        (stamp(), stamp(), person_id),
    )
    db.commit()
    if not cursor.rowcount:
        return api_error("Pending enrollment was not found.", 404)
    invalidate_recognition_model()
    return jsonify(ok=True)


@app.post("/api/admin/devices/code")
@admin_required
def generate_device_code_api():
    code = "".join(secrets.choice(DEVICE_CODE_CHARS) for _ in range(8))
    db = get_db()
    db.execute("UPDATE device_codes SET used_at=? WHERE used_at IS NULL", (stamp(),))
    db.execute(
        "INSERT INTO device_codes(code_hash, created_at, expires_at) VALUES (?, ?, ?)",
        (
            hashlib.sha256(code.encode()).hexdigest(),
            stamp(),
            stamp(now_local() + timedelta(minutes=30)),
        ),
    )
    db.commit()
    return jsonify(code=code, expires_in_minutes=30)


@app.post("/api/pair")
def pair_device_api():
    data = request.get_json(silent=True) or {}
    code = str(data.get("code", "")).strip().upper()
    name = str(data.get("device_name", "")).strip()[:100] or "Attendance browser"
    client_id = str(data.get("client_id", ""))
    if client_id and not re.fullmatch(r"[a-f0-9]{32}", client_id):
        return api_error("This browser could not be identified. Refresh and try again.")
    client_id_hash = hashlib.sha256(client_id.encode()).hexdigest() if client_id else None
    if not re.fullmatch(r"[A-Z0-9]{8}", code):
        return api_error("Enter the 8-character device code shown by the administrator.")
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    row = db.execute(
        "SELECT id FROM device_codes WHERE code_hash=? AND used_at IS NULL AND expires_at>=?",
        (hashlib.sha256(code.encode()).hexdigest(), stamp()),
    ).fetchone()
    if not row:
        db.rollback()
        return api_error("That device code is invalid, expired, or already used.")
    db.execute("UPDATE device_codes SET used_at=? WHERE id=?", (stamp(), row["id"]))
    token = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    existing_device = None
    if client_id_hash:
        existing_device = db.execute(
            "SELECT id FROM devices WHERE client_id_hash=?",
            (client_id_hash,),
        ).fetchone()
    if existing_device:
        db.execute(
            """UPDATE devices SET name=?, token_hash=?, paired_at=?, active=1
               WHERE id=?""",
            (name, token_hash, stamp(), existing_device["id"]),
        )
    else:
        db.execute(
            """INSERT INTO devices(name, token_hash, client_id_hash, paired_at)
               VALUES (?, ?, ?, ?)""",
            (name, token_hash, client_id_hash, stamp()),
        )
    db.commit()
    response = jsonify(ok=True)
    response.set_cookie(
        DEVICE_COOKIE,
        token,
        max_age=60 * 60 * 24 * 365,
        httponly=True,
        secure=app.config["SESSION_COOKIE_SECURE"],
        samesite="Strict",
        path="/",
    )
    return response


@app.delete("/api/admin/devices/<int:device_id>")
@admin_required
def disconnect_device_api(device_id):
    cursor = get_db().execute("UPDATE devices SET active=0 WHERE id=?", (device_id,))
    get_db().commit()
    if not cursor.rowcount:
        return api_error("Device not found.", 404)
    return jsonify(ok=True)


@app.get("/api/attendance/state")
def attendance_state_api():
    token = request.cookies.get(DEVICE_COOKIE, "")
    db = get_db()
    device = None
    response_token = None
    if token:
        device = db.execute(
            "SELECT id, active FROM devices WHERE token_hash=?",
            (hashlib.sha256(token.encode()).hexdigest(),),
        ).fetchone()
        if device and not device["active"]:
            device = None
    client_id = request.args.get("client_id", "")
    client_id_hash = (
        hashlib.sha256(client_id.encode()).hexdigest()
        if re.fullmatch(r"[a-f0-9]{32}", client_id)
        else None
    )
    client_device = None
    if not device and client_id_hash:
        client_device = db.execute(
            "SELECT id, active FROM devices WHERE client_id_hash=?",
            (client_id_hash,),
        ).fetchone()
        if client_device and client_device["active"]:
            response_token = secrets.token_urlsafe(48)
            db.execute(
                "UPDATE devices SET token_hash=?, paired_at=? WHERE id=?",
                (
                    hashlib.sha256(response_token.encode()).hexdigest(),
                    stamp(),
                    client_device["id"],
                ),
            )
            db.commit()
            device = client_device
    settings = get_settings()
    _attendance_date, active_schedule = attendance_schedule_context()
    classes = db.execute(
        "SELECT id, name FROM classes WHERE active=1 ORDER BY name COLLATE NOCASE"
    ).fetchall()
    if (
        not device
        and not (client_device and not client_device["active"])
        and settings["allow_unpaired_devices"] == "1"
    ):
        db.execute("BEGIN IMMEDIATE")
        response_token = secrets.token_urlsafe(48)
        db.execute(
            """INSERT INTO devices(name, device_type, token_hash, client_id_hash, paired_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                "Automatically connected browser",
                "No-code browser",
                hashlib.sha256(response_token.encode()).hexdigest(),
                client_id_hash,
                stamp(),
            ),
        )
        db.commit()
        device = True
    response = jsonify(
        paired=bool(device),
        self_enrollment=settings["self_enrollment"] == "1",
        allow_unpaired_devices=settings["allow_unpaired_devices"] == "1",
        classes=[dict(row) for row in classes],
        schedule={
            **active_schedule,
        },
    )
    if response_token:
        response.set_cookie(
            DEVICE_COOKIE,
            response_token,
            max_age=60 * 60 * 24 * 365,
            httponly=True,
            secure=app.config["SESSION_COOKIE_SECURE"],
            samesite="Strict",
            path="/",
        )
    return response


@app.post("/api/recognize")
@device_required
def recognize_api():
    data = request.get_json(silent=True) or {}
    try:
        image = parse_image_data(data.get("image"))
        _face, face_blob = extract_face(image)
        face = cv2.imdecode(np.frombuffer(face_blob, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        model, labels = recognition_model()
    except ValueError as exc:
        return jsonify(recognized=False, error=str(exc))
    except (RuntimeError, cv2.error) as exc:
        app.logger.exception("Face recognition is unavailable")
        return api_error(str(exc), 503)
    if model is None:
        return jsonify(
            recognized=False,
            error="No confirmed face profiles are enrolled yet. Ask an administrator to add one.",
        )
    label, confidence = model.predict(face)
    person_id = labels.get(label)
    if person_id is None or confidence > FACE_THRESHOLD:
        return jsonify(
            recognized=False,
            error="Face not recognized with enough confidence. Keep your face centered and try again.",
            confidence=round(float(confidence), 1),
        )
    person = get_person(person_id)
    if not person:
        return jsonify(recognized=False, error="That person is no longer active.")
    now = now_local()
    attendance_date, settings = attendance_schedule_context(now)
    if not settings["configured"]:
        return jsonify(
            recognized=True,
            schedule_unconfigured=True,
            error="The school schedule has not been configured. Please contact an administrator.",
            person={
                "id": person["id"],
                "kind": person["kind"],
                "name": person["name"],
                "group_name": person["group_name"],
            },
            confidence=round(float(confidence), 1),
        )
    stored_row = get_db().execute(
        "SELECT departure_time, finalized FROM attendance WHERE person_id=? AND attendance_date=?",
        (person_id, attendance_date),
    ).fetchone()
    if not stored_row or (not stored_row["departure_time"] and not stored_row["finalized"]):
        result = ensure_attendance(person_id, "arrival")
        if result.get("error"):
            if result.get("attendance_closed"):
                return jsonify(
                    recognized=True,
                    attendance_closed=True,
                    error=result["error"],
                    person={
                        "id": person["id"],
                        "kind": person["kind"],
                        "name": person["name"],
                        "group_name": person["group_name"],
                    },
                    confidence=round(float(confidence), 1),
                )
            return api_error(result["error"], 409)
    else:
        result = {"duplicate": True}
    attendance_row = get_db().execute(
        """SELECT * FROM attendance WHERE person_id=? AND attendance_date=?""",
        (person_id, attendance_date),
    ).fetchone()
    first_arrival = not result.get("duplicate", False)
    mode = "arrival" if first_arrival else "departure"
    leaving = parse_clock_time(settings["leaving_time"])
    leaving_datetime = schedule_event_datetime(
        settings, attendance_date, "leaving_time", now.tzinfo
    )
    departure_grace_ends_at = leaving_datetime + timedelta(
        minutes=int(settings["flexibility_minutes"])
    )
    early_leave_closes_in_seconds = max(
        0,
        math.ceil((leaving_datetime - now).total_seconds()),
    )
    late_departure_remaining_seconds = max(
        0,
        math.ceil((departure_grace_ends_at - now).total_seconds()),
    )
    if leaving_datetime <= now <= departure_grace_ends_at:
        late_departure_remaining_seconds = max(1, late_departure_remaining_seconds)
    early_remaining_seconds = 0
    if attendance_row["arrival_time"]:
        arrival_clock = parse_clock_time(attendance_row["arrival_time"])
        arrival_datetime = datetime.combine(
            date.fromisoformat(attendance_date), arrival_clock
        ).replace(tzinfo=now.tzinfo)
        elapsed_seconds = max(0, int((now - arrival_datetime).total_seconds()))
        early_remaining_seconds = max(
            0,
            int(settings["minimum_early_leave_minutes"]) * 60 - elapsed_seconds,
        )
    else:
        early_remaining_seconds = 0 if attendance_row["finalized"] else (
            int(settings["minimum_early_leave_minutes"]) * 60
        )
    if data.get("confirm_checkout") is True:
        if now < leaving_datetime:
            return api_error(
                f"Regular check-out opens at {leaving_datetime.strftime('%H:%M')}.",
                409,
            )
        result = ensure_attendance(person_id, "departure")
        if result.get("error"):
            return api_error(result["error"], 409)
        attendance_row = get_db().execute(
            "SELECT * FROM attendance WHERE person_id=? AND attendance_date=?",
            (person_id, attendance_date),
        ).fetchone()
    early_available = (
        not attendance_row["departure_time"]
        and not attendance_row["finalized"]
        and now < leaving_datetime
        and early_remaining_seconds == 0
    )
    requested_early_departure = data.get("confirm_early_departure") is True
    ticket = (
        issue_ticket(person_id, mode="departure", face_verified=True)
        if early_available and requested_early_departure
        else None
    )
    departed = bool(attendance_row["departure_time"] or attendance_row["finalized"])
    final_status = attendance_row["departure_status"] or attendance_row["attendance_status"] if departed else None
    return jsonify(
        recognized=True,
        person={
            "id": person["id"],
            "kind": person["kind"],
            "name": person["name"],
            "group_name": person["group_name"],
        },
        confidence=round(float(confidence), 1),
        ticket=ticket,
        mode=mode,
        first_arrival=first_arrival,
        remaining_seconds=early_remaining_seconds if now < leaving_datetime else 0,
        early_leave_closes_in_seconds=early_leave_closes_in_seconds,
        late_departure_remaining_seconds=late_departure_remaining_seconds,
        late_departure=now > departure_grace_ends_at,
        leaving_time=leaving.strftime("%H:%M"),
        flexibility_minutes=int(settings["flexibility_minutes"]),
        minimum_early_leave_minutes=int(settings["minimum_early_leave_minutes"]),
        early_available=early_available,
        already_departed=departed,
        final_status=final_status,
        day_finalized=bool(attendance_row["finalized"]),
        current_status={
            "attendance": attendance_row["attendance_status"] if attendance_row else "Not recorded",
            "arrival_time": attendance_row["arrival_time"] if attendance_row else None,
            "departure": attendance_row["departure_status"] if attendance_row else "",
            "departure_time": attendance_row["departure_time"] if attendance_row else None,
        },
        departure_action="Leave Early" if now < leaving_datetime else "Check Out",
    )


@app.post("/api/attendance/early-leave")
@device_required
def early_leave_api():
    data = request.get_json(silent=True) or {}
    token = str(data.get("ticket", ""))
    if not token:
        return api_error("Face verification expired. Scan your face again to continue.")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    db = get_db()
    refresh_early_codes()
    if not attendance_schedule_context()[1]["configured"]:
        return api_error(
            "The school schedule has not been configured. Please contact an administrator.",
            409,
        )
    db.execute("BEGIN IMMEDIATE")
    ticket = db.execute(
        """SELECT person_id FROM face_tickets
           WHERE token_hash=? AND expires_at>=? AND face_verified=1 AND mode='departure'""",
        (token_hash, stamp()),
    ).fetchone()
    if not ticket:
        db.rollback()
        return api_error("Face verification expired. Scan your face again to continue.")
    result = ensure_attendance(
        ticket["person_id"],
        "early_departure",
        code=str(data.get("code", "")),
        reason=str(data.get("reason", "")),
        transaction_open=True,
    )
    if result.get("error"):
        return api_error(result["error"], 409 if result.get("early_departure_closed") else 400)
    if result.get("duplicate"):
        db.rollback()
        return api_error("A departure is already recorded for today.", 409)
    consumed = db.execute(
        "DELETE FROM face_tickets WHERE token_hash=? AND expires_at>=?",
        (token_hash, stamp()),
    )
    if consumed.rowcount != 1:
        db.rollback()
        return api_error("Face verification expired. Scan your face again to continue.")
    db.commit()
    return jsonify(ok=True, **result)


@app.post("/api/attendance/self-enroll")
@device_required
def self_enroll_api():
    settings = get_settings()
    data = request.get_json(silent=True) or {}
    kind = str(data.get("kind", "")).lower()
    if kind == "student" and settings["self_enrollment"] != "1":
        return api_error("Please contact an administrator to be added.", 403)
    if kind == "teacher":
        confirmed = 0
        student_number = None
        class_id = None
        group_name = "Teachers"
    elif kind == "student":
        confirmed = 0
        student_number = str(data.get("student_number", "")).strip()
        if not student_number or len(student_number) > 50:
            return api_error("Enter the student's ID or number (50 characters maximum).")
        try:
            class_id = int(data.get("class_id"))
        except (TypeError, ValueError):
            return api_error("Select a class created by the administrator.")
        selected_class = get_db().execute(
            "SELECT name FROM classes WHERE id=? AND active=1",
            (class_id,),
        ).fetchone()
        if not selected_class:
            return api_error("That class is unavailable. Select an active class created by the administrator.")
        group_name = selected_class["name"]
    else:
        return api_error("Choose Student or Teacher.")
    name = str(data.get("name", "")).strip()
    if not name or len(name) > 100:
        return api_error("Enter a full name (100 characters maximum).")
    try:
        profiles = parse_enrollment_profiles(data.get("images"))
    except ValueError as exc:
        return api_error(str(exc))
    db = get_db()
    if kind == "student":
        duplicate = db.execute(
            """SELECT id FROM people WHERE active=1 AND kind='student'
               AND (student_number=? OR (lower(name)=lower(?) AND class_id=?))""",
            (student_number, name, class_id),
        ).fetchone()
    else:
        duplicate = db.execute(
            "SELECT id FROM people WHERE active=1 AND kind='teacher' AND lower(name)=lower(?)",
            (name,),
        ).fetchone()
    if duplicate:
        return api_error("A matching person already exists. Please ask an administrator for help.")
    now = stamp()
    cursor = db.execute(
        """INSERT INTO people(
               kind, name, group_name, student_number, class_id, face_image,
               confirmed, confirmed_at, created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
        (
            kind,
            name,
            group_name,
            student_number,
            class_id,
            profiles["center"],
            confirmed,
            now,
            now,
        ),
    )
    save_face_profiles(db, cursor.lastrowid, profiles)
    db.commit()
    invalidate_recognition_model()
    return jsonify(ok=True, message="Enrollment request sent to an administrator for confirmation.")


@app.get("/api/admin/attendance")
@admin_required
def attendance_list_api():
    requested_date = request.args.get("date", current_day())
    if not parse_iso_date(requested_date):
        return api_error("Date must use YYYY-MM-DD.")
    rows = get_db().execute(
        """SELECT a.*,
                  COALESCE(a.person_name_snapshot, p.name) AS name,
                  COALESCE(a.person_kind_snapshot, p.kind) AS kind,
                  COALESCE(a.group_name_snapshot, p.group_name) AS group_name,
                  COALESCE(a.student_number_snapshot, p.student_number) AS student_number
           FROM attendance a
           JOIN people p ON p.id=a.person_id WHERE a.attendance_date=?
           ORDER BY kind, group_name, name""",
        (requested_date,),
    ).fetchall()
    return jsonify(date=requested_date, attendance=[dict(row) for row in rows])


@app.post("/api/admin/attendance/<int:record_id>")
@admin_required
def correct_attendance_api(record_id):
    data = request.get_json(silent=True) or {}
    db = get_db()
    existing = db.execute("SELECT * FROM attendance WHERE id=?", (record_id,)).fetchone()
    if not existing:
        return api_error("Attendance record not found.", 404)
    allowed = {
        "attendance_status": {"Present", "Late", "Absent"},
        "departure_status": {"", "Left", "Left Early", "Left Late"},
    }
    updates = {}
    for field in ("attendance_status", "departure_status", "arrival_time", "departure_time", "early_leave_reason"):
        if field in data:
            value = "" if data[field] is None else str(data[field]).strip()
            if field in allowed and value not in allowed[field]:
                return api_error(f"Invalid {field.replace('_', ' ')}.")
            if field in {"arrival_time", "departure_time"} and value:
                try:
                    value = parse_clock_time(value).strftime("%H:%M:%S")
                except ValueError:
                    return api_error(f"{field.replace('_', ' ').title()} must be a valid HH:MM or HH:MM:SS time.")
            updates[field] = value or None
    if not updates:
        return api_error("No record changes were provided.")
    candidate = dict(existing)
    candidate.update(updates)
    if candidate["attendance_status"] == "Absent" and any(
        candidate[field]
        for field in ("arrival_time", "departure_status", "departure_time", "early_leave_reason")
    ):
        return api_error("An Absent record cannot include arrival, departure, or early-leave details.")
    if candidate["attendance_status"] in {"Present", "Late"} and not candidate["arrival_time"]:
        return api_error("Present and Late records require an arrival time.")
    if candidate["departure_time"] and not candidate["departure_status"]:
        return api_error("A departure time requires a departure status.")
    if candidate["departure_status"] and not candidate["departure_time"]:
        return api_error("A departure status requires a departure time.")
    if candidate["departure_status"] == "Left Early" and not candidate["early_leave_reason"]:
        return api_error("A Left Early record requires a reason.")
    if candidate["early_leave_reason"] and candidate["departure_status"] != "Left Early":
        return api_error("An early-leave reason requires the Left Early status.")
    if candidate["arrival_time"] and candidate["departure_time"]:
        try:
            departure_clock = parse_clock_time(candidate["departure_time"])
            arrival_clock = parse_clock_time(candidate["arrival_time"])
        except ValueError:
            return api_error("Correct the invalid arrival or departure time before saving.")
        if departure_clock < arrival_clock:
            return api_error("Departure time cannot be earlier than arrival time.")
    updates["correction_note"] = str(data.get("correction_note", "Corrected by administrator"))[:500]
    assignments = ", ".join(f"{key}=?" for key in updates)
    cursor = db.execute(
        f"UPDATE attendance SET {assignments} WHERE id=?",
        (*updates.values(), record_id),
    )
    db.commit()
    if not cursor.rowcount:
        return api_error("Attendance record not found.", 404)
    return jsonify(ok=True)


@app.delete("/api/admin/attendance/<int:record_id>")
@admin_required
def delete_attendance_api(record_id):
    cursor = get_db().execute("DELETE FROM attendance WHERE id=?", (record_id,))
    get_db().commit()
    if not cursor.rowcount:
        return api_error("Attendance record not found.", 404)
    return jsonify(ok=True, message="Attendance record deleted.")


@app.post("/api/admin/end-day")
@admin_required
def end_day_api():
    data = request.get_json(silent=True) or {}
    target_date = str(data.get("date", current_day()))
    parsed_date = parse_iso_date(target_date)
    if not parsed_date:
        return api_error("Date must use YYYY-MM-DD.")
    if parsed_date > now_local().date():
        return api_error("A future school day cannot be finalized.")
    db = get_db()
    history_start = db.execute(
        "SELECT value FROM settings WHERE key='roster_history_start_date'"
    ).fetchone()
    if not history_start or not parse_iso_date(history_start["value"]):
        return api_error(
            "Reliable roster history is unavailable. Attendance was not finalized.",
            409,
        )
    if target_date < history_start["value"]:
        return api_error(
            "This date predates reliable roster tracking. Review and finalize its records manually.",
            409,
        )
    schedule = (
        get_active_schedule(target_date)
        if target_date == current_day()
        else schedule_snapshot(target_date)
    )
    if not schedule["configured"]:
        return api_error(
            "No saved schedule exists for this historical date, so its attendance roster cannot be finalized safely.",
            409,
        )
    arrival_opens_at = schedule_event_datetime(
        schedule, target_date, "attendance_start", now_local().tzinfo
    ) - timedelta(minutes=int(schedule["flexibility_minutes"]))
    opening_timestamp = arrival_opens_at.timestamp()
    db.execute("BEGIN IMMEDIATE")
    db.execute(
        "INSERT OR IGNORE INTO attendance_days(attendance_date, finalized_at) VALUES (?, ?)",
        (target_date, stamp()),
    )
    db.execute(
        "UPDATE attendance SET finalized=1 WHERE attendance_date=?",
        (target_date,),
    )
    people = db.execute(
        """SELECT id, active, confirmed, created_at, confirmed_at, deactivated_at
           FROM people"""
    ).fetchall()
    for person in people:
        if not person["confirmed"]:
            continue
        created_at = timestamp_value(person["created_at"])
        confirmed_at = timestamp_value(person["confirmed_at"])
        deactivated_at = timestamp_value(person["deactivated_at"])
        if (
            created_at is None
            or confirmed_at is None
            or (not person["active"] and deactivated_at is None)
        ):
            db.rollback()
            return api_error(
                "Roster lifecycle history is incomplete. Attendance was not finalized.",
                409,
            )
        if (
            created_at > opening_timestamp
            or confirmed_at > opening_timestamp
            or (deactivated_at is not None and deactivated_at <= opening_timestamp)
        ):
            continue
        row = db.execute(
            "SELECT id FROM attendance WHERE person_id=? AND attendance_date=?",
            (person["id"], target_date),
        ).fetchone()
        if row:
            db.execute("UPDATE attendance SET finalized=1 WHERE id=?", (row["id"],))
        else:
            db.execute(
                """INSERT INTO attendance(
                       person_id, attendance_date, attendance_status, finalized,
                       person_name_snapshot, person_kind_snapshot, group_name_snapshot,
                       student_number_snapshot
                   )
                   SELECT id, ?, 'Absent', 1, name, kind, group_name, student_number
                   FROM people WHERE id=?""",
                (target_date, person["id"]),
            )
    db.commit()
    return jsonify(ok=True, date=target_date, message="Day finalized. People without attendance are marked Absent.")


@app.get("/api/admin/report.xlsx")
@admin_required
def export_report_api():
    requested_date = request.args.get("date", current_day())
    if not parse_iso_date(requested_date):
        return api_error("Date must use YYYY-MM-DD.")
    wb = Workbook()
    default_sheet = wb.active
    wb.remove(default_sheet)
    headers = [
        "Date",
        "Name",
        "Student ID",
        "Class / Subject",
        "Attendance Status",
        "Arrival Time",
        "Departure Status",
        "Departure Time",
        "Early-Leave Reason",
    ]
    rows = get_db().execute(
        """SELECT COALESCE(a.person_kind_snapshot, p.kind) AS kind,
                  COALESCE(a.person_name_snapshot, p.name) AS name,
                  COALESCE(a.student_number_snapshot, p.student_number) AS student_number,
                  COALESCE(a.group_name_snapshot, p.group_name) AS group_name,
                  a.attendance_date, a.attendance_status,
                  a.arrival_time, a.departure_status, a.departure_time, a.early_leave_reason
           FROM attendance a JOIN people p ON p.id=a.person_id
           WHERE a.attendance_date=?
           ORDER BY kind, group_name, name""",
        (requested_date,),
    ).fetchall()

    def safe_excel_value(value):
        if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
            return "'" + value
        return value

    class_names = sorted({row["group_name"] for row in rows if row["kind"] == "student"})
    sheet_names = {}
    used_sheet_names = {"teachers"}
    for group in class_names:
        safe_name = re.sub(r"[\[\]:*?/\\]", "-", group).strip("'")[:31] or "Students"
        base, index = safe_name, 1
        while safe_name.lower() in used_sheet_names:
            suffix = f" ({index})"
            safe_name = base[: 31 - len(suffix)] + suffix
            index += 1
        sheet_names[group] = safe_name
        used_sheet_names.add(safe_name.lower())
    if not class_names:
        sheet_names["Students"] = "Students"
    sheet_names["__teachers__"] = "Teachers"
    for key, sheet_name in sheet_names.items():
        sheet = wb.create_sheet(sheet_name)
        sheet.append(headers)
        sheet.freeze_panes = "A2"
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="183A58")
        for row in rows:
            if (key == "__teachers__" and row["kind"] == "teacher") or (
                key != "__teachers__" and row["kind"] == "student" and row["group_name"] == key
            ):
                sheet.append(
                    [
                        safe_excel_value(value)
                        for value in [
                            row["attendance_date"],
                            row["name"],
                            row["student_number"] or "",
                            row["group_name"],
                            row["attendance_status"],
                            row["arrival_time"] or "",
                            (
                                row["departure_status"]
                                or ("Did Not Depart" if row["kind"] == "teacher" else "")
                            ),
                            row["departure_time"] or "",
                            row["early_leave_reason"] or "",
                        ]
                    ]
                )
        sheet.auto_filter.ref = f"A1:I{sheet.max_row}"
        for column, width in {"A": 14, "B": 26, "C": 18, "D": 24, "E": 20, "F": 18, "G": 20, "H": 18, "I": 42}.items():
            sheet.column_dimensions[column].width = width
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name=f"attendance-{requested_date}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.errorhandler(413)
def request_too_large(_error):
    return jsonify(error="The image request is too large. Capture smaller photos and try again."), 413


init_db()

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    validate_host_credentials(host)
    if (
        os.environ.get("FLASK_DEBUG", "0") != "1"
        or os.environ.get("WERKZEUG_RUN_MAIN") == "true"
    ):
        start_early_code_rotation()
    app.run(
        host=host,
        port=int(os.environ.get("PORT", "5001")),
        debug=os.environ.get("FLASK_DEBUG", "0") == "1",
    )
