const panelLocks = new WeakMap();
const panelMutations = new Map();
const uploadPreferenceWrites = new Map();

function status(node, message, error = false) {
  if (!node) return;
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
  if (response.status === 401) {
    throw new Error("登录已过期，请在新标签页登录后重试，当前输入已保留");
  }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  return response;
}

function syncControls(panel) {
  const busy = panel.dataset.busy === "true";
  const dialog = panel.querySelector("#entry-dialog");
  const editor = dialog?.querySelector("form.entry");
  let locks = panelLocks.get(panel);
  if (busy || dialog?.open) {
    if (!locks) panelLocks.set(panel, locks = new Map());
    for (const control of panel.querySelectorAll("[data-editor-trigger], .entry-table tbody button")) {
      if (!locks.has(control)) locks.set(control, control.disabled);
      control.disabled = true;
    }
  } else if (locks) {
    for (const [control, disabled] of locks) {
      if (control.isConnected) control.disabled = disabled;
    }
    panelLocks.delete(panel);
  }
  for (const button of dialog?.querySelectorAll(".editor-close") || []) button.disabled = busy;
  if (editor) {
    for (const control of editor.elements) control.disabled = busy;
    editor.setAttribute("aria-busy", String(busy));
    for (const authors of editor.querySelectorAll(".authors-editor")) syncAuthorControls(authors);
    for (const zone of editor.querySelectorAll(".ocr-zone")) syncOcrControls(zone);
    const submit = editor.querySelector('button[type="submit"]');
    if (submit && editor._activeOcrZones?.size) submit.disabled = true;
  }
}

export async function mutatePanel(panel, action) {
  if (panel.dataset.busy === "true") throw new Error("栏目正在保存，请稍后重试");
  panel.dataset.busy = "true";
  syncControls(panel);
  panel.dispatchEvent(new Event("panel-state-change"));
  const pending = (async () => {
    try {
      return await action();
    } finally {
      delete panel.dataset.busy;
      syncControls(panel);
      panel.dispatchEvent(new Event("panel-state-change"));
    }
  })();
  panelMutations.set(panel, pending);
  try {
    return await pending;
  } finally {
    panelMutations.delete(panel);
  }
}

export function hasPendingPanelMutations() { return panelMutations.size > 0; }
export async function waitForPanelMutations() {
  await Promise.allSettled([...panelMutations.values()]);
}

async function perform(panel, action) {
  status(panel.querySelector(".panel-status"), "");
  try {
    await mutatePanel(panel, action);
    return true;
  } catch (error) {
    const message = panel.querySelector("form.entry .status") || panel.querySelector(".panel-status");
    status(message, `操作失败：${error.message}`, true);
    return false;
  }
}


function acceptedExtensions(input) {
  return (input.accept || "").split(",").map((value) => value.trim().toLowerCase()).filter(Boolean);
}

function validateOcrFile(file, input, maxMb) {
  const extensions = acceptedExtensions(input);
  const name = (file.name || "").toLowerCase();
  const extension = name.includes(".") ? name.slice(name.lastIndexOf(".")) : "";
  if (extensions.length && !extensions.includes(extension)) return `仅支持 ${extensions.join("、")} 文件`;
  if (maxMb && file.size > maxMb * 1024 * 1024) return `文件超过 ${maxMb}MB`;
  return "";
}

function authorRows(editor) {
  return [...editor.querySelector(".authors-list").children];
}

function syncAuthorControls(editor) {
  const rows = authorRows(editor);
  const busy = editor.closest(".tab-panel").dataset.busy === "true";
  rows.forEach((row, index) => {
    const position = `第 ${index + 1} 位作者`;
    row.querySelector(".author-position").textContent = position;
    for (const [selector, boundary, action] of [
      [".btn-author-up", index === 0, "上移"],
      [".btn-author-down", index === rows.length - 1, "下移"],
      [".btn-author-remove", false, "删除"],
    ]) {
      const button = row.querySelector(selector);
      button.disabled = busy || boundary;
      button.setAttribute("aria-label", `${action}${position}`);
    }
  });
  editor.querySelector(".authors-empty").hidden = rows.length > 0;
}

function markControlEdited(control) {
  control.dataset.userEdited = "true";
  control._editRevision = (control._editRevision || 0) + 1;
}

function syncAuthors(editor, edited = false) {
  const control = editor.querySelector('input[type="hidden"]');
  control.value = JSON.stringify(authorRows(editor).map((row) => ({
    name: row.querySelector("[data-author-name]").value,
    affiliation: row.querySelector("[data-author-affiliation]").value,
    is_first: row.querySelector("[data-author-first]").checked,
    is_corresponding: row.querySelector("[data-author-corresponding]").checked,
  })));
  if (edited) markControlEdited(control);
  syncAuthorControls(editor);
}

function createAuthorRow(editor, author = {}) {
  const row = editor.querySelector(".author-template").content.firstElementChild.cloneNode(true);
  row.querySelector("[data-author-name]").value = author.name || "";
  row.querySelector("[data-author-affiliation]").value = author.affiliation || "";
  row.querySelector("[data-author-first]").checked = author.is_first === true;
  row.querySelector("[data-author-corresponding]").checked = author.is_corresponding === true;
  return row;
}

