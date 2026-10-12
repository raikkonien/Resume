const collator = new Intl.Collator('zh-CN', { numeric: true, sensitivity: 'base' });
const clone = value => JSON.parse(JSON.stringify(value));
const text = value => String(value ?? '');
const folded = value => text(value).toLowerCase();
const empty = value => value == null || (typeof value === 'string' && !value.trim());
const rangeTypes = new Set(['date', 'month', 'year', 'int', 'number']);

function element(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content != null) node.textContent = content;
  return node;
}

function button(label, className = '') {
  const node = element('button', className, label);
  node.type = 'button';
  return node;
}

function json(value, fallback) {
  try { return JSON.parse(value); } catch { return fallback; }
}

function numeric(value) {
  if (typeof value === 'boolean' || empty(value)) return null;
  const result = Number(value);
  return Number.isFinite(result) ? result : null;
}

function rank(value) {
  const parts = text(value).trim().match(/^(\d+)\s*\/\s*(\d+)$/);
  return parts ? [Number(parts[1]), Number(parts[2])] : null;
}

function compareValue(type, left, right) {
  if (['year', 'int', 'number', 'checkbox'].includes(type)) {
    return Number(left) - Number(right);
  }
  if (type === 'rank') {
    const a = rank(left), b = rank(right);
    if (a && b) return a[0] - b[0] || a[1] - b[1];
  }
  if (type === 'date' || type === 'month') {
    return text(left) < text(right) ? -1 : text(left) > text(right) ? 1 : 0;
  }
  return collator.compare(text(left), text(right));
}

function matchesFilter(field, cell, filter) {
  if (!cell) return false;
  if (filter.op === 'contains') return folded(cell.search).includes(folded(filter.value));
  if (filter.op === 'eq') return cell.value === filter.value;
  if (filter.op === 'presence') return cell.present === (filter.value === 'present');
  if (filter.op === 'range') {
    if (empty(cell.value)) return false;
    const value = ['date', 'month'].includes(field.type) ? cell.value : numeric(cell.value);
    if (value == null) return false;
    return (filter.min == null || value >= filter.min) && (filter.max == null || value <= filter.max);
  }
  return true;
}

// Only our own marks/evidence are removed. Links, author emphasis and controls survive.
function clearHighlight(cell) {
  cell.querySelectorAll('[data-table-evidence]').forEach(node => node.remove());
  cell.querySelectorAll('mark[data-table-mark]').forEach(mark => {
    const parent = mark.parentNode;
    mark.replaceWith(document.createTextNode(mark.textContent));
    parent.normalize();
  });
}

function highlight(root, query) {
  if (!query) return;
  const needle = folded(query);
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      return node.parentElement.closest('button:not(.paper-title), svg, script, style, mark, [data-table-evidence]')
        ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
    },
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    const original = node.nodeValue, lower = original.toLowerCase();
    let at = lower.indexOf(needle), start = 0;
    let offsets = null;
    // Unicode lowercase can expand a character (for example İ → i + dot).
    // Search folded text, but always mark the corresponding original characters.
    if (original.length !== lower.length) {
      offsets = [];
      let sourceIndex = 0;
      for (const character of original) {
        for (let index = 0; index < character.toLowerCase().length; index++) {
          offsets.push([sourceIndex, sourceIndex + character.length]);
        }
        sourceIndex += character.length;
      }
    }
    if (at < 0) continue;
    const fragment = document.createDocumentFragment();
    while (at >= 0) {
      const from = offsets ? offsets[at][0] : at;
      const to = offsets ? offsets[at + needle.length - 1][1] : at + needle.length;
      fragment.append(document.createTextNode(original.slice(start, from)));
      const mark = element('mark', '', original.slice(from, to));
      mark.dataset.tableMark = 'true';
      fragment.append(mark);
      start = to;
      at = lower.indexOf(needle, at + needle.length);
    }
    fragment.append(document.createTextNode(original.slice(start)));
    node.replaceWith(fragment);
  }
}

