const SECTION_COLORS = [
  '#0284c7', '#0891b2', '#0d9488', '#059669', '#4d7c0f', '#a16207',
  '#d97706', '#ea580c', '#dc2626', '#be185d', '#9333ea', '#7c3aed',
  '#4f46e5', '#2563eb', '#475569', '#0f766e', '#6d28d9',
];
const YEAR_COLORS = ['#93c5fd', '#22b8cf', '#0369a1'];
const ACTION_LABELS = {create:'新建', update:'修改', archive:'归档', restore:'恢复', reorder:'调整顺序'};
const KIND_LABELS = {self:'本人', section:'栏目', record:'记录', year:'业务年份', author:'作者', organization:'单位'};
const EDGE_LABELS = {section:'本人与栏目', record:'栏目与记录', year:'业务年份', author:'共同作者', organization:'共同单位'};
const $ = id => document.getElementById(id);
const content = $('dashboard-content');
const errorBox = $('dashboard-error');
const retry = $('dashboard-retry');
const dialog = $('graph-dialog');
const motion = matchMedia('(prefers-reduced-motion: reduce)');
const charts = new Map();
const graphMotions = new Map();
let rotationPaused = false;
let payload = null;
let sections = new Map();
let nodeById = new Map();
let controller = null;
let loading = false;
let disposed = false;
let recentTimer = null;
let edgesByNode = new Map();
let resizeFrame = null;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}
function resumeHref(tab = 'overview', year = null, entry = null) {
  const params = new URLSearchParams({tab});
  if (year !== null) params.set('year', String(year));
  if (entry !== null) params.set('entry', String(entry));
  return `/resume?${params}`;
}
function nodeHref(node) {
  if (node.kind === 'record') return resumeHref(node.section, null, node.entry_id);
  if (node.kind === 'section') return resumeHref(node.section);
  if (node.kind === 'year') return resumeHref('overview', node.year);
  if (node.kind === 'self') return resumeHref('basic');
  return null;
}
function link(text, href, className) {
  const node = element('a', text, className);
  node.href = href;
  return node;
}
function percentage(count) {
  return payload.current_total ? (100 * count / payload.current_total).toFixed(1) : '0.0';
}
// ECharts rich-text tooltips never interpret HTML. Neutralize rich-text delimiters
// too, so a user's title remains plain text rather than a formatting token.
function tooltipText(value) {
  return String(value ?? '').replaceAll('{', '｛').replaceAll('}', '｝');
}
function wrapText(value, width = 36) {
  return tooltipText(value).split('\n').map(line => {
    const chars = Array.from(line);
    const rows = [];
    for (let i = 0; i < chars.length; i += width) rows.push(chars.slice(i, i + width).join(''));
    return rows.join('\n');
  }).join('\n');
}
function tooltipOptions(formatter, trigger = 'item') {
  return {
    trigger, renderMode:'richText', confine:true, formatter,
    backgroundColor:'#ffffff', borderColor:'#e5e7eb', borderWidth:1,
    textStyle:{color:'#111827', fontSize:13, lineHeight:20},
    ...(trigger === 'axis' ? {axisPointer:{type:'shadow'}} : {}),
  };
}
function sectionLabel(key) { return key === 'basic' ? '个人基本信息' : sections.get(key)?.label || key || '—'; }
function sectionColor(key) { return sections.get(key)?.color || '#64748b'; }
function commonOptions() {
  return {animation:!motion.matches, animationDuration:220, animationDurationUpdate:180,
    textStyle:{fontFamily:'system-ui, -apple-system, sans-serif', color:'#64748b'}};
}

