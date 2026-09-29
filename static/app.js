"use strict";

const $ = id => document.getElementById(id);
const CASES = ["INDIVIDUAL", "GROUP", "DUAL", "RELATIONAL"];
// Each page owns its draft slot; one tab must not erase another tab's work.
const draftWriter = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
const state = {
  user: null, csrf: "", llm: false, users: [], adminTasks: [], offset: 0,
  selected: new Set(), importText: "", importReady: false, importBusy: false, adminRequest: 0,
  queue: [], queueVisible: 300, current: null, task: null, annotation: null, assignment: new Map(),
  activeSubject: 1, workStatus: "pending", workSearch: "", workCase: "",
  dirty: false, version: 0, saving: null, saveTimer: null, loading: false,
  suggestion: null, suggesting: false, submitting: false, reopening: false,
  reopenedTask: null,
  session: 0, loggingOut: false, recovery: null, storageWarning: false,
  taskGeneration: 0,
  recoverySource: null,
  view: { query: { scale: 1, x: 0, y: 0 }, target: { scale: 1, x: 0, y: 0 } },
};

async function api(path, options = {}, download = false) {
  const session = state.session, controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), path.endsWith('/suggest') ? 75000 : 30000);
  try {
  const response = await fetch(path, {
    credentials: "same-origin", ...options,
    signal: controller.signal,
    headers: { ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.method && options.method !== "GET" && state.csrf ? { "X-CSRF-Token": state.csrf } : {}),
      ...options.headers },
  });
  if (session !== state.session) throw new Error("Phiên đăng nhập đã thay đổi.");
  if (download && response.ok) {
    const blob = await response.blob();
    if (session !== state.session) throw new Error("Phiên đăng nhập đã thay đổi.");
    return { blob, count: response.headers.get("X-Export-Count") };
  }
  const data = await response.json().catch(() => {
    throw new Error(`Phản hồi server không hợp lệ (HTTP ${response.status}). Hãy thử lại.`);
  });
  if (session !== state.session) throw new Error("Phiên đăng nhập đã thay đổi.");
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/auth/login") showLogin();
    const error = new Error(data.error || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return data;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("Server phản hồi quá lâu. Bản nháp vẫn được giữ; hãy thử lưu lại.");
    throw error;
  } finally { clearTimeout(timeout); }
}
const post = (path, data) => api(path, { method: "POST", body: JSON.stringify(data) });

function showLogin() {
  persistLocalDraft();
  clearTimeout(state.saveTimer);
  state.session += 1;
  state.taskGeneration += 1;
  state.user = null;
  state.csrf = "";
  state.current = null; state.task = null; state.annotation = null;
  state.reopenedTask = null;
  state.queue = []; state.assignment = new Map(); state.recovery = null; state.recoverySource = null;
  state.dirty = false; state.saving = null; state.loading = false;
  state.submitting = false; state.suggesting = false; state.reopening = false; state.loggingOut = false;
  state.suggestion = null; state.selected.clear();
  state.users = []; state.adminTasks = []; state.offset = 0;
  state.importText = ""; state.importReady = false;
  $("importFile").value = ""; $("importResult").textContent = "";
  $("commitImport").disabled = true;
  $("createUsersBulk").reset(); resetCreateUserForm();
  $("userRows").replaceChildren(); $("taskRows").replaceChildren();
  $("loginForm").reset();
  $("taskEditor").hidden = true; $("emptyWork").hidden = false;
  $("logout").disabled = false;
  $("app").hidden = true;
  $("login").hidden = false;
}
function showApp(me) {
  state.user = me.user;
  state.csrf = me.csrf;
  state.llm = me.llm_enabled ?? false;
  $("login").hidden = true;
  $("app").hidden = false;
  $("username").textContent = me.user.username;
  $("roleLabel").textContent = me.user.role === "ADMIN" ? "Administration" : "Annotation workspace";
  $("admin").hidden = me.user.role !== "ADMIN";
  $("work").hidden = me.user.role !== "ANNOTATOR";
  if (me.user.role === "ADMIN") {
    resetCreateUserForm();
    return loadAdmin();
  }
  const queuePreference = stored("rcr:queueCollapsed");
  setQueueCollapsed(queuePreference === "1" ||
    (queuePreference === null && !!window.matchMedia?.("(max-width: 1200px)").matches));
  return loadQueue();
}

function setQueueCollapsed(collapsed) {
  $("work").classList.toggle("queue-collapsed", collapsed);
  $("toggleQueue").textContent = collapsed ? "›" : "‹";
  $("toggleQueue").title = collapsed ? "Mở hàng đợi" : "Thu gọn hàng đợi";
  $("toggleQueue").setAttribute("aria-label", $("toggleQueue").title);
  $("toggleQueue").setAttribute("aria-expanded", String(!collapsed));
  $("queueContent").hidden = collapsed;
  try { localStorage.setItem("rcr:queueCollapsed", collapsed ? "1" : "0"); } catch { /* Optional. */ }
  // The image container changes width when the queue closes or opens.
  requestAnimationFrame(() => {
    for (const side of ["query", "target"]) fitImage(side);
  });
}

