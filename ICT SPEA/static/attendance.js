const csrf = document.body.dataset.csrf;
const video = document.querySelector("#camera");
const canvas = document.createElement("canvas");
const pairPanel = document.querySelector("#pair-panel");
const stationPanel = document.querySelector("#station-panel");
const cameraOverlay = document.querySelector("#camera-overlay");
const cameraError = document.querySelector("#camera-error");
let cameraStream = null;
let scanTimer = null;
let countdownTimer = null;
let unknownCount = 0;
let currentTicket = null;
let selfEnrollmentAllowed = false;
let busy = false;
let pairingPoll = null;
const selfEnrollmentImages = {};
const enrollmentPoseOrder = ["center", "left", "right"];
let availableClasses = [];

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "X-CSRFToken": csrf,
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "The request could not be completed.");
  return data;
}

function showNotice(message, type = "info") {
  const box = document.querySelector("#result-notice");
  box.textContent = message;
  box.className = `notice ${type}`;
}

function setCameraStatus(text) {
  document.querySelector("#station-status").textContent = text;
}

async function checkPairing() {
  try {
    const state = await fetch("/api/attendance/state").then((response) => response.json());
    selfEnrollmentAllowed = state.self_enrollment;
    document.querySelector('#enroll-kind option[value="student"]').disabled = !selfEnrollmentAllowed;
    if (!selfEnrollmentAllowed) document.querySelector("#enroll-kind").value = "teacher";
    availableClasses = state.classes || [];
    renderSelfClassOptions();
    if (state.paired) {
      if (pairingPoll) window.clearInterval(pairingPoll);
      pairingPoll = null;
      pairPanel.classList.add("hidden");
      stationPanel.classList.remove("hidden");
      document.querySelector("#connection-dot").classList.add("online");
      setCameraStatus("Device connected");
      startCamera();
    } else {
      stationPanel.classList.add("hidden");
      pairPanel.classList.remove("hidden");
      pairPanel.querySelector(".muted").textContent = state.allow_unpaired_devices
        ? "Automatic connection is enabled. Reload this page if it does not connect."
        : "Enter the one-time code provided by your administrator.";
      document.querySelector("#connection-dot").classList.remove("online");
      setCameraStatus("Device not connected");
      stopCamera();
      if (!pairingPoll) {
        pairingPoll = window.setInterval(checkPairing, 10000);
      }
    }
  } catch {
    setCameraStatus("Unable to check device");
  }
}

document.querySelector("#pair-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = document.querySelector("#pair-error");
  error.classList.add("hidden");
  const values = Object.fromEntries(new FormData(event.currentTarget));
  try {
    await request("/api/pair", { method: "POST", body: JSON.stringify(values) });
    await checkPairing();
  } catch (err) {
    error.textContent = err.message;
    error.classList.remove("hidden");
  }
});

async function startCamera() {
  if (cameraStream) return;
  cameraError.classList.add("hidden");
  cameraOverlay.classList.remove("hidden");
  document.querySelector("#camera-overlay-text").textContent = "Requesting camera permission…";
  try {
    cameraStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "user", width: { ideal: 640 }, height: { ideal: 480 } },
      audio: false,
    });
    video.srcObject = cameraStream;
    await video.play();
    cameraOverlay.classList.add("hidden");
    document.querySelector("#camera-toggle").textContent = "Stop camera";
    document.querySelector("#result-title").textContent = "Looking for a face";
    document.querySelector("#result-copy").textContent = "Recognition retries automatically while the camera is active.";
    scanTimer = window.setInterval(scanFace, 2200);
    scanFace();
  } catch (error) {
    cameraOverlay.classList.add("hidden");
    cameraError.textContent = error.name === "NotAllowedError"
      ? "Camera permission was denied. Allow camera access in Chrome’s address-bar settings, then try again."
      : "Could not start the camera. Check that a camera is connected and that this page is using localhost or HTTPS.";
    cameraError.classList.remove("hidden");
    setCameraStatus("Camera unavailable");
  }
}

function stopCamera() {
  if (scanTimer) window.clearInterval(scanTimer);
  scanTimer = null;
  if (cameraStream) cameraStream.getTracks().forEach((track) => track.stop());
  cameraStream = null;
  video.srcObject = null;
}

document.querySelector("#camera-toggle").addEventListener("click", () => {
  cameraStream ? stopCamera() : startCamera();
  document.querySelector("#camera-toggle").textContent = cameraStream ? "Stop camera" : "Start camera";
});

function captureFrame() {
  if (!video.videoWidth || !video.videoHeight) throw new Error("Camera is still starting. Please wait a moment.");
  const scale = Math.min(1, 640 / video.videoWidth);
  canvas.width = Math.round(video.videoWidth * scale);
  canvas.height = Math.round(video.videoHeight * scale);
  canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL("image/jpeg", 0.82);
}

