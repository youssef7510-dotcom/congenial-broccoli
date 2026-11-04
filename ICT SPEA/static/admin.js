const csrf = document.body.dataset.csrf;
const state = { people: [], attendance: [], devices: [], classes: [], settings: {}, scheduleState: null, capturedImages: {}, cameraStream: null, today: "" };
let attendanceLoadVersion = 0;
const form = document.querySelector("#person-form");
const classForm = document.querySelector("#class-form");

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "X-CSRFToken": csrf,
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });
  if (response.status === 401) {
    location.href = "/admin";
    throw new Error("Please sign in again.");
  }
  const data = response.headers.get("content-type")?.includes("application/json")
    ? await response.json() : null;
  if (!response.ok) throw new Error(data?.error || "The request could not be completed.");
  return data;
}

function message(text, type = "success") {
  const box = document.querySelector("#global-message");
  box.textContent = text;
  box.className = `notice ${type}`;
  window.scrollTo({ top: 0, behavior: "smooth" });
  window.setTimeout(() => box.classList.add("hidden"), 5000);
}

async function refreshDashboard() {
  try {
    const data = await api("/api/admin/dashboard");
    const selectedReportDate = document.querySelector("#report-date").value;
    state.people = data.people;
    state.devices = data.devices;
    state.classes = data.classes;
    state.settings = data.settings;
    state.scheduleState = data.schedule_state;
    state.today = data.date;
    document.querySelector("#today-date").textContent = data.date;
    document.querySelector("#report-date").value ||= data.date;
    document.querySelector("#metric-people").textContent = data.people.filter((person) => person.active).length;
    document.querySelector("#metric-attendance").textContent = data.attendance.filter((row) => row.arrival_time).length;
    document.querySelector("#metric-devices").textContent = data.devices.length;
    document.querySelector("#metric-pending").textContent = data.pending_people.length;
    renderSettings(data.settings);
    renderScheduleStatus(data.schedule_state);
    renderClasses();
    renderPeople();
    renderDevices(data.devices);
    renderCodes(data.early_codes);
    renderPending(data.pending_people);
    updatePersonFields();
    if (selectedReportDate && selectedReportDate !== data.date) {
      await loadAttendance();
    } else {
      state.attendance = data.attendance;
      renderAttendance(data.attendance);
    }
  } catch (error) {
    message(error.message, "error");
  }
}

function renderSettings(settings) {
  const settingsForm = document.querySelector("#settings-form");
  for (const [key, value] of Object.entries(settings)) {
    const control = settingsForm.elements.namedItem(key);
    if (!control) continue;
    if (control.type === "checkbox") control.checked = value === "1";
    else control.value = value;
  }
  document.querySelector("#enrollment-settings-form").elements.self_enrollment.checked =
    settings.self_enrollment === "1";
  document.querySelector("#allow-unpaired-devices").checked =
    settings.allow_unpaired_devices === "1";
}

function renderScheduleStatus(scheduleState) {
  const activeStatus = document.querySelector("#active-schedule-status");
  const pendingStatus = document.querySelector("#pending-schedule-status");
  if (!scheduleState?.active?.configured) {
    activeStatus.textContent = "No active schedule for today. Saving the first complete schedule activates it today.";
    pendingStatus.textContent = "";
    return;
  }
  const active = scheduleState.active;
  activeStatus.textContent =
    `Active for ${active.date}: arrival ${active.attendance_start}, cut-off ${active.attendance_cutoff}, departure ${active.leaving_time}; flexibility ${active.flexibility_minutes} min; minimum early-departure duration ${active.minimum_early_leave_minutes} min.`;
  const pending = scheduleState.pending;
  pendingStatus.textContent = scheduleState.has_pending_changes
    ? `Pending from ${scheduleState.pending_effective_date}: arrival ${pending.attendance_start}, cut-off ${pending.attendance_cutoff}, departure ${pending.leaving_time}; flexibility ${pending.flexibility_minutes} min; minimum early-departure duration ${pending.minimum_early_leave_minutes} min. Today's schedule remains unchanged.`
    : `No pending changes. Schedule changes saved now will take effect on ${scheduleState.pending_effective_date}.`;
}