function localPrefix(sid = state.current) {
  return `rcr:draft:${state.user?.id}:${sid}`;
}
function localKey(sid = state.current) { return `${localPrefix(sid)}:${draftWriter}`; }
function stored(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}
function reopenedKey(sid = state.current) {
  return `rcr:reopened:${state.user?.id}:${sid}`;
}
function rememberReopenedTask(task) {
  state.reopenedTask = task.sample_id;
  try {
    localStorage.setItem(reopenedKey(task.sample_id), JSON.stringify({
      sample_id: task.sample_id, revision: task.revision,
    }));
  } catch { /* The in-memory marker still covers the current session. */ }
}
function restoreReopenedMarker(task) {
  state.reopenedTask = null;
  if (task.status !== "SUBMITTED" && task.reopened) {
    rememberReopenedTask(task);
    return;
  }
  try {
    const marker = JSON.parse(stored(reopenedKey(task.sample_id)) || "null");
    if (marker?.sample_id === task.sample_id && marker.revision === task.revision) {
      state.reopenedTask = task.sample_id;
    } else localStorage.removeItem(reopenedKey(task.sample_id));
  } catch {
    try { localStorage.removeItem(reopenedKey(task.sample_id)); } catch { /* Optional. */ }
  }
}
function clearReopenedMarker(sid = state.current) {
  if (state.reopenedTask === sid) state.reopenedTask = null;
  try { localStorage.removeItem(reopenedKey(sid)); } catch { /* Optional. */ }
}
function removeLocalDraft(sid = state.current) {
  try { localStorage.removeItem(localKey(sid)); } catch { /* Server data is authoritative. */ }
}
function removeRecoverySource(source) {
  if (!source) return;
  try {
    if (stored(source.key) === source.text) localStorage.removeItem(source.key);
  } catch { /* Keep the recovery copy if storage is unavailable. */ }
}
function findRecoveryDraft() {
  const prefix = localPrefix(), candidates = [], confirmed = [];
  const current = JSON.stringify(currentAnnotation());
  try {
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i);
      if (key !== prefix && !key?.startsWith(`${prefix}:`)) continue;
      try {
        const text = stored(key), record = JSON.parse(text);
        if (record?.sample_id !== state.current || !record.annotation ||
            !Array.isArray(record.annotation.subjects) || !Array.isArray(record.annotation.select_texts)) continue;
        if (JSON.stringify(record.annotation) !== current) candidates.push({ record, key, text });
        else confirmed.push({ key, text });
      } catch { /* A broken cache entry must not hide other valid drafts. */ }
    }
  } catch { return; }
  confirmed.forEach(removeRecoverySource);
  candidates.sort((a, b) => (b.record.updated_at || 0) - (a.record.updated_at || 0));
  const latest = candidates[0];
  state.recovery = latest ? latest.record : null;
  state.recoverySource = latest ? { key: latest.key, text: latest.text } : null;
}
function persistLocalDraft() {
  if (!state.user || !state.task || !state.dirty || state.recovery) return;
  try {
    localStorage.setItem(localKey(), JSON.stringify({
      sample_id: state.current, revision: state.task.revision,
      annotation: currentAnnotation(), updated_at: Date.now(),
    }));
  } catch {
    state.storageWarning = true;
    $("workError").textContent = "Trình duyệt không lưu được bản nháp dự phòng. Hãy giữ tab mở và bấm Lưu nháp.";
  }
}
function downloadDraft(record = null) {
  const data = record || { sample_id: state.current, revision: state.task?.revision,
    annotation: currentAnnotation() };
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
  const link = document.createElement("a"); link.href = url; link.download = "rcr-draft-recovery.json";
  link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
const busy = () => state.loading || state.submitting || state.reopening || state.loggingOut;
const editingBlocked = () => busy() || !!state.recovery;
function fillAnnotation(annotation) {
  state.annotation = annotation;
  state.assignment = new Map();
  for (const subject of annotation.subjects) {
    for (const id of subject.identity_ids) state.assignment.set(String(id), subject.subject_id);
  }
  $("select1").value = annotation.select_texts[0] || "";
  $("select2").value = annotation.select_texts[1] || "";
  $("targetText").value = annotation.target_condition || "";
}
function restoreLocalDraft() {
  if (!state.recovery || busy() || state.task.status === "SUBMITTED") return;
  if (!confirm("Dùng bản nháp trên trình duyệt thay cho nội dung đang hiển thị? Bấm Lưu nháp sau khi kiểm tra.")) return;
  fillAnnotation(state.recovery.annotation); state.recovery = null;
  state.dirty = true; state.version += 1;
  persistLocalDraft(); renderWork();
  $("saveState").textContent = "Đã khôi phục nội dung · hãy kiểm tra rồi lưu nháp";
}
function toast(message) {
  const node = $("adminMessage");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.hidden = true; }, 6500);
}
function resetCreateUserForm() {
  const form = $("createUser");
  form.reset();
  form.querySelectorAll("input").forEach(input => { input.value = ""; });
}
function timeLabel(seconds) {
  return seconds ? new Date(seconds * 1000).toLocaleString("vi-VN") : "—";
}
function td(row, value, tag = "td") {
  const cell = document.createElement(tag);
  cell.textContent = String(value ?? "—");
  row.appendChild(cell);
  return cell;
}
function badge(status) {
  const span = document.createElement("span");
  span.className = `pill ${status}`;
  span.textContent = status;
  return span;
}

async function loadAdmin() {
  try {
    await Promise.all([loadUsers(), loadAdminTasks()]);
  } catch (error) { toast(error.message); }
}
async function loadUsers() {
  const data = await api("/api/admin/users");
  state.users = data.users;
  for (const id of ["adminAssignee", "assignUser"]) {
    const select = $(id), old = select.value;
    while (select.options.length > (id === "adminAssignee" ? 2 : 1)) select.remove(select.options.length - 1);
    for (const user of state.users.filter(u => u.role === "ANNOTATOR")) {
      const option = document.createElement("option");
      option.value = user.id;
      option.textContent = `${user.username}${user.active ? "" : " (khóa)"}`;
      if (!user.active && id === "assignUser") option.disabled = true;
      select.appendChild(option);
    }
    select.value = old;
  }
  const root = $("userRows"); root.replaceChildren();
  for (const user of state.users) {
    const row = document.createElement("tr");
    td(row, user.username); td(row, user.email); td(row, user.role); td(row, user.assigned);
    td(row, user.pending); td(row, user.in_progress);
    td(row, user.completed); td(row, user.ever_completed);
    td(row, user.assigned ? `${Math.round(100 * user.completed / user.assigned)}%` : "0%");
    td(row, user.active ? "Hoạt động" : "Đã khóa");
    const actions = document.createElement("td");
    if (user.role === "ANNOTATOR") {
      const setEmail = document.createElement("button"); setEmail.textContent = "Đặt email";
      setEmail.onclick = async () => {
        const email = prompt(`Email cho ${user.username}:`, user.email || "");
        if (email === null) return;
        try { const result = await post("/api/admin/users/update", { user_id: user.id, email });
          await loadUsers(); toast(result.backup_warning ||
            (email.trim() ? "Đã cập nhật email." : "Đã xóa email.")); }
        catch (error) { toast(error.message); }
      };
      const reset = document.createElement("button"); reset.textContent = "Đặt lại mật khẩu";
      reset.onclick = async () => {
        const password = prompt(`Mật khẩu mới cho ${user.username} (ít nhất 4 ký tự):`);
        if (password === null) return;
        try { await post("/api/admin/users/update", { user_id: user.id, password });
          toast("Đã đổi mật khẩu và đăng xuất các phiên cũ."); }
        catch (error) { toast(error.message); }
      };
      const toggle = document.createElement("button");
      toggle.textContent = user.active ? "Khóa" : "Mở khóa";
      toggle.onclick = async () => {
        if (user.active && !confirm(`Khóa ${user.username}? Những task đã giao sẽ được giữ nguyên.`)) return;
        try { await post("/api/admin/users/update", { user_id: user.id, active: !user.active });
          await loadUsers(); toast("Đã cập nhật user."); }
        catch (error) { toast(error.message); }
      };
      actions.append(setEmail, document.createTextNode(" "), reset, document.createTextNode(" "), toggle);
    }
    row.appendChild(actions); root.appendChild(row);
  }
}

