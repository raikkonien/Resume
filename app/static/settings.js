import { confirmAction, initModelSettings } from "resume-editor";

const accountForm = document.querySelector("#account-form");
const modelForm = document.querySelector("#ocr-settings-form");
const archive = document.querySelector("#archive");
const model = initModelSettings(modelForm);
const originalUsername = accountForm.elements.username.value;
const sectionLabels = JSON.parse(document.querySelector("#archive-section-labels").textContent);
let leaving = false;
let navigating = false;
let loadingArchive = false;
let archivePending = false;
let archivedRecords = [];
const archiveSelection = new Set();

function status(node, message, error = false) {
  node.textContent = message;
  node.classList.toggle("error", error);
}

async function request(url, options = {}) {
  const response = await fetch(url, {
    credentials: "same-origin",
    cache: "no-store",
    ...options,
    headers: { ...options.headers, "X-Requested-With": "fetch" },
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const error = new Error(response.status === 401
      ? "登录已过期，请在新标签页登录后重试，当前输入已保留"
      : data.error || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return response;
}

function isBusy() {
  return accountForm.dataset.busy === "true" || model.isBusy() || archivePending;
}

function isDirty() {
  return accountForm.dataset.dirty === "true" || model.isDirty();
}

function syncAccountControls(busy) {
  accountForm.dataset.busy = String(busy);
  accountForm.setAttribute("aria-busy", String(busy));
  for (const control of accountForm.elements) control.disabled = busy;
}

function markAccountEdited() {
  if (accountForm.dataset.busy === "true") return;
  accountForm.dataset.dirty = "true";
  accountForm.elements.confirm_password.setCustomValidity("");
  status(accountForm.querySelector(".panel-status"), "账号信息已修改，尚未保存。");
}

accountForm.addEventListener("input", markAccountEdited);
accountForm.addEventListener("change", markAccountEdited);
accountForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (isBusy()) return;
  const controls = accountForm.elements;
  const password = controls.new_password.value;
  controls.confirm_password.setCustomValidity(password !== controls.confirm_password.value
    ? "两次新密码输入不一致" : "");
  if (!accountForm.reportValidity()) return;
  const payload = {
    current_password: controls.current_password.value,
    username: controls.username.value,
    new_password: password,
    confirm_password: controls.confirm_password.value,
  };
  if ((payload.username !== originalUsername || password) && model.isDirty()) {
    if (!(await confirmAction({
      title: "账号变更后需要重新登录",
      message: "模型配置还有未保存的修改。继续修改账号可能退出登录并放弃这些模型配置；请确认已保存需要保留的配置。",
      confirmLabel: "继续修改账号",
      danger: true,
    }))) return;
  }
  if (isBusy()) return;
  const message = accountForm.querySelector(".panel-status");
  syncAccountControls(true);
  status(message, "正在保存账号…");
  try {
    const response = await request(accountForm.dataset.url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    delete accountForm.dataset.dirty;
    controls.current_password.value = "";
    controls.new_password.value = "";
    controls.confirm_password.value = "";
    if (data.reauthenticate) {
      leaving = true;
      status(message, "账号已更新，现有登录已失效。正在前往登录页，请使用更新后的账号重新登录。");
      await new Promise((resolve) => setTimeout(resolve, 900));
      location.assign("/login");
    } else {
      status(message, "账号信息未变化，无需重新登录。");
    }
  } catch (error) {
    status(message, `保存失败：${error.message}`, true);
  } finally {
    if (!leaving) syncAccountControls(false);
  }
});

const archiveList = archive.querySelector(".archive-list");
const archiveStatus = archive.querySelector(".archive-status");
const archiveLoading = archive.querySelector(".archive-loading");
const archiveRetry = archive.querySelector(".btn-retry-archive");
const archiveCount = archive.querySelector(".archive-selection-count");
const restoreSelected = archive.querySelector(".btn-restore-selected");
const deleteSelected = archive.querySelector(".btn-delete-selected");
const dateFormat = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
});

function archiveSummary() {
  status(archiveStatus, archivedRecords.length ? `共 ${archivedRecords.length} 条归档记录` : "暂无归档记录。");
}

function recordKey(record) {
  return `${record.section}:${record.id}`;
}

function selectedArchiveRecords() {
  return archivedRecords.filter((record) => archiveSelection.has(recordKey(record)));
}

function syncArchiveControls() {
  const busy = loadingArchive || archivePending;
  archive.dataset.busy = String(busy);
  archiveList.setAttribute("aria-busy", String(busy));
  for (const control of archive.querySelectorAll("button, input")) control.disabled = busy;
  const count = archiveSelection.size;
  archiveCount.textContent = `已选择 ${count} 条`;
  restoreSelected.disabled = busy || count === 0;
  deleteSelected.disabled = busy || count === 0;
  const selectAll = archiveList.querySelector(".archive-select-all");
  if (selectAll) {
    selectAll.checked = archivedRecords.length > 0 && count === archivedRecords.length;
    selectAll.indeterminate = count > 0 && count < archivedRecords.length;
  }
  for (const checkbox of archiveList.querySelectorAll(".archive-select")) {
    const row = checkbox.closest("tr");
    checkbox.checked = archiveSelection.has(recordKey(row.dataset));
    row.classList.toggle("is-selected", checkbox.checked);
  }
}

