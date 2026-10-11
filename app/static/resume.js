"use strict";

const NAV_OFFSET = 100; // matches .block[id] { scroll-margin-top: 100px } in style.css
const MAX_RESULTS = 8;
const CONTEXT_RADIUS = 40;

function reducedMotion() {
  return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function scrollBehavior() {
  return reducedMotion() ? "auto" : "smooth";
}

// ---------------------------------------------------------------------------
// Scrollspy: highlight the resume-nav link matching whichever card is under
// the sticky-nav offset line. A trailing spacer guarantees every section
// (including short tail sections) can actually be scrolled up to that offset
// line, so a single plain threshold scan is correct for all of them — no
// bottom-of-page special-casing or hash-based overrides that could go stale
// against real user scroll. Read-only rect checks + minimal class/attr
// writes (only when the active id actually changes) — never writes
// scrollTop on window — so there is no feedback loop with scroll position.
// ---------------------------------------------------------------------------
function ensureTailReachable(lastBlock) {
  if (!lastBlock) return;
  const spacer = document.createElement("div");
  spacer.className = "resume-scroll-spacer";
  spacer.setAttribute("aria-hidden", "true");
  spacer.style.height = "0px";
  (document.querySelector("main") || document.body).appendChild(spacer);

  function update() {
    spacer.style.height = "0px";
    const topAbs = lastBlock.getBoundingClientRect().top + window.scrollY;
    const needed = topAbs - NAV_OFFSET + window.innerHeight;
    const current = document.documentElement.scrollHeight;
    spacer.style.height = Math.max(0, needed - current + 2) + "px";
  }

  window.addEventListener("resize", update);
  update();
}

function initScrollspy() {
  const nav = document.querySelector(".resume-nav");
  if (!nav) return [];
  const navLinks = Array.from(nav.querySelectorAll('a[href^="#"]'));
  const blocks = navLinks
    .map((link) => document.getElementById(link.getAttribute("href").slice(1)))
    .filter(Boolean);
  if (!blocks.length) return navLinks;

  ensureTailReachable(blocks[blocks.length - 1]);

  function revealActiveTab(link) {
    const navRect = nav.getBoundingClientRect();
    const linkRect = link.getBoundingClientRect();
    if (linkRect.left >= navRect.left && linkRect.right <= navRect.right) return;
    const target = nav.scrollLeft + (linkRect.left - navRect.left) - (nav.clientWidth - linkRect.width) / 2;
    nav.scrollLeft = Math.max(0, Math.min(target, nav.scrollWidth - nav.clientWidth));
  }

  let currentActiveLink = null;
  function setActive(id) {
    const nextLink = navLinks.find((link) => link.getAttribute("href") === "#" + id) || null;
    if (nextLink === currentActiveLink) return;
    if (currentActiveLink) {
      currentActiveLink.classList.remove("active");
      currentActiveLink.removeAttribute("aria-current");
    }
    if (nextLink) {
      nextLink.classList.add("active");
      nextLink.setAttribute("aria-current", "location");
      revealActiveTab(nextLink);
    }
    currentActiveLink = nextLink;
  }

  let currentActiveId = null;
  let ticking = false;
  function computeActive() {
    ticking = false;
    let activeId = blocks[0].id;
    for (const block of blocks) {
      if (block.getBoundingClientRect().top - NAV_OFFSET <= 1) activeId = block.id;
      else break;
    }
    if (activeId === currentActiveId) return;
    currentActiveId = activeId;
    setActive(activeId);
  }

  function onScroll() {
    if (ticking) return;
    ticking = true;
    window.requestAnimationFrame(computeActive);
  }

  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", onScroll);
  computeActive();
  return navLinks;
}

// ---------------------------------------------------------------------------
// Search index: built once from the already-rendered DOM (no second data
// source, no fetch). Each entry targets the exact element holding a single
// text node, so highlighting can safely splice that node without touching
// sibling markup (e.g. the "下载" link next to a filename caption). Text is
// pre-lowercased once so every keystroke only does a cheap indexOf scan.
// ---------------------------------------------------------------------------
function buildIndex(navLinks) {
  const labelByBlockId = new Map();
  navLinks.forEach((link) => {
    const id = link.getAttribute("href").slice(1);
    labelByBlockId.set(id, link.textContent.trim());
  });

  const entries = [];
  function addEntry(blockId, sectionLabel, fieldLabel, text, element) {
    if (!text || text === "—") return;
    entries.push({ blockId, sectionLabel, fieldLabel, text, textLower: text.toLowerCase(), element });
  }

  document.querySelectorAll(".block[id]").forEach((block) => {
    const sectionLabel = labelByBlockId.get(block.id) || "";
    if (block.id === "resume-basic") {
      block.querySelectorAll(".basic-item").forEach((item) => {
        const dt = item.querySelector("dt");
        const dd = item.querySelector("dd");
        if (!dt || !dd) return;
        addEntry(block.id, sectionLabel, dt.textContent.trim(), (dd.textContent || "").trim(), dd);
      });
      const photoName = block.querySelector(".portrait .file-name");
      if (photoName) addEntry(block.id, sectionLabel, "照片", (photoName.textContent || "").trim(), photoName);
      return;
    }
    const table = block.querySelector("table");
    if (!table) return;
    const headers = Array.from(table.querySelectorAll("thead th")).map((th) => th.textContent.trim());
    table.querySelectorAll("tbody tr").forEach((tr) => {
      Array.from(tr.children).forEach((td, index) => {
        const fieldLabel = headers[index] || "";
        const authorNames = td.querySelectorAll(".paper-author-name");
        if (authorNames.length) {
          authorNames.forEach((name) => addEntry(block.id, sectionLabel, fieldLabel, name.textContent.trim(), name));
          return;
        }
        const paperTitle = td.querySelector(".paper-title");
        if (paperTitle) {
          addEntry(block.id, sectionLabel, fieldLabel, paperTitle.textContent.trim(), paperTitle);
          return;
        }
        const fileName = td.querySelector(".file-name");
        if (fileName) {
          addEntry(block.id, sectionLabel, fieldLabel, (fileName.textContent || "").trim(), fileName);
          return;
        }
        addEntry(block.id, sectionLabel, fieldLabel, (td.textContent || "").trim(), td);
      });
    });
  });
  return entries;
}

function matchEntries(entries, query) {
  const needle = query.trim().toLowerCase();
  if (!needle) return { list: [], total: 0 };
  const list = [];
  let total = 0;
  for (const entry of entries) {
    if (entry.textLower.indexOf(needle) !== -1) {
      total += 1;
      if (list.length < MAX_RESULTS) list.push(entry);
    }
  }
  return { list, total };
}

function snippetParts(text, query) {
  const lower = text.toLowerCase();
  const idx = lower.indexOf(query.toLowerCase());
  if (idx === -1) return { before: text, match: "", after: "" };
  const start = Math.max(0, idx - CONTEXT_RADIUS);
  const end = Math.min(text.length, idx + query.length + CONTEXT_RADIUS);
  return {
    before: (start > 0 ? "…" : "") + text.slice(start, idx),
    match: text.slice(idx, idx + query.length),
    after: text.slice(idx + query.length, end) + (end < text.length ? "…" : ""),
  };
}

function renderOption(entry, query, index) {
  const li = document.createElement("li");
  li.id = "resume-search-option-" + index;
  li.className = "search-option";
  li.setAttribute("role", "option");
  li.setAttribute("aria-selected", "false");
  li.dataset.index = String(index);

  const category = document.createElement("span");
  category.className = "search-option-category";
  category.textContent = entry.sectionLabel;
  li.appendChild(category);

  const content = document.createElement("span");
  content.className = "search-option-content";

  const field = document.createElement("span");
  field.className = "search-option-field";
  field.textContent = entry.fieldLabel;
  content.appendChild(field);

  // Match + a little context are shown on every result; the "before" span
  // truncates from its own left edge (via a direction:rtl ellipsis trick) so
  // the keyword and the "after" span truncating on the right never lose the
  // match itself, however narrow the dropdown gets (see style.css).
  const snippet = document.createElement("span");
  snippet.className = "search-option-snippet";
  const parts = snippetParts(entry.text, query);

  const before = document.createElement("span");
  before.className = "snippet-before";
  before.textContent = parts.before;
  snippet.appendChild(before);

  if (parts.match) {
    const mark = document.createElement("mark");
    mark.textContent = parts.match;
    snippet.appendChild(mark);
  }

  const after = document.createElement("span");
  after.className = "snippet-after";
  after.textContent = parts.after;
  snippet.appendChild(after);

  content.appendChild(snippet);
  li.appendChild(content);
  return li;
}

// ---------------------------------------------------------------------------
// Highlighting: every indexed element has exactly one text-node child (plain
// text rendered by format_value / the filename caption). We re-read the LIVE
// text node (not the trimmed index copy) so clearing restores the exact
// original text/whitespace, and splitText() lets us wrap the matched
// substring in a <mark> using pure DOM APIs — no innerHTML, so stored
// content or the typed query can never be parsed as markup.
// ---------------------------------------------------------------------------
let activeHighlight = null;

function clearHighlight() {
  if (!activeHighlight) return;
  activeHighlight.element.textContent = activeHighlight.originalText;
  activeHighlight = null;
}

function applyHighlight(entry, query) {
  clearHighlight();
  const el = entry.element;
  const startNode = el.firstChild;
  if (!startNode || startNode.nodeType !== Node.TEXT_NODE) return null;
  const rawText = startNode.textContent;
  const idx = rawText.toLowerCase().indexOf(query.toLowerCase());
  if (idx === -1) return null;
  activeHighlight = { element: el, originalText: rawText };
  const matchNode = startNode.splitText(idx);
  matchNode.splitText(query.length);
  const mark = document.createElement("mark");
  mark.className = "search-highlight";
  el.replaceChild(mark, matchNode);
  mark.appendChild(matchNode);
  return mark;
}

// ---------------------------------------------------------------------------
// Navigation: vertical and horizontal scrolling are two fully independent
// scroll containers (document vs. the matched .table-wrap) computed from a
// single measurement pass, so there is no risk of one scroll cancelling the
// other. Highlighting happens first so the actual <mark> position — not
// just the whole cell — drives targeting, keeping oversize cells and
// matches buried deep in wrapped text vertically and horizontally visible.
// ---------------------------------------------------------------------------
function pickVerticalTarget(hitEl, heading) {
  const headingRect = heading.getBoundingClientRect();
  const hitRect = hitEl.getBoundingClientRect();
  const available = window.innerHeight - NAV_OFFSET;
  if (hitRect.bottom - headingRect.top <= available) return heading;
  const row = hitEl.closest("tr") || hitEl.closest(".basic-item");
  if (row && row.getBoundingClientRect().height <= available) return row;
  return hitEl;
}

function scrollTableHorizontally(hitEl) {
  const wrap = hitEl.closest(".table-wrap");
  if (!wrap) return;
  const wrapRect = wrap.getBoundingClientRect();
  const elRect = hitEl.getBoundingClientRect();
  if (elRect.left >= wrapRect.left && elRect.right <= wrapRect.right) return;
  const target = wrap.scrollLeft + (elRect.left - wrapRect.left) - (wrap.clientWidth - elRect.width) / 2;
  wrap.scrollLeft = Math.max(0, Math.min(target, wrap.scrollWidth - wrap.clientWidth));
}

function navigateToEntry(entry, query) {
  const block = document.getElementById(entry.blockId);
  if (!block) return;
  const mark = applyHighlight(entry, query);
  const hitEl = mark || entry.element;
  const heading = block.querySelector("h2") || block;
  const behavior = scrollBehavior();
  const verticalTarget = pickVerticalTarget(hitEl, heading);
  const rect = verticalTarget.getBoundingClientRect();
  const y = Math.max(0, rect.top + window.scrollY - NAV_OFFSET);
  window.scrollTo({ top: y, behavior });
  scrollTableHorizontally(hitEl);
  if (window.history && window.history.pushState) {
    window.history.pushState(null, "", "#" + entry.blockId);
  }
}

// ---------------------------------------------------------------------------
// Combobox controller: synchronous input matching (the index is small enough
// that a debounce only introduces stale-result/reopen-after-close bugs),
// arrow/enter/escape keyboard support (ignoring IME composition so
// confirming Chinese input doesn't navigate), click selection, a no-results
// state, and a clear affordance that also removes any live page highlight.
// ---------------------------------------------------------------------------
function initSearch(entries) {
  const root = document.querySelector(".resume-search");
  if (!root) return;
  const input = root.querySelector("#resume-search-input");
  const clearBtn = root.querySelector("#resume-search-clear");
  const list = root.querySelector("#resume-search-list");
  const status = root.querySelector("#resume-search-status");
  if (!input || !clearBtn || !list || !status) return;

  let results = [];
  let activeIndex = -1;

  function closeList() {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    activeIndex = -1;
  }

  function openList() {
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
  }

  function setActiveOption(index) {
    const options = list.querySelectorAll(".search-option");
    activeIndex = index;
    options.forEach((option, i) => {
      const isActive = i === index;
      option.classList.toggle("active", isActive);
      option.setAttribute("aria-selected", isActive ? "true" : "false");
    });
    if (index >= 0 && options[index]) {
      options[index].scrollIntoView({ block: "nearest" });
      input.setAttribute("aria-activedescendant", options[index].id);
    } else {
      input.removeAttribute("aria-activedescendant");
    }
  }

  function renderResults(query, total) {
    list.textContent = "";
    if (!results.length) {
      const li = document.createElement("li");
      li.className = "search-empty";
      li.textContent = "未找到匹配内容";
      list.appendChild(li);
      status.textContent = "未找到匹配内容";
      openList();
      return;
    }
    results.forEach((entry, index) => list.appendChild(renderOption(entry, query, index)));
    status.textContent = total > results.length
      ? "显示前 " + results.length + " 项匹配（共 " + total + " 项）"
      : "共 " + results.length + " 项匹配";
    openList();
  }

  function runSearch() {
    const query = input.value.trim();
    if (!query) {
      results = [];
      closeList();
      list.textContent = "";
      status.textContent = "";
      clearHighlight();
      return;
    }
    const matched = matchEntries(entries, query);
    results = matched.list;
    renderResults(query, matched.total);
    setActiveOption(-1);
  }

  function clearSearch() {
    input.value = "";
    clearBtn.hidden = true;
    results = [];
    closeList();
    list.textContent = "";
    status.textContent = "";
    clearHighlight();
    input.focus();
  }

  function maybeReopen() {
    if (list.hidden && input.value.trim()) runSearch();
  }

  input.addEventListener("input", () => {
    clearBtn.hidden = !input.value;
    runSearch();
  });

  input.addEventListener("focus", maybeReopen);
  input.addEventListener("click", maybeReopen);

  input.addEventListener("keydown", (event) => {
    if (event.isComposing || event.keyCode === 229) return;
    if (event.key === "ArrowDown") {
      if (!results.length) return;
      event.preventDefault();
      if (list.hidden) openList();
      setActiveOption((activeIndex + 1) % results.length);
    } else if (event.key === "ArrowUp") {
      if (!results.length) return;
      event.preventDefault();
      if (list.hidden) openList();
      setActiveOption(activeIndex <= 0 ? results.length - 1 : activeIndex - 1);
    } else if (event.key === "Enter") {
      if (!results.length) return;
      event.preventDefault();
      const entry = results[activeIndex >= 0 ? activeIndex : 0];
      navigateToEntry(entry, input.value.trim());
      closeList();
    } else if (event.key === "Escape") {
      if (!list.hidden) {
        event.preventDefault();
        closeList();
      } else if (input.value) {
        event.preventDefault();
        clearSearch();
      }
    }
  });

  list.addEventListener("mousedown", (event) => event.preventDefault());
  list.addEventListener("click", (event) => {
    const option = event.target.closest(".search-option");
    if (!option) return;
    const entry = results[Number(option.dataset.index)];
    if (!entry) return;
    navigateToEntry(entry, input.value.trim());
    closeList();
  });

  clearBtn.addEventListener("click", clearSearch);

  root.addEventListener("focusout", (event) => {
    if (!root.contains(event.relatedTarget)) closeList();
  });
  document.addEventListener("click", (event) => {
    if (!root.contains(event.target)) closeList();
  });
}

const scrollspyLinks = initScrollspy();
initSearch(buildIndex(scrollspyLinks));

function initPdfExport() {
  const button = document.getElementById("export-resume");
  const dialog = document.getElementById("export-dialog");
  if (!button || !dialog) return;
  const title = document.getElementById("export-title");
  const message = document.getElementById("export-message");
  const spinner = dialog.querySelector(".export-spinner");
  const dismiss = document.getElementById("export-dismiss");
  const status = document.getElementById("export-status");
  dismiss.addEventListener("click", () => dialog.close());

  button.addEventListener("click", async () => {
    if (button.disabled) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = "正在生成…";
    title.textContent = "正在生成简历 PDF";
    message.textContent = "正在整理资料与分页，完成后将自动下载。关闭此窗口后仍会继续生成。";
    dismiss.textContent = "后台继续生成";
    spinner.hidden = false;
    status.hidden = true;
    status.classList.remove("error");
    dialog.showModal();
    try {
      const response = await fetch(button.dataset.url, {
        method: "POST",
        headers: { "X-Requested-With": "fetch" },
        credentials: "same-origin",
      });
      if (response.status === 401) throw new Error("登录已过期，请重新登录后导出。");
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.error || data.detail || "PDF 生成失败，请稍后重试。");
      }
      if (!response.headers.get("Content-Type")?.startsWith("application/pdf")) {
        throw new Error("未收到 PDF 文件，请重新登录后重试。");
      }
      const blob = await response.blob();
      const filename = response.headers.get("Content-Disposition")?.match(/filename\*=UTF-8''([^;]+)/i);
      const link = document.createElement("a");
      const url = URL.createObjectURL(blob);
      link.href = url;
      link.download = filename ? decodeURIComponent(filename[1]) : "简历.pdf";
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 60000);
      if (dialog.open) dialog.close();
      status.textContent = "简历 PDF 已生成并开始下载。";
    } catch (error) {
      title.textContent = "导出未完成";
      message.textContent = error.message || "网络连接失败，请稍后重试。";
      spinner.hidden = true;
      dismiss.textContent = "关闭";
      status.textContent = message.textContent;
      status.classList.add("error");
    } finally {
      status.hidden = false;
      button.disabled = false;
      button.removeAttribute("aria-busy");
      button.textContent = "导出简历";
    }
  });
}