function filters() {
  const query = new URLSearchParams();
  for (const [id, key] of [["adminSearch", "search"], ["adminStatus", "status"],
    ["adminCase", "case_type"], ["adminSplit", "split"], ["adminAssignee", "assignee_id"]]) {
    if ($(id).value.trim()) query.set(key, $(id).value.trim());
  }
  return query;
}
async function loadAdminTasks() {
  const requestId = ++state.adminRequest;
  const query = filters(); query.set("offset", state.offset); query.set("limit", "100");
  const data = await api(`/api/admin/tasks?${query}`);
  if (requestId !== state.adminRequest) return;
  state.adminTasks = data.tasks;
  state.selected.clear();
  $("selectPage").checked = false;
  renderAdminRows();
  const labels = [["UNASSIGNED", "Chưa giao"], ["ASSIGNED", "Đã giao"],
    ["IN_PROGRESS", "Đang làm"], ["SUBMITTED", "Đã nộp"]];
  $("adminStats").replaceChildren();
  for (const [key, label] of labels) {
    const card = document.createElement("div"); card.className = "stat";
    const number = document.createElement("strong"); number.textContent = data.counts[key] || 0;
    const caption = document.createElement("span"); caption.textContent = label;
    card.append(number, caption); $("adminStats").appendChild(card);
  }
  $("taskTotal").textContent = `${data.total} task · trang ${Math.floor(state.offset / 100) + 1}`;
  $("pagePrev").disabled = state.offset === 0;
  $("pageNext").disabled = state.offset + 100 >= data.total;
}
function renderAdminRows() {
  const root = $("taskRows"); root.replaceChildren();
  for (const task of state.adminTasks) {
    const row = document.createElement("tr");
    const selection = document.createElement("td");
    const checkbox = document.createElement("input"); checkbox.type = "checkbox";
    checkbox.checked = state.selected.has(task.sample_id);
    checkbox.setAttribute("aria-label", `Chọn ${task.sample_id}`);
    checkbox.onchange = () => {
      if (checkbox.checked) state.selected.add(task.sample_id);
      else state.selected.delete(task.sample_id);
      $("selectionSummary").textContent = `${state.selected.size} task được chọn`;
    };
    selection.appendChild(checkbox); row.appendChild(selection);
    td(row, task.sample_id); td(row, task.case_type); td(row, task.split);
    td(row, task.assignee); const status = document.createElement("td");
    status.appendChild(badge(task.status)); row.appendChild(status);
    td(row, timeLabel(task.updated_at)); root.appendChild(row);
  }
  if (!state.adminTasks.length) {
    const row = document.createElement("tr"); const empty = td(row, "Không có task phù hợp.");
    empty.colSpan = 7; root.appendChild(row);
  }
  $("selectionSummary").textContent = `${state.selected.size} task được chọn`;
}
async function assignSelected(assignee) {
  const ids = [...state.selected];
  if (!ids.length) { toast("Hãy chọn task trong bảng."); return; }
  if (assignee !== null && !assignee) { toast("Hãy chọn annotator."); return; }
  let result;
  try {
    result = await post("/api/admin/assign", { ids, assignee_id: assignee });
  } catch (error) {
    if (error.status !== 409 || !confirm(
      `${error.message}\n\nNếu tiếp tục, bản cũ được LƯU VÀO LỊCH SỬ, task sẽ mở lại và nháp hiện tại bị thay thế. Tiếp tục?`
    )) { toast(error.message); return; }
    result = await post("/api/admin/assign", { ids, assignee_id: assignee, force: true });
  }
  await loadAdminTasks(); await loadUsers();
  toast(result.backup_warning || "Đã cập nhật người làm task.");
}
async function importPreview(commit) {
  if (state.importBusy || (commit && !state.importReady)) return;
  state.importBusy = true;
  $("importFile").disabled = true; $("previewImport").disabled = true;
  $("commitImport").disabled = true;
  try {
    if (!commit) {
      const file = $("importFile").files[0];
      if (!file) { toast("Chọn file JSON/JSONL trước."); return; }
      if (file.size > 30_000_000) { toast("File lớn hơn 30 MB."); return; }
      state.importText = await file.text();
    }
    const result = await post("/api/admin/import", { text: state.importText, commit });
    state.importReady = !commit && !result.invalid && result.valid > 0;
    $("commitImport").disabled = commit || !state.importReady;
    $("importResult").textContent = `${result.valid} mới hợp lệ · ${result.duplicate} đã có · ${result.invalid} lỗi` +
      (result.errors.length ? "\n" + result.errors.map(e => `Dòng ${e.line}: ${e.error}`).join("\n") : "") +
      (commit ? `\nĐã import ${result.imported} task.` : "") +
      (result.backup_warning ? `\n${result.backup_warning}` : "");
    if (commit) { await loadAdminTasks(); toast(result.backup_warning || "Import hoàn tất."); }
  } catch (error) {
    state.importReady = false;
    $("importResult").textContent = error.message;
    if (!commit) $("commitImport").disabled = true;
  } finally {
    state.importBusy = false;
    $("importFile").disabled = false; $("previewImport").disabled = false;
    $("commitImport").disabled = !state.importReady;
  }
}
async function exportResults() {
  try {
    const query = filters(); query.delete("status");
    const { blob, count } = await api(`/api/admin/export?${query}`, {}, true);
    if (!blob.size) { toast("Bộ lọc hiện tại chưa có task đã nộp."); return; }
    const url = URL.createObjectURL(blob), a = document.createElement("a");
    a.href = url; a.download = "rcr-submitted.jsonl"; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast(`Đã export ${count} task đã nộp.`);
  } catch (error) { toast(error.message); }
}