async function refreshScheduleStatus() {
  try {
    const data = await api("/api/admin/schedule");
    state.scheduleState = data.schedule_state;
    state.today = data.date;
    document.querySelector("#today-date").textContent = data.date;
    renderScheduleStatus(data.schedule_state);
  } catch (error) {
    if (error.message !== "Please sign in again.") message(error.message, "error");
  }
}

document.querySelector("#settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.currentTarget));
  values.flexibility_minutes = Number(values.flexibility_minutes);
  values.minimum_early_leave_minutes = Number(values.minimum_early_leave_minutes);
  try {
    const result = await api("/api/admin/settings", { method: "POST", body: JSON.stringify(values) });
    state.settings = result.settings;
    state.scheduleState = result.schedule_state;
    renderSettings(result.settings);
    renderScheduleStatus(result.schedule_state);
    message(result.schedule_activated_today
      ? "The initial schedule is active for today."
      : `Schedule changes saved and will take effect on ${result.schedule_state.pending_effective_date}.`);
  } catch (error) { message(error.message, "error"); }
});

document.querySelector("#enrollment-settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const selfEnrollment = event.currentTarget.elements.self_enrollment.checked;
  try {
    const result = await api("/api/admin/settings", {
      method: "POST",
      body: JSON.stringify({ self_enrollment: selfEnrollment }),
    });
    state.settings = result.settings;
    renderSettings(result.settings);
    message("Student enrollment setting saved.");
  } catch (error) { message(error.message, "error"); }
});

document.querySelector("#password-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.currentTarget));
  try {
    await api("/api/admin/password", { method: "POST", body: JSON.stringify(values) });
    location.href = "/admin";
  } catch (error) { message(error.message, "error"); }
});

function renderClasses() {
  const activeClasses = state.classes.filter((item) => item.active);
  const classSelect = document.querySelector("#person-class");
  const selectedClass = classSelect.value;
  classSelect.innerHTML = activeClasses.length
    ? activeClasses.map((item) => `<option value="${item.id}">${esc(item.name)}</option>`).join("")
    : '<option value="">Create a class first</option>';
  if (activeClasses.some((item) => String(item.id) === selectedClass)) classSelect.value = selectedClass;
  classSelect.disabled = activeClasses.length === 0;
  classSelect.required = activeClasses.length > 0;

  document.querySelector("#classes-list").innerHTML = state.classes.map((item) => `
    <div class="class-item ${item.active ? "" : "archived"}">
      <span>${esc(item.name)}${item.active ? "" : '<small>Archived</small>'}</span>
      <div>
        ${item.active
          ? `<button class="text-button" data-class-edit="${item.id}">Rename</button><button class="text-button danger-text" data-class-delete="${item.id}">Delete</button>`
          : `<button class="text-button" data-class-restore="${item.id}">Restore</button>`}
      </div>
    </div>`).join("") || '<p class="help-text">No classes yet. Add a class to enroll students.</p>';
}

classForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const classId = classForm.elements.class_id.value;
  const name = classForm.elements.name.value.trim();
  try {
    await api(classId ? `/api/admin/classes/${classId}` : "/api/admin/classes", {
      method: "POST",
      body: JSON.stringify({ name }),
    });
    resetClassForm();
    message(classId ? "Class renamed." : "Class created.");
    await refreshDashboard();
  } catch (error) { message(error.message, "error"); }
});

document.querySelector("#cancel-class-edit").addEventListener("click", resetClassForm);
function resetClassForm() {
  classForm.reset();
  classForm.elements.class_id.value = "";
  document.querySelector("#save-class").textContent = "Add class";
  document.querySelector("#cancel-class-edit").classList.add("hidden");
}