const resizeObserver = new ResizeObserver(() => {
  if (resizeFrame !== null) return;
  resizeFrame = requestAnimationFrame(() => {
    resizeFrame = null;
    for (const [id, chart] of charts) {
      const container = $(id);
      if (container.clientWidth && container.clientHeight) chart.resize();
      graphMotions.get(id)?.refresh();
    }
  });
});
function chartFor(id) {
  let chart = charts.get(id);
  if (chart) return chart;
  chart = window.echarts.init($(id), null, {renderer:'canvas'});
  charts.set(id, chart);
  resizeObserver.observe($(id));
  if (id === 'constellation' || id === 'constellation-expanded') {
    chart.on('click', params => {
      if (params.dataType !== 'node') return;
      const href = nodeHref(params.data);
      if (href) location.assign(href);
    });
  } else if (id === 'annual-share') {
    chart.on('click', params => {
      if (params.componentType === 'series') location.assign(resumeHref(params.data.section, payload.year));
    });
  } else if (id === 'annual-counts') {
    chart.on('click', params => {
      if (params.componentType !== 'series') return;
      location.assign(resumeHref(payload.sections[params.dataIndex].key, payload.years[params.seriesIndex]));
    });
  }
  return chart;
}
function disposeChart(id) {
  const chart = charts.get(id);
  if (!chart) return;
  resizeObserver.unobserve($(id));
  graphMotions.get(id)?.destroy();
  graphMotions.delete(id);
  chart.dispose();
  charts.delete(id);
}
function disposeCharts() {
  for (const id of [...charts.keys()]) disposeChart(id);
}

function graphTooltip(params) {
  const data = params.dataType === 'edge' ? params.data : nodeById.get(params.data.id) || params.data;
  if (params.dataType === 'edge') {
    const source = nodeById.get(data.source);
    const target = nodeById.get(data.target);
    return [EDGE_LABELS[data.kind] || data.kind,
      `${wrapText(source?.label)} → ${wrapText(target?.label)}`,
      `依据：${wrapText(data.evidence)}`,
      ['author','organization'].includes(data.kind) ? '按填写文本匹配，不代表已核验同名身份' : '',
    ].filter(Boolean).join('\n');
  }
  const lines = [wrapText(data.label), `类型：${KIND_LABELS[data.kind]}`];
  if (data.section) lines.push(`栏目：${sectionLabel(data.section)}`);
  if (data.kind === 'record') {
    lines.push(`业务年份：${data.year ?? '年份未填'}`);
    const yearEdge = edgesByNode.get(data.id)?.find(edge => edge.kind === 'year');
    lines.push(`依据：${yearEdge ? yearEdge.evidence : '业务日期未填写或无有效年份'}`);
    lines.push('点击定位此记录');
  } else {
    lines.push(`关联记录：${data.value} 条`);
    if (data.kind === 'author' || data.kind === 'organization') {
      lines.push('按填写文本匹配，不代表已核验同名身份');
      const evidence = [...new Set((edgesByNode.get(data.id) || []).map(edge => edge.evidence))];
      lines.push(`依据：${evidence.join('、')}`);
    }
  }
  return lines.map(line => wrapText(line)).join('\n');
}
function graphOption() {
  const includeRelations = $('graph-relations').checked;
  const visibleNodes = payload.graph.nodes.filter(node => includeRelations || !['author','organization'].includes(node.kind));
  const visibleIds = new Set(visibleNodes.map(node => node.id));
  const groups = new Map();
  for (const node of visibleNodes) {
    if (!groups.has(node.kind)) groups.set(node.kind, []);
    groups.get(node.kind).push(node);
  }
  const positionById = new Map();
  const radii = {self:0, section:135, record:230, year:320, author:385, organization:445};
  for (const [kind, nodes] of groups) {
    nodes.forEach((node, index) => {
      const angle = index / nodes.length * Math.PI * 2 - Math.PI / 2;
      positionById.set(node.id, {x:Math.cos(angle) * radii[kind], y:Math.sin(angle) * radii[kind]});
    });
  }
  const symbols = {self:28, section:21, record:10, year:18, author:15, organization:15};
  const colors = {self:'#111827', year:'#94a3b8', author:'#a855f7', organization:'#14b8a6'};
  return {
    ...commonOptions(), animation:false,
    tooltip:tooltipOptions(graphTooltip),
    series:[{
      type:'graph', layout:'force', roam:true, draggable:true,
      zoom:1, scaleLimit:{min:0.25, max:5},
      // Deterministic, distinct seed coordinates avoid random initial positions.
      // Compute the force layout before painting, including in reduced-motion mode.
      force:{repulsion:1500, edgeLength:[180,300], gravity:0.08, friction:0.6, layoutAnimation:false},
      data:visibleNodes.map(node => ({...node, name:node.label, ...positionById.get(node.id),
        symbol:node.kind === 'organization' ? 'roundRect' : 'circle', symbolSize:symbols[node.kind],
        itemStyle:{color:node.section ? sectionColor(node.section) : colors[node.kind], borderColor:'#ffffff', borderWidth:1},
        label:{show:['self','section'].includes(node.kind), formatter:() => node.kind === 'year' ? String(node.year) : tooltipText(node.label),
          fontSize:11, color:'#475569', position:'right', width:110, overflow:'truncate'},
      })),
      links:payload.graph.edges.filter(edge => visibleIds.has(edge.source) && visibleIds.has(edge.target)).map(edge => ({...edge,
        lineStyle:{color:edge.kind === 'year' ? '#cbd5e1' : edge.kind === 'author' ? '#c084fc' : edge.kind === 'organization' ? '#5eead4' :
          sectionColor(nodeById.get(edge.source)?.section || nodeById.get(edge.target)?.section),
          type:edge.kind === 'year' ? 'dashed' : ['author','organization'].includes(edge.kind) ? 'dotted' : 'solid',
          opacity:edge.kind === 'year' ? 0.35 : 0.55, width:1},
      })),
      emphasis:{focus:'adjacency', label:{show:true}, lineStyle:{opacity:1, width:2}},
      blur:{itemStyle:{opacity:0.15}, lineStyle:{opacity:0.08}, label:{opacity:0.15}},
      labelLayout:{hideOverlap:true},
    }],
  };
}