const isTwo = () => state.annotation && ["DUAL", "RELATIONAL"].includes(state.annotation.case_type);
const selectedIds = subject => state.task.candidate_identity_ids.filter(id => state.assignment.get(id) === subject);
function filteredQueue() {
  return state.queue.filter(task => {
    if (state.workStatus === "pending" && task.status === "SUBMITTED") return false;
    if (state.workStatus === "completed" && task.status !== "SUBMITTED") return false;
    if (state.workCase && task.case_type !== state.workCase) return false;
    if (state.workSearch && !task.sample_id.toLowerCase().includes(state.workSearch)) return false;
    return true;
  });
}
async function loadQueue() {
  if (busy()) return;
  state.loading = true;
  if (state.task) renderWork();
  renderQueue();
  try {
    await flushDraft();
    const data = await api("/api/work/tasks");
    state.queue = data.tasks;
    renderQueue();
    if (!state.queue.length) {
      state.current = null; state.task = null; state.annotation = null;
      $("emptyWork").textContent = "Bạn chưa được giao task nào.";
      $("taskEditor").hidden = true; $("emptyWork").hidden = false;
      return;
    }
    const previous = stored(`rcr:lastTask:${state.user.id}`);
    const initial = state.queue.find(t => t.sample_id === previous) ||
      state.queue.find(t => t.status !== "SUBMITTED") || state.queue[0];
    await loadTask(initial.sample_id);
  } catch (error) {
    $("emptyWork").textContent = error.message;
    $("workError").textContent = error.message;
  } finally {
    state.loading = false;
    if (state.task) renderWork();
    renderQueue();
  }
}
function renderQueue() {
  const completed = state.queue.filter(task => task.status === "SUBMITTED").length;
  $("progressText").textContent = `${completed} / ${state.queue.length} đã nộp`;
  $("progressFill").style.width = state.queue.length ? `${100 * completed / state.queue.length}%` : "0%";
  const root = $("queueList"); root.replaceChildren();
  const filtered = filteredQueue();
  const currentPosition = filtered.findIndex(t => t.sample_id === state.current);
  if (currentPosition >= state.queueVisible) state.queueVisible = currentPosition + 1;
  for (const task of filtered.slice(0, state.queueVisible)) {
    const button = document.createElement("button");
    button.className = `queue-item ${task.sample_id === state.current ? "active" : ""}`;
    const mark = document.createElement("span"); mark.className = "check";
    mark.textContent = task.status === "SUBMITTED" ? "✓" : "○";
    const detail = document.createElement("span"), name = document.createElement("strong"), kind = document.createElement("small");
    name.textContent = task.sample_id; kind.textContent = `${task.case_type} · ${task.status}`;
    detail.append(name, kind); button.append(mark, detail);
    button.onclick = () => switchTask(task.sample_id);
    button.disabled = busy();
    root.appendChild(button);
  }
  if (!root.children.length) {
    const empty = document.createElement("div"); empty.className = "queue-empty";
    empty.textContent = "Không có task phù hợp bộ lọc."; root.appendChild(empty);
  }
  if (filtered.length > state.queueVisible) {
    const more = document.createElement("button");
    more.className = "queue-item";
    more.textContent = `Xem thêm (${filtered.length - state.queueVisible} task còn lại)`;
    more.onclick = () => { state.queueVisible += 300; renderQueue(); };
    root.appendChild(more);
  }
  const items = filteredQueue(), pos = items.findIndex(t => t.sample_id === state.current);
  $("prevTask").disabled = busy() || pos <= 0;
  $("nextTask").disabled = busy() || pos < 0 || pos >= items.length - 1;
}
function updateMeta(task) {
  const index = state.queue.findIndex(t => t.sample_id === task.sample_id);
  if (index >= 0) state.queue[index] = { ...state.queue[index], ...task };
  renderQueue();
  $("workStatus").className = `pill ${task.status}`;
  $("workStatus").textContent = task.status;
}
async function switchTask(sid) {
  if (sid === state.current || busy()) return;
  try {
    state.loading = true;
    renderWork(); renderQueue();
    await flushDraft();
    await loadTask(sid);
  } catch (error) { $("workError").textContent = error.message; }
  finally { state.loading = false; if (state.task) renderWork(); renderQueue(); }
}
function navigate(delta) {
  const items = filteredQueue(), pos = items.findIndex(t => t.sample_id === state.current);
  if (items[pos + delta]) switchTask(items[pos + delta].sample_id);
}
async function loadTask(sid) {
  const response = await api(`/api/work/tasks/${encodeURIComponent(sid)}`);
  state.taskGeneration += 1;
  state.current = sid; state.task = response.task;
  restoreReopenedMarker(state.task);
  state.annotation = response.annotation || {
    case_type: state.task.case_type,
    subjects: state.task.initial_subjects.length ? state.task.initial_subjects :
      [{ subject_id: 1, identity_ids: [] }, ...(state.task.case_type === "DUAL" ||
         state.task.case_type === "RELATIONAL" ? [{ subject_id: 2, identity_ids: [] }] : [])],
    select_texts: ["", ...(state.task.case_type === "DUAL" ||
      state.task.case_type === "RELATIONAL" ? [""] : [])], target_condition: "",
  };
  fillAnnotation(state.annotation);
  state.recovery = null; state.recoverySource = null;
  findRecoveryDraft();
  state.activeSubject = 1; state.dirty = false; state.version = 0;
  clearTimeout(state.saveTimer);
  $("workTitle").textContent = `Task ${state.queue.findIndex(t => t.sample_id === sid) + 1} / ${state.queue.length}`;
  $("workId").textContent = sid;
  $("emptyWork").hidden = true; $("taskEditor").hidden = false;
  $("select1").value = state.annotation.select_texts[0] || "";
  $("select2").value = state.annotation.select_texts[1] || "";
  $("targetText").value = state.annotation.target_condition || "";
  $("llmNote").value = "";
  showSuggestion(response.suggestion);
  $("workError").textContent = "";
  $("saveState").textContent = state.task.status === "SUBMITTED" ? "Đã nộp · chỉ đọc" : "Đã tải task";
  for (const side of ["query", "target"]) {
    $(`${side}Error`).hidden = true;
    $(`${side}Image`).src = state.task[side].image_url;
  }
  try { localStorage.setItem(`rcr:lastTask:${state.user.id}`, sid); } catch { /* Optional. */ }
  renderWork(); updateMeta(state.task);
}
function showSuggestion(suggestion) {
  state.suggestion = suggestion;
  $("suggestion").hidden = !suggestion;
  const fields = $("suggestionFields");
  fields.replaceChildren();
  if (!suggestion) return;
  const entries = [
    ["Subject 1", "select1", suggestion.select_texts?.[0]],
    ...(isTwo() ? [["Subject 2", "select2", suggestion.select_texts?.[1]]] : []),
    ["Target condition", "targetText", suggestion.target_condition],
  ];
  for (const [label, id, value] of entries) {
    if (typeof value !== "string") continue;
    const row = document.createElement("div"); row.className = "suggestion-row";
    const heading = document.createElement("div"); heading.className = "suggestion-heading";
    const name = document.createElement("strong"); name.textContent = label;
    const apply = document.createElement("button"); apply.type = "button";
    apply.textContent = "Áp dụng"; apply.dataset.applyTo = id;
    apply.onclick = () => applySuggestion(id, value, apply);
    const content = document.createElement("p"); content.textContent = value;
    heading.append(name, apply); row.append(heading, content); fields.append(row);
  }
  updateSuggestionButtons();
}
function updateSuggestionButtons() {
  const blocked = !state.task || state.task.status === "SUBMITTED" || editingBlocked();
  $("suggestionFields").querySelectorAll("[data-apply-to]").forEach(button => {
    button.disabled = blocked || button.classList.contains("applied");
  });
}
function applySuggestion(id, value, button) {
  if (!state.suggestion || !state.task || state.task.status === "SUBMITTED" || editingBlocked()) return;
  $(id).value = value;
  button.classList.add("applied"); button.textContent = "✓ Đã áp dụng";
  markDirty(); updateSuggestionButtons();
}
function syncCase() {
  if (!state.annotation) return;
  if (!isTwo()) state.annotation.case_type = selectedIds(1).length > 1 ? "GROUP" : "INDIVIDUAL";
}
function setCase(mode) {
  if (!state.task || state.task.status === "SUBMITTED" || editingBlocked()) return;
  const two = mode !== "one";
  if (!two && isTwo() && selectedIds(2).length &&
      !confirm("Đổi về một Subject sẽ bỏ các identity đã gán cho S2. Tiếp tục?")) return;
  if (!two) for (const id of selectedIds(2)) state.assignment.delete(id);
  state.annotation.case_type = two ? mode : "INDIVIDUAL";
  showSuggestion(null);
  if (!two) state.activeSubject = 1;
  syncCase(); markDirty(); renderWork();
}
function toggleIdentity(id) {
  if (!state.task || state.task.status === "SUBMITTED" || editingBlocked() ||
      !state.task.candidate_identity_ids.includes(id)) return;
  if (state.assignment.get(id) === state.activeSubject) state.assignment.delete(id);
  else state.assignment.set(id, state.activeSubject);
  showSuggestion(null);
  syncCase(); markDirty(); renderWork();
}
function renderBoxes() {
  for (const side of ["query", "target"]) {
    const root = $(`${side}Boxes`); root.replaceChildren();
    for (const box of state.task[side].boxes) {
      const subject = state.assignment.get(box.identity_id);
      const button = document.createElement("button");
      button.type = "button";
      button.disabled = state.task.status === "SUBMITTED" || editingBlocked() ||
        !state.task.candidate_identity_ids.includes(box.identity_id);
      button.className = `person-box ${subject ? `s${subject}` : ""}`;
      button.style.left = `${100 * box.x}%`; button.style.top = `${100 * box.y}%`;
      button.style.width = `${100 * box.width}%`; button.style.height = `${100 * box.height}%`;
      button.title = `Identity ${box.identity_id}${subject ? ` → S${subject}` : ""}`;
      button.onpointerdown = event => event.stopPropagation();
      button.onclick = event => { event.stopPropagation(); toggleIdentity(box.identity_id); };
      const tag = document.createElement("span"); tag.className = "box-tag";
      tag.textContent = `${subject ? `S${subject} · ` : ""}${box.identity_id}`;
      button.appendChild(tag); root.appendChild(button);
    }
  }
}
function cleanSlot(value) { return value.replace(/\s+/g, " ").trim().replace(/[.;,\s]+$/g, ""); }
function conditionBody(value) {
  return cleanSlot(value).replace(/^then retrieve target images where\s+/i, "");
}
function currentAnnotation() {
  const count = isTwo() ? 2 : 1;
  return {
    case_type: state.annotation.case_type,
    subjects: Array.from({ length: count }, (_, i) => ({
      subject_id: i + 1, identity_ids: selectedIds(i + 1),
    })),
    select_texts: [$("select1").value, ...(count === 2 ? [$("select2").value] : [])],
    target_condition: $("targetText").value,
  };
}
function hasLlmDraft() {
  if (!state.task) return false;
  const a = currentAnnotation();
  return a.select_texts.every(text => text.trim()) && a.target_condition.trim();
}
function updateSuggestButton() {
  if (!state.task) return;
  const completed = state.task.status === "SUBMITTED", blocked = editingBlocked();
  $("suggestBtn").disabled = completed || blocked || !state.llm || state.suggesting || !hasLlmDraft();
  $("suggestBtn").title = !state.llm
    ? "Cần cấu hình Gemini trên server"
    : !hasLlmDraft()
      ? "Hãy gạch ý cho tất cả SELECT và TARGET trước; có thể viết tiếng Việt"
      : "Sửa/dịch bản nháp sang tiếng Anh và chuẩn hóa format RCR";
}
function renderPreview() {
  if (!state.task) return;
  const a = currentAnnotation(), desc1 = cleanSlot(a.select_texts[0]),
    desc2 = a.select_texts[1] ? cleanSlot(a.select_texts[1]) : "",
    condition = conditionBody(a.target_condition);
  const desc = `Identify Subject 1 as ${desc1 || "[…]"}` +
    (isTwo() ? ` and Subject 2 as ${desc2 || "[…]"}` : "");
  $("previewText").textContent = `${desc}; then retrieve target images where ${condition || "[…]"}.`;
}
function renderWork() {
  if (!state.task) return;
  syncCase();
  const two = isTwo(), completed = state.task.status === "SUBMITTED", blocked = editingBlocked();
  $("draftRecovery").hidden = !state.recovery;
  $("restoreLocal").disabled = completed || busy();
  $("reloadTask").disabled = busy();
  $("downloadDraft").disabled = busy();
  $("subject1").disabled = completed || blocked;
  $("subject2").disabled = completed || blocked;
  $("oneCase").textContent = selectedIds(1).length > 1 ? "GROUP" : "INDIVIDUAL";
  const instructions = {
    INDIVIDUAL: "Một người: SELECT mô tả S1 trong QUERY; TARGET mô tả trạng thái mới của S1.",
    GROUP: "Một nhóm: SELECT phải nhận diện cả nhóm S1; TARGET nói rõ các thành viên nhóm ở TARGET.",
    DUAL: "Hai Subject: một ô TARGET ghi thay đổi độc lập của cả S1 và S2.",
    RELATIONAL: "Hai Subject: một ô TARGET ghi quan hệ có hướng, ai làm gì với ai.",
  };
  $("caseHint").textContent = instructions[state.annotation.case_type];
  $("select1").placeholder = selectedIds(1).length > 1
    ? "the group consisting of the man in black and the woman in white"
    : "the man in a dark suit standing on the left";
  $("select2").placeholder = selectedIds(2).length > 1
    ? "the group of people standing on the right" : "the woman in a white dress";
  $("targetText").placeholder = state.annotation.case_type === "RELATIONAL"
    ? "Subject 1 is presenting a diploma to Subject 2"
    : state.annotation.case_type === "DUAL"
      ? "Subject 1 is holding a diploma and Subject 2 is clapping"
      : state.annotation.case_type === "GROUP"
        ? "The members of Subject 1 are standing together on the stage"
        : "Subject 1 is holding a diploma";
  document.querySelectorAll("[data-case-mode]").forEach(button => {
    const mode = button.dataset.caseMode;
    button.classList.toggle("active", mode === "one" ? !two : state.annotation.case_type === mode);
    button.disabled = completed || blocked;
  });
  $("subject2").hidden = !two;
  $("select2Group").hidden = !two;
  $("subject1").classList.toggle("active", state.activeSubject === 1);
  $("subject2").classList.toggle("active", state.activeSubject === 2);
  $("subjectCounts").textContent = `S1: ${selectedIds(1).length} identity` +
    (two ? ` · S2: ${selectedIds(2).length} identity` : "");
  for (const id of ["select1", "select2", "targetText"]) $(id).readOnly = completed || blocked;
  $("saveDraft").disabled = completed || blocked;
  $("submitTask").disabled = completed || blocked || state.suggesting;
  $("submitTask").textContent = state.reopenedTask === state.current
    ? "Nộp lại" : "Nộp & task tiếp theo";
  $("reopenTask").hidden = !completed;
  $("reopenTask").disabled = !completed || blocked;
  $("saveDraft").hidden = completed;
  $("submitTask").hidden = completed;
  updateSuggestButton();
  updateSuggestionButtons();
  $("refreshQueue").disabled = busy();
  renderBoxes(); renderPreview();
}
function markDirty() {
  if (!state.task || state.task.status === "SUBMITTED" || editingBlocked()) return;
  state.dirty = true; state.version += 1;
  $("saveState").textContent = "Chưa lưu…";
  $("workError").textContent = "";
  persistLocalDraft();
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(() => flushDraft().catch(error => {
    $("workError").textContent = error.message;
    $("saveState").textContent = "Lưu nháp thất bại";
  }), 900);
  renderPreview();
  updateSuggestButton();
}
async function saveOnce() {
  const version = state.version, sid = state.current, session = state.session;
  $("saveState").textContent = "Đang lưu nháp…";
  const pending = post(`/api/work/tasks/${encodeURIComponent(sid)}/draft`, {
    expected_revision: state.task.revision, annotation: currentAnnotation(),
  });
  state.saving = pending;
  try {
    const result = await pending;
    if (state.current !== sid || session !== state.session) return;
    state.task = { ...state.task, ...result.task };
    if (state.reopenedTask === sid) rememberReopenedTask(state.task);
    if (state.version === version) state.annotation = result.annotation;
    if (state.version === version) {
      state.dirty = false; $("saveState").textContent = "Đã lưu nháp";
      removeLocalDraft(sid);
      removeRecoverySource(state.recoverySource); state.recoverySource = null;
    } else $("saveState").textContent = "Có thay đổi mới…";
    persistLocalDraft();
    updateMeta(result.task);
    if (result.backup_warning) $("workError").textContent = result.backup_warning;
  } finally { if (state.saving === pending) state.saving = null; }
}
async function flushDraft() {
  clearTimeout(state.saveTimer);
  while (state.dirty) {
    if (state.saving) await state.saving;
    else await saveOnce();
  }
}
async function submitTask() {
  if (!state.task || state.task.status === "SUBMITTED" || editingBlocked() || state.suggesting) return;
  const sid = state.current, isResubmission = state.reopenedTask === sid;
  state.submitting = true;
  renderWork(); renderQueue();
  try {
    $("workError").textContent = "";
    await flushDraft();
    const result = await post(`/api/work/tasks/${encodeURIComponent(sid)}/submit`, {
      expected_revision: state.task.revision, annotation: currentAnnotation(),
    });
    state.task = { ...state.task, ...result.task };
    state.annotation = result.annotation;
    showSuggestion(null);
    state.dirty = false;
    removeLocalDraft(sid);
    clearReopenedMarker(sid);
    $("saveState").textContent = isResubmission ? "Đã nộp lại" : "Đã nộp";
    updateMeta(result.task);
    if (result.backup_warning) {
      $("workError").textContent = result.backup_warning;
      alert(result.backup_warning);
    }
    renderWork();
    state.submitting = false;
    if (!isResubmission) {
      const next = filteredQueue().find(t => t.status !== "SUBMITTED");
      if (next) await switchTask(next.sample_id);
      else $("saveState").textContent = state.queue.some(t => t.status !== "SUBMITTED")
        ? "Đã xong các task trong bộ lọc này." : "Bạn đã hoàn thành tất cả task được giao.";
    }
  } catch (error) { $("workError").textContent = error.message; }
  finally {
    state.submitting = false;
    if (state.task) renderWork();
    renderQueue();
  }
}
async function reopenTask() {
  if (!state.task || state.task.status !== "SUBMITTED" || busy()) return;
  const sid = state.current;
  state.reopening = true;
  renderWork(); renderQueue();
  try {
    $("workError").textContent = "";
    const result = await post(`/api/work/tasks/${encodeURIComponent(sid)}/reopen`, {
      expected_revision: state.task.revision,
    });
    if (state.current !== sid) return;
    state.task = { ...state.task, ...result.task };
    rememberReopenedTask(state.task);
    fillAnnotation(result.annotation);
    showSuggestion(null);
    state.dirty = false; state.version = 0;
    $("saveState").textContent = "Đã mở lại · có thể tiếp tục sửa";
    updateMeta(result.task);
    if (result.backup_warning) $("workError").textContent = result.backup_warning;
  } catch (error) { $("workError").textContent = error.message; }
  finally {
    state.reopening = false;
    if (state.task) renderWork();
    renderQueue();
  }
}
async function suggest() {
  if (!state.task || !state.llm || state.suggesting || editingBlocked() || state.task.status === "SUBMITTED") return;
  if (!hasLlmDraft()) {
    $("workError").textContent = "Hãy gạch ý cho tất cả SELECT và TARGET trước khi dùng Fix with LLM. Bạn có thể viết bằng tiếng Việt; LLM sẽ sửa/dịch sang tiếng Anh.";
    return;
  }
  const sid = state.current, generation = state.taskGeneration, session = state.session;
  state.suggesting = true;
  $("suggestBtn").disabled = true;
  $("submitTask").disabled = true;
  $("suggestBtn").textContent = "Đang sửa với LLM…";
  $("workError").textContent = "";
  try {
    await flushDraft();
    if (state.current !== sid || generation !== state.taskGeneration) return;
    const version = state.version;
    const original = currentAnnotation();
    const subjectSignature = JSON.stringify([original.case_type, original.subjects]);
    const result = await post(`/api/work/tasks/${encodeURIComponent(sid)}/suggest`, {
      expected_revision: state.task.revision,
      annotation: original, note: $("llmNote").value,
    });
    if (state.current !== sid || generation !== state.taskGeneration) return;
    const current = currentAnnotation();
    if (subjectSignature !== JSON.stringify([current.case_type, current.subjects])) {
      $("workError").textContent = "Subject/case đã đổi trong lúc LLM chạy; hãy tạo gợi ý mới.";
      return;
    }
    showSuggestion(result.suggestion);
    if (state.version !== version) {
      $("workError").textContent = "Bạn đã sửa bài trong lúc LLM chạy; kiểm tra kỹ đề xuất trước khi áp dụng.";
    } else if (result.backup_warning) $("workError").textContent = result.backup_warning;
  } catch (error) {
    if (state.current === sid && generation === state.taskGeneration) $("workError").textContent = error.message;
  }
  finally {
    if (session === state.session) {
      state.suggesting = false;
      $("suggestBtn").textContent = "✦ Fix with LLM";
      if (state.task) renderWork();
    }
  }
}
function applyView(side) {
  const view = state.view[side];
  $(`${side}Stage`).style.transform = `translate(${view.x}px,${view.y}px) scale(${view.scale})`;
  $(`${side}Zoom`).textContent = `${Math.round(view.scale * 100)}%`;
}
function fitImage(side) {
  const image = $(`${side}Image`), viewport = $(`${side}Viewport`), stage = $(`${side}Stage`);
  if (!image.naturalWidth || !image.naturalHeight) return;
  const fit = Math.min(viewport.clientWidth / image.naturalWidth,
                       viewport.clientHeight / image.naturalHeight);
  const width = image.naturalWidth * fit, height = image.naturalHeight * fit;
  stage.style.width = `${width}px`; stage.style.height = `${height}px`;
  state.view[side] = { scale: 1, x: (viewport.clientWidth - width) / 2,
                       y: (viewport.clientHeight - height) / 2 };
  applyView(side);
}
function zoom(side, factor, clientX, clientY) {
  const viewport = $(`${side}Viewport`), view = state.view[side], rect = viewport.getBoundingClientRect();
  const x = clientX == null ? rect.width / 2 : clientX - rect.left;
  const y = clientY == null ? rect.height / 2 : clientY - rect.top;
  const next = Math.max(.5, Math.min(5, view.scale * factor));
  view.x = x - (x - view.x) * next / view.scale;
  view.y = y - (y - view.y) * next / view.scale;
  view.scale = next; applyView(side);
}
function setupViewport(side) {
  const viewport = $(`${side}Viewport`); let start = null;
  viewport.addEventListener("wheel", event => {
    event.preventDefault(); zoom(side, event.deltaY < 0 ? 1.12 : 1 / 1.12, event.clientX, event.clientY);
  }, { passive: false });
  viewport.addEventListener("pointerdown", event => {
    if (event.target.closest(".person-box")) return;
    start = { x: event.clientX, y: event.clientY, originX: state.view[side].x,
              originY: state.view[side].y };
    viewport.setPointerCapture(event.pointerId); viewport.classList.add("dragging");
  });
  viewport.addEventListener("pointermove", event => {
    if (!start) return;
    state.view[side].x = start.originX + event.clientX - start.x;
    state.view[side].y = start.originY + event.clientY - start.y;
    applyView(side);
  });
  for (const kind of ["pointerup", "pointercancel"]) viewport.addEventListener(kind, () => {
    start = null; viewport.classList.remove("dragging");
  });
}