async function scanFace({ confirmEarlyDeparture = false } = {}) {
  if (busy || !cameraStream) return false;
  busy = true;
  let recognized = false;
  try {
    const frame = captureFrame();
    const result = await request("/api/recognize", {
      method: "POST",
      body: JSON.stringify({
        image: frame,
        confirm_early_departure: confirmEarlyDeparture,
      }),
    });
    if (result.recognized) {
      recognized = true;
      unknownCount = 0;
      if (result.attendance_closed) {
        if (scanTimer) window.clearInterval(scanTimer);
        scanTimer = null;
        const person = result.person;
        document.querySelector("#result-eyebrow").textContent = "ATTENDANCE CLOSED";
        document.querySelector("#result-title").textContent = person.name;
        document.querySelector("#result-copy").textContent = result.error;
        document.querySelector("#result-symbol").textContent = "×";
        document.querySelector("#result-symbol").classList.remove("success");
        document.querySelector("#person-details").classList.add("hidden");
        document.querySelector("#add-person-panel").classList.add("hidden");
        document.querySelector("#early-panel").classList.add("hidden");
        document.querySelector("#next-scan").classList.remove("hidden");
        showNotice("Attendance was not recorded because the arrival window has closed.", "error");
        return true;
      }
      currentTicket = result.ticket;
      if (scanTimer) window.clearInterval(scanTimer);
      scanTimer = null;
      renderRecognized(result);
    } else {
      unknownCount += 1;
      document.querySelector("#result-eyebrow").textContent = "FACE NOT CONFIRMED";
      document.querySelector("#result-title").textContent = "Please try again";
      document.querySelector("#result-copy").textContent = result.error;
      if (unknownCount >= 3) showUnknownFlow();
    }
  } catch (error) {
    if (error.message.includes("not paired") || error.message.includes("disconnected")) {
      stopCamera();
      await checkPairing();
    } else {
      document.querySelector("#result-copy").textContent = error.message;
    }
  } finally {
    busy = false;
  }
  return recognized;
}

function renderRecognized(result) {
  const notice = document.querySelector("#result-notice");
  notice.className = "notice hidden";
  notice.textContent = "";
  const person = result.person;
  const modeName = result.mode === "arrival" ? "Arrival" : "Departure";
  document.querySelector("#result-eyebrow").textContent = `${person.kind.toUpperCase()} · ${modeName.toUpperCase()}`;
  document.querySelector("#result-title").textContent = person.name;
  document.querySelector("#result-copy").textContent = `${person.kind === "student" ? "Class / grade" : "Subject"}: ${person.group_name}`;
  document.querySelector("#result-symbol").textContent = "✓";
  document.querySelector("#result-symbol").classList.add("success");
  const departureStatus = result.current_status.departure || "Not departed";
  document.querySelector("#person-details").innerHTML =
    `<span>${escapeHtml(person.group_name)}</span><span>Attendance: ${escapeHtml(result.current_status.attendance)}</span><span>Arrival: ${escapeHtml(result.current_status.arrival_time || "Not recorded")}</span><span>Departure: ${escapeHtml(departureStatus)}${result.current_status.departure_time ? ` · ${escapeHtml(result.current_status.departure_time)}` : ""}</span><span>Match confidence: ${result.confidence}</span>`;
  document.querySelector("#person-details").classList.remove("hidden");
  document.querySelector("#add-person-panel").classList.add("hidden");
  document.querySelector("#next-scan").classList.remove("hidden");
  const departurePanel = document.querySelector("#early-panel");
  const leaveButton = document.querySelector("#leave-early-trigger");
  const earlyForm = document.querySelector("#early-form");
  if (countdownTimer) window.clearInterval(countdownTimer);
  if (result.already_departed) {
    departurePanel.classList.add("hidden");
    const finalMessage = result.day_finalized
      ? "Attendance day is finalized."
      : "No further departure can be recorded today.";
    document.querySelector("#result-copy").textContent =
      `${result.final_status || "Departure recorded"} — ${finalMessage}`;
    showNotice(`Final status: ${result.final_status || "Left"}. ${finalMessage}`, "success");
    return;
  }
  departurePanel.classList.remove("hidden");
  if (result.early_available && result.ticket) {
    document.querySelector("#departure-heading").textContent = "Leave Early";
    document.querySelector("#leave-countdown").textContent = "Face confirmed. Enter the active code and reason to confirm your departure.";
    leaveButton.classList.add("hidden");
    earlyForm.classList.remove("hidden");
    return;
  }
  earlyForm.classList.add("hidden");
  leaveButton.classList.remove("hidden");
  leaveButton.disabled = result.remaining_seconds > 0;
  leaveButton.textContent = result.departure_action === "Check Out"
    ? "Check Out"
    : "Leave Early";
  const attendanceStatus = result.current_status.attendance || "Present";
  updateLeaveCountdown(
    result.remaining_seconds || 0,
    result.minimum_early_leave_minutes,
    attendanceStatus,
  );
  document.querySelector("#result-copy").textContent = result.first_arrival
    ? attendanceStatus === "Late"
      ? `You're marked late. Arrival recorded at ${result.current_status.arrival_time}.`
      : `You're marked present. Arrival recorded at ${result.current_status.arrival_time}.`
    : `You're already marked ${attendanceStatus.toLowerCase()}.`;
}