document.querySelector("#classes-list").addEventListener("click", async (event) => {
  const editId = event.target.dataset.classEdit;
  const deleteId = event.target.dataset.classDelete;
  const restoreId = event.target.dataset.classRestore;
  if (editId) {
    const item = state.classes.find((entry) => entry.id === Number(editId));
    if (!item) return;
    classForm.elements.class_id.value = item.id;
    classForm.elements.name.value = item.name;
    document.querySelector("#save-class").textContent = "Save class";
    document.querySelector("#cancel-class-edit").classList.remove("hidden");
    classForm.elements.name.focus();
    return;
  }
  if (deleteId) {
    const item = state.classes.find((entry) => entry.id === Number(deleteId));
    if (!item || !window.confirm(`Delete or archive "${item.name}"? Classes with student records are archived so records and attendance history remain intact.`)) return;
    try {
      const result = await api(`/api/admin/classes/${deleteId}`, { method: "DELETE" });
      message(result.message || "Class deleted.");
      await refreshDashboard();
    } catch (error) { message(error.message, "error"); }
  }
  if (restoreId) {
    try {
      await api(`/api/admin/classes/${restoreId}/restore`, { method: "POST", body: "{}" });
      message("Class restored and available for student enrollment.");
      await refreshDashboard();
    } catch (error) { message(error.message, "error"); }
  }
});

function updateGroupFilter(select, rows, selectedValue = "all") {
  const groups = new Map();
  rows.forEach((row) => {
    const key = `${row.kind}|${row.group_name}`;
    groups.set(key, `${row.kind === "student" ? "Class" : "Subject"} · ${row.group_name}`);
  });
  const nextValue = groups.has(selectedValue) ? selectedValue : "all";
  select.innerHTML = '<option value="all">All classes / subjects</option>'
    + [...groups.entries()].sort((a, b) => a[1].localeCompare(b[1]))
      .map(([value, label]) => `<option value="${esc(value)}">${esc(label)}</option>`).join("");
  select.value = nextValue;
}