function bindEvents() {
  window.addEventListener("beforeunload", event => {
    persistLocalDraft();
    if (state.dirty || state.saving || state.submitting || state.reopening) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && state.dirty) flushDraft().catch(() => {});
  });
  $("loginForm").onsubmit = async event => {
    event.preventDefault(); $("loginError").textContent = "";
    const data = Object.fromEntries(new FormData(event.currentTarget));
    const button = event.currentTarget.querySelector("button"); button.disabled = true;
    try { await post("/api/auth/login", data);
      const config = await api("/api/me"); await showApp(config); }
    catch (error) { $("loginError").textContent = error.message; }
    finally { button.disabled = false; }
  };
  $("logout").onclick = async () => {
    if (busy()) return;
    state.loggingOut = true; $("logout").disabled = true;
    if (state.task) renderWork(); renderQueue();
    try {
      await flushDraft();
      await post("/api/auth/logout", {});
      showLogin();
    } catch (error) {
      $("workError").textContent = error.message;
      if (state.user?.role === "ADMIN") toast(error.message);
    } finally {
      state.loggingOut = false; $("logout").disabled = false;
      if (state.task) renderWork(); renderQueue();
    }
  };
  document.querySelectorAll("[data-tab]").forEach(button => button.onclick = () => {
    document.querySelectorAll("[data-tab]").forEach(b => b.classList.toggle("active", b === button));
    $("adminTasks").hidden = button.dataset.tab !== "tasks";
    $("adminUsers").hidden = button.dataset.tab !== "users";
    if (button.dataset.tab === "users") {
      resetCreateUserForm();
      loadUsers().catch(e => toast(e.message));
    }
  });
  $("createUser").onsubmit = async event => {
    event.preventDefault();
    const form = event.currentTarget, button = form.querySelector("button"); button.disabled = true;
    try { await post("/api/admin/users", Object.fromEntries(new FormData(form)));
      form.reset(); await loadUsers(); toast("Đã tạo annotator."); }
    catch (error) { toast(error.message); }
    finally { button.disabled = false; }
  };
  $("createUsersBulk").onsubmit = async event => {
    event.preventDefault();
    const form = event.currentTarget, button = form.querySelector("button");
    button.disabled = true;
    try {
      const result = await post("/api/admin/users/bulk", { text: $("bulkUsersText").value });
      form.reset(); await loadUsers();
      toast(`Đã tạo ${result.created} annotator.`);
    } catch (error) { toast(error.message); }
    finally { button.disabled = false; }
  };
  $("applyFilters").onclick = () => { state.offset = 0; loadAdminTasks().catch(e => toast(e.message)); };
  $("adminSearch").onkeydown = e => { if (e.key === "Enter") $("applyFilters").click(); };
  $("pagePrev").onclick = () => { state.offset = Math.max(0, state.offset - 100); loadAdminTasks().catch(e => toast(e.message)); };
  $("pageNext").onclick = () => { state.offset += 100; loadAdminTasks().catch(e => toast(e.message)); };
  $("selectPage").onchange = event => {
    state.selected = event.target.checked ? new Set(state.adminTasks.map(t => t.sample_id)) : new Set();
    renderAdminRows();
  };
  $("assignBtn").onclick = () => {
    if (!$("assignUser").value) { toast("Hãy chọn annotator."); return; }
    assignSelected(Number($("assignUser").value)).catch(e => toast(e.message));
  };
  $("unassignBtn").onclick = () => assignSelected(null).catch(e => toast(e.message));
  $("exportBtn").onclick = exportResults;
  $("importFile").onchange = () => { state.importText = ""; state.importReady = false;
    $("commitImport").disabled = true; $("importResult").textContent = ""; };
  $("previewImport").onclick = () => importPreview(false);
  $("commitImport").onclick = () => importPreview(true);
  document.querySelectorAll("[data-work-status]").forEach(button => button.onclick = () => {
    state.workStatus = button.dataset.workStatus;
    document.querySelectorAll("[data-work-status]").forEach(b => b.classList.toggle("active", b === button));
    renderQueue();
  });
  $("workSearch").oninput = e => { state.workSearch = e.target.value.toLowerCase().trim(); renderQueue(); };
  $("refreshQueue").onclick = async () => {
    try { await loadQueue(); }
    catch (error) { $("workError").textContent = error.message; }
  };
  $("workCase").onchange = e => { state.workCase = e.target.value; renderQueue(); };
  $("toggleQueue").onclick = () => setQueueCollapsed(!$("work").classList.contains("queue-collapsed"));
  $("prevTask").onclick = () => navigate(-1);
  $("nextTask").onclick = () => navigate(1);
  document.querySelectorAll("[data-case-mode]").forEach(button => button.onclick = () => setCase(button.dataset.caseMode));
  $("subject1").onclick = () => { state.activeSubject = 1; renderWork(); };
  $("subject2").onclick = () => { state.activeSubject = 2; renderWork(); };
  for (const id of ["select1", "select2", "targetText"]) $(id).oninput = () => {
    const applied = $("suggestionFields").querySelector(`[data-apply-to="${id}"]`);
    if (applied?.classList.contains("applied")) {
      applied.classList.remove("applied"); applied.textContent = "Áp dụng";
    }
    markDirty();
  };
  $("saveDraft").onclick = () => flushDraft().catch(e => { $("workError").textContent = e.message; });
  $("reloadTask").onclick = async () => {
    if (!state.task || busy()) return;
    persistLocalDraft();
    state.loading = true; clearTimeout(state.saveTimer); renderWork(); renderQueue();
    try {
      if (state.saving) await state.saving.catch(() => {});
      if (state.current) await loadTask(state.current);
    } catch (error) { $("workError").textContent = error.message; }
    finally { state.loading = false; if (state.task) renderWork(); renderQueue(); }
  };
  $("downloadDraft").onclick = () => downloadDraft(state.recovery);
  $("downloadLocal").onclick = () => downloadDraft(state.recovery);
  $("restoreLocal").onclick = restoreLocalDraft;
  $("discardLocal").onclick = () => {
    if (!confirm("Bỏ bản nháp dự phòng trên trình duyệt và dùng bản đã lưu trên server?")) return;
    removeRecoverySource(state.recoverySource);
    state.recovery = null; state.recoverySource = null;
    findRecoveryDraft(); renderWork();
  };
  $("submitTask").onclick = submitTask;
  $("reopenTask").onclick = reopenTask;
  $("suggestBtn").onclick = suggest;
  for (const side of ["query", "target"]) {
    setupViewport(side);
    $(`${side}Image`).onload = () => fitImage(side);
    $(`${side}Image`).onerror = () => { $(`${side}Error`).hidden = false; };
  }
  document.querySelectorAll("[data-zoom]").forEach(button => button.onclick = () =>
    zoom(button.dataset.zoom, button.dataset.delta === "1" ? 1.2 : 1 / 1.2));
  document.querySelectorAll("[data-reset]").forEach(button => button.onclick = () => fitImage(button.dataset.reset));
  window.addEventListener("resize", () => { for (const side of ["query", "target"]) fitImage(side); });
}

bindEvents();
api("/api/me").then(showApp).catch(() => showLogin());