function updateLeaveCountdown(secondsRemaining, minimumMinutes = 30, attendanceStatus = "Present") {
  const leaveButton = document.querySelector("#leave-early-trigger");
  const countdown = document.querySelector("#leave-countdown");
  const update = () => {
    if (secondsRemaining <= 0) {
      leaveButton.disabled = false;
      leaveButton.textContent = "Leave Early";
      countdown.textContent = `You're already marked ${attendanceStatus.toLowerCase()}. The ${minimumMinutes}-minute wait is complete. Press the button and scan your face again to continue.`;
      if (countdownTimer) window.clearInterval(countdownTimer);
      countdownTimer = null;
      return;
    }
    leaveButton.disabled = true;
    leaveButton.textContent = "I want to leave early";
    const minutes = Math.floor(secondsRemaining / 60);
    const seconds = secondsRemaining % 60;
    countdown.textContent = `You're already marked ${attendanceStatus.toLowerCase()}. The early-leave option will be available after ${minimumMinutes} minutes from arrival. Time remaining: ${minutes}:${String(seconds).padStart(2, "0")}.`;
    secondsRemaining -= 1;
  };
  const shouldStartTimer = secondsRemaining > 0;
  update();
  if (shouldStartTimer) countdownTimer = window.setInterval(update, 1000);
}

function showUnknownFlow() {
  document.querySelector("#result-symbol").textContent = "?";
  document.querySelector("#result-symbol").classList.remove("success");
  document.querySelector("#result-eyebrow").textContent = "UNKNOWN PERSON";
  document.querySelector("#result-title").textContent = "Profile not found";
  document.querySelector("#result-copy").textContent = selfEnrollmentAllowed
    ? "Submit a student enrollment request or a teacher request below."
    : "Students should contact an administrator. Teachers may submit a request below.";
  if (!selfEnrollmentAllowed) document.querySelector("#enroll-kind").value = "teacher";
  updateSelfEnrollmentFields();
  document.querySelector("#add-person-panel").classList.remove("hidden");
  if (!selfEnrollmentAllowed) showNotice("Student self-enrollment is disabled. Teacher requests still require administrator confirmation.", "warning");
}

document.querySelector("#leave-early-trigger").addEventListener("click", async (event) => {
  event.currentTarget.disabled = true;
  const recognized = await scanFace({ confirmEarlyDeparture: true });
  if (!recognized) event.currentTarget.disabled = false;
});

document.querySelector("#next-scan").addEventListener("click", () => {
  if (countdownTimer) window.clearInterval(countdownTimer);
  countdownTimer = null;
  currentTicket = null;
  unknownCount = 0;
  document.querySelector("#early-panel").classList.add("hidden");
  document.querySelector("#early-form").classList.add("hidden");
  document.querySelector("#add-person-panel").classList.add("hidden");
  document.querySelector("#person-details").classList.add("hidden");
  document.querySelector("#result-symbol").textContent = "⌖";
  document.querySelector("#result-symbol").classList.remove("success");
  document.querySelector("#result-eyebrow").textContent = "READY TO SCAN";
  document.querySelector("#result-title").textContent = "Looking for a face";
  document.querySelector("#result-copy").textContent = "Recognition retries automatically while the camera is active.";
  if (cameraStream && !scanTimer) scanTimer = window.setInterval(scanFace, 2200);
});

document.querySelector("#early-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!currentTicket) return showNotice("Please scan your face again before confirming departure.", "error");
  const values = Object.fromEntries(new FormData(event.currentTarget));
  try {
    const result = await request("/api/attendance/early-leave", {
      method: "POST",
      body: JSON.stringify({ ...values, ticket: currentTicket }),
    });
    showNotice(result.message, "success");
    const details = document.querySelector("#person-details");
    if (details.children.length >= 4) {
      details.children[3].textContent = `Departure: Left Early · ${result.record.departure_time}`;
    }
    document.querySelector("#result-copy").textContent = "Departure recorded: Left Early.";
    document.querySelector("#early-panel").classList.add("hidden");
    currentTicket = null;
  } catch (error) {
    showNotice(error.message, "error");
    if (error.message.includes("Face verification expired")) {
      currentTicket = null;
      document.querySelector("#early-form").classList.add("hidden");
      const leaveButton = document.querySelector("#leave-early-trigger");
      leaveButton.classList.remove("hidden");
      leaveButton.disabled = false;
      leaveButton.textContent = "Scan again to verify departure";
    } else {
      showNotice(`${error.message} You can correct the code or reason and try again without rescanning.`, "error");
    }
  }
});