function renderPeople() {
  const kind = document.querySelector("#people-filter").value;
  const groupFilter = document.querySelector("#people-group-filter");
  const query = document.querySelector("#people-search").value.toLowerCase();
  const activePeople = state.people.filter((person) => person.active);
  const selectedGroup = groupFilter.value;
  updateGroupFilter(groupFilter, activePeople, selectedGroup);
  const people = activePeople.filter((person) => person.active
    && (kind === "all" || person.kind === kind)
    && (groupFilter.value === "all" || `${person.kind}|${person.group_name}` === groupFilter.value)
    && `${person.name} ${person.group_name}`.toLowerCase().includes(query));
  const groups = new Map();
  people.sort((a, b) => a.kind.localeCompare(b.kind) || a.group_name.localeCompare(b.group_name) || a.name.localeCompare(b.name))
    .forEach((person) => {
      const key = `${person.kind}|${person.group_name}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(person);
    });
  const rendered = [...groups.entries()].map(([key, members]) => {
    const [groupKind, ...groupParts] = key.split("|");
    const groupName = groupParts.join("|");
    const title = groupKind === "student" ? `Students · ${groupName}` : `Teachers · ${groupName}`;
    return `<tr class="group-row"><td colspan="5">${esc(title)}<span>${members.length} ${members.length === 1 ? "person" : "people"}</span></td></tr>`
      + members.map((person) => `
        <tr>
          <td><strong>${esc(person.name)}</strong>${person.kind === "student" && person.student_number ? `<small>ID · ${esc(person.student_number)}</small>` : ""}</td>
          <td><span class="tag ${person.kind}">${esc(person.kind)}</span></td>
          <td>${esc(person.group_name)}</td>
          <td>${person.has_face ? '<span class="profile-ok">● Enrolled</span>' : '<span class="profile-missing">● Missing</span>'}</td>
          <td class="action-cell"><button class="text-button" data-edit="${person.id}">Edit</button><button class="text-button danger-text" data-remove="${person.id}">Remove</button></td>
        </tr>`).join("");
  }).join("");
  document.querySelector("#people-table").innerHTML = rendered
    || '<tr><td colspan="5" class="empty-state">No matching people found.</td></tr>';
}

document.querySelector("#people-filter").addEventListener("change", () => {
  document.querySelector("#people-group-filter").value = "all";
  renderPeople();
});
document.querySelector("#people-group-filter").addEventListener("change", renderPeople);
document.querySelector("#people-search").addEventListener("input", renderPeople);
document.querySelector("#people-table").addEventListener("click", async (event) => {
  const editId = event.target.dataset.edit;
  const removeId = event.target.dataset.remove;
  if (editId) {
    const person = state.people.find((item) => item.id === Number(editId));
    if (!person) return;
    form.elements.person_id.value = person.id;
    form.elements.kind.value = person.kind;
    form.elements.kind.disabled = true;
    form.elements.name.value = person.name;
    if (person.kind === "student") {
      form.elements.student_number.value = person.student_number || "";
      form.elements.class_id.value = person.class_id || "";
    }
    updatePersonFields();
    document.querySelector("#person-form-title").textContent = "Edit person";
    document.querySelector("#save-person").textContent = "Save changes";
    document.querySelector("#cancel-edit").classList.remove("hidden");
    document.querySelector("#people").scrollIntoView({ behavior: "smooth" });
  }
  if (removeId && window.confirm("Remove this person from active records? Their attendance history will be retained.")) {
    try {
      await api(`/api/admin/people/${removeId}`, { method: "DELETE" });
      message("Person removed. Historical attendance is retained.");
      await refreshDashboard();
    } catch (error) { message(error.message, "error"); }
  }
});

document.querySelector("#save-device-access").addEventListener("click", async () => {
  const enabled = document.querySelector("#allow-unpaired-devices").checked;
  const warning = enabled
    ? "Allowing all devices means any browser that opens the attendance page will be approved without a code. Continue?"
    : "Turn off automatic device approval? New browsers will need a pairing code.";
  if (!window.confirm(warning)) {
    document.querySelector("#allow-unpaired-devices").checked = state.settings.allow_unpaired_devices === "1";
    return;
  }
  try {
    const result = await api("/api/admin/settings", {
      method: "POST",
      body: JSON.stringify({ allow_unpaired_devices: enabled }),
    });
    state.settings = result.settings;
    message(enabled
      ? "No-code access enabled. Reload the attendance page on each browser to connect it."
      : "No-code access disabled. New browsers now require a pairing code.");
  } catch (error) {
    document.querySelector("#allow-unpaired-devices").checked = state.settings.allow_unpaired_devices === "1";
    message(error.message, "error");
  }
});

document.querySelector("#person-kind").addEventListener("change", updatePersonFields);
function updatePersonFields() {
  const isStudent = form.elements.kind.value === "student";
  document.querySelector("#student-number-field").classList.toggle("hidden", !isStudent);
  document.querySelector("#student-class-field").classList.toggle("hidden", !isStudent);
  document.querySelector("#teacher-info").classList.toggle("hidden", isStudent);
  form.elements.student_number.required = isStudent;
  form.elements.class_id.required = isStudent && state.classes.some((item) => item.active);
}

async function startAdminCamera() {
  try {
    if (!state.cameraStream) {
      state.cameraStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "user" }, audio: false });
      const camera = document.querySelector("#admin-camera");
      camera.srcObject = state.cameraStream;
      await camera.play();
      document.querySelector("#admin-camera-placeholder").classList.add("hidden");
      document.querySelector("#admin-camera-start").textContent = "Stop camera";
    } else {
      state.cameraStream.getTracks().forEach((track) => track.stop());
      state.cameraStream = null;
      document.querySelector("#admin-camera").srcObject = null;
      document.querySelector("#admin-camera-placeholder").classList.remove("hidden");
      document.querySelector("#admin-camera-start").textContent = "Start camera";
    }
  } catch (error) {
    message(error.name === "NotAllowedError" ? "Camera permission denied. Allow camera access in Chrome." : "Unable to start camera. Use localhost or HTTPS and check the camera.", "error");
  }
}

document.querySelector("#admin-camera-start").addEventListener("click", startAdminCamera);
document.querySelector("#capture-photo").addEventListener("click", () => {
  const camera = document.querySelector("#admin-camera");
  if (!camera.videoWidth) return message("Start the camera and wait for the preview before capturing.", "error");
  const pose = nextPose(state.capturedImages);
  if (!pose) {
    state.capturedImages = {};
    renderPoseThumbnails("#photo-preview", state.capturedImages);
  }
  const capturePose = pose || "center";
  const canvas = document.createElement("canvas");
  const ratio = Math.min(1, 640 / camera.videoWidth);
  canvas.width = Math.round(camera.videoWidth * ratio);
  canvas.height = Math.round(camera.videoHeight * ratio);
  canvas.getContext("2d").drawImage(camera, 0, 0, canvas.width, canvas.height);
  state.capturedImages[capturePose] = canvas.toDataURL("image/jpeg", 0.85);
  renderPoseThumbnails("#photo-preview", state.capturedImages);
  const next = nextPose(state.capturedImages);
  document.querySelector("#photo-status").textContent = next
    ? `Captured ${capturePose}. ${posePrompt(next)}`
    : "All three photos captured. The server will verify the center face and both side profiles.";
  document.querySelector("#capture-photo").textContent = next ? `Capture ${next} ${next === "center" ? "photo" : "profile"}` : "Retake all photos";
});
document.querySelector("#clear-photo").addEventListener("click", clearPhoto);
function clearPhoto() {
  state.capturedImages = {};
  renderPoseThumbnails("#photo-preview", state.capturedImages);
  document.querySelector("#capture-photo").textContent = "Capture center photo";
  document.querySelector("#photo-status").textContent = "Capture three photos in order: look straight ahead, turn your head left, then right. Keep one face in the frame.";
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const personId = form.elements.person_id.value;
  const kind = form.elements.kind.value;
  const values = { kind, name: form.elements.name.value };
  if (kind === "student") {
    values.student_number = form.elements.student_number.value;
    values.class_id = form.elements.class_id.value;
  }
  if (Object.keys(state.capturedImages).length) values.images = state.capturedImages;
  if (!personId && !hasAllPoses(state.capturedImages)) return message("Capture the center, left, and right face photos before saving.", "error");
  try {
    await api(personId ? `/api/admin/people/${personId}` : "/api/admin/people", {
      method: "POST", body: JSON.stringify(values),
    });
    message(personId ? "Person record updated." : "Person enrolled with a real face profile.");
    resetPersonForm();
    await refreshDashboard();
  } catch (error) { message(error.message, "error"); }
});

function resetPersonForm() {
  form.reset();
  form.elements.kind.disabled = false;
  form.elements.person_id.value = "";
  form.elements.kind.value = "student";
  document.querySelector("#person-form-title").textContent = "Add a person";
  document.querySelector("#save-person").textContent = "Save person";
  document.querySelector("#cancel-edit").classList.add("hidden");
  clearPhoto();
  updatePersonFields();
}
document.querySelector("#cancel-edit").addEventListener("click", resetPersonForm);

function renderCodes(codes) {
  document.querySelector("#early-codes").innerHTML = codes.map((item) => `
    <div class="code-item"><code>${esc(item.code)}</code><span>Unused · rotates 15 min after generation</span><button class="text-button copy-code" data-code="${esc(item.code)}">Copy</button></div>`).join("");
}
async function refreshEarlyCodes() {
  try {
    const data = await api("/api/admin/early-codes");
    renderCodes(data.early_codes);
  } catch (error) {
    if (error.message !== "Please sign in again.") message(error.message, "error");
  }
}
document.querySelector("#early-codes").addEventListener("click", async (event) => {
  if (!event.target.dataset.code) return;
  try {
    await navigator.clipboard.writeText(event.target.dataset.code);
    message("Early-leave code copied.");
  } catch { message(`Code: ${event.target.dataset.code}`, "info"); }
});

document.querySelector("#generate-device-code").addEventListener("click", async () => {
  try {
    const data = await api("/api/admin/devices/code", { method: "POST", body: "{}" });
    const result = document.querySelector("#device-code-result");
    result.innerHTML = `<span>One-time device code</span><strong>${esc(data.code)}</strong><small>Expires in ${data.expires_in_minutes} minutes · can be used once</small>`;
    result.classList.remove("hidden");
  } catch (error) { message(error.message, "error"); }
});

function renderDevices(devices) {
  document.querySelector("#devices-table").innerHTML = devices.map((device) => `
    <tr><td><strong>${esc(device.name)}</strong></td><td>${esc(device.device_type)}</td><td>${new Date(device.paired_at).toLocaleString()}</td><td><span class="profile-ok">● Connected</span></td><td><button class="text-button danger-text" data-disconnect="${device.id}">Disconnect</button></td></tr>`).join("")
    || '<tr><td colspan="5" class="empty-state">No approved devices. Generate a code to pair one.</td></tr>';
}
document.querySelector("#devices-table").addEventListener("click", async (event) => {
  const id = event.target.dataset.disconnect;
  if (!id || !window.confirm("Disconnect this browser? It will need to be paired again.")) return;
  try {
    await api(`/api/admin/devices/${id}`, { method: "DELETE" });
    message("Device disconnected.");
    await refreshDashboard();
  } catch (error) { message(error.message, "error"); }
});

function renderPending(people) {
  const box = document.querySelector("#pending-box");
  box.classList.toggle("hidden", people.length === 0);
  document.querySelector("#pending-teachers").innerHTML = people.map((person) => `
    <div class="pending-person"><span>${esc(person.kind === "student" ? "Student" : "Teacher")} · ${esc(person.name)} · ${esc(person.group_name)}</span><button class="button small-button primary" data-confirm="${person.id}">Confirm enrollment</button></div>`).join("");
}
document.querySelector("#pending-teachers").addEventListener("click", async (event) => {
  const id = event.target.dataset.confirm;
  if (!id) return;
  try {
    await api(`/api/admin/people/${id}/confirm`, { method: "POST", body: "{}" });
    message("Enrollment confirmed.");
    await refreshDashboard();
  } catch (error) { message(error.message, "error"); }
});

function renderAttendance(rows) {
  document.querySelector("#empty-attendance").classList.toggle("hidden", rows.length > 0);
  const lateDepartures = rows.filter((row) => row.departure_status === "Left Late");
  const lateDepartureAlert = document.querySelector("#late-departure-alert");
  lateDepartureAlert.classList.toggle("hidden", lateDepartures.length === 0);
  lateDepartureAlert.textContent = lateDepartures.length
    ? `Late departures on ${document.querySelector("#report-date").value}: ${lateDepartures.map((row) => `${row.name} (${row.departure_time || "time missing"})`).join(", ")}. Please review these records.`
    : "";
  const groupFilter = document.querySelector("#attendance-group-filter");
  const selectedGroup = groupFilter.value;
  updateGroupFilter(groupFilter, rows, selectedGroup);
  const filteredRows = rows.filter((row) => groupFilter.value === "all"
    || `${row.kind}|${row.group_name}` === groupFilter.value);
  const groups = new Map();
  filteredRows.sort((a, b) => a.kind.localeCompare(b.kind) || a.group_name.localeCompare(b.group_name) || a.name.localeCompare(b.name))
    .forEach((row) => {
      const key = `${row.kind}|${row.group_name}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(row);
    });
  document.querySelector("#attendance-table").innerHTML = [...groups.entries()].map(([key, members]) => {
    const [kind, ...groupParts] = key.split("|");
    const groupName = groupParts.join("|");
    const title = kind === "student" ? `Students · ${groupName}` : `Teachers · ${groupName}`;
    return `<tr class="group-row"><td colspan="9">${esc(title)}<span>${members.length} records</span></td></tr>`
    + members.map((row) => `
    <tr data-record="${row.id}">
      <td><strong>${esc(row.name)}</strong><small>${row.kind === "student" && row.student_number ? `ID · ${esc(row.student_number)}` : esc(row.kind)}</small></td>
      <td>${esc(row.group_name)}</td>
      <td><select data-field="attendance_status">${["Present", "Late", "Absent"].map((item) => `<option ${row.attendance_status === item ? "selected" : ""}>${item}</option>`).join("")}</select></td>
      <td><input data-field="arrival_time" type="time" value="${esc((row.arrival_time || "").slice(0, 5))}"></td>
      <td><select data-field="departure_status">${["", "Left", "Left Early", "Left Late"].map((item) => `<option value="${item}" ${row.departure_status === item ? "selected" : ""}>${item || "—"}</option>`).join("")}</select></td>
      <td><input data-field="departure_time" type="time" value="${esc((row.departure_time || "").slice(0, 5))}"></td>
      <td><input data-field="early_leave_reason" value="${esc(row.early_leave_reason || "")}" placeholder="Reason" maxlength="500"></td>
      <td><button class="text-button" data-save-record="${row.id}">Save</button></td>
      <td><button class="text-button danger-text" data-delete-record="${row.id}">Delete</button></td>
    </tr>`).join("");
  }).join("");
}
document.querySelector("#attendance-group-filter").addEventListener("change", () => renderAttendance(state.attendance));

