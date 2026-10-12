import { createTableController } from "resume-table";
import { createEntryEditor, confirmAction, mutatePanel, hasPendingPanelMutations, waitForPanelMutations } from "resume-editor";

const panels = new Map([...document.querySelectorAll('.tab-panel[data-section]')].map(panel => [panel.dataset.section, panel]));
const layouts = JSON.parse(document.getElementById('initial-layouts').textContent);
const controllers = new Map();
const queues = new Map();
const input = document.getElementById('resume-search-input');
const searchStatus = document.getElementById('resume-search-status');
let state = parseLocation();
let searchTimer;
let composing = false;
let leaving = false;

async function request(url, options = {}) {
  const response = await fetch(url, { credentials:'same-origin', ...options,
    headers:{'X-Requested-With':'fetch', ...options.headers} });
  if (response.status === 401) throw new Error('登录已过期，请在新标签页登录后重试；当前操作已保留');
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const error = new Error(data.error || data.detail || `请求失败 (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return response;
}

function parseLocation() {
  const params = new URL(location.href).searchParams;
  const tab = params.get('tab');
  const rawYear = params.get('year');
  const rawEntry = params.get('entry');
  return { tab:tab === 'overview' || panels.has(tab) ? tab : 'overview',
    query:(params.get('q') || '').trim(),
    year:rawYear === 'undated' || (/^\d{4}$/.test(rawYear || '') && Number(rawYear)>=1900 && Number(rawYear)<=2100) ? rawYear : null,
    entry:panels.has(tab) && /^\d+$/.test(rawEntry || '') && Number.isSafeInteger(Number(rawEntry)) && Number(rawEntry)>0 ? Number(rawEntry) : null };
}
function writeLocation() {
  const url = new URL(location.href);
  for (const key of ['tab','q','year','entry']) url.searchParams.delete(key);
  url.searchParams.set('tab',state.tab);
  if (state.query) url.searchParams.set('q',state.query);
  if (state.year) url.searchParams.set('year',state.year);
  if (state.entry) url.searchParams.set('entry',String(state.entry));
  history.replaceState(null,'',url);
}
function queueStatus(section, message, failed = false) {
  const panel = panels.get(section);
  const node = panel.querySelector('.layout-status');
  node.textContent = message;
  node.classList.toggle('error',failed);
  panel.querySelector('.btn-layout-retry').hidden = !failed;
}
function scheduleLayout(section, layout) {
  const queue = queues.get(section);
  queue.pending = structuredClone(layout);
  queue.failed = false;
  clearTimeout(queue.timer);
  queueStatus(section,'保存中…');
  queue.timer = setTimeout(() => { queue.timer=null; saveQueue(section); },350);
}
async function saveQueue(section) {
  const queue = queues.get(section);
  if (queue.running) return queue.running;
  if (!queue.pending) return !queue.failed;
  queue.running = (async () => {
    while (queue.pending) {
      const snapshot = queue.pending;
      queue.pending = null;
      queueStatus(section,'保存中…');
      try {
        await request(`/api/layouts/${section}`, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(snapshot)});
      } catch (error) {
        queue.pending ||= snapshot;
        queue.failed = true;
        queueStatus(section,'布局未保存',true);
        const panelStatus = panels.get(section).querySelector('.panel-status');
        panelStatus.textContent = error.message;
        panelStatus.classList.add('error');
        return false;
      }
    }
    queue.failed = false;
    queueStatus(section,'已保存');
    return true;
  })();
  try { return await queue.running; }
  finally { queue.running=null; }
}
export async function flushLayouts() {
  for (const queue of queues.values()) { clearTimeout(queue.timer); queue.timer=null; }
  const results = await Promise.all([...queues.keys()].map(saveQueue));
  return results.every(Boolean);
}
function dirtyLayouts() { return [...queues.values()].some(q => q.pending || q.running || q.failed); }

function renderView(updateUrl = true) {
  if (!searchTimer && !composing) input.value = state.query;
  document.getElementById('resume-search-clear').hidden = !state.query;
  let total = 0;
  for (const [key,panel] of panels) {
    const visible = controllers.get(key).setView({query:state.query,year:state.year,entry:state.tab===key ? state.entry : null,showActions:state.tab!=='overview'});
    const selected = state.tab === key;
    panel.hidden = state.tab === 'overview' ? (state.query || state.year ? visible === 0 : key!=='basic' && visible===0) : !selected;
    panel.querySelector('[data-editor-trigger]').hidden = !selected;
    if (!panel.hidden) total += visible;
  }
  for (const tab of document.querySelectorAll('.resume-tab')) {
    const active = tab.dataset.tab === state.tab;
    tab.classList.toggle('active',active);
    tab.setAttribute('aria-selected',String(active));
    tab.tabIndex = active ? 0 : -1;
  }
  document.getElementById('export-resume').hidden = state.tab !== 'overview';
  document.getElementById('resume-empty').hidden = total!==0 || state.tab!=='overview';
  let message = state.query ? '搜索全部内容，栏目筛选暂未应用' : '';
  if (state.entry) {
    const exists = panels.get(state.tab).querySelector(`tbody tr[data-id="${state.entry}"]`);
    if (!exists) message = '记录不存在或已归档';
    else if (!exists.hidden) {
      exists.classList.add('located-row');
    }
  }
  searchStatus.textContent = message;
  const filter = document.getElementById('temporary-filter');
  filter.hidden = !state.year && !state.entry;
  filter.querySelector('span').textContent = [state.year ? state.year==='undated' ? '年份未填' : `${state.year} 年` : '', state.entry ? `定位记录 #${state.entry}` : ''].filter(Boolean).join(' · ');
  if (updateUrl) writeLocation();
}
function replaceTable(section, html) {
  const panel = panels.get(section);
  const old = panel.querySelector('.entry-list');
  const scrollLeft = old.querySelector('.table-wrap')?.scrollLeft || 0;
  const holder = document.createElement('template');
  holder.innerHTML = html.trim();
  const next = holder.content.querySelector('.entry-list');
  if (!next || next.dataset.section !== section) throw new Error('未收到完整栏目表格，请刷新后重试');
  old.replaceWith(next);
  controllers.get(section).refresh();
  renderView();
  panel.querySelector('.table-wrap').scrollLeft = scrollLeft;
  const queue = queues.get(section);
  queueStatus(section,queue.failed ? '布局未保存' : queue.pending || queue.running ? '保存中…' : '已保存',queue.failed);
}
async function reorder(section, ids) {
  if (leaving) throw new Error('正在离开页面，请稍后重试');
  return mutatePanel(panels.get(section), async () => {
    try {
      const response = await request(`/api/entries/${section}/reorder`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids})});
      replaceTable(section,await response.text());
    } catch (error) {
      if (error.status === 400) {
        const response = await request(`/api/entries/${section}`);
        replaceTable(section,await response.text());
        throw new Error('记录集合已变化，栏目已重新加载；布局与搜索已保留，请重新调整顺序');
      }
      throw error;
    }
  });
}
for (const [key,panel] of panels) {
  queues.set(key,{pending:null,timer:null,running:null,failed:false});
  controllers.set(key,createTableController(panel,layouts[key],{
    onLayoutChange:layout => scheduleLayout(key,layout),onReorder:ids => reorder(key,ids)
  }));
}
const editor = createEntryEditor(document.getElementById('entry-dialog'), {
  onSaved:async (section,html) => {
    replaceTable(section,html);
    const status = panels.get(section).querySelector('.panel-status');
    status.textContent='记录已保存'; status.classList.remove('error');
  }
});