document.querySelector("#enroll-kind").addEventListener("change", updateSelfEnrollmentFields);

function updateSelfEnrollmentFields() {
  const select = document.querySelector("#enroll-kind");
  const isStudent = select.value === "student" && selfEnrollmentAllowed;
  if (select.value === "student" && !selfEnrollmentAllowed) select.value = "teacher";
  document.querySelector("#self-student-number").classList.toggle("hidden", !isStudent);
  document.querySelector("#self-student-class").classList.toggle("hidden", !isStudent);
  document.querySelector("#self-student-note").classList.toggle("hidden", !isStudent);
  document.querySelector("#self-teacher-note").classList.toggle("hidden", isStudent);
  document.querySelector("#self-enroll-form").elements.student_number.required = isStudent;
  document.querySelector("#self-class-select").required = isStudent && availableClasses.length > 0;
}

function renderSelfClassOptions() {
  const select = document.querySelector("#self-class-select");
  select.innerHTML = availableClasses.length
    ? availableClasses.map((item) => `<option value="${item.id}">${escapeHtml(item.name)}</option>`).join("")
    : '<option value="">No classes available — ask an administrator to create one</option>';
  select.disabled = availableClasses.length === 0;
  updateSelfEnrollmentFields();
}

document.querySelector("#self-enroll-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const notice = document.querySelector("#enroll-notice");
  notice.classList.add("hidden");
  if (!enrollmentPoseOrder.every((pose) => selfEnrollmentImages[pose])) {
    notice.textContent = "Capture the center, left, and right face photos before submitting.";
    notice.className = "notice error";
    return;
  }
  try {
    const values = Object.fromEntries(new FormData(event.currentTarget));
    if (values.kind === "student") {
      values.class_id = Number(values.class_id);
    } else {
      delete values.student_number;
      delete values.class_id;
    }
    const result = await request("/api/attendance/self-enroll", {
      method: "POST",
      body: JSON.stringify({ ...values, images: { ...selfEnrollmentImages } }),
    });
    notice.textContent = result.message;
    notice.className = "notice success";
    event.currentTarget.reset();
    updateSelfEnrollmentFields();
    Object.keys(selfEnrollmentImages).forEach((pose) => delete selfEnrollmentImages[pose]);
    renderSelfPoseProgress();
    unknownCount = 0;
  } catch (error) {
    notice.textContent = error.message;
    notice.className = "notice error";
  }
});

document.querySelector("#capture-self-pose").addEventListener("click", () => {
  try {
    let pose = enrollmentPoseOrder.find((item) => !selfEnrollmentImages[item]);
    if (!pose) {
      enrollmentPoseOrder.forEach((item) => delete selfEnrollmentImages[item]);
      renderSelfPoseProgress();
      pose = "center";
    }
    selfEnrollmentImages[pose] = captureFrame();
    renderSelfPoseProgress();
  } catch (error) {
    const notice = document.querySelector("#enroll-notice");
    notice.textContent = error.message;
    notice.className = "notice error";
  }
});

function renderSelfPoseProgress() {
  const pose = enrollmentPoseOrder.find((item) => !selfEnrollmentImages[item]);
  const status = document.querySelector("#self-pose-status");
  const button = document.querySelector("#capture-self-pose");
  const preview = document.querySelector("#self-pose-preview");
  status.textContent = pose
    ? `${Object.keys(selfEnrollmentImages).length} of 3 captured. ${posePrompt(pose)}`
    : "All three captured. The server verifies the center face and both side profiles.";
  button.textContent = pose
    ? `Capture ${pose} ${pose === "center" ? "photo" : "profile"}`
    : "Retake all photos";
  preview.classList.toggle("hidden", Object.keys(selfEnrollmentImages).length === 0);
  preview.innerHTML = enrollmentPoseOrder.filter((item) => selfEnrollmentImages[item]).map((item) => `
    <div class="pose-thumbnail"><img src="${selfEnrollmentImages[item]}" alt="${item} face profile"><span>${item}</span></div>`).join("");
}

function posePrompt(pose) {
  if (pose === "center") return "Look straight at the camera and capture the center photo.";
  return `Turn your head to your ${pose} and capture the ${pose} profile.`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

document.querySelector("#clock").textContent = new Intl.DateTimeFormat([], {
  weekday: "long", year: "numeric", month: "short", day: "numeric",
}).format(new Date());
checkPairing();