async function loadAttendance() {
  const date = document.querySelector("#report-date").value;
  if (!date) return;
  const version = ++attendanceLoadVersion;
  try {
    const data = await api(`/api/admin/attendance?date=${encodeURIComponent(date)}`);
    if (version !== attendanceLoadVersion) return;
    state.attendance = data.attendance;
    renderAttendance(data.attendance);
    if (data.date === state.today) {
      document.querySelector("#metric-attendance").textContent =
        data.attendance.filter((row) => row.arrival_time).length;
    }
  } catch (error) {
    if (version === attendanceLoadVersion) message(error.message, "error");
  }
}
document.querySelector("#report-date").addEventListener("change", loadAttendance);
window.setInterval(() => {
  if (document.querySelector("#report-date").value === state.today) loadAttendance();
}, 10_000);
document.querySelector("#attendance-table").addEventListener("click", async (event) => {
  const deleteId = event.target.dataset.deleteRecord;
  if (deleteId) {
    if (!window.confirm("Permanently delete this attendance record? This cannot be undone.")) return;
    try {
      const result = await api(`/api/admin/attendance/${deleteId}`, { method: "DELETE" });
      message(result.message);
      await loadAttendance();
      await refreshDashboard();
    } catch (error) { message(error.message, "error"); }
    return;
  }
  const id = event.target.dataset.saveRecord;
  if (!id) return;
  const row = event.target.closest("tr");
  const values = {};
  row.querySelectorAll("[data-field]").forEach((control) => { values[control.dataset.field] = control.value; });
  try {
    await api(`/api/admin/attendance/${id}`, { method: "POST", body: JSON.stringify(values) });
    message("Attendance correction saved.");
    await loadAttendance();
  } catch (error) { message(error.message, "error"); }
});