function createGraphMotion(chart) {
  const container = chart.getDom();
  const initial = chart.getModel().getSeriesByIndex(0);
  const initialData = initial.getData();
  const positioned = initial.option.data.map((node, index) => {
    const point = initialData.getItemLayout(index);
    return {...node, x:point[0], y:point[1]};
  });
  const pivot = positioned.find(node => node.kind === 'self') || positioned[0];
  // Solve force layout once. Rotating reusable layouts keeps labels upright and
  // lets ECharts retain native node hit-testing, dragging and wheel/pan gestures.
  chart.setOption({series:[{layout:'none', data:positioned, center:[pivot.x, pivot.y]}]});
  const coordinate = chart.getModel().getSeriesByIndex(0).coordinateSystem;
  const center = coordinate.dataToPoint([pivot.x, pivot.y]);
  const radius = Math.max(...positioned.map(node => {
    const point = coordinate.dataToPoint([node.x, node.y]);
    return Math.hypot(point[0] - center[0], point[1] - center[1]);
  }));
  chart.setOption({series:[{zoom:Math.min(1, Math.max(0.25,
    (Math.min(chart.getWidth(), chart.getHeight()) - 56) / (2 * radius || 1)))}]});

  const zr = chart.getZr();
  const listeners = new AbortController();
  let model, data, graph, view, nodes, incident;
  let frame = null;
  let previousTime = null;
  let pointerInside = false;
  let dragging = false;
  let inView = false;
  let destroyed = false;
  let flows = [];
  let flowStart = 0;
  function refresh() {
    model = chart.getModel().getSeriesByIndex(0);
    data = model.getData();
    graph = model.getGraph();
    view = chart.getViewOfSeriesModel(model);
    nodes = model.option.data.map((raw, index) => ({raw, index}));
    incident = new Map(nodes.map(({raw}) => [raw.id, []]));
    graph.edges.forEach((edge, index) => {
      incident.get(edge.node1.id)?.push(index);
      incident.get(edge.node2.id)?.push(index);
    });
    schedule();
  }
  function enabled() {
    return !destroyed && !motion.matches && !document.hidden && inView &&
      !(container.id === 'constellation' && dialog.open);
  }
  function spinning() {
    return enabled() && !rotationPaused && !pointerInside && !dragging;
  }
  function clearFlows() {
    for (const flow of flows) zr.remove(flow.dot);
    flows = [];
  }
  function updateEdges() {
    for (const edge of graph.edges) {
      // ECharts caches each layout's original endpoints before clipping them.
      // Supply a fresh layout, not its cached/clipped points from the last frame.
      edge.setLayout([edge.node1.getLayout().slice(), edge.node2.getLayout().slice()]);
    }
    view.updateLayout(model);
  }
  function schedule() {
    if (!enabled() || (!spinning() && !flows.length && !dragging)) {
      if (frame !== null) cancelAnimationFrame(frame);
      frame = null;
      previousTime = null;
    } else if (frame === null) {
      frame = requestAnimationFrame(animate);
    }
  }
  function animate(time) {
    frame = null;
    if (!enabled()) { previousTime = null; return; }
    const elapsed = previousTime === null ? 0 : Math.min(time - previousTime, 100);
    previousTime = time;
    if (spinning() && elapsed) {
      const angle = elapsed * Math.PI / 60000;
      const cosine = Math.cos(angle), sine = Math.sin(angle);
      const pivotNode = graph.getNodeById(pivot.id);
      const origin = pivotNode.getLayout();
      for (const node of nodes) {
        const point = data.getItemLayout(node.index);
        const x = point[0] - origin[0], y = point[1] - origin[1];
        point[0] = origin[0] + x * cosine - y * sine;
        point[1] = origin[1] + x * sine + y * cosine;
        node.raw.x = point[0];
        node.raw.y = point[1];
      }
    }
    if ((spinning() && elapsed) || dragging) {
      updateEdges();
    }
    for (const flow of flows) {
      const line = graph.edges[flow.index].getLayout();
      const start = model.coordinateSystem.dataToPoint(line[flow.reverse ? 1 : 0]);
      const end = model.coordinateSystem.dataToPoint(line[flow.reverse ? 0 : 1]);
      const progress = ((time - flowStart) / 1800 + flow.offset) % 1;
      flow.dot.shape.cx = start[0] + (end[0] - start[0]) * progress;
      flow.dot.shape.cy = start[1] + (end[1] - start[1]) * progress;
      flow.dot.style.opacity = 0.35 + 0.65 * Math.sin(progress * Math.PI);
      flow.dot.dirtyShape();
      flow.dot.dirtyStyle();
    }
    zr.refresh();
    schedule();
  }
  function hover(params) {
    clearFlows();
    if (!enabled() || params.componentType !== 'series') { schedule(); return; }
    const indexes = params.dataType === 'node' ? incident.get(params.data.id) || [] :
      params.dataType === 'edge' ? [params.dataIndex] : [];
    flowStart = performance.now();
    flows = indexes.map((index, order) => {
      const edge = graph.edges[index];
      const colorNode = nodeById.get(edge.node2.id)?.section ? edge.node2 : edge.node1;
      const color = sectionColor(nodeById.get(colorNode.id)?.section);
      const dot = new window.echarts.graphic.Circle({
        name:'constellation-flow-dot', silent:true, z:10,
        shape:{cx:0, cy:0, r:3},
        style:{fill:color, shadowColor:color, shadowBlur:8, opacity:0},
      });
      zr.add(dot);
      return {index, dot, reverse:params.dataType === 'node' && edge.node2.id === params.data.id,
        offset:order / Math.max(indexes.length, 1)};
    });
    schedule();
  }
  function leave() { clearFlows(); schedule(); }
  chart.on('mouseover', hover);
  chart.on('mouseout', leave);
  chart.on('graphroam', refresh);
  zr.on('globalout', leave);
  const options = {signal:listeners.signal};
  container.addEventListener('pointerenter', () => { pointerInside = true; schedule(); }, options);
  container.addEventListener('pointerleave', () => { pointerInside = false; leave(); }, options);
  container.addEventListener('pointerdown', () => { dragging = true; schedule(); }, options);
  function finishDrag() {
    if (!dragging) return;
    dragging = false;
    for (const node of nodes) {
      const point = data.getItemLayout(node.index);
      node.raw.x = point[0]; node.raw.y = point[1];
    }
    updateEdges();
    schedule();
  }
  window.addEventListener('pointerup', finishDrag, options);
  window.addEventListener('pointercancel', finishDrag, options);
  document.addEventListener('visibilitychange', schedule, options);
  const observer = new IntersectionObserver(entries => {
    inView = entries[0].intersectionRatio > 0.1;
    schedule();
  }, {threshold:[0, 0.1]});
  observer.observe(container);
  refresh();
  return {refresh, schedule, destroy() {
    destroyed = true;
    schedule();
    clearFlows();
    listeners.abort();
    observer.disconnect();
    chart.off('mouseover', hover);
    chart.off('mouseout', leave);
    chart.off('graphroam', refresh);
    zr.off('globalout', leave);
  }};
}
function drawGraph(id) {
  graphMotions.get(id)?.destroy();
  const chart = chartFor(id);
  chart.setOption(graphOption(), {notMerge:true});
  graphMotions.set(id, createGraphMotion(chart));
}
function syncRotationControls() {
  for (const id of ['graph-rotation', 'graph-rotation-expanded']) {
    const button = $(id);
    button.textContent = motion.matches ? '动态效果已关闭' : rotationPaused ? '继续旋转' : '暂停旋转';
    button.setAttribute('aria-pressed', String(rotationPaused));
    button.disabled = motion.matches || !payload?.graph.nodes.some(node => node.kind === 'record');
  }
  for (const animation of graphMotions.values()) animation.schedule();
}
function renderGraph() {
  const empty = !payload.graph.nodes.some(node => node.kind === 'record');
  $('constellation').setAttribute('aria-label', empty ? '添加记录后生成星座图' :
    `履历星座图，${payload.graph.nodes.filter(node => node.kind === 'record').length} 条真实记录；可在下方数据与键盘定位入口查看所有记录与关系依据`);
  $('graph-empty').hidden = !empty;
  $('constellation').hidden = empty;
  $('graph-expand').disabled = empty;
  $('graph-reset').disabled = empty;
  $('graph-relations').disabled = empty;
  syncRotationControls();
  if (empty) {
    disposeChart('constellation');
    if (dialog.open) dialog.close();
  } else {
    drawGraph('constellation');
    if (dialog.open) drawGraph('constellation-expanded');
  }
}
function renderLegend() {
  const legend = $('graph-legend');
  legend.replaceChildren();
  for (const section of payload.sections) {
    if (!payload.graph.nodes.some(node => node.kind === 'section' && node.section === section.key)) continue;
    const entry = link(section.label, resumeHref(section.key));
    const swatch = element('span', '', 'graph-legend-swatch');
    swatch.style.setProperty('--legend-color', sectionColor(section.key));
    swatch.setAttribute('aria-hidden', 'true');
    entry.prepend(swatch);
    legend.append(entry);
  }
}
function addGraphControls() {
  for (const id of ['graph-rotation', 'graph-rotation-expanded']) {
    const button = element('button', '暂停旋转');
    button.type = 'button';
    button.id = id;
    button.setAttribute('aria-pressed', 'false');
    button.addEventListener('click', () => {
      rotationPaused = !rotationPaused;
      syncRotationControls();
    });
    (id === 'graph-rotation' ? $('graph-reset') : $('graph-dialog-close')).before(button);
  }
  const reset = element('button', '重置视图');
  reset.type = 'button';
  reset.addEventListener('click', renderGraph);
  $('graph-dialog-close').before(reset);
  const label = element('label', undefined, 'inline-check');
  const check = element('input');
  check.type = 'checkbox';
  check.id = 'graph-relations-expanded';
  check.addEventListener('change', () => {
    $('graph-relations').checked = check.checked;
    renderGraph();
  });
  label.append(check, document.createTextNode('作者 / 单位关联'));
  $('constellation-expanded').before(label);
}