function editAuthorList(button) {
  const editor = button.closest(".authors-editor");
  const form = editor.closest("form.entry");
  if (button.disabled || form.closest(".tab-panel").dataset.busy === "true") return;
  const list = editor.querySelector(".authors-list");
  const row = button.closest(".author-row");
  let focusTarget;
  let message;
  if (button.classList.contains("btn-author-add")) {
    const added = createAuthorRow(editor);
    list.append(added);
    focusTarget = added.querySelector("[data-author-name]");
    message = "已添加作者，请填写姓名和单位。";
  } else if (button.classList.contains("btn-author-remove")) {
    focusTarget = (row.nextElementSibling || row.previousElementSibling)?.querySelector("[data-author-name]")
      || editor.querySelector(".btn-author-add");
    row.remove();
    message = "已删除作者，尚未保存。";
  } else {
    if (button.classList.contains("btn-author-up") && row.previousElementSibling) {
      list.insertBefore(row, row.previousElementSibling);
    } else if (button.classList.contains("btn-author-down") && row.nextElementSibling) {
      list.insertBefore(row.nextElementSibling, row);
    }
    focusTarget = button;
    message = "作者顺序已调整，尚未保存。";
  }
  syncAuthors(editor, true);
  if (focusTarget.disabled) focusTarget = row.querySelector("[data-author-name]");
  focusTarget.focus();
  status(editor.querySelector(".authors-status"), message);
  form.dataset.dirty = "true";
  status(form.querySelector(".status"), "有未保存的修改");
}

function syncDottedDate(control) {
  if (!control.matches(".dotted-date-native")) return;
  const display = control.closest(".dotted-date").querySelector(".dotted-date-display");
  display.value = control.value.replaceAll("-", ".");
  display.setCustomValidity("");
}

function editDottedDate(display) {
  const control = display.closest(".dotted-date").querySelector(".dotted-date-native");
  const value = display.value;
  control.value = /^\d{4}\.\d{2}\.\d{2}$/.test(value) ? value.replaceAll(".", "-") : "";
  display.setCustomValidity(value && !control.value ? "请输入有效日期，格式为 YYYY.MM.DD，或使用日历选择。" : "");
  return control;
}

function ocrFieldSnapshot(form) {
  return new Map([...form.elements].filter((control) => control.name).map((control) => [
    control, {
      value: control.type === "checkbox" ? control.checked : control.value,
      revision: control._editRevision || 0,
    },
  ]));
}

function applyOcrFields(form, fields, replacement = null) {
  let filled = 0;
  let preserved = 0;
  for (const [key, value] of Object.entries(fields)) {
    if (["cas_quartile", "jcr_quartile", "remove_file"].includes(key)) continue;
    const control = form.elements.namedItem(key);
    if (!(control instanceof HTMLInputElement || control instanceof HTMLSelectElement
          || control instanceof HTMLTextAreaElement) || control.type === "file"
        || value == null || (control.type === "hidden" && key !== "authors")) continue;
    const authors = key === "authors" ? control.closest(".authors-editor") : null;
    if (authors) {
      if (!Array.isArray(value) || !value.length || !value.every((author) =>
        author && typeof author.name === "string" && author.name.trim()
        && typeof author.affiliation === "string" && typeof author.is_first === "boolean"
        && typeof author.is_corresponding === "boolean")) continue;
    } else if (control.type === "checkbox") {
      if (![true, false, 0, 1].includes(value)) continue;
    } else if (!["string", "number"].includes(typeof value) || !String(value).trim()) continue;
    if (replacement) {
      const before = replacement.get(control);
      const current = control.type === "checkbox" ? control.checked : control.value;
      if (!before || before.value !== current || before.revision !== (control._editRevision || 0)) {
        preserved += 1;
        continue;
      }
    } else if (control.dataset.userEdited === "true"
        || (authors ? authorRows(authors).length > 0
          : control.type === "checkbox" ? control.checked : control.value.trim() !== "")) continue;
    if (authors) {
      const hadFocus = authors.contains(document.activeElement);
      authors.querySelector(".authors-list").replaceChildren(...value.map((author) => createAuthorRow(authors, author)));
      syncAuthors(authors);
      if (hadFocus) authors.querySelector("[data-author-name]").focus();
    } else if (control.type === "checkbox") {
      control.checked = value === true || value === 1;
    } else {
      const text = String(value);
      if (control instanceof HTMLSelectElement && ![...control.options].some((option) => option.value === text)) continue;
      const previous = control.value;
      control.value = text;
      if (control.value === "") {
        control.value = previous;
        continue;
      }
    }
    syncDottedDate(control);
    filled += 1;
  }
  return { filled, preserved };
}

function uploadPreferenceState(zone) {
  return zone._uploadPreference || (zone._uploadPreference = { pending: false, failed: false });
}

function ocrPreferenceProblem(zone) {
  const state = uploadPreferenceState(zone);
  if (state.pending) return "识别偏好正在保存，暂不进行 AI 识别。";
  if (state.failed) return "识别偏好尚未保存，请先重试保存偏好；当前文件仅上传保存。";
  if (!zone.querySelector(".ocr-enabled").checked) return "AI 识别已关闭，当前文件仅上传保存。";
  return "";
}