function excerpt(value, query) {
  const source = text(value), index = folded(source).indexOf(folded(query));
  const start = Math.max(0, index - 35), end = Math.min(source.length, index + query.length + 65);
  return (start ? '…' : '') + source.slice(start, end) + (end < source.length ? '…' : '');
}

function evidence(cell, label, value, query, className) {
  const node = element('div', `table-match-evidence muted ${className || ''}`);
  // Highlight before attaching the evidence marker so the walker can visit its text.
  node.append(document.createTextNode(label + value));
  highlight(node, query);
  node.dataset.tableEvidence = 'true';
  cell.append(node);
}

/** One controller per persistent section panel; refresh replaces only its DOM index. */
export function createTableController(panel, initialLayout, { onLayoutChange, onReorder } = {}) {
  let list, table, tbody, wrap, fields = new Map(), records = [], byID = new Map();
  let layout = clone(initialLayout || { version: 1, column_order: [], hidden_columns: [], sort: null, filters: {} });
  let view = { query: '', year: null, entry: null, showActions: false };
  let visible = [], pending = false, popover = null, popoverCleanup = null;
  let drag = null, suppressClick = null, preview = null, reorderMessage = '';
  let indexVersion = 0;

  function defaultLayout() {
    return { version: 1, column_order: [...fields.keys()], hidden_columns: [], sort: null, filters: {} };
  }

  function normalizeLayout() {
    const keys = [...fields.keys()];
    const order = [...new Set((layout.column_order || []).filter(key => fields.has(key)))];
    for (const key of keys) if (!order.includes(key)) order.push(key);
    const hidden = [...new Set((layout.hidden_columns || []).filter(key => fields.has(key)))];
    if (order.length && hidden.length === order.length) hidden.splice(hidden.indexOf(order[0]), 1);
    const sort = layout.sort && fields.has(layout.sort.field) && ['asc', 'desc'].includes(layout.sort.direction)
      ? { field: layout.sort.field, direction: layout.sort.direction } : null;
    const filters = Object.create(null);
    for (const [key, filter] of Object.entries(layout.filters || {})) {
      if (fields.has(key) && filter && typeof filter === 'object') filters[key] = clone(filter);
    }
    layout = { version: 1, column_order: order, hidden_columns: hidden, sort, filters };
  }

  function changed() {
    render();
    onLayoutChange?.(getLayout());
  }

  function applyColumns() {
    const hidden = new Set(layout.hidden_columns);
    const head = table.tHead?.rows[0];
    for (const key of layout.column_order) {
      const field = fields.get(key);
      head?.append(field.th);
      field.th.hidden = hidden.has(key);
      field.th.setAttribute('aria-sort', layout.sort?.field === key
        ? (layout.sort.direction === 'asc' ? 'ascending' : 'descending') : 'none');
      const indicator = field.th.querySelector('.sort-indicator');
      if (indicator) indicator.textContent = layout.sort?.field === key ? (layout.sort.direction === 'asc' ? ' ↑' : ' ↓') : '';
      const filterButton = field.th.querySelector('.btn-filter');
      if (filterButton) {
        const active = Object.hasOwn(layout.filters, key);
        filterButton.classList.toggle('filter-active', active);
        filterButton.setAttribute('aria-pressed', String(active));
        filterButton.title = `筛选${field.label}${active ? '（已设置）' : ''}`;
      }
      for (const record of records) {
        const cell = record.cells.get(key)?.node;
        if (cell) { record.node.append(cell); cell.hidden = hidden.has(key); }
      }
    }
    head?.querySelectorAll('.record-actions').forEach(node => { head.append(node); node.hidden = !view.showActions; });
    for (const record of records) {
      record.node.querySelectorAll('.record-actions').forEach(node => { record.node.append(node); node.hidden = !view.showActions; });
    }
  }

  function matches(record) {
    const query = folded(view.query);
    if (query && ![...record.cells.values()].some(cell => folded(cell.search).includes(query))) return false;
    if (view.year != null && view.year !== '') {
      if (list.dataset.single === 'true') return false;
      if (view.year === 'undated' ? record.year !== '' : record.year !== String(view.year)) return false;
    }
    if (view.entry != null && view.entry !== '' && record.id !== Number(view.entry)) return false;
    // Explicit navigation/search must never be concealed by a saved field filter.
    if (!query && view.year == null && view.entry == null) {
      for (const [key, filter] of Object.entries(layout.filters)) {
        if (!matchesFilter(fields.get(key), record.cells.get(key), filter)) return false;
      }
    }
    return true;
  }

  function statusNodes() {
    let status = list.querySelector('.table-reorder-status');
    if (!status) {
      status = element('p', 'table-reorder-status muted');
      status.setAttribute('role', 'status');
      status.setAttribute('aria-live', 'polite');
      list.append(status);
    }
    status.replaceChildren();
    if (pending) status.append(document.createTextNode('正在保存行顺序…'));
    else if (reorderMessage) status.append(document.createTextNode(reorderMessage));
    if (layout.sort && list.dataset.single !== 'true') {
      if (status.childNodes.length) status.append(document.createTextNode(' '));
      status.append(document.createTextNode('切换到手动顺序后拖动。'));
      const manual = button('切换到手动顺序', 'btn-manual-order quiet');
      manual.disabled = pending;
      status.append(manual);
    }
    status.hidden = !status.childNodes.length;
  }

  function render() {
    if (!table || !tbody) return 0;
    applyColumns();
    const hidden = new Set(layout.hidden_columns);
    const firstKey = layout.column_order.find(key => !hidden.has(key));
    let ordered = preview ? preview.map(id => byID.get(id)).filter(Boolean) : records.slice();
    if (layout.sort) {
      const { field: key, direction } = layout.sort, type = fields.get(key).type;
      const positions = new Map(records.map((row, index) => [row.id, index]));
      ordered.sort((a, b) => {
        const left = a.cells.get(key)?.value, right = b.cells.get(key)?.value;
        const leftEmpty = empty(left), rightEmpty = empty(right);
        if (leftEmpty !== rightEmpty) return leftEmpty ? 1 : -1;
        const comparison = leftEmpty ? 0 : compareValue(type, left, right);
        return (direction === 'desc' ? -comparison : comparison) || positions.get(a.id) - positions.get(b.id) || a.id - b.id;
      });
    }
    visible = [];
    for (const record of ordered) {
      record.node.hidden = !matches(record);
      tbody.append(record.node);
      for (const cell of record.cells.values()) clearHighlight(cell.node);
      if (record.node.hidden) continue;
      visible.push(record);
      if (view.query) {
        for (const [key, cell] of record.cells) {
          if (hidden.has(key)) {
            if (folded(cell.search).includes(folded(view.query))) {
              const target = record.cells.get(firstKey)?.node;
              if (target) evidence(target, `命中隐藏字段：${fields.get(key).label} · `,
                excerpt(cell.search, view.query), view.query, 'hidden-field-match');
            }
          } else {
            highlight(cell.node, view.query);
            if (fields.get(key).type === 'file' && folded(cell.search).includes(folded(view.query))) {
              evidence(cell.node, '', text(cell.value || cell.search), view.query, 'file-name-match');
            } else if (folded(cell.search).includes(folded(view.query))
              && !cell.node.querySelector('mark[data-table-mark]:not([data-table-evidence] mark)')) {
              evidence(cell.node, '匹配内容：', excerpt(cell.search, view.query), view.query, 'visible-field-match');
            }
          }
        }
      }
    }
    const canReorder = !pending && panel.dataset.busy !== 'true' && !panel.querySelector('#entry-dialog')?.open
      && !layout.sort && list.dataset.single !== 'true' && typeof onReorder === 'function';
    const visiblePositions = new Map(visible.map((record, index) => [record.id, index]));
    for (const record of records) {
      const at = visiblePositions.get(record.id) ?? -1;
      for (const control of record.node.querySelectorAll('.row-drag, .row-up, .row-down')) {
        const blocked = !canReorder || at < 0 || (control.matches('.row-up') && at === 0)
          || (control.matches('.row-down') && at === visible.length - 1);
        control.disabled = blocked;
        control.title = layout.sort ? '切换到手动顺序后拖动' : pending ? '正在保存行顺序'
          : control.matches('.row-up') ? '上移' : control.matches('.row-down') ? '下移' : '拖动调整顺序';
      }
    }
    const count = list.querySelector('.table-count');
    if (count) count.textContent = visible.length === records.length ? `共 ${records.length} 条记录` : `显示 ${visible.length} / ${records.length} 条记录`;
    const emptyNode = list.querySelector('.table-empty');
    if (emptyNode) {
      emptyNode.hidden = visible.length > 0;
      emptyNode.textContent = records.length ? '没有匹配的记录' : '暂无记录';
    }
    statusNodes();
    return visible.length;
  }

  function getLayout() { return clone(layout); }

  function setView(next = {}) {
    cancelDrag();
    closePopover(false);
    view = { ...view, ...next };
    view.query = text(view.query).trim();
    if (view.year === '' || view.year === undefined) view.year = null;
    if (view.entry === '' || view.entry === undefined) view.entry = null;
    return render();
  }

  function refresh() {
    cancelDrag();
    preview = null;
    closePopover(false);
    indexVersion++;
    list = panel.querySelector('.entry-list');
    table = list?.querySelector('table');
    tbody = table?.tBodies[0];
    wrap = list?.querySelector('.table-wrap');
    fields = new Map();
    records = [];
    byID = new Map();
    if (!table || !tbody) return 0;
    for (const th of table.tHead?.querySelectorAll('th[data-field]') || []) {
      fields.set(th.dataset.field, {
        key: th.dataset.field, label: th.dataset.label || th.textContent.trim(),
        type: th.dataset.type || 'text', choices: json(th.dataset.choices, []), th,
      });
    }
    for (const row of tbody.querySelectorAll('tr[data-id]')) {
      const id = Number(row.dataset.id);
      if (!Number.isSafeInteger(id) || byID.has(id)) continue;
      const cells = new Map();
      for (const cell of row.querySelectorAll('td[data-field]')) {
        clearHighlight(cell);
        cells.set(cell.dataset.field, {
          node: cell, value: json(cell.dataset.value, null),
          search: cell.dataset.search ?? cell.textContent, present: cell.dataset.present === 'true',
        });
      }
      const record = { id, node: row, year: row.dataset.eventYear || '', cells };
      records.push(record);
      byID.set(id, record);
    }
    normalizeLayout();
    return render();
  }

  function slots(full, before, after) {
    const selected = new Set(before);
    let index = 0;
    return full.map(id => selected.has(id) ? after[index++] : id);
  }

  async function reorder(before, after, focusID, focusClass) {
    if (pending || panel.dataset.busy === 'true' || panel.querySelector('#entry-dialog')?.open
      || layout.sort || typeof onReorder !== 'function') return;
    const old = records.map(row => row.id), next = slots(old, before, after);
    if (next.every((id, index) => id === old[index])) return;
    const version = indexVersion;
    pending = true;
    reorderMessage = '';
    preview = next;
    render();
    try {
      const result = await onReorder(next.slice());
      if (result === false) throw new Error('行顺序未保存，请重试。');
      // The parent normally refreshes with server HTML while the promise is pending.
      // If it did not replace the DOM, confirm only after the request succeeded.
      if (version === indexVersion) records = next.map(id => byID.get(id));
    } catch (error) {
      reorderMessage = text(error?.message || '行顺序保存失败，已恢复原顺序，请重试。');
      // A parent conflict refresh is authoritative; never restore stale IDs over it.
      if (version === indexVersion) records = old.map(id => byID.get(id));
    } finally {
      preview = null;
      pending = false;
      render();
      if (focusID != null) {
        const row = byID.get(focusID)?.node;
        const preferred = row?.querySelector(focusClass || '.row-drag');
        const control = preferred && !preferred.disabled ? preferred : row?.querySelector('.row-drag:not(:disabled)');
        control?.focus({ preventScroll: true });
      }
    }
  }

  function closePopover(restoreFocus = true) {
    if (!popover) return;
    const trigger = popover.trigger;
    popoverCleanup?.();
    popoverCleanup = null;
    popover.node.remove();
    popover = null;
    trigger?.setAttribute('aria-expanded', 'false');
    if (restoreFocus && trigger?.isConnected) trigger.focus({ preventScroll: true });
  }

  function openPopover(trigger, title) {
    closePopover(false);
    const node = element('div', 'table-popover');
    node.setAttribute('role', 'dialog');
    node.setAttribute('aria-label', title);
    const heading = element('div', 'popover-actions');
    heading.append(element('strong', '', title));
    const close = button('关闭', 'quiet');
    heading.append(close);
    node.append(heading);
    panel.append(node);
    popover = { node, trigger };
    trigger.setAttribute('aria-expanded', 'true');
    const position = () => {
      if (!trigger.isConnected) { closePopover(false); return; }
      const rect = trigger.getBoundingClientRect(), size = node.getBoundingClientRect();
      node.style.left = `${Math.max(12, Math.min(rect.left, window.innerWidth - size.width - 12))}px`;
      node.style.top = `${Math.max(12, Math.min(rect.bottom + 6, window.innerHeight - size.height - 12))}px`;
    };
    const events = new AbortController();
    close.addEventListener('click', () => closePopover(), { signal: events.signal });
    document.addEventListener('pointerdown', event => {
      if (!node.contains(event.target) && !trigger.contains(event.target)) closePopover(false);
    }, { signal: events.signal });
    node.addEventListener('keydown', event => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closePopover(); }
      if (event.key === 'Tab') {
        const controls = [...node.querySelectorAll('button, input, select')].filter(control => !control.disabled && !control.hidden);
        const first = controls[0], last = controls[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    }, { signal: events.signal });
    window.addEventListener('resize', position, { signal: events.signal });
    window.addEventListener('scroll', position, { capture: true, signal: events.signal });
    popoverCleanup = () => events.abort();
    return { node, position };
  }

  function columns(trigger) {
    const { node, position } = openPopover(trigger, '列显示与顺序');
    const options = element('div', 'column-options');
    node.append(options);
    const help = element('p', 'muted', '至少保留一个业务列。可使用上移、下移调整列顺序。');
    node.append(help);
    function draw(focusKey, focusKind) {
      options.replaceChildren();
      for (const [index, key] of layout.column_order.entries()) {
        const option = element('div', 'column-option');
        const label = element('label');
        const checkbox = element('input');
        checkbox.type = 'checkbox';
        checkbox.checked = !layout.hidden_columns.includes(key);
        checkbox.disabled = checkbox.checked && layout.column_order.length - layout.hidden_columns.length === 1;
        checkbox.addEventListener('change', () => {
          if (checkbox.checked) layout.hidden_columns = layout.hidden_columns.filter(value => value !== key);
          else if (layout.column_order.length - layout.hidden_columns.length > 1) layout.hidden_columns.push(key);
          changed();
          draw(key, 'checkbox');
        });
        label.append(checkbox, document.createTextNode(fields.get(key).label));
        option.append(label);
        const moves = element('div');
        for (const [kind, delta, title] of [['up', -1, '上移'], ['down', 1, '下移']]) {
          const move = button(title, 'quiet');
          move.setAttribute('aria-label', `${title}${fields.get(key).label}`);
          move.disabled = index + delta < 0 || index + delta >= layout.column_order.length;
          move.addEventListener('click', () => {
            const order = layout.column_order;
            [order[index], order[index + delta]] = [order[index + delta], order[index]];
            changed();
            draw(key, kind);
          });
          if (focusKey === key && focusKind === kind) queueMicrotask(() => {
            if (!move.isConnected) return;
            const focus = move.disabled ? option.querySelector('button:not(:disabled), input:not(:disabled)') : move;
            focus?.focus();
          });
          moves.append(move);
        }
        if (focusKey === key && focusKind === 'checkbox') queueMicrotask(() => {
          if (!checkbox.isConnected) return;
          const focus = checkbox.disabled ? option.querySelector('button:not(:disabled)') : checkbox;
          (focus || node.querySelector('button'))?.focus();
        });
        option.append(moves);
        options.append(option);
      }
      position();
    }
    draw();
    options.querySelector('input:not(:disabled)')?.focus();
  }

  function filterPopover(trigger, field) {
    const { node, position } = openPopover(trigger, `筛选${field.label}`);
    const form = element('form', 'column-filter-form');
    const controls = element('div', 'fields');
    form.append(controls);
    const current = layout.filters[field.key];
    const inputs = [];
    const addInput = (labelText, input) => {
      const label = element('label', 'field', labelText);
      label.append(input);
      controls.append(label);
      inputs.push(input);
      return input;
    };
    if (rangeTypes.has(field.type)) {
      for (const [key, title] of [['min', '从（包含）'], ['max', '到（包含）']]) {
        const input = element('input');
        input.type = ['date', 'month'].includes(field.type) ? field.type : 'number';
        input.step = field.type === 'number' ? 'any' : '1';
        if (field.type === 'year') { input.min = '1900'; input.max = '2100'; }
        input.value = current?.[key] == null ? '' : String(current[key]);
        addInput(title, input);
      }
    } else if (['select', 'checkbox', 'file'].includes(field.type)) {
      const select = element('select');
      const all = element('option', '', '不限');
      all.value = '';
      select.append(all);
      const choices = field.type === 'checkbox' ? [['true', '是'], ['false', '否']]
        : field.type === 'file' ? [['present', '有附件'], ['missing', '无附件']]
          : field.choices.map(value => [String(value), String(value)]);
      // Index values distinguish an empty-string business choice from “不限”.
      for (const [index, [, label]] of choices.entries()) {
        const option = element('option', '', label);
        option.value = String(index);
        select.append(option);
      }
      if (current) {
        const index = choices.findIndex(([value]) => value === String(current.value));
        if (index >= 0) select.value = String(index);
      }
      select.tableChoices = choices;
      addInput('匹配', select);
    } else {
      const input = element('input');
      input.type = 'text';
      input.value = current?.value || '';
      input.placeholder = '包含文字（不区分大小写）';
      addInput('包含', input);
    }
    if (view.query || view.year != null || view.entry != null) {
      form.append(element('p', 'muted', '当前搜索或定位期间暂不应用栏目筛选，关闭定位后恢复。'));
    }
    const error = element('p', 'error');
    error.hidden = true;
    error.setAttribute('role', 'alert');
    form.append(error);
    const actions = element('div', 'popover-actions');
    const apply = button('应用');
    apply.type = 'submit';
    const clear = button('清除此筛选', 'quiet');
    clear.addEventListener('click', () => { delete layout.filters[field.key]; changed(); closePopover(); });
    actions.append(apply, clear);
    form.append(actions);
    form.addEventListener('submit', event => {
      event.preventDefault();
      let filter = null;
      try {
        if (rangeTypes.has(field.type)) {
          const values = inputs.map(input => {
            if (!input.value) return null;
            if (field.type === 'date' || field.type === 'month') {
              const value = input.value;
              const full = field.type === 'month' ? `${value}-01` : value;
              const date = new Date(`${full}T00:00:00Z`);
              if (!Number.isFinite(date.getTime()) || date.toISOString().slice(0, 10) !== full) throw new Error('日期范围无效');
              return value;
            }
            const value = numeric(input.value);
            if (value == null || (field.type !== 'number' && !Number.isSafeInteger(value))) throw new Error('范围须为有效数字；年份和整数不能含小数');
            if (field.type === 'year' && (value < 1900 || value > 2100)) throw new Error('年份须在 1900–2100 之间');
            return value;
          });
          if (values[0] != null && values[1] != null && values[0] > values[1]) throw new Error('范围下限不能大于上限');
          if (values.some(value => value != null)) filter = { op: 'range', min: values[0], max: values[1] };
        } else if (['select', 'checkbox', 'file'].includes(field.type)) {
          const select = inputs[0];
          if (select.value !== '') {
            const value = select.tableChoices[Number(select.value)][0];
            filter = { op: field.type === 'file' ? 'presence' : 'eq', value: field.type === 'checkbox' ? value === 'true' : value };
          }
        } else if (inputs[0].value.trim()) filter = { op: 'contains', value: inputs[0].value.trim() };
        if (filter) layout.filters[field.key] = filter;
        else delete layout.filters[field.key];
        changed();
        closePopover();
      } catch (failure) { error.textContent = failure.message; error.hidden = false; position(); }
    });
    node.append(form);
    position();
    inputs[0]?.focus();
  }

  function cancelDrag() {
    if (!drag) return;
    const current = drag;
    drag = null;
    current.events.abort();
    cancelAnimationFrame(current.frame);
    try { if (current.handle.hasPointerCapture(current.pointerID)) current.handle.releasePointerCapture(current.pointerID); } catch { /* Detached handle after refresh. */ }
    current.handle.classList.remove('table-dragging');
    current.target?.classList.remove('drop-target');
    preview = null;
  }

  function scrollForDrag(current) {
    if (current.kind === 'column' && wrap) {
      const rect = wrap.getBoundingClientRect();
      if (current.y >= rect.top && current.y <= rect.bottom) {
        if (current.x < rect.left + 40) wrap.scrollLeft -= Math.min(18, (rect.left + 40 - current.x) / 3);
        else if (current.x > rect.right - 40) wrap.scrollLeft += Math.min(18, (current.x - rect.right + 40) / 3);
      }
    } else if (current.kind === 'row') {
      let scroller = wrap;
      while (scroller && (scroller.scrollHeight <= scroller.clientHeight || !/(auto|scroll)/.test(getComputedStyle(scroller).overflowY))) scroller = scroller.parentElement;
      const rect = scroller ? scroller.getBoundingClientRect() : { top: 0, bottom: window.innerHeight };
      let amount = 0;
      if (current.y < rect.top + 40) amount = -Math.min(18, (rect.top + 40 - current.y) / 3);
      else if (current.y > rect.bottom - 40) amount = Math.min(18, (current.y - rect.bottom + 40) / 3);
      if (amount) {
        if (scroller) scroller.scrollTop += amount;
        else window.scrollBy(0, amount);
      }
    }
  }

  function updateDrag(current) {
    if (current.kind === 'column') {
      const candidates = layout.column_order.map(key => fields.get(key).th).filter(th => !th.hidden);
      let target = candidates.find(th => {
        const rect = th.getBoundingClientRect();
        return current.x >= rect.left && current.x <= rect.right;
      });
      if (!target && candidates.length) {
        target = current.x < candidates[0].getBoundingClientRect().left ? candidates[0] : candidates[candidates.length - 1];
      }
      current.target?.classList.remove('drop-target');
      current.target = target;
      current.after = target ? current.x > target.getBoundingClientRect().left + target.getBoundingClientRect().width / 2 : false;
      if (target && target.dataset.field !== current.key) target.classList.add('drop-target');
    } else {
      const hit = document.elementFromPoint(current.x, current.y)?.closest('tr[data-id]');
      if (!hit || hit.closest('table') !== table || hit.hidden) return;
      const targetID = Number(hit.dataset.id);
      if (targetID === current.id || !current.order.includes(targetID)) return;
      const rect = hit.getBoundingClientRect(), after = current.y > rect.top + rect.height / 2;
      const next = current.order.filter(id => id !== current.id);
      next.splice(next.indexOf(targetID) + (after ? 1 : 0), 0, current.id);
      if (next.every((id, index) => id === current.order[index])) return;
      current.order = next;
      preview = slots(records.map(row => row.id), current.before, next);
      render();
    }
  }

  function startDrag(event) {
    if (event.button !== 0 || !event.isPrimary || pending || !table) return;
    const handle = event.target.closest('.column-sort, .row-drag');
    if (!handle || !list.contains(handle) || handle.disabled || event.target.closest('.btn-filter')) return;
    const th = handle.closest('th[data-field]');
    const kind = th ? 'column' : 'row';
    if (kind === 'row' && (layout.sort || list.dataset.single === 'true' || typeof onReorder !== 'function')) return;
    cancelDrag();
    closePopover(false);
    const current = {
      kind, handle, key: th?.dataset.field, id: Number(handle.closest('tr[data-id]')?.dataset.id),
      pointerID: event.pointerId, startX: event.clientX, startY: event.clientY,
      x: event.clientX, y: event.clientY, active: false, events: new AbortController(), frame: 0,
      before: visible.map(row => row.id), order: visible.map(row => row.id), target: null,
    };
    drag = current;
    const frame = () => {
      if (drag !== current || !current.active) return;
      scrollForDrag(current);
      updateDrag(current);
      current.frame = requestAnimationFrame(frame);
    };
    document.addEventListener('pointermove', move => {
      if (move.pointerId !== current.pointerID) return;
      current.x = move.clientX;
      current.y = move.clientY;
      if (!current.active && Math.hypot(current.x - current.startX, current.y - current.startY) > 6) {
        current.active = true;
        handle.classList.add('table-dragging');
        try { handle.setPointerCapture(current.pointerID); } catch { /* Pointer may already have ended. */ }
        current.frame = requestAnimationFrame(frame);
      }
      if (current.active) { move.preventDefault(); updateDrag(current); }
    }, { signal: current.events.signal, passive: false });
    document.addEventListener('pointerup', up => {
      if (up.pointerId !== current.pointerID) return;
      const active = current.active, targetKey = current.target?.dataset.field, after = current.after;
      const before = current.before.slice(), order = current.order.slice();
      cancelDrag();
      if (!active) return;
      up.preventDefault();
      suppressClick = { handle, until: performance.now() + 600 };
      if (kind === 'column') {
        if (targetKey && targetKey !== current.key) {
          const next = layout.column_order.filter(key => key !== current.key);
          next.splice(next.indexOf(targetKey) + (after ? 1 : 0), 0, current.key);
          layout.column_order = next;
          changed();
        } else render();
      } else {
        render();
        void reorder(before, order, current.id);
      }
    }, { signal: current.events.signal });
    document.addEventListener('pointercancel', cancel => {
      if (cancel.pointerId === current.pointerID) { cancelDrag(); render(); }
    }, { signal: current.events.signal });
    document.addEventListener('keydown', key => {
      if (key.key === 'Escape') { key.preventDefault(); key.stopPropagation(); cancelDrag(); render(); }
    }, { signal: current.events.signal });
  }

  // Delegated listeners attach to the persistent panel, never to replaced table DOM.
  panel.addEventListener('pointerdown', startDrag);
  panel.addEventListener('panel-state-change', render);
  panel.addEventListener('click', event => {
    const target = event.target.closest('button');
    if (!target || !list?.contains(target)) return;
    if (suppressClick && suppressClick.handle === target && performance.now() < suppressClick.until) {
      event.preventDefault();
      event.stopPropagation();
      suppressClick = null;
      return;
    }
    if (target.matches('.btn-columns')) { columns(target); return; }
    if (target.matches('.btn-reset-layout')) { closePopover(false); layout = defaultLayout(); changed(); return; }
    if (target.matches('.btn-manual-order')) { layout.sort = null; changed(); return; }
    const field = fields.get(target.closest('th[data-field]')?.dataset.field);
    if (target.matches('.btn-filter') && field) { filterPopover(target, field); return; }
    if (target.matches('.column-sort') && field) {
      closePopover(false);
      const previous = layout.sort;
      layout.sort = previous?.field !== field.key ? { field: field.key, direction: 'asc' }
        : previous.direction === 'asc' ? { field: field.key, direction: 'desc' } : null;
      changed();
      return;
    }
    if (target.matches('.row-up, .row-down') && !target.disabled && !layout.sort && !pending) {
      const id = Number(target.closest('tr[data-id]').dataset.id), before = visible.map(row => row.id);
      const order = before.slice(), index = order.indexOf(id), other = index + (target.matches('.row-up') ? -1 : 1);
      if (index >= 0 && other >= 0 && other < order.length) {
        [order[index], order[other]] = [order[other], order[index]];
        void reorder(before, order, id, target.matches('.row-up') ? '.row-up' : '.row-down');
      }
    }
  });
  refresh();
  return { setView, refresh, getLayout };
}