function renderAnnual() {
  $('current-year-title').textContent = `${payload.year}年记录`;
  $('current-total').textContent = payload.current_total;
  const counts = $('current-counts');
  counts.replaceChildren();
  for (const section of payload.sections) {
    if (section.current_count) {
      counts.append(link(`${section.label} ${section.current_count} 条 · ${percentage(section.current_count)}%`, resumeHref(section.key, payload.year)));
    }
  }
  const empty = payload.current_total === 0;
  $('annual-empty').hidden = !empty;
  $('annual-share').hidden = empty;
  if (empty) disposeChart('annual-share');
  else chartFor('annual-share').setOption({
    ...commonOptions(),
    grid:{top:12, bottom:12, left:0, right:0},
    xAxis:{type:'value', show:false, max:payload.current_total},
    yAxis:{type:'category', show:false, data:['记录']},
    tooltip:tooltipOptions(params => {
      const section = sections.get(params.data.section);
      return `${section.label}\n${section.current_count} 条 · ${percentage(section.current_count)}%\n${payload.year}年合计：${payload.current_total} 条`;
    }),
    series:payload.sections.map(section => ({
      name:section.label, type:'bar', stack:'current-share', barWidth:38,
      itemStyle:{color:sectionColor(section.key)},
      data:[{value:section.current_count, section:section.key}],
      label:{show:section.current_count / payload.current_total >= 0.12, position:'inside', color:'#ffffff', fontSize:12,
        formatter:() => `${section.current_count} · ${percentage(section.current_count)}%`},
      emphasis:{itemStyle:{borderColor:'#111827', borderWidth:1}},
    })),
  }, {notMerge:true});
  const undated = $('undated-summary');
  undated.replaceChildren(document.createTextNode(`年份未填：${payload.undated_total} 条，不计入年度统计。`),
    link('查看并补录业务日期', resumeHref('overview', 'undated')));

  chartFor('annual-counts').setOption({
    ...commonOptions(),
    legend:{data:payload.years.map(String), selectedMode:false, top:0},
    grid:{top:48, right:24, bottom:70, left:52},
    xAxis:{type:'category', data:payload.sections.map(section => section.label), axisLabel:{interval:0, fontSize:11},
      axisLine:{lineStyle:{color:'#e5e7eb'}}, axisTick:{show:false}},
    yAxis:{type:'value', name:'数量', min:0, minInterval:1, axisLabel:{formatter:value => String(value)},
      splitLine:{lineStyle:{color:'#eef2f6'}}},
    tooltip:tooltipOptions(params => {
      if (!params.length) return '';
      const section = payload.sections[params[0].dataIndex];
      return [section.label, ...payload.years.map((year, index) => `${year}年：${section.counts[index]} 条`),
        `合计：${section.counts.reduce((sum, count) => sum + count, 0)} 条`].join('\n');
    }, 'axis'),
    series:payload.years.map((year, index) => ({
      name:String(year), type:'bar', stack:'annual-count', barMaxWidth:42,
      itemStyle:{color:YEAR_COLORS[index]}, data:payload.sections.map(section => section.counts[index]),
      emphasis:{focus:'series'},
    })),
  }, {notMerge:true});
  $('annual-share').setAttribute('aria-label', `${payload.year}年共 ${payload.current_total} 条，栏目数量与占比可在下方数据表查看`);
  $('annual-counts').setAttribute('aria-label', `${payload.years.join('、')}年全部 ${payload.sections.length} 个栏目数量；下方数据表提供键盘定位`);
}