function archiveCheckbox(className, label) {
  const target = document.createElement("label");
  target.className = "archive-checkbox";
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.className = className;
  checkbox.setAttribute("aria-label", label);
  target.append(checkbox);
  return target;
}

function renderArchive() {
  archiveList.replaceChildren();
  archiveSummary();
  if (!archivedRecords.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    const title = document.createElement("h3");
    title.className = "empty-state-title";
    title.textContent = "还没有归档记录";
    const copy = document.createElement("p");
    copy.className = "empty-state-copy";
    copy.textContent = "需要整理履历时，可在简历页将记录移入归档。";
    const link = document.createElement("a");
    link.href = "/resume";
    link.textContent = "前往简历页";
    empty.append(title, copy, link);
    archiveList.append(empty);
    syncArchiveControls();
    return;
  }
  const wrap = document.createElement("div");
  wrap.className = "table-wrap";
  const table = document.createElement("table");
  table.className = "entry-table archive-table";
  table.setAttribute("aria-describedby", "archive-selection-count");
  const caption = table.createCaption();
  caption.className = "sr-only";
  caption.textContent = "归档记录，可选择多条恢复或彻底删除";
  const head = table.createTHead().insertRow();
  const selectionHead = document.createElement("th");
  selectionHead.scope = "col";
  selectionHead.className = "archive-selection-cell";
  selectionHead.append(archiveCheckbox("archive-select-all", "选择全部归档记录"));
  head.append(selectionHead);
  for (const label of ["栏目", "标题", "归档时间（UTC+08:00）", "操作"]) {
    const cell = document.createElement("th");
    cell.scope = "col";
    cell.textContent = label;
    head.append(cell);
  }
  const body = table.createTBody();
  for (const record of archivedRecords) {
    const row = body.insertRow();
    row.dataset.section = record.section;
    row.dataset.id = String(record.id);
    const selectionCell = row.insertCell();
    selectionCell.className = "archive-selection-cell";
    selectionCell.append(archiveCheckbox("archive-select", `选择${sectionLabels[record.section] || record.section}：${record.title}`));
    row.insertCell().textContent = sectionLabels[record.section] || record.section;
    row.insertCell().textContent = record.title;
    const timeCell = row.insertCell();
    const parsed = new Date(record.archived_at);
    if (Number.isNaN(parsed.getTime())) timeCell.textContent = "时间未记录";
    else {
      const time = document.createElement("time");
      time.dateTime = record.archived_at;
      time.textContent = dateFormat.format(parsed);
      timeCell.append(time);
    }
    const action = row.insertCell();
    action.className = "archive-actions-cell";
    const buttons = document.createElement("div");
    buttons.className = "archive-row-actions";
    for (const [name, label] of [["restore", "恢复"], ["delete", "彻底删除"]]) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = name === "restore" ? "btn-restore" : "btn-delete-archive archive-danger";
      button.dataset.archiveAction = name;
      button.textContent = label;
      button.setAttribute("aria-label", `${label}${sectionLabels[record.section] || record.section}：${record.title}`);
      buttons.append(button);
    }
    action.append(buttons);
  }
  wrap.append(table);
  archiveList.append(wrap);
  syncArchiveControls();
}

async function loadArchive() {
  if (loadingArchive || archivePending) return;
  loadingArchive = true;
  archiveLoading.hidden = archiveList.childElementCount > 0;
  syncArchiveControls();
  status(archiveStatus, "正在加载归档记录…");
  try {
    const response = await request("/api/archive");
    const data = await response.json();
    if (!Array.isArray(data)) throw new Error("归档列表响应无效");
    archivedRecords = data;
    const available = new Set(archivedRecords.map(recordKey));
    for (const key of archiveSelection) {
      if (!available.has(key)) archiveSelection.delete(key);
    }
    renderArchive();
  } catch (error) {
    const retained = archiveList.childElementCount > 0 ? "当前列表和选择已保留。" : "";
    status(archiveStatus, `归档加载失败：${error.message}。${retained}可重新加载。`, true);
  } finally {
    loadingArchive = false;
    archiveLoading.hidden = true;
    syncArchiveControls();
  }
}

