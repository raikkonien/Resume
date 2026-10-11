"use strict";

let saving = 0;

function activate(key, focus = false) {
  const tabs = [...document.querySelectorAll(".tab")];
  const selected = tabs.find((tab) => tab.dataset.target === key) || tabs[0];
  if (!selected) return;
  for (const tab of tabs) {
    const active = tab === selected;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    document.getElementById(`tab-${tab.dataset.target}`).classList.toggle("active", active);
  }
  const strip = selected.parentElement;
  if (strip.scrollWidth > strip.clientWidth) {
    strip.scrollLeft += selected.getBoundingClientRect().left - strip.getBoundingClientRect().left;
  }
  if (focus) selected.focus();
}

function status(node, message, error = false) {
  node.textContent = message;
  node.classList.toggle("error", error);
}

async function request(url, options = {}) {
  const response = await fetch(url, {
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
  const editor = panel.querySelector("form.entry");
  panel.querySelector(".btn-add").disabled = busy || !!editor;
  for (const row of panel.querySelectorAll(".entry-table tbody tr")) {
    for (const control of row.querySelectorAll("button")) {
      const boundary = (control.dataset.direction === "-1" && !row.previousElementSibling)
        || (control.dataset.direction === "1" && !row.nextElementSibling);
      control.disabled = busy || !!editor || boundary;
    }
  }
  if (editor) {
    for (const control of editor.elements) control.disabled = busy;
    editor.setAttribute("aria-busy", String(busy));
    for (const authors of editor.querySelectorAll(".authors-editor")) syncAuthorControls(authors);
    for (const zone of editor.querySelectorAll("[data-saved-ocr-url]")) syncSavedOcrControls(zone);
    const submit = editor.querySelector('button[type="submit"]');
    if (submit && editor._activeOcrZones?.size) submit.disabled = true;
  }
}

async function perform(panel, action) {
  if (panel.dataset.busy === "true") return;
  panel.dataset.busy = "true";
  saving += 1;
  syncControls(panel);
  status(panel.querySelector(".panel-status"), "");
  let focusTarget;
  try {
    focusTarget = await action();
  } catch (error) {
    const message = panel.querySelector("form.entry .status") || panel.querySelector(".panel-status");
    status(message, `操作失败：${error.message}`, true);
  } finally {
    delete panel.dataset.busy;
    saving -= 1;
    syncControls(panel);
  }
  focusTarget?.focus();
}

async function replaceTable(panel, response) {
  const holder = document.createElement("template");
  holder.innerHTML = (await response.text()).trim();
  const list = panel.querySelector(".entry-list");
  const scrollLeft = list.querySelector(".table-wrap")?.scrollLeft || 0;
  list.replaceWith(holder.content.firstElementChild);
  const scroller = panel.querySelector(".table-wrap");
  if (scroller) scroller.scrollLeft = scrollLeft;
}

function rowButton(panel, id) {
  return panel.querySelector(`tr[data-id="${id}"] .btn-edit`) || panel.querySelector(".btn-add");
}

async function openEditor(panel, url, id = "") {
  await perform(panel, async () => {
    const response = await request(url);
    const editor = panel.querySelector(".entry-editor");
    editor.innerHTML = await response.text();
    for (const authors of editor.querySelectorAll(".authors-editor")) syncAuthors(authors);
    editor.dataset.returnId = id;
    return editor.querySelector("input, select, textarea");
  });
}

async function moveRow(panel, row, position) {
  const rows = [...panel.querySelectorAll(".entry-table tbody tr")];
  const current = rows.indexOf(row);
  if (current === position) return;
  const ids = rows.map((item) => Number(item.dataset.id));
  ids.splice(position, 0, ids.splice(current, 1)[0]);
  const section = panel.querySelector(".entry-list").dataset.section;
  await perform(panel, async () => {
    const response = await request(`/admin/${section}/reorder`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
    });
    await replaceTable(panel, response);
    status(panel.querySelector(".panel-status"), "顺序已保存，展示页已同步。");
    return rowButton(panel, row.dataset.id);
  });
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
  const problem = savedOcrProblem(zone);
  const active = Boolean(zoneOcrState(zone).controller);
  button.disabled = active || Boolean(problem) || zone.closest(".tab-panel").dataset.busy === "true";
  const help = zone.querySelector(".saved-ocr-help");
  if (!help.dataset.defaultText) help.dataset.defaultText = help.textContent;
  help.textContent = problem || help.dataset.defaultText;
  zone.querySelector(".ocr-cancel").hidden = !active || zoneOcrState(zone).mode !== "saved";
}

async function triggerSavedOcr(panel, form, zone) {
  if (!zone.dataset.savedOcrUrl || panel.dataset.busy === "true" || zoneOcrState(zone).controller) return;
  const problem = savedOcrProblem(zone);
  if (problem) {
    status(zone.querySelector(".ocr-status"), problem, true);
    return;
  }
  if (!confirm("重新识别当前已保存 PDF 的首页？\n识别结果将替换识别到的字段；已识别作者列表整体替换，请核对单位及作者身份。\n未识别到的内容、空作者列表、中科院/JCR 分区及识别期间手动修改的字段不会覆盖当前输入。\n结果仅写入当前编辑草稿，核对后点击「保存记录」才会保存。")) return;
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
  syncSavedOcrControls(zone);
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
  if (zone.classList.contains("attachment-only")) {
    const input = zone.querySelector('input[type="file"]');
    const statusNode = zone.querySelector(".ocr-status");
    const problem = validateOcrFile(file, input, Number(form.dataset.maxUploadMb || 0));
    status(statusNode, problem || "已选择，保存时作为附件上传。", Boolean(problem));
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
  if (panel.dataset.busy === "true") return;
  if (!saved) form.dataset.dirty = "true";
  status(form.querySelector(".status"), saved ? "正在识别已保存附件，记录未更改" : "附件已选择，尚未保存");

  const state = zoneOcrState(zone);
  state.controller?.abort();
  const controller = new AbortController();
  state.controller = controller;
  state.mode = saved ? "saved" : "upload";
  const token = ++state.token;
  const isCurrent = () => state.token === token;
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
  saving += 1;
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
    saving -= 1;
    syncSettingsControls(form);
  }
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest(".btn-test-ocr");
  if (button && !button.disabled) await submitOcrSettings(button.form, true);
});