function eventDate(value) {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date : null;
}
function localTime(date) {
  const fixed = new Date(date.getTime() + 8 * 60 * 60 * 1000);
  const pad = value => String(value).padStart(2, '0');
  return `${fixed.getUTCFullYear()}-${pad(fixed.getUTCMonth() + 1)}-${pad(fixed.getUTCDate())} ${pad(fixed.getUTCHours())}:${pad(fixed.getUTCMinutes())}:${pad(fixed.getUTCSeconds())} (UTC+08:00)`;
}
function relativeTime(date) {
  const seconds = Math.floor((Date.now() - date.getTime()) / 1000);
  if (seconds < 0) return localTime(date);
  if (seconds < 60) return '刚刚';
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  if (seconds < 7 * 86400) return `${Math.floor(seconds / 86400)} 天前`;
  return localTime(date).split(' ')[0];
}
function updateRecentTimes() {
  for (const time of $('recent-events').querySelectorAll('time')) {
    const date = eventDate(time.dateTime);
    if (date) time.textContent = relativeTime(date);
  }
}
function renderRecent() {
  const list = $('recent-events');
  list.replaceChildren();
  $('recent-empty').hidden = payload.recent.length !== 0;
  for (const event of payload.recent) {
    const item = element('li');
    const heading = element('div');
    heading.append(element('span', ACTION_LABELS[event.action], 'recent-action'),
      element('span', sectionLabel(event.section_key), 'recent-section'));
    const title = link(event.title, event.href, 'recent-title');
    const date = eventDate(event.occurred_at);
    const time = element('time', date ? relativeTime(date) : '时间未填写', 'recent-time');
    if (date) { time.dateTime = event.occurred_at; time.title = localTime(date); }
    item.append(heading, title, time);
    list.append(item);
  }
  clearInterval(recentTimer);
  recentTimer = setInterval(updateRecentTimes, 60000);
}