async function mutateArchive(action, records, trigger) {
  if (loadingArchive || archivePending || navigating || leaving || isBusy() || !records.length) return;
  const payload = records.map(({ section, id }) => ({ section, id }));
  const keys = new Set(payload.map(recordKey));
  const count = payload.length;
  const deleting = action === "delete";
  const verb = deleting ? "彻底删除" : "恢复";
  const firstIndex = archivedRecords.findIndex((record) => keys.has(recordKey(record)));
  let focusKey;
  let completed = false;
  archivePending = true;
  syncArchiveControls();
  try {
    if (!(await confirmAction({
      title: deleting ? `彻底删除 ${count} 条归档记录？` : `恢复 ${count} 条归档记录？`,
      message: deleting
        ? `将彻底删除 ${count} 条归档记录及其全部附件。删除后无法恢复，此操作不可撤销。`
        : `将恢复 ${count} 条归档记录，保留原记录和附件，按列表顺序追加到各自栏目的活动记录末尾。`,
      confirmLabel: deleting ? "彻底删除" : "确认恢复",
      danger: deleting,
    }))) return;
    status(archiveStatus, `正在${verb} ${count} 条记录，请勿离开…`);
    const response = await request(`/api/archive/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ records: payload }),
    });
    const data = await response.json();
    if (data[deleting ? "deleted" : "restored"] !== count) {
      throw new Error("返回的完成数量与请求不一致，请重新加载列表确认状态");
    }
    archivedRecords = archivedRecords.filter((record) => !keys.has(recordKey(record)));
    for (const key of keys) archiveSelection.delete(key);
    focusKey = archivedRecords.length
      ? recordKey(archivedRecords[Math.min(firstIndex, archivedRecords.length - 1)])
      : null;
    renderArchive();
    status(archiveStatus, `已${verb} ${count} 条记录。${archivedRecords.length ? `剩余 ${archivedRecords.length} 条归档记录。` : "暂无归档记录。"}`);
    completed = true;
  } catch (error) {
    status(archiveStatus, error.status === 404
      ? "归档状态已变化，记录可能不存在或已被处理；当前列表和选择已保留，请重新加载后再操作。"
      : `${verb}请求失败：${error.message}。当前列表和选择已保留，可重试或重新加载确认状态。`, true);
  } finally {
    archivePending = false;
    syncArchiveControls();
    if (completed) {
      const row = [...archiveList.querySelectorAll("tbody tr")].find((item) => recordKey(item.dataset) === focusKey);
      (row?.querySelector("[data-archive-action]") || archiveStatus).focus();
    } else if (trigger.isConnected && !trigger.disabled) {
      trigger.focus();
    }
  }
}

archiveRetry.addEventListener("click", loadArchive);
archiveList.addEventListener("change", (event) => {
  const checkbox = event.target.closest('input[type="checkbox"]');
  if (!checkbox || loadingArchive || archivePending) return;
  if (checkbox.matches(".archive-select-all")) {
    archiveSelection.clear();
    if (checkbox.checked) {
      for (const record of archivedRecords) archiveSelection.add(recordKey(record));
    }
  } else if (checkbox.matches(".archive-select")) {
    const key = recordKey(checkbox.closest("tr").dataset);
    if (checkbox.checked) archiveSelection.add(key);
    else archiveSelection.delete(key);
  }
  syncArchiveControls();
});
archiveList.addEventListener("click", (event) => {
  const button = event.target.closest("[data-archive-action]");
  if (!button || button.disabled) return;
  const row = button.closest("tr");
  const record = archivedRecords.find((item) => recordKey(item) === recordKey(row.dataset));
  if (record) void mutateArchive(button.dataset.archiveAction, [record], button);
});
restoreSelected.addEventListener("click", () => {
  void mutateArchive("restore", selectedArchiveRecords(), restoreSelected);
});
deleteSelected.addEventListener("click", () => {
  void mutateArchive("delete", selectedArchiveRecords(), deleteSelected);
});

async function canLeave() {
  if (isBusy()) {
    status(accountForm.querySelector(".panel-status"), "操作正在进行，请等待完成后再离开。", true);
    return false;
  }
  if (isDirty() && !(await confirmAction({
    title: "放弃未保存的设置？",
    message: "账号或模型配置尚未保存。离开将放弃当前输入，已保存的配置不会改变。",
    confirmLabel: "放弃修改",
    danger: true,
  }))) return false;
  return !isBusy();
}

document.addEventListener("click", async (event) => {
  const link = event.target.closest("a[href]");
  if (!link || event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey
      || event.shiftKey || event.altKey || link.hasAttribute("download")
      || (link.target && link.target !== "_self")) return;
  const url = new URL(link.href, location.href);
  if (url.pathname === location.pathname && url.search === location.search && url.hash) return;
  event.preventDefault();
  if (navigating) return;
  navigating = true;
  try {
    if (await canLeave()) {
      leaving = true;
      location.assign(url.href);
    }
  } finally {
    navigating = false;
  }
});

document.addEventListener("submit", async (event) => {
  const logout = event.target.closest("form[data-leave]");
  if (!logout) return;
  event.preventDefault();
  if (navigating) return;
  navigating = true;
  try {
    if (await canLeave()) {
      leaving = true;
      HTMLFormElement.prototype.submit.call(logout);
    }
  } finally {
    navigating = false;
  }
});

window.addEventListener("beforeunload", (event) => {
  if (!leaving && (isDirty() || isBusy())) {
    event.preventDefault();
    event.returnValue = "";
  }
});

void loadArchive();