function isZipFile(file) {
  return Boolean(file?.name.toLowerCase().endsWith(".zip"));
}

function syncOcrControls(zone) {
  const busy = zone.closest(".tab-panel").dataset.busy === "true";
  const state = uploadPreferenceState(zone);
  zone.querySelector(".ocr-enabled").disabled = busy || state.pending;
  zone.querySelector(".ocr-preference-retry").disabled = busy || state.pending;
  const file = zone.querySelector('input[type="file"]').files[0];
  zone.querySelector(".ocr-retry").disabled = busy || Boolean(ocrPreferenceProblem(zone))
    || Boolean(zoneOcrState(zone).controller) || isZipFile(file);
  syncSavedOcrControls(zone);
}

async function saveUploadPreference(zone) {
  const state = uploadPreferenceState(zone);
  if (state.pending) return;
  const checkbox = zone.querySelector(".ocr-enabled");
  const enabled = checkbox.checked;
  const feedback = zone.querySelector(".ocr-preference-status");
  const retry = zone.querySelector(".ocr-preference-retry");
  stopZoneOcr(zone);
  status(zone.querySelector(".ocr-status"), "");
  state.pending = true;
  state.failed = false;
  retry.hidden = true;
  status(feedback, "正在保存识别偏好…");
  syncOcrControls(zone);
  const pending = request(checkbox.dataset.preferenceUrl, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
    keepalive: true,
  });
  uploadPreferenceWrites.set(checkbox.dataset.preferenceUrl, pending);
  try {
    await pending;
    status(feedback, enabled ? "已保存：AI 识别开启。" : "已保存：仅上传，不进行 AI 识别。");
    const file = zone.querySelector('input[type="file"]').files[0];
    const input = zone.querySelector('input[type="file"]');
    const maxMb = Number(zone.closest("form.entry").dataset.maxUploadMb || 0);
    zone.querySelector(".ocr-retry").hidden = !enabled || !file || isZipFile(file)
      || Boolean(validateOcrFile(file, input, maxMb));
    if (!zone.querySelector(".ocr-retry").hidden) {
      zoneOcrState(zone).mode = "upload";
      zone.querySelector(".ocr-retry").textContent = "识别所选文件";
    }
    if (isZipFile(file)) status(zone.querySelector(".ocr-status"), "ZIP 仅作为附件保存，不进行 AI 识别；请点击保存。");
  } catch (error) {
    state.failed = true;
    status(feedback, `识别偏好保存失败：${error.message}。偏好尚未保存，AI 识别已暂停，请重试。`, true);
    retry.hidden = false;
  } finally {
    if (uploadPreferenceWrites.get(checkbox.dataset.preferenceUrl) === pending) {
      uploadPreferenceWrites.delete(checkbox.dataset.preferenceUrl);
    }
    state.pending = false;
    if (zone.isConnected) syncOcrControls(zone);
  }
}

function savedOcrProblem(zone) {
  if (zone.querySelector('input[type="file"]').files.length) {
    return "已选择新附件，请先清除所选文件以识别原 PDF；如需识别新文件，请使用上传自动识别，或保存新附件后再重新识别。";
  }
  if (zone.querySelector('input[name="remove_file"]:checked')) {
    return "当前附件已标记为保存时删除，请先取消删除标记，再重新识别。";
  }
  return "";
}

function syncSavedOcrControls(zone) {
  const button = zone.querySelector(".ocr-saved");
  if (!button) return;
  const problem = ocrPreferenceProblem(zone) || savedOcrProblem(zone);
  const active = Boolean(zoneOcrState(zone).controller);
  button.disabled = active || Boolean(problem) || zone.closest(".tab-panel").dataset.busy === "true";
  const help = zone.querySelector(".saved-ocr-help");
  if (!help.dataset.defaultText) help.dataset.defaultText = help.textContent;
  help.textContent = problem || help.dataset.defaultText;
  zone.querySelector(".ocr-cancel").hidden = !active || zoneOcrState(zone).mode !== "saved";
}

async function triggerSavedOcr(panel, form, zone) {
  if (!zone.dataset.savedOcrUrl || panel.dataset.busy === "true" || zoneOcrState(zone).controller) return;
  const problem = ocrPreferenceProblem(zone) || savedOcrProblem(zone);
  if (problem) {
    status(zone.querySelector(".ocr-status"), problem, true);
    return;
  }
  if (!(await confirmAction({
    title: "重新识别已保存 PDF",
    message: "重新识别当前已保存 PDF 的首页？\n识别结果将替换明确识别到的字段及非空作者列表，请核对单位和作者身份。\n未识别内容、中科院/JCR 分区及识别期间手动修改的字段保持不变。\n结果仅写入当前编辑草稿，核对后点击「保存记录」才会保存。",
    confirmLabel: "重新识别",
  }))) return;
  if (!form.isConnected || !form.closest("dialog")?.open) return;
  await triggerOcr(panel, form, zone, null);
}

function zoneOcrState(zone) {
  return zone._ocrState || (zone._ocrState = { token: 0, controller: null });
}