function headerCell(text) {
  const cell = element('th', text);
  cell.scope = 'col';
  return cell;
}
function renderDataTable() {
  const table = element('table', undefined, 'dashboard-table');
  table.append(element('caption', '按业务发生年份计数；基本信息和归档记录不计入'));
  const head = element('thead');
  const heading = element('tr');
  heading.append(headerCell('栏目'), ...payload.years.map(year => headerCell(`${year}年`)), headerCell(`${payload.year}年占比`), headerCell('年份未填'));
  head.append(heading);
  const body = element('tbody');
  for (const section of payload.sections) {
    const row = element('tr');
    const name = element('th');
    name.scope = 'row';
    name.append(link(section.label, resumeHref(section.key)));
    row.append(name);
    payload.years.forEach((year, index) => {
      const cell = element('td');
      const anchor = link(section.counts[index], resumeHref(section.key, year));
      anchor.setAttribute('aria-label', `${section.label} ${year}年 ${section.counts[index]} 条，查看记录`);
      cell.append(anchor);
      row.append(cell);
    });
    row.append(element('td', `${percentage(section.current_count)}%`));
    const undated = element('td');
    const anchor = link(section.undated_count, resumeHref(section.key, 'undated'));
    anchor.setAttribute('aria-label', `${section.label} 年份未填 ${section.undated_count} 条，查看记录`);
    undated.append(anchor);
    row.append(undated);
    body.append(row);
  }
  table.append(head, body);
  $('dashboard-data-table').replaceChildren(table);

  const recordLinks = $('graph-record-links');
  recordLinks.replaceChildren(element('h3', '星座图记录定位'));
  const yearLinks = element('p');
  for (const node of payload.graph.nodes.filter(node => node.kind === 'year')) {
    yearLinks.append(link(`${node.year}年 · ${node.value} 条`, nodeHref(node)), document.createTextNode(' '));
  }
  yearLinks.append(link(`年份未填 · ${payload.undated_total} 条`, resumeHref('overview', 'undated')));
  recordLinks.append(yearLinks);
  for (const section of payload.sections) {
    const records = payload.graph.nodes.filter(node => node.kind === 'record' && node.section === section.key);
    if (!records.length) continue;
    const details = element('details');
    details.append(element('summary', `${section.label} · ${records.length} 条`));
    const list = element('ul');
    for (const record of records) {
      const item = element('li');
      item.append(link(record.label, nodeHref(record)), document.createTextNode(` · ${record.year ?? '年份未填'}`));
      list.append(item);
    }
    details.append(list);
    recordLinks.append(details);
  }
  const relationNodes = payload.graph.nodes.filter(node => ['author','organization'].includes(node.kind));
  if (!relationNodes.length) return;
  const details = element('details');
  details.append(element('summary', '作者 / 单位关联依据'),
    element('p', '按填写文本匹配，不代表已核验同名身份；仅列至少两条不同记录共同填写的作者或单位。', 'chart-note'));
  const wrap = element('div', undefined, 'table-wrap');
  const relations = element('table', undefined, 'dashboard-relation-table');
  const relationsHead = element('thead');
  const relationsHeading = element('tr');
  relationsHeading.append(headerCell('关联类型'), headerCell('填写文本'), headerCell('实际记录与来源字段'));
  relationsHead.append(relationsHeading);
  const relationsBody = element('tbody');
  const edgesByRelation = new Map(relationNodes.map(node => [node.id, []]));
  for (const edge of payload.graph.edges) {
    const relationId = edgesByRelation.has(edge.source) ? edge.source : edge.target;
    edgesByRelation.get(relationId)?.push(edge);
  }
  for (const node of relationNodes) {
    const row = element('tr');
    row.append(element('td', KIND_LABELS[node.kind]), element('td', node.label));
    const cell = element('td');
    const list = element('ul');
    for (const edge of edgesByRelation.get(node.id)) {
      const record = nodeById.get(edge.source === node.id ? edge.target : edge.source);
      const item = element('li');
      item.append(link(record.label, nodeHref(record)), document.createTextNode(` · 依据：${edge.evidence}`));
      list.append(item);
    }
    cell.append(list);
    row.append(cell);
    relationsBody.append(row);
  }
  relations.append(relationsHead, relationsBody);
  wrap.append(relations);
  details.append(wrap);
  recordLinks.append(details);
}