document.addEventListener("input", (event) => {
  const settingsForm = event.target.closest("#ocr-settings-form");
  if (settingsForm) {
    settingsForm.dataset.dirty = "true";
    invalidateOcrTest(settingsForm);
    status(settingsForm.querySelector(".panel-status"), "配置已修改，请重新测试后保存。");
    return;
  }
  const form = event.target.closest("form.entry");
  if (!form) return;
  const authors = event.target.closest(".authors-editor");
  if (authors) syncAuthors(authors, true);
  else if (event.target.matches(".dotted-date-display")) markControlEdited(editDottedDate(event.target));
  else if (event.target.type !== "file") {
    syncDottedDate(event.target);
    markControlEdited(event.target);
  }
  form.dataset.dirty = "true";
  status(form.querySelector(".status"), "有未保存的修改");
});

document.addEventListener("submit", async (event) => {
  const settingsForm = event.target.closest("#ocr-settings-form");
  if (!settingsForm) return;
  event.preventDefault();
  await submitOcrSettings(settingsForm, false);
});

document.addEventListener("submit", async (event) => {
  const form = event.target.closest("form.entry");
  if (!form) return;
  event.preventDefault();
  const panel = form.closest(".tab-panel");
  if (panel.dataset.busy === "true" || form._activeOcrZones?.size) return;
  for (const authors of form.querySelectorAll(".authors-editor")) syncAuthors(authors);
  const body = new FormData(form);
  await perform(panel, async () => {
    status(form.querySelector(".status"), "保存中…");
    const response = await request(form.dataset.url, { method: form.dataset.method, body });
    await replaceTable(panel, response);
    const editor = panel.querySelector(".entry-editor");
    const id = editor.dataset.returnId || panel.querySelector(".entry-table tbody tr:last-child").dataset.id;
    editor.replaceChildren();
    status(panel.querySelector(".panel-status"), "记录已保存。");
    return rowButton(panel, id);
  });
});

