# AttendAI — Camera Attendance System

A locally hosted Flask application for student and teacher attendance. It includes a SQLite database, administrator console, browser pairing, real camera-based face matching with OpenCV's LBPH recognizer, early-leave codes, and Excel reports.

## Requirements

- Python **3.11–3.14** when an OpenCV contrib wheel is available for your platform (the app was installed and tested with Python 3.14 in this workspace; 3.11/3.12 are also good choices).
- Google Chrome and a camera.
- macOS, Windows, or Linux.

Camera access works on `http://localhost` / `http://127.0.0.1` during local testing, or on an HTTPS deployment. Browsers block camera access from ordinary HTTP hostnames and remote IP addresses.

## Install and run

From the project directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python app.py
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1`. If dependency installation reports that no OpenCV wheel is available, use Python 3.12.

On macOS, you can instead start the app and open the correct page with these Terminal commands from this project folder:

```bash
./startadmin
./startuser
```

Run one command at a time. The first launch creates `.venv` and installs dependencies if needed; keep its Terminal window open while using the app and press **Control-C** there to stop the server. `./startadmin` opens administrator sign-in; `./startuser` opens the attendance station. If the server is already running, either command just opens its page in Chrome's default browser.

Open:

- Attendance station: <http://127.0.0.1:5001/>
- Administrator sign-in: <http://127.0.0.1:5001/admin>

The app uses port **5001** by default because macOS may reserve port 5000 for AirPlay Receiver. To choose another port, set `PORT` when starting the app (the launcher scripts honor it too), for example `PORT=5050 ./startadmin`.

The first run creates `instance/attendance.sqlite3` and a persistent local Flask signing key. The `instance/` folder contains the database and should be backed up securely; it is excluded from Git.

## Demonstration sign-in and sample records

The first run creates the demo administrator:

- Username: `admin`
- Password: `admin123`

Set `ADMIN_USERNAME` and `ADMIN_PASSWORD` before the **first** run to choose initial credentials:

```bash
ADMIN_USERNAME=schooladmin ADMIN_PASSWORD='use-a-unique-password' python app.py
```

Changing these variables after the initial administrator has been created does not reset its password. Sign in and use **Administrator password** on the dashboard to set a new password (minimum 12 characters) before any deployment. Do not expose the demo credentials to a school network or use them outside a local demonstration.

Sample people are created once:

- Aisha Rahman — Grade 8
- Omar Hassan — Grade 8
- Maya Chen — Grade 9
- Mr. Daniel Lee — Mathematics

Sample rows intentionally have **no face profiles**. Enroll each demonstrator with a live camera photo in Admin → Students & teachers before testing recognition. Existing records survive server restarts.

## Demonstration walkthrough

1. Sign in at `/admin`.
2. In **Students & teachers**, first add a class in **Manage student classes**. For a student, choose the class, enter their full name and unique student ID/number, start the camera, then capture three enrollment photos in sequence: face the camera, turn your head left, and turn your head right. The server requires a detected front face and profile faces in the side shots, then trains LBPH with all three samples. For a teacher, the form asks only for their full name and the face photos.
3. Use **Connect a device → Generate device code**. On the attendance station, enter that code and a device name. A code is single-use and expires after 30 minutes.
4. Allow Chrome camera access. No arrival/departure switch is needed: the first successful scan records Arrival immediately and confirms the person is present. The original arrival time is never reset. After the configured minimum has elapsed, the station offers **I want to leave early**. The person must press it and scan again; only then does the station request an active early-leave code and reason. At or after normal leaving time, a successful scan records `Left` without applying the early-leave minimum.
5. To test early departure, set the normal leaving time later than the current time, wait the configured minimum after check-in, then rescan, enter an unused code, and provide a reason.
6. In Admin, inspect or correct records. Select a report date, press **End day** to mark missing attendance as Absent and finalize rows, then download the Excel workbook.

The five early-leave codes are single-use and rotate immediately when used. Unused codes expire 15 minutes after generation and are replaced independently of the configurable minimum-arrival wait. While the local Flask server is running, a background worker refreshes the pool every 30 seconds; the admin dashboard also refreshes its displayed codes. The server rejects expired or previously used codes at redemption. An invalid code or missing reason does not consume the short-lived face-match ticket, so the user can correct the form and retry without scanning again. Device-pairing codes are separate.

## Face recognition behavior and limitations

- The app uses `opencv-contrib-python-headless`'s **LBPHFaceRecognizer**, trained from the enrolled face crops stored in SQLite and cached until a face profile or enrollment status changes. Haar cascade face detection still runs on each camera capture. Unknown results use a configurable LBPH distance threshold (`FACE_CONFIDENCE_THRESHOLD`, default `65`); lower confidence distance is a closer match.
- LBPH matches the face image captured from the camera, but this build does not implement liveness or anti-spoof detection. A photograph or replay may be accepted if it matches an enrolled profile. Do not treat face matching alone as secure proof of a live person.
- LBPH is a lightweight classical recognition method, not a modern embedding model. Results depend on lighting, camera position, image quality, and enrollment photos. The threshold may need to be adjusted for the test environment; re-enroll under representative lighting if matching is unreliable.
- This is a demonstration system, not a production biometric access-control product. Test false matches and false rejections before any real use. Obtain appropriate consent and follow applicable privacy, retention, and school policies. Limit access to the host and protect/backup the `instance/` directory.
- Face crops and attendance are stored in the local SQLite database. This local build does not encrypt that database at rest. Use disk encryption and a secured host; do not expose the Flask development server to the public internet.
- The default server binds to `127.0.0.1`. To test from another device on a trusted LAN, set `HOST=0.0.0.0`; startup refuses a network bind while the demo `admin123` password remains active. Chrome camera access from another device requires HTTPS. Deploy behind a properly configured HTTPS reverse proxy, set `COOKIE_SECURE=1`, configure a strong `FLASK_SECRET_KEY`, and use a production WSGI server.

## Attendance rules

The arrival is recorded once per person per local calendar day. No arrival is accepted before the configured attendance start. Arrivals after the configured cut-off are marked `Late` during the flexibility period; no new arrival is accepted after cut-off plus flexibility. Arrivals on or before cut-off are `Present`. Repeated scans never change the original arrival timestamp. Admins set the minimum early-departure wait in **Schedule & enrollment** (0–1440 minutes; default 30). Before normal leaving time, early departure is allowed only after that many minutes from the original arrival, and requires an active single-use code and non-empty reason. The attendance record separately retains attendance status, departure status, time, reason, and valid code-use result. At or after normal leaving time, a successful scan records `Left` without applying the early-leave minimum or requiring an early-leave code. Student departure is optional: if no checkout is recorded, the student’s departure status and time are blank in Excel. For teachers, Excel shows `Did Not Depart` when there is no recorded departure; their departure time remains blank.

## Administration notes

- Student enrollment requests can be enabled under **Students & teachers**. Students must enter a unique student number and select an active class created by an administrator; they cannot create classes. Student and teacher station requests remain pending and cannot be recognized until an administrator confirms them. Teacher requests need only a name. Enrollment requires the three detected camera poses.
- Admins can add, rename, and delete classes under **Manage student classes**. Deleting a class with student records archives it instead so records are not orphaned; move students out before deleting the remaining class record.
- Attendance rows can be corrected or deleted by an administrator from **Attendance & reports**. Corrections validate real clock times and keep status/time/reason fields consistent.
- **End day** rejects future dates and closes the selected date as a whole; deleting an individual row does not reopen that school day. End-of-day reports include inactive people and retain each attendance row’s name, class/subject, and student number as recorded on that date.
- Removing a person deactivates the profile and preserves attendance history.
- Browser pairing is independent of administrator authentication. The admin console always requires username and password.
- Admins can optionally enable **Allow all devices without a code**. Then any attendance browser that opens the station is automatically registered; disable this setting to require pairing codes again for new browsers. This deliberately removes the pairing-code access barrier and is intended only for a controlled demonstration.
- Time values use the server's local timezone. Configure the host clock/timezone before a school demonstration.
- This app does not check MAC addresses; pairing is tied to a random browser cookie and can be revoked by an administrator.

## Troubleshooting

- **`cv2.face` / LBPH unavailable:** activate the project virtual environment and install `opencv-contrib-python-headless` from `requirements.txt`. Do not install only `opencv-python`; it does not include the contrib face module.
- **Camera denied:** allow camera permission in Chrome site settings and reload. Use `localhost` or HTTPS.
- **Face not detected:** move closer, face the camera, use even lighting, and ensure only one person is in the image.
- **Face not recognized:** capture a fresh admin enrollment photo under similar lighting, check that the person is active and confirmed, and adjust `FACE_CONFIDENCE_THRESHOLD` cautiously. Restart the server after changing the environment variable.
- **Lost local data:** restore the SQLite database from a protected backup. The database, signing key, face profiles, device registrations, and records are all in `instance/`.