async function switchTab(tab) {
  if (leaving) return;
  if (!(await editor.close())) return;
  if (state.tab!==tab) state.entry=null;
  state.tab=tab;
  renderView();
}
document.querySelector('.resume-tabs').addEventListener('click',event => {
  const tab=event.target.closest('.resume-tab');
  if (tab) switchTab(tab.dataset.tab);
});
document.querySelector('.resume-tabs').addEventListener('keydown',event => {
  const tabs=[...document.querySelectorAll('.resume-tab')];
  const index=tabs.indexOf(event.target.closest('.resume-tab'));
  if (index<0) return;
  const next=event.key==='ArrowRight' ? (index+1)%tabs.length : event.key==='ArrowLeft' ? (index+tabs.length-1)%tabs.length : event.key==='Home' ? 0 : event.key==='End' ? tabs.length-1 : null;
  if (next!==null) {event.preventDefault(); switchTab(tabs[next].dataset.tab).then(()=>tabs[next].focus());}
});
function searchChanged() {
  clearTimeout(searchTimer);
  searchTimer = null;
  if (composing) return;
  const query = input.value.trim();
  searchTimer=setTimeout(()=>{ searchTimer=null;state.query=query;state.tab='overview';state.year=null;state.entry=null;renderView(); },150);
}
input.addEventListener('compositionstart',()=>{composing=true;clearTimeout(searchTimer);searchTimer=null;});
input.addEventListener('compositionend',()=>{composing=false;searchChanged();});
input.addEventListener('input',searchChanged);
document.getElementById('resume-search-clear').addEventListener('click',()=>{ clearTimeout(searchTimer);searchTimer=null;state.query='';state.tab='overview';state.year=null;state.entry=null;renderView();input.focus(); });
document.querySelector('#temporary-filter button').addEventListener('click',()=>{state.year=null;state.entry=null;renderView();});
window.addEventListener('popstate',async()=>{ if (!(await editor.close())) {writeLocation();return;}clearTimeout(searchTimer);searchTimer=null;state=parseLocation();renderView(false); });