document.addEventListener("click", async (event) => {
  const clearDate = event.target.closest(".btn-clear-date");
  if (clearDate) {
    if (clearDate.disabled) return;
    const wrapper = clearDate.closest(".dotted-date");
    const control = wrapper.querySelector(".dotted-date-native");
    control.value = "";
    control.dispatchEvent(new Event("input", { bubbles: true }));
    control.dispatchEvent(new Event("change", { bubbles: true }));
    wrapper.querySelector(".dotted-date-display").focus();
    return;
  }
  const tab = event.target.closest(".tab");
  if (tab) {
    activate(tab.dataset.target);
    location.hash = tab.dataset.target;
    return;
  }
  const authorButton = event.target.closest(".btn-author-add, .btn-author-remove, .btn-author-up, .btn-author-down");
  if (authorButton) {
    editAuthorList(authorButton);
    return;
  }
  const savedButton = event.target.closest(".ocr-saved");
  if (savedButton) {
    if (!savedButton.disabled) await triggerSavedOcr(savedButton.closest(".tab-panel"), savedButton.closest("form.entry"), savedButton.closest(".ocr-zone"));
    return;
  }
  const stopButton = event.target.closest(".ocr-cancel");
  if (stopButton) {
    if (stopButton.disabled) return;
    const zone = stopButton.closest(".ocr-zone");
    stopZoneOcr(zone);
    status(zone.querySelector(".ocr-status"), "已停止识别，当前输入与附件已保留；可点击重新识别再次尝试。");
    const form = stopButton.closest("form.entry");
    status(form.querySelector(".status"), form.dataset.dirty === "true" ? "有未保存的修改" : "识别已停止，记录未更改");
    zone.querySelector(".ocr-saved").focus();
    return;
  }
  const clearButton = event.target.closest(".btn-clear-upload");
  if (clearButton) {
    if (clearButton.disabled) return;
    clearZoneSelection(clearButton.closest(".upload-control"));
    return;
  }
  const retryButton = event.target.closest(".ocr-retry");
  if (retryButton) {
    if (retryButton.disabled) return;
    const zone = retryButton.closest(".ocr-zone");
    const form = retryButton.closest("form.entry");
    const panel = retryButton.closest(".tab-panel");
    const input = zone.querySelector('input[type="file"]');
    const file = input.files[0];
    if (zoneOcrState(zone).mode === "saved") await triggerSavedOcr(panel, form, zone);
    else if (file) await triggerOcr(panel, form, zone, file);
    return;
  }
  const button = event.target.closest(".btn-add, .btn-edit, .btn-cancel, .btn-delete, .btn-move");
  if (!button || button.disabled) return;
  const panel = button.closest(".tab-panel");
  if (panel.dataset.busy === "true") return;
  const section = panel.querySelector(".entry-list").dataset.section;
  if (button.classList.contains("btn-add")) {
    await openEditor(panel, `/admin/${section}/new`);
    return;
  }
  if (button.classList.contains("btn-cancel")) {
    const editor = panel.querySelector(".entry-editor");
    if (editor.querySelector('[data-dirty="true"]') && !confirm("放弃未保存的修改？")) return;
    const openForm = editor.querySelector("form.entry");
    if (openForm) abortFormOcr(openForm);
    const id = editor.dataset.returnId;
    editor.replaceChildren();
    syncControls(panel);
    status(panel.querySelector(".panel-status"), "已取消，记录未更改。");
    rowButton(panel, id).focus();
    return;
  }
  const row = button.closest("tr");
  if (button.classList.contains("btn-edit")) {
    await openEditor(panel, `/admin/${section}/${row.dataset.id}/edit`, row.dataset.id);
  } else if (button.classList.contains("btn-move")) {
    const index = [...row.parentElement.children].indexOf(row);
    await moveRow(panel, row, index + Number(button.dataset.direction));
  } else if (confirm("确定删除该条记录及其附件？")) {
    await perform(panel, async () => {
      const nextId = (row.nextElementSibling || row.previousElementSibling)?.dataset.id;
      const response = await request(`/admin/${section}/${row.dataset.id}`, { method: "DELETE" });
      await replaceTable(panel, response);
      status(panel.querySelector(".panel-status"), "记录已删除。");
      return rowButton(panel, nextId);
    });
  }
});