function setOcrLock(form, zone, active) {
  const zones = form._activeOcrZones || (form._activeOcrZones = new Set());
  if (active) zones.add(zone);
  else zones.delete(zone);
  const submit = form.querySelector('button[type="submit"]');
  if (submit) submit.disabled = zones.size > 0 || form.closest(".tab-panel").dataset.busy === "true";
  syncOcrControls(zone);
}

function stopZoneOcr(zone) {
  const state = zoneOcrState(zone);
  state.token += 1;
  const controller = state.controller;
  state.controller = null;
  controller?.abort();
  const statusNode = zone.querySelector(".ocr-status");
  statusNode?.classList.remove("busy");
  const retryButton = zone.querySelector(".ocr-retry");
  if (retryButton) retryButton.hidden = true;
  const form = zone.closest("form.entry");
  if (form) setOcrLock(form, zone, false);
}

function abortFormOcr(form) {
  for (const zone of form.querySelectorAll(".ocr-zone")) stopZoneOcr(zone);
}

function updateUploadSelection(control) {
  const input = control.querySelector('input[type="file"]');
  const file = input.files[0];
  control.querySelector(".upload-selection").hidden = !file;
  control.querySelector(".btn-clear-upload").hidden = !file;
  control.querySelector(".upload-filename").textContent = file ? `已选择：${file.name}` : "";
  status(control.querySelector(".upload-status"), "");
}

function clearZoneSelection(control) {
  const input = control.querySelector('input[type="file"]');
  const zone = control.closest(".ocr-zone");
  if (zone) stopZoneOcr(zone);
  input.value = "";
  updateUploadSelection(control);
  if (zone) syncSavedOcrControls(zone);
  if (zone) status(zone.querySelector(".ocr-status"), "");
  const form = control.closest("form.entry");
  if (form) {
    form.dataset.dirty = "true";
    status(form.querySelector(".status"), "已清除所选文件，其他内容已保留");
  }
  input.focus();
}