async function vendorReady() {
  if (window.echarts) return;
  if (document.readyState !== 'complete') await new Promise(resolve => window.addEventListener('load', resolve, {once:true}));
  if (!window.echarts) throw new Error('图表组件加载失败');
}
function setLoading() {
  content.hidden = false;
  content.setAttribute('aria-busy', 'true');
  errorBox.hidden = true;
  retry.disabled = true;
  for (const id of ['constellation','annual-share','annual-counts']) $(id).classList.add('skeleton');
  $('recent-events').replaceChildren(element('li', '正在加载修改记录…', 'muted'));
  $('recent-empty').hidden = true;
}
async function loadDashboard() {
  if (loading) return;
  disposed = false;
  loading = true;
  controller = new AbortController();
  setLoading();
  try {
    const response = await fetch('/api/dashboard', {credentials:'same-origin', cache:'no-store',
      headers:{'Accept':'application/json', 'X-Requested-With':'fetch'}, signal:controller.signal});
    if (!response.ok) throw new Error('首页数据加载失败');
    const data = await response.json();
    await vendorReady();
    if (disposed) return;
    payload = data;
    sections = new Map(payload.sections.map((section, index) => [section.key, {...section, color:SECTION_COLORS[index]}]));
    nodeById = new Map(payload.graph.nodes.map(node => [node.id, node]));
    edgesByNode = new Map(payload.graph.nodes.map(node => [node.id, []]));
    for (const edge of payload.graph.edges) {
      edgesByNode.get(edge.source)?.push(edge);
      edgesByNode.get(edge.target)?.push(edge);
    }
    for (const id of ['constellation','annual-share','annual-counts']) $(id).classList.remove('skeleton');
    renderGraph();
    renderLegend();
    renderRecent();
    renderAnnual();
    renderDataTable();
  } catch (error) {
    if (disposed || error.name === 'AbortError') return;
    disposeCharts();
    clearInterval(recentTimer);
    content.hidden = true;
    errorBox.hidden = false;
  } finally {
    content.setAttribute('aria-busy', 'false');
    retry.disabled = false;
    loading = false;
    controller = null;
  }
}