document.addEventListener("change", async (event) => {
  const clearBox = event.target.closest(".ocr-key-clear input[type=checkbox]");
  if (clearBox) {
    const settingsForm = clearBox.closest("#ocr-settings-form");
    const keyInput = settingsForm.elements.api_key;
    keyInput.disabled = clearBox.checked;
    if (clearBox.checked) keyInput.value = "";
    invalidateOcrTest(settingsForm);
    settingsForm.dataset.dirty = "true";
    status(settingsForm.querySelector(".panel-status"), "配置已修改，请重新测试后保存。");
    return;
  }
  const entry = event.target.closest("form.entry");
  if (entry && event.target.matches('select, input[type="checkbox"], .dotted-date-native, .dotted-date-display')) {
    const authors = event.target.closest(".authors-editor");
    if (authors) syncAuthors(authors, true);
    else if (event.target.matches(".dotted-date-display")) markControlEdited(editDottedDate(event.target));
    else {
      syncDottedDate(event.target);
      markControlEdited(event.target);
    }
    entry.dataset.dirty = "true";
    status(entry.querySelector(".status"), "有未保存的修改");
  }
  const removeBox = event.target.closest('input[name="remove_file"]');
  const savedZone = removeBox?.closest("[data-saved-ocr-url]");
  if (savedZone) {
    if (zoneOcrState(savedZone).mode === "saved") {
      stopZoneOcr(savedZone);
      status(savedZone.querySelector(".ocr-status"), "附件删除标记已变化，当前输入已保留；请核对后重新识别。");
    }
    syncSavedOcrControls(savedZone);
    return;
  }
  const input = event.target.closest(".upload-input");
  if (!input) return;
  const control = input.closest(".upload-control");
  updateUploadSelection(control);
  const zone = input.closest(".ocr-zone");
  if (!zone) return;
  syncSavedOcrControls(zone);
  const form = input.closest("form.entry");
  const panel = input.closest(".tab-panel");
  const file = input.files[0];
  form.dataset.dirty = "true";
  status(form.querySelector(".status"), "有未保存的修改");
  if (file) await handleZoneFileSelected(panel, form, zone, file);
  else clearZoneSelection(control);
});

document.addEventListener("dragover", (event) => {
  const zone = event.target.closest(".upload-dropzone");
  if (!zone || !event.dataTransfer?.types.includes("Files")) return;
  event.preventDefault();
  const disabled = zone.querySelector(".upload-input").disabled;
  zone.classList.toggle("dragover", !disabled);
  event.dataTransfer.dropEffect = disabled ? "none" : "copy";
});

document.addEventListener("dragleave", (event) => {
  const zone = event.target.closest(".upload-dropzone");
  if (!zone || (event.relatedTarget instanceof Node && zone.contains(event.relatedTarget))) return;
  zone.classList.remove("dragover");
});

document.addEventListener("drop", (event) => {
  const zone = event.target.closest(".upload-dropzone");
  if (!zone) return;
  event.preventDefault();
  zone.classList.remove("dragover");
  const input = zone.querySelector(".upload-input");
  if (input.disabled) return;
  const control = zone.closest(".upload-control");
  const files = event.dataTransfer?.files;
  if (!files?.length) return;
  if (files.length !== 1) {
    status(control.querySelector(".upload-status"), "请一次只拖入一个文件", true);
    return;
  }
  input.files = files;
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.dispatchEvent(new Event("change", { bubbles: true }));
});

document.addEventListener("keydown", (event) => {
  const calendar = event.target.closest(".dotted-date-native");
  if (calendar && !calendar.disabled && ["Enter", " "].includes(event.key)
      && typeof calendar.showPicker === "function") {
    event.preventDefault();
    calendar.showPicker();
    return;
  }
  const tab = event.target.closest(".tab");
  if (!tab) return;
  const tabs = [...document.querySelectorAll(".tab")];
  const index = tabs.indexOf(tab);
  let next;
  if (["ArrowRight", "ArrowDown"].includes(event.key)) next = tabs[(index + 1) % tabs.length];
  else if (["ArrowLeft", "ArrowUp"].includes(event.key)) next = tabs[(index + tabs.length - 1) % tabs.length];
  else if (event.key === "Home") next = tabs[0];
  else if (event.key === "End") next = tabs[tabs.length - 1];
  if (next) {
    event.preventDefault();
    activate(next.dataset.target, true);
    location.hash = next.dataset.target;
  }
});

window.addEventListener("hashchange", () => activate(location.hash.slice(1)));
window.addEventListener("beforeunload", (event) => {
  if (saving || document.querySelector('form.entry[data-dirty="true"], #ocr-settings-form[data-dirty="true"]')) {
    event.preventDefault();
    event.returnValue = "";
  }
});
activate(location.hash.slice(1) || "basic");