initPdfExport();

function initPaperDetails() {
  const dialog = document.getElementById("paper-dialog");
  if (!dialog) return;
  const close = document.getElementById("paper-dialog-close");
  const content = document.getElementById("paper-dialog-content");
  const status = document.getElementById("paper-dialog-status");
  const retry = document.getElementById("paper-dialog-retry");
  const body = dialog.querySelector(".paper-dialog-body");
  let controller = null;
  let opener = null;
  let url = "";

  async function load() {
    controller?.abort();
    const active = new AbortController();
    controller = active;
    content.replaceChildren();
    content.setAttribute("aria-busy", "true");
    status.classList.remove("error");
    status.textContent = "正在加载文章详情…";
    retry.hidden = true;
    body.scrollTop = 0;
    try {
      const response = await fetch(url, {
        headers: { "X-Requested-With": "fetch" },
        credentials: "same-origin",
        cache: "no-store",
        signal: active.signal,
      });
      if (response.status === 401 || response.redirected) throw new Error("登录已过期，请重新登录后查看。");
      if (response.status === 404) throw new Error("文章不存在或已删除，请刷新列表。");
      if (!response.ok || !response.headers.get("Content-Type")?.startsWith("text/html")) {
        throw new Error("文章详情加载失败，请重试。");
      }
      const html = await response.text();
      if (controller !== active || !dialog.open) return;
      // Only this authenticated, same-origin, auto-escaped template is inserted.
      content.innerHTML = html;
      status.textContent = "";
    } catch (error) {
      if (controller !== active || !dialog.open || error.name === "AbortError") return;
      status.textContent = error.message || "网络连接失败，请重试。";
      status.classList.add("error");
      retry.hidden = false;
    } finally {
      if (controller === active) {
        controller = null;
        content.removeAttribute("aria-busy");
      }
    }
  }

  document.getElementById("resume-papers")?.addEventListener("click", (event) => {
    const button = event.target.closest(".paper-title");
    if (!button) return;
    opener = button;
    url = button.dataset.url;
    if (!dialog.open) dialog.showModal();
    document.body.classList.add("paper-dialog-open");
    close.focus({ preventScroll: true });
    load();
  });
  close.addEventListener("click", () => dialog.close());
  retry.addEventListener("click", load);
  dialog.addEventListener("close", () => {
    controller?.abort();
    controller = null;
    content.replaceChildren();
    content.removeAttribute("aria-busy");
    document.body.classList.remove("paper-dialog-open");
    if (opener?.isConnected) opener.focus({ preventScroll: true });
    opener = null;
  });
}

initPaperDetails();