addGraphControls();
retry.addEventListener('click', loadDashboard);
$('graph-reset').addEventListener('click', () => { if (payload) renderGraph(); });
$('graph-relations').addEventListener('change', () => {
  $('graph-relations-expanded').checked = $('graph-relations').checked;
  if (payload) renderGraph();
});
$('graph-expand').addEventListener('click', () => {
  if (!payload || $('graph-expand').disabled) return;
  dialog.showModal();
  drawGraph('constellation-expanded');
  syncRotationControls();
  $('graph-dialog-close').focus();
});
$('graph-dialog-close').addEventListener('click', () => dialog.close());
dialog.addEventListener('close', () => {
  disposeChart('constellation-expanded');
  syncRotationControls();
  $('graph-expand').focus();
});
motion.addEventListener('change', () => {
  if (!payload || disposed) return;
  renderGraph();
  renderAnnual();
});
window.addEventListener('pagehide', () => {
  disposed = true;
  controller?.abort();
  clearInterval(recentTimer);
  if (resizeFrame !== null) cancelAnimationFrame(resizeFrame);
  resizeFrame = null;
  disposeCharts();
  resizeObserver.disconnect();
});
window.addEventListener('pageshow', event => {
  if (event.persisted) {
    if (dialog.open) dialog.close();
    loadDashboard();
  }
});
loadDashboard();