document.querySelector('.resume-panels').addEventListener('click',async event => {
  const panel=event.target.closest('.tab-panel[data-section]');
  if (!panel) return;
  const section=panel.dataset.section;
  if (leaving || panel.dataset.busy === 'true') return;
  const trigger=event.target.closest('[data-editor-trigger]');
  if (trigger && !trigger.disabled) {
    const row=trigger.closest('tr');
    await editor.open(panel,row ? section==='basic' ? '/api/basic/edit' : `/api/entries/${section}/${row.dataset.id}/edit` : trigger.dataset.url);
    return;
  }
  const retry=event.target.closest('.btn-layout-retry');
  if (retry) {const queue=queues.get(section);queue.failed=false;await saveQueue(section);return;}
  const remove=event.target.closest('.btn-delete');
  if (!remove || remove.disabled) return;
  const row=remove.closest('tr');
  if (!(await confirmAction({title:'删除记录',message:'归档后将从简历和统计中隐藏，附件保留，可在设置中恢复',confirmLabel:'确认归档',danger:true}))) return;
  if (leaving || panel.dataset.busy === 'true') return;
  try {
    await mutatePanel(panel, async () => {
      const response=await request(`/api/entries/${section}/${row.dataset.id}/archive`,{method:'POST'});
      replaceTable(section,await response.text());
      const status=panel.querySelector('.panel-status');status.textContent='记录已归档，可在设置中恢复';status.classList.remove('error');
    });
    panel.querySelector('[data-editor-trigger]')?.focus();
  } catch (error) {const status=panel.querySelector('.panel-status');status.textContent=error.message;status.classList.add('error');}
});
async function mayLeave() {
  if (leaving) return false;
  leaving = true;
  try {
    await waitForPanelMutations();
    if (!(await editor.close())) return false;
    if (await flushLayouts()) return true;
    return await confirmAction({title:'布局未保存',message:'部分栏目布局尚未保存。是否放弃未保存布局并离开？',confirmLabel:'放弃并离开',danger:true});
  } finally {
    leaving = false;
  }
}
document.addEventListener('click',async event=>{
  const link=event.target.closest('a[data-leave]');
  if (!link || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button!==0) return;
  event.preventDefault(); if (await mayLeave()) location.assign(link.href);
});
document.addEventListener('submit',async event=>{
  const form=event.target.closest('form[data-leave]');
  if (!form) return;
  event.preventDefault();if (await mayLeave()) HTMLFormElement.prototype.submit.call(form);
});
window.addEventListener('beforeunload',event=>{if(dirtyLayouts() || hasPendingPanelMutations() || editor.isDirty() || editor.isBusy()){event.preventDefault();event.returnValue='';}});
renderView();