async function readNdjson(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finished = false;
  try {
    while (!finished) {
      const chunk = await reader.read();
      finished = chunk.done;
      buffer += decoder.decode(chunk.value, { stream: !finished });
      let index;
      while ((index = buffer.indexOf("\n")) !== -1) {
        const line = buffer.slice(0, index).trim();
        buffer = buffer.slice(index + 1);
        if (line) onEvent(JSON.parse(line));
      }
    }
    if (buffer.trim()) onEvent(JSON.parse(buffer));
  } finally {
    if (!finished) await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

async function handleZoneFileSelected(panel, form, zone, file) {
  zoneOcrState(zone).mode = "upload";
  const input = zone.querySelector('input[type="file"]');
  const problem = validateOcrFile(file, input, Number(form.dataset.maxUploadMb || 0));
  if (problem || isZipFile(file) || ocrPreferenceProblem(zone)) {
    status(zone.querySelector(".ocr-status"), problem
      || (isZipFile(file) ? "ZIP 仅作为附件保存，不进行 AI 识别；请点击保存。" : ocrPreferenceProblem(zone)), Boolean(problem));
    return;
  }
  await triggerOcr(panel, form, zone, file);
}

async function triggerOcr(panel, form, zone, file) {
  const input = zone.querySelector('input[type="file"]');
  const statusNode = zone.querySelector(".ocr-status");
  const retryButton = zone.querySelector(".ocr-retry");
  const saved = !file;
  const maxMb = Number(form.dataset.maxUploadMb || 0);
  const problem = saved ? savedOcrProblem(zone) : validateOcrFile(file, input, maxMb);
  if (problem) {
    stopZoneOcr(zone);
    status(statusNode, problem, true);
    return;
  }
  if ((!saved && isZipFile(file)) || ocrPreferenceProblem(zone)) {
    stopZoneOcr(zone);
    status(statusNode, !saved && isZipFile(file)
      ? "ZIP 仅作为附件保存，不进行 AI 识别；请点击保存。" : ocrPreferenceProblem(zone));
    return;
  }
  if (panel.dataset.busy === "true") return;
  if (!saved) form.dataset.dirty = "true";
  status(form.querySelector(".status"), saved ? "正在识别已保存附件，记录未更改" : "附件已选择，尚未保存");

  const state = zoneOcrState(zone);
  state.controller?.abort();
  const controller = new AbortController();
  state.controller = controller;
  state.mode = saved ? "saved" : "upload";
  const token = ++state.token;
  const isCurrent = () => state.token === token && !ocrPreferenceProblem(zone);
  const replacement = saved ? ocrFieldSnapshot(form) : null;

  retryButton.hidden = true;
  retryButton.textContent = saved ? "重试重新识别" : "重试";
  statusNode.classList.add("busy");
  const isPdf = saved || file.name.toLowerCase().endsWith(".pdf");
  const pagewise = isPdf && zone.dataset.ocrAllPages === "true";
  status(statusNode, pagewise
    ? zone.dataset.ocrField === "contract_pdf"
      ? "正在按顺序识别整份 PDF 合同… 优先读取文字，扫描页进行图像识别；仅填空，核对后需保存。"
      : "正在逐页识别合同… 取齐字段或读完整份文件后停止，结题日期不自动填写。"
    : saved ? "正在重新识别已保存 PDF 的首页… 结果不会自动保存。"
      : isPdf ? "正在识别… PDF 仅处理首页，优先读取文字。" : "正在识别图像…");
  setOcrLock(form, zone, true);
  try {
    const options = { method: "POST", signal: controller.signal };
    if (!saved) {
      options.body = new FormData();
      options.body.set("file", file);
      options.body.set("field", zone.dataset.ocrField || "");
    }
    const response = await request(saved ? zone.dataset.savedOcrUrl : form.dataset.ocrUrl, options);
    let result = null;
    let failure = "";
    await readNdjson(response, (event) => {
      if (!isCurrent() || !form.isConnected) return;
      if (event.type === "progress") status(statusNode, `当前正在识别第 ${event.page} 页`);
      else if (event.type === "result") result = event;
      else if (event.type === "error") failure = event.error || "识别失败";
    });
    if (!isCurrent() || !form.isConnected) return;
    if (saved ? savedOcrProblem(zone) : input.files[0] !== file) {
      status(statusNode, saved ? "附件选择已变化，本次结果未填入；请核对附件后重新识别。"
        : "文件已更换，请点击重试识别当前文件。", true);
      retryButton.hidden = saved || !input.files.length;
      return;
    }
    if (failure) throw new Error(failure);
    if (!result) throw new Error("识别未返回结果");
    const { filled, preserved } = applyOcrFields(form, result.fields || {}, replacement);
    if (filled) form.dataset.dirty = "true";
    status(form.querySelector(".status"), form.dataset.dirty === "true" ? "有未保存的修改" : "记录未更改");
    const warnings = result.warnings || [];
    const source = { pdf_text: "已读取 PDF 文字", pdf_image: "已识别 PDF 扫描图像", pdf_mixed: "已读取 PDF 文字及扫描图像", image: "图像识别完成" };
    const pageCount = isPdf && result.pages_read ? `（${result.pages_read} 页）` : "";
    const outcome = result.skipped_pages?.length ? "部分识别完成" : source[result.source] || "识别完成";
    const parts = [`${outcome}${pageCount}，${saved ? `更新了 ${filled} 个字段` : `填充了 ${filled} 个空字段`}。`];
    if (preserved) parts.push(`已保留识别期间修改的 ${preserved} 个字段（作者作为完整列表保护）。`);
    if (saved) parts.push("未识别到的内容和手工分区保持不变。");
    if (warnings.length) parts.push(`提示：${warnings.join("；")}`);
    parts.push("请核对后点击保存记录。");
    status(statusNode, parts.join(" "));
  } catch (error) {
    if (!isCurrent() || !form.isConnected) return;
    if (error.name === "AbortError") return;
    status(statusNode, `识别失败：${error.message}；当前输入已保留，可重试。`, true);
    retryButton.hidden = saved ? Boolean(savedOcrProblem(zone)) : !input.files.length;
  } finally {
    if (isCurrent() && form.isConnected) {
      statusNode.classList.remove("busy");
      if (state.controller === controller) state.controller = null;
      setOcrLock(form, zone, false);
    }
  }
}

function updateOcrKeyState(form, hasApiKey) {
  const keyInput = form.elements.api_key;
  const hint = form.querySelector(".ocr-key-hint");
  const clearField = form.querySelector(".ocr-key-clear");
  keyInput.placeholder = hasApiKey ? "留空则保留已保存的 Key" : "留空则不设置 Key";
  if (hint) {
    hint.textContent = hasApiKey
      ? "已保存 Key，出于安全不会回显明文，如需更换请直接输入新值。"
      : "尚未设置 Key，可留空使用无鉴权接口。";
  }
  if (clearField) {
    clearField.hidden = !hasApiKey;
    clearField.querySelector("input").checked = false;
  }
}

function ocrSettingsPayload(form) {
  const payload = { endpoint: form.elements.endpoint.value.trim(), model: form.elements.model.value.trim() };
  if (form.elements.clear_api_key.checked) payload.api_key = "";
  else if (form.elements.api_key.value) payload.api_key = form.elements.api_key.value;
  return payload;
}

function syncSettingsControls(form) {
  const busy = form.dataset.busy === "true";
  for (const control of form.elements) control.disabled = busy;
  form.elements.api_key.disabled = busy || form.elements.clear_api_key.checked;
  form.querySelector('button[type="submit"]').disabled = busy || !form.dataset.testToken;
  form.setAttribute("aria-busy", String(busy));
}

function invalidateOcrTest(form) {
  delete form.dataset.testToken;
  syncSettingsControls(form);
}

async function submitOcrSettings(form, testing) {
  if (form.dataset.busy === "true" || !form.reportValidity()) return;
  const statusNode = form.querySelector(".panel-status");
  if (!testing && !form.dataset.testToken) {
    status(statusNode, "请先测试当前配置，测试通过后才能保存。", true);
    return;
  }
  const payload = ocrSettingsPayload(form);
  const snapshot = JSON.stringify(payload);
  if (testing) invalidateOcrTest(form);
  else payload.test_token = form.dataset.testToken;
  form.dataset.busy = "true";
  syncSettingsControls(form);
  status(statusNode, testing ? "正在测试接口与图像识别能力…" : "保存中…");
  try {
    const response = await request(form.dataset.url + (testing ? "/test" : ""), {
      method: testing ? "POST" : "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (testing) {
      if (snapshot !== JSON.stringify(ocrSettingsPayload(form))) {
        status(statusNode, "配置已变更，请重新测试。", true);
        return;
      }
      if (typeof data.test_token !== "string" || !data.test_token) throw new Error("测试未返回有效凭证，请重新测试");
      form.dataset.testToken = data.test_token;
      status(statusNode, "测试通过，接口和图像识别正常。请在 10 分钟内保存设置。");
    } else {
      form.elements.endpoint.value = data.endpoint;
      form.elements.model.value = data.model;
      form.elements.api_key.value = "";
      updateOcrKeyState(form, data.has_api_key);
      delete form.dataset.dirty;
      delete form.dataset.testToken;
      status(statusNode, "设置已保存。");
    }
  } catch (error) {
    delete form.dataset.testToken;
    status(statusNode, `${testing ? "测试" : "保存"}失败：${error.message}`, true);
  } finally {
    delete form.dataset.busy;
    syncSettingsControls(form);
  }
}

const editorControllers = new WeakMap();
const modelControllers = new WeakMap();
let confirmationQueue = Promise.resolve();
let confirmationDialog;

function getConfirmationDialog() {
  if (confirmationDialog) return confirmationDialog;
  const dialog = document.createElement("dialog");
  dialog.className = "confirm-dialog modal";
  dialog.setAttribute("aria-labelledby", "confirmation-title");
  dialog.setAttribute("aria-describedby", "confirmation-message");
  const header = document.createElement("div");
  header.className = "dialog-header";
  const title = document.createElement("h2");
  title.id = "confirmation-title";
  header.append(title);
  const body = document.createElement("div");
  body.className = "modal-body";
  const message = document.createElement("p");
  message.id = "confirmation-message";
  message.className = "confirm-message";
  body.append(message);
  const footer = document.createElement("div");
  footer.className = "modal-footer";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.textContent = "取消";
  const confirm = document.createElement("button");
  confirm.type = "button";
  footer.append(cancel, confirm);
  dialog.append(header, body, footer);
  document.body.append(dialog);
  cancel.addEventListener("click", () => dialog.close("cancel"));
  confirm.addEventListener("click", () => dialog.close("confirm"));
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    dialog.close("cancel");
  });
  confirmationDialog = { dialog, title, message, confirm, cancel };
  return confirmationDialog;
}

export function confirmAction({ title, message, confirmLabel = "确认", danger = false }) {
  const result = confirmationQueue.then(() => new Promise((resolve) => {
    const controls = getConfirmationDialog();
    const previousFocus = document.activeElement;
    controls.title.textContent = title || "请确认";
    controls.message.textContent = message || "";
    controls.confirm.textContent = confirmLabel;
    controls.confirm.className = danger ? "danger" : "primary";
    controls.dialog.returnValue = "";
    controls.dialog.addEventListener("close", () => {
      const accepted = controls.dialog.returnValue === "confirm";
      if (previousFocus?.isConnected) previousFocus.focus();
      resolve(accepted);
    }, { once: true });
    controls.dialog.showModal();
    (danger ? controls.cancel : controls.confirm).focus();
  }));
  confirmationQueue = result.catch(() => {});
  return result;
}

export function createEntryEditor(dialog, { onSaved }) {
  if (editorControllers.has(dialog)) return editorControllers.get(dialog);
  const holder = dialog.querySelector(".entry-editor");
  const loadStatus = dialog.querySelector(".editor-load-status");
  const title = dialog.querySelector("#entry-dialog-title");
  let panel = null;
  let loadController = null;
  let generation = 0;
  let openIntent = 0;
  let returnFocus = null;
  let returnId = "";
  let closePromise = null;
  let lastUrl = "";

  const form = () => holder.querySelector("form.entry");
  const isBusy = () => panel?.dataset.busy === "true";
  const isDirty = () => Boolean(dialog.open && (
    form()?.dataset.dirty === "true" || form()?._activeOcrZones?.size || isBusy()
  ));

  function restoreFocus() {
    const row = panel?.querySelector(`tr[data-id="${returnId}"]`);
    const target = returnFocus?.isConnected ? returnFocus
      : row?.querySelector("[data-editor-trigger]")
        || panel?.querySelector("[data-editor-trigger]");
    if (target && !target.disabled) target.focus();
  }

  function clearEditor() {
    generation += 1;
    loadController?.abort();
    loadController = null;
    const current = form();
    if (current) abortFormOcr(current);
    holder.replaceChildren();
    holder.removeAttribute("aria-busy");
    status(loadStatus, "");
    loadStatus?.querySelector(".editor-load-retry")?.remove();
    if (dialog.open) dialog.close();
    if (panel) syncControls(panel);
    if (panel) panel.dispatchEvent(new Event("panel-state-change"));
    restoreFocus();
  }

  async function close() {
    if (isBusy()) return false;
    if (!dialog.open) return true;
    if (closePromise) return closePromise;
    const currentGeneration = generation;
    closePromise = (async () => {
      if (isDirty() && !(await confirmAction({
        title: "放弃未保存的修改？",
        message: "当前草稿尚未保存。离开将放弃草稿并停止正在进行的识别，原记录和附件不会更改。",
        confirmLabel: "放弃修改",
        danger: true,
      }))) return false;
      if (currentGeneration !== generation || isBusy()) return false;
      clearEditor();
      return true;
    })();
    try {
      return await closePromise;
    } finally {
      closePromise = null;
    }
  }

  async function load(url) {
    const token = ++generation;
    loadController?.abort();
    const controller = new AbortController();
    loadController = controller;
    holder.replaceChildren();
    holder.setAttribute("aria-busy", "true");
    status(loadStatus, "正在加载编辑表单…");
    try {
      await Promise.allSettled([...uploadPreferenceWrites.values()]);
      if (token !== generation || !dialog.open) return false;
      const response = await request(url, { signal: controller.signal });
      const html = await response.text();
      if (token !== generation || !dialog.open) return false;
      const fragment = document.createElement("template");
      fragment.innerHTML = html.trim();
      if (!fragment.content.querySelector("form.entry")) throw new Error("编辑表单加载不完整，请重试");
      holder.replaceChildren(fragment.content);
      const current = form();
      for (const authors of current.querySelectorAll(".authors-editor")) syncAuthors(authors);
      for (const upload of current.querySelectorAll(".upload-control")) updateUploadSelection(upload);
      syncControls(panel);
      status(loadStatus, "");
      const label = current.querySelector(".entry-label")?.textContent.trim();
      if (label && title) title.textContent = label;
      current.querySelector("input:not([type=hidden]):not([type=file]), select, textarea, button")?.focus();
      return true;
    } catch (error) {
      if (token !== generation || error.name === "AbortError") return false;
      status(loadStatus, `加载失败：${error.message}`, true);
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "editor-load-retry";
      retry.textContent = "重新加载";
      loadStatus?.append(retry);
      return false;
    } finally {
      if (token === generation) {
        loadController = null;
        holder.removeAttribute("aria-busy");
      }
    }
  }

  async function open(nextPanel, url) {
    const intent = ++openIntent;
    const trigger = document.activeElement;
    if (!(await close()) || intent !== openIntent) return false;
    if (nextPanel.dataset.busy === "true") return false;
    panel = nextPanel;
    lastUrl = url;
    returnFocus = trigger;
    returnId = trigger?.closest("tr[data-id]")?.dataset.id || "";
    panel.append(dialog);
    if (title) title.textContent = panel.dataset.section === "basic" ? "编辑个人基本信息" : "编辑记录";
    dialog.showModal();
    syncControls(panel);
    panel.dispatchEvent(new Event("panel-state-change"));
    return load(url);
  }

  function markEdited(control) {
    const current = control.closest("form.entry");
    if (!current || isBusy() || control.matches(".ocr-enabled")) return;
    const authors = control.closest(".authors-editor");
    if (authors) syncAuthors(authors, true);
    else if (control.matches(".dotted-date-display")) markControlEdited(editDottedDate(control));
    else if (control.type !== "file") {
      syncDottedDate(control);
      markControlEdited(control);
    }
    current.dataset.dirty = "true";
    status(current.querySelector(".status"), "有未保存的修改");
  }

  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    void close();
  });
  dialog.addEventListener("input", (event) => markEdited(event.target));
  dialog.addEventListener("change", async (event) => {
    const control = event.target;
    const current = control.closest("form.entry");
    if (!current || isBusy()) return;
    if (control.matches(".ocr-enabled")) {
      await saveUploadPreference(control.closest(".ocr-zone"));
      return;
    }
    markEdited(control);
    const savedZone = control.matches('input[name="remove_file"]')
      ? control.closest("[data-saved-ocr-url]") : null;
    if (savedZone) {
      if (zoneOcrState(savedZone).mode === "saved") {
        stopZoneOcr(savedZone);
        status(savedZone.querySelector(".ocr-status"), "附件删除标记已变化，当前输入已保留；请核对后重新识别。");
      }
      syncSavedOcrControls(savedZone);
    }
    if (!control.matches(".upload-input")) return;
    const upload = control.closest(".upload-control");
    updateUploadSelection(upload);
    const file = control.files[0];
    const problem = file ? validateOcrFile(file, control, Number(current.dataset.maxUploadMb || 0)) : "";
    control.setCustomValidity(problem);
    if (problem) status(upload.querySelector(".upload-status"), problem, true);
    const zone = control.closest(".ocr-zone");
    if (!zone) return;
    stopZoneOcr(zone);
    syncSavedOcrControls(zone);
    if (file) await handleZoneFileSelected(panel, current, zone, file);
    else clearZoneSelection(upload);
  });
  dialog.addEventListener("submit", async (event) => {
    const current = event.target.closest("form.entry");
    if (!current) return;
    event.preventDefault();
    if (isBusy() || current._activeOcrZones?.size || !current.reportValidity()) return;
    for (const authors of current.querySelectorAll(".authors-editor")) syncAuthors(authors);
    let body;
    try {
      body = new FormData(current);
    } catch {
      status(current.querySelector(".status"), "附件选择已失效，请重新选择本地文件后保存；其他输入已保留。", true);
      return;
    }
    const currentPanel = panel;
    const section = currentPanel.dataset.section;
    const scroller = currentPanel.querySelector(".table-wrap");
    const scrollLeft = scroller?.scrollLeft || 0;
    const succeeded = await perform(currentPanel, async () => {
      status(current.querySelector(".status"), "保存中…");
      const response = await request(current.dataset.url, { method: current.dataset.method, body });
      const html = await response.text();
      await onSaved(section, html);
      const newScroller = currentPanel.querySelector(".table-wrap");
      if (newScroller) newScroller.scrollLeft = scrollLeft;
      delete current.dataset.dirty;
      status(currentPanel.querySelector(".panel-status"), "记录已保存。");
    });
    if (succeeded) clearEditor();
  });
  dialog.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (!button || button.disabled) return;
    if (button.matches(".editor-close, .btn-cancel")) {
      await close();
      return;
    }
    if (button.matches(".editor-load-retry")) {
      await load(lastUrl);
      return;
    }
    if (isBusy()) return;
    if (button.matches(".ocr-preference-retry")) {
      await saveUploadPreference(button.closest(".ocr-zone"));
      return;
    }
    if (button.matches(".btn-clear-date")) {
      const wrapper = button.closest(".dotted-date");
      const control = wrapper.querySelector(".dotted-date-native");
      control.value = "";
      control.dispatchEvent(new Event("input", { bubbles: true }));
      wrapper.querySelector(".dotted-date-display").focus();
    } else if (button.matches(".btn-author-add, .btn-author-remove, .btn-author-up, .btn-author-down")) {
      editAuthorList(button);
    } else if (button.matches(".ocr-saved")) {
      await triggerSavedOcr(panel, button.closest("form.entry"), button.closest(".ocr-zone"));
    } else if (button.matches(".ocr-cancel")) {
      const zone = button.closest(".ocr-zone");
      stopZoneOcr(zone);
      status(zone.querySelector(".ocr-status"), "已停止识别，当前输入与附件已保留；可重新识别。");
      const current = form();
      status(current.querySelector(".status"), current.dataset.dirty === "true" ? "有未保存的修改" : "记录未更改");
      zone.querySelector(".ocr-saved, .ocr-retry, .upload-input")?.focus();
    } else if (button.matches(".btn-clear-upload")) {
      const upload = button.closest(".upload-control");
      upload.querySelector(".upload-input").setCustomValidity("");
      clearZoneSelection(upload);
    } else if (button.matches(".ocr-retry")) {
      const zone = button.closest(".ocr-zone");
      const current = button.closest("form.entry");
      const file = zone.querySelector('input[type="file"]').files[0];
      if (zoneOcrState(zone).mode === "saved") await triggerSavedOcr(panel, current, zone);
      else if (file) await triggerOcr(panel, current, zone, file);
    }
  });
  dialog.addEventListener("dragover", (event) => {
    const zone = event.target.closest(".upload-dropzone");
    if (!zone || !event.dataTransfer?.types.includes("Files")) return;
    event.preventDefault();
    const disabled = zone.querySelector(".upload-input").disabled;
    zone.classList.toggle("dragover", !disabled);
    event.dataTransfer.dropEffect = disabled ? "none" : "copy";
  });
  dialog.addEventListener("dragleave", (event) => {
    const zone = event.target.closest(".upload-dropzone");
    if (!zone || (event.relatedTarget instanceof Node && zone.contains(event.relatedTarget))) return;
    zone.classList.remove("dragover");
  });
  dialog.addEventListener("drop", (event) => {
    const zone = event.target.closest(".upload-dropzone");
    if (!zone) return;
    event.preventDefault();
    zone.classList.remove("dragover");
    const input = zone.querySelector(".upload-input");
    const files = event.dataTransfer?.files;
    if (input.disabled || !files?.length) return;
    const message = zone.closest(".upload-control").querySelector(".upload-status");
    if (files.length !== 1) {
      status(message, "请一次只拖入一个文件", true);
      return;
    }
    try {
      input.files = files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
    } catch {
      status(message, "无法接收拖入的文件，请点击上传区域重新选择本地文件。", true);
    }
  });
  dialog.addEventListener("keydown", (event) => {
    const calendar = event.target.closest(".dotted-date-native");
    if (!calendar || calendar.disabled || !["Enter", " "].includes(event.key)
        || typeof calendar.showPicker !== "function") return;
    event.preventDefault();
    calendar.showPicker();
  });
  const controller = { open, close, isDirty, isBusy };
  editorControllers.set(dialog, controller);
  return controller;
}

export function initModelSettings(form) {
  if (modelControllers.has(form)) return modelControllers.get(form);
  function changed() {
    if (form.dataset.busy === "true") return;
    form.dataset.dirty = "true";
    if (form.elements.clear_api_key.checked) form.elements.api_key.value = "";
    invalidateOcrTest(form);
    status(form.querySelector(".panel-status"), "配置已修改，请重新测试后保存。");
  }
  form.addEventListener("input", changed);
  form.addEventListener("change", changed);
  form.addEventListener("click", async (event) => {
    const button = event.target.closest(".btn-test-ocr");
    if (button && !button.disabled) await submitOcrSettings(form, true);
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    await submitOcrSettings(form, false);
  });
  syncSettingsControls(form);
  const controller = {
    isDirty: () => form.dataset.dirty === "true",
    isBusy: () => form.dataset.busy === "true",
  };
  modelControllers.set(form, controller);
  return controller;
}