document.querySelector("#download-report").addEventListener("click", async () => {
  const date = document.querySelector("#report-date").value;
  try {
    const response = await fetch(`/api/admin/report.xlsx?date=${encodeURIComponent(date)}`);
    if (!response.ok) {
      const data = await response.json();
      throw new Error(data.error || "Report export failed.");
    }
    const blob = await response.blob();
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `attendance-${date}.xlsx`;
    link.click();
    URL.revokeObjectURL(link.href);
  } catch (error) { message(error.message, "error"); }
});
document.querySelector("#end-day").addEventListener("click", async () => {
  const date = document.querySelector("#report-date").value;
  if (!date || !window.confirm(`End and finalize the school day for ${date}? People without attendance will be marked Absent.`)) return;
  try {
    const result = await api("/api/admin/end-day", { method: "POST", body: JSON.stringify({ date }) });
    message(result.message);
    await refreshDashboard();
    await loadAttendance();
  } catch (error) { message(error.message, "error"); }
});

document.querySelector("#logout-button").addEventListener("click", async () => {
  try {
    await api("/api/admin/logout", { method: "POST", body: "{}" });
    location.href = "/admin";
  } catch (error) { message(error.message, "error"); }
});

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}
const poseOrder = ["center", "left", "right"];
function nextPose(images) {
  return poseOrder.find((pose) => !images[pose]) || null;
}
function posePrompt(pose) {
  if (pose === "center") return "Look straight at the camera and capture the center photo.";
  return `Turn your head to your ${pose} and capture the ${pose} profile.`;
}
function hasAllPoses(images) {
  return poseOrder.every((pose) => images[pose]);
}
function renderPoseThumbnails(selector, images) {
  const container = document.querySelector(selector);
  const poses = poseOrder.filter((pose) => images[pose]);
  container.classList.toggle("hidden", poses.length === 0);
  document.querySelector("#clear-photo").classList.toggle("hidden", poses.length === 0);
  container.innerHTML = poses.map((pose) => `
    <div class="pose-thumbnail"><img src="${images[pose]}" alt="${pose} face profile"><span>${pose}</span></div>`).join("");
}
refreshDashboard();
window.setInterval(refreshEarlyCodes, 60_000);
window.setInterval(refreshScheduleStatus, 30_000);