function initPdfExport() {
  const button=document.getElementById('export-resume'),dialog=document.getElementById('export-dialog');
  const title=document.getElementById('export-title'),message=document.getElementById('export-message'),spinner=dialog.querySelector('.export-spinner'),dismiss=document.getElementById('export-dismiss'),status=document.getElementById('export-status');
  dismiss.addEventListener('click',()=>dialog.close());
  button.addEventListener('click',async()=>{
    if(button.disabled)return;
    button.disabled=true;button.setAttribute('aria-busy','true');button.textContent='正在生成…';
    title.textContent='正在生成简历 PDF';message.textContent='正在整理资料与分页，完成后将自动下载。关闭此窗口后仍会继续生成。';dismiss.textContent='后台继续生成';spinner.hidden=false;status.hidden=true;status.classList.remove('error');dialog.showModal();
    try {
      const response=await request(button.dataset.url,{method:'POST'});
      if(!response.headers.get('Content-Type')?.startsWith('application/pdf'))throw new Error('未收到 PDF 文件，请重新登录后重试');
      const blob=await response.blob();
      const filename=response.headers.get('Content-Disposition')?.match(/filename\*=UTF-8''([^;]+)/i);
      const link=document.createElement('a'),url=URL.createObjectURL(blob);
      link.href=url;link.download=filename?decodeURIComponent(filename[1]):'简历.pdf';
      document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);
      if(dialog.open)dialog.close();status.textContent='简历 PDF 已生成并开始下载';
    } catch(error) {title.textContent='导出未完成';message.textContent=error.message;spinner.hidden=true;dismiss.textContent='关闭';status.textContent=error.message;status.classList.add('error');}
    finally {status.hidden=false;button.disabled=false;button.removeAttribute('aria-busy');button.textContent='导出简历';}
  });
}
function initPaperDetails() {
  const dialog=document.getElementById('paper-dialog'),close=document.getElementById('paper-dialog-close'),content=document.getElementById('paper-dialog-content'),status=document.getElementById('paper-dialog-status'),retry=document.getElementById('paper-dialog-retry'),body=dialog.querySelector('.paper-dialog-body');
  let controller=null,opener=null,url='';
  async function load() {
    controller?.abort();const active=new AbortController();controller=active;content.replaceChildren();content.setAttribute('aria-busy','true');status.classList.remove('error');status.textContent='正在加载文章详情…';retry.hidden=true;body.scrollTop=0;
    try {
      const response=await request(url,{cache:'no-store',signal:active.signal});
      if(!response.headers.get('Content-Type')?.startsWith('text/html'))throw new Error('文章详情加载失败，请重试');
      const html=await response.text();if(controller!==active || !dialog.open)return;content.innerHTML=html;status.textContent='';
    } catch(error) {if(controller!==active || !dialog.open || error.name==='AbortError')return;status.textContent=error.message;status.classList.add('error');retry.hidden=false;}
    finally {if(controller===active){controller=null;content.removeAttribute('aria-busy');}}
  }
  panels.get('papers').addEventListener('click',event=>{const button=event.target.closest('.paper-title');if(!button)return;opener=button;url=button.dataset.url;if(!dialog.open)dialog.showModal();close.focus({preventScroll:true});load();});
  close.addEventListener('click',()=>dialog.close());retry.addEventListener('click',load);
  dialog.addEventListener('close',()=>{controller?.abort();controller=null;content.replaceChildren();content.removeAttribute('aria-busy');if(opener?.isConnected)opener.focus({preventScroll:true});opener=null;});
}
initPdfExport();
initPaperDetails();
