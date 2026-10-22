const state = {
  conversations: [],
  messageOffset: 0,
  messageTotal: 0,
  messagePageSize: 100,
  jobPollTimer: null,
};

const $ = (selector) => document.querySelector(selector);
const fmtTime = (value) => value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString() : '—';
// Date inputs use local calendar days; toISOString() would give the UTC day.
const localDate = (date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
const daysAgo = (days) => { const date = new Date(); date.setDate(date.getDate() - days); return date; };
const api = async (path, options = {}) => {
  const response = await fetch(path, {headers: {'Content-Type': 'application/json'}, ...options});
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(typeof data.detail === 'string' ? data.detail : '请求失败');
    error.status = response.status;
    throw error;
  }
  return data;
};

function toast(text, error = false) {
  const element = $('#toast');
  element.textContent = text;
  element.style.color = error ? '#ad3f35' : '#1f6f4a';
  setTimeout(() => { if (element.textContent === text) element.textContent = ''; }, 4500);
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'\"]/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '\"': '&quot;'
  })[character]);
}

const titles = {
  status: ['运行状态', '查看环境状态、同步进度和最近结果'],
  conversations: ['会话选择', '只有明确选择的会话才会被归档'],
  messages: ['消息浏览', '无需关键字，可按时间顺序快速浏览整个会话'],
  summary: ['话题总结', '按时间范围把会话整理成话题：问题、讨论、结论和开放问题'],
};

document.querySelectorAll('.nav').forEach((button) => {
  button.onclick = () => {
    document.querySelectorAll('.nav,.page').forEach((element) => element.classList.remove('active'));
    button.classList.add('active');
    $(`#page-${button.dataset.page}`).classList.add('active');
    $('#page-title').textContent = titles[button.dataset.page][0];
    $('#page-subtitle').textContent = titles[button.dataset.page][1];
    history.replaceState(null, '', `#${button.dataset.page}`);
    if (button.dataset.page === 'conversations') loadConversations();
    if (button.dataset.page === 'messages' || button.dataset.page === 'summary') fillConversationSelectors();
    // Show the latest messages right away instead of an empty window.
    if (button.dataset.page === 'messages' && !state.messageOffset && $('#message-conversation').value) searchMessages(false);
    if (button.dataset.page === 'summary') openSummaryPage();
  };
});

async function loadStatus() {
  try {
    const status = await api('/api/status');
    state.wechatRunning = status.running;
    $('#orch-start').textContent = status.running ? '等待稳定后归档' : '启动微信并等待同步';
    const metrics = [
      ['微信状态', !status.running ? '未运行' : status.logged_in ? '已登录'
        : status.logged_out ? '已退出登录' : '未登录', status.running && status.logged_in],
      ['微信版本', status.version || '未知', status.compatible],
      ['只读兼容', status.compatible ? '已验证' : '已阻止', status.compatible],
      ['已选会话', String(status.selected_conversations), true],
    ];
    $('#status-grid').innerHTML = metrics.map((metric) => `
      <div class="metric"><span>${metric[0]}</span><strong class="${metric[2] ? 'ok' : 'bad'}">${metric[1]}</strong></div>
    `).join('');
  } catch (error) {
    toast(error.message, true);
  }
}

function statusName(status) {
  return ({
    queued: '等待中', running: '同步中', success: '已完成', partial: '部分成功',
    failed: '失败', cancelled: '已停止', interrupted: '已中断',
  })[status] || status;
}

function renderActiveSync(job) {
  const card = $('#active-sync');
  const button = $('#sync-button');
  if (!job || !['queued', 'running'].includes(job.status)) {
    card.hidden = true;
    button.disabled = false;
    return;
  }

  card.hidden = false;
  button.disabled = true;
  state.activeJobId = job.id;
  const total = Number(job.total_count || 0);
  const scanned = Number(job.scanned_count || 0);
  const percent = total > 0 ? Math.min(100, Math.round(scanned * 100 / total)) : 0;
  $('#sync-title').textContent = job.current_conversation ? `正在同步：${job.current_conversation}` : statusName(job.status);
  $('#sync-percent').textContent = total > 0 ? `${percent}%` : '准备中';
  const progress = $('#sync-progress-bar');
  progress.style.width = total > 0 ? `${percent}%` : '35%';
  progress.classList.toggle('indeterminate', total === 0);
  $('#sync-stats').innerHTML = syncStatsHtml([
    ['已扫描', total > 0 ? `${scanned} / ${total}` : String(scanned)],
    ['新增', job.inserted_count || 0],
    ['更新', job.updated_count || 0],
    ['跳过', job.skipped_count || 0],
    ['错误', job.error_count || 0],
  ]);
}

const syncStatsHtml = (stats) => stats
  .map(([label, value]) => `<span><small>${label}</small><strong>${value}</strong></span>`).join('');

// Final result of the latest finished sync: totals plus what changed per conversation.
async function renderSyncResult(job) {
  const card = $('#sync-result');
  if (!job || ['queued', 'running'].includes(job.status)) {
    card.hidden = true;
    return;
  }
  if (state.resultJobKey !== `${job.id}:${job.status}`) {
    state.resultItems = await api(`/api/jobs/${job.id}/items`);
    state.resultJobKey = `${job.id}:${job.status}`;
  }
  const items = state.resultItems;
  const inserted = Number(job.inserted_count || 0);
  const label = job.status === 'success' ? '同步完成' : statusName(job.status);
  $('#sync-result-title').textContent = inserted ? `${label}：新增 ${inserted} 条消息` : `${label}：没有新消息`;
  $('#sync-result-time').textContent = fmtTime(job.finished_at || job.created_at);
  $('#sync-result-stats').innerHTML = syncStatsHtml([
    ['会话', items.length],
    ['新增', inserted],
    ['更新', job.updated_count || 0],
    ['跳过', job.skipped_count || 0],
    ['错误', job.error_count || 0],
  ]);
  const changed = items.filter((item) => item.inserted_count || item.updated_count || item.status !== 'success');
  const unchanged = items.length - changed.length;
  $('#sync-result-items').innerHTML = changed.map((item) => `
    <div class="result-row" title="${escapeHtml(item.error_message || '')}">
      <span>${escapeHtml(item.display_name)}</span>
      ${item.status === 'success' ? '' : `<span class="pill bad">${statusName(item.status)}</span>`}
      <strong>${item.inserted_count ? `+${item.inserted_count}` : ''}</strong>
      <span class="muted">${item.updated_count ? `更新 ${item.updated_count}` : ''}</span>
    </div>
  `).join('') + (unchanged ? `<p class="muted">${changed.length ? '其余 ' : ''}${unchanged} 个会话没有新消息</p>` : '');
  card.hidden = false;
}

async function loadJobs() {
  try {
    const jobs = await api('/api/jobs');
    const active = jobs.find((job) => ['queued', 'running'].includes(job.status));
    renderActiveSync(active);
    await renderSyncResult(active ? null : jobs[0]);
    $('#jobs').innerHTML = jobs.length ? jobs.map((job) => `
      <div class="job" title="${escapeHtml(job.error_message || '')}">
        <span class="pill">${statusName(job.status)}</span>
        <span>${fmtTime(job.created_at)}</span>
        <span>扫描 ${job.scanned_count}${job.total_count ? ` / ${job.total_count}` : ''}</span>
        <span>新增 ${job.inserted_count}</span>
        <span>更新 ${job.updated_count}</span>
        <span>跳过 ${job.skipped_count}</span>
        <span>错误 ${job.error_count}</span>
      </div>
    `).join('') : '<p class="muted">还没有同步记录</p>';

    clearTimeout(state.jobPollTimer);
    if (active) {
      state.jobPollTimer = setTimeout(loadJobs, 1000);
    } else {
      await Promise.all([loadStatus(), loadConversations()]);
    }
  } catch (error) {
    toast(error.message, true);
    clearTimeout(state.jobPollTimer);
    state.jobPollTimer = setTimeout(loadJobs, 3000);
  }
}

async function loadConversations() {
  try {
    state.conversations = await api('/api/conversations');
    renderConversations();
    fillConversationSelectors();
  } catch (error) {
    toast(error.message, true);
  }
}

// "今天" / "3 天前" / "5 个月前" / "2 年前": enough to judge whether a chat is stale.
function ageLabel(seconds) {
  if (!seconds) return '—';
  const days = Math.floor((Date.now() / 1000 - seconds) / 86400);
  if (days < 1) return '今天';
  if (days < 31) return `${days} 天前`;
  if (days < 365) return `${Math.floor(days / 30)} 个月前`;
  return `${Math.floor(days / 365)} 年前`;
}

function renderConversations() {
  const query = $('#conversation-filter').value.trim().toLowerCase();
  const showHidden = $('#show-hidden').checked;
  $('#hidden-count').textContent = state.conversations.filter((conversation) => conversation.hidden).length;
  const rows = state.conversations.filter((conversation) =>
    (showHidden || !conversation.hidden) && conversation.display_name.toLowerCase().includes(query));
  $('#conversation-list').innerHTML = rows.map((conversation) => {
    const mode = conversation.history_mode || 'all';
    const since = conversation.history_start_at ? localDate(new Date(conversation.history_start_at * 1000)) : '';
    const lastAt = conversation.last_source_message_at;
    return `
    <tr data-id="${conversation.id}" class="${conversation.hidden ? 'is-hidden' : ''}">
      <td><input class="switch" type="checkbox" ${conversation.sync_enabled ? 'checked' : ''}></td>
      <td>${escapeHtml(conversation.display_name)}${conversation.hidden_reason === 'unnamed' ? ' <small class="muted">（无群名，自动不跟踪）</small>' : ''}</td>
      <td>${conversation.conversation_type === 'group' ? '群聊' : '单聊'}</td>
      <td title="${lastAt ? new Date(lastAt * 1000).toLocaleString() : ''}">${ageLabel(lastAt)}</td>
      <td><div class="history-cell">
        <select class="history-mode">
          ${Object.entries(historyModes).map(([value, label]) => `<option value="${value}" ${value === mode ? 'selected' : ''}>${label}</option>`).join('')}
        </select>
        <input class="history-since" type="date" value="${since}" ${mode === 'since' ? '' : 'hidden'}>
      </div></td>
      <td>${conversation.archived_message_count}</td>
      <td>${fmtTime(conversation.last_successful_sync_at)}</td>
      <td><button class="row-action">${conversation.hidden ? '恢复' : '不跟踪'}</button></td>
    </tr>
  `;
  }).join('') || `<tr><td colspan="8" class="muted">${state.conversations.length ? '没有符合条件的会话' : '还没有会话，请点击“扫描本机会话”'}</td></tr>`;
  document.querySelectorAll('#conversation-list tr[data-id]').forEach((row) => {
    const conversation = state.conversations.find((item) => String(item.id) === row.dataset.id);
    row.querySelector('.row-action').onclick = async () => {
      const hide = !conversation.hidden;
      if (hide && conversation.sync_enabled
        && !confirm(`“${conversation.display_name}”正在同步。不跟踪后将停止后续同步，已归档的消息和话题总结保留。确定？`)) return;
      try {
        await api('/api/conversations/hide', {method: 'POST', body: JSON.stringify({conversation_ids: [conversation.id], hidden: hide})});
        toast(hide ? `已不跟踪“${conversation.display_name}”` : `已恢复“${conversation.display_name}”`);
        await Promise.all([loadStatus(), loadConversations()]);
      } catch (error) {
        toast(error.message, true);
      }
    };
    const toggle = row.querySelector('.switch');
    const mode = row.querySelector('.history-mode');
    const since = row.querySelector('.history-since');
    toggle.onchange = () => saveConversation(row, toggle.checked ? '已加入同步' : '已停止后续同步，已归档消息保留');
    mode.onchange = () => {
      since.hidden = mode.value !== 'since';
      if (mode.value === 'since' && !since.value) return; // wait for a date
      saveConversation(row, '历史范围已更新，下次同步将按新范围全量扫描');
    };
    since.onchange = () => { if (since.value) saveConversation(row, '历史范围已更新，下次同步将按新范围全量扫描'); };
  });
}

const historyModes = {all: '全部本机历史', recent_30d: '最近 30 天', recent_90d: '最近 90 天', since: '指定日期起'};

async function saveConversation(row, message) {
  const mode = row.querySelector('.history-mode').value;
  const body = {
    enabled: row.querySelector('.switch').checked,
    history_mode: mode,
    history_start_at: mode === 'since' ? epochStart(row.querySelector('.history-since').value) : null,
  };
  try {
    await api(`/api/conversations/${row.dataset.id}`, {method: 'PUT', body: JSON.stringify(body)});
    toast(message);
  } catch (error) {
    toast(error.message, true);
  }
  await Promise.all([loadStatus(), loadConversations()]);
}

function fillConversationSelectors() {
  // Unselecting a conversation stops future syncs but keeps its archive, so
  // anything with archived messages stays browsable and summarizable.
  const browsable = state.conversations.filter((conversation) => conversation.sync_enabled || conversation.archived_message_count > 0);
  const options = browsable.map((conversation) => {
    const label = conversation.sync_enabled ? conversation.display_name
      : `${conversation.display_name}（${conversation.hidden ? '不跟踪' : '已停止同步'}）`;
    return `<option value="${conversation.id}">${escapeHtml(label)}</option>`;
  }).join('');
  const message = $('#message-conversation');
  const summary = $('#summary-conversation');
  const previousMessage = message.value;
  const previousSummary = summary.value;
  message.innerHTML = options || '<option value="">请先选择会话并同步</option>';
  summary.innerHTML = options || '<option value="">请先选择会话并同步</option>';
  if ([...message.options].some((option) => option.value === previousMessage)) message.value = previousMessage;
  if ([...summary.options].some((option) => option.value === previousSummary)) summary.value = previousSummary;
}

function epochStart(value) {
  return value ? Math.floor(new Date(`${value}T00:00:00`).getTime() / 1000) : null;
}

function epochEnd(value) {
  return value ? Math.floor(new Date(`${value}T23:59:59`).getTime() / 1000) : null;
}

async function searchMessages(append = false) {
  try {
    const conversationId = $('#message-conversation').value;
    if (!conversationId) throw new Error('请先选择一个会话');
    if (!append) state.messageOffset = 0;
    const params = new URLSearchParams({
      conversation_id: conversationId,
      order: $('#message-order').value,
      limit: String(state.messagePageSize),
      offset: String(state.messageOffset),
    });
    const start = epochStart($('#message-start').value);
    const end = epochEnd($('#message-end').value);
    const query = $('#message-query').value.trim();
    if (start) params.set('start_at', start);
    if (end) params.set('end_at', end);
    if (query) params.set('q', query);
    state.messageLoading = true;
    const result = await api(`/api/messages?${params}`);
    state.messageTotal = result.total;
    const terms = query.split(/\s+/).filter(Boolean);
    const markup = result.items.map((message) => consoleLine(message, terms)).join('');
    const list = $('#message-list');
    if (append) list.insertAdjacentHTML('beforeend', markup);
    else {
      list.innerHTML = markup || '<div class="console-empty">没有符合条件的消息</div>';
      list.scrollTop = 0;
      updateMessageSelection();  // old lines and their selection are gone
    }
    state.messageOffset += result.items.length;
    $('#message-count').textContent = `已显示 ${state.messageOffset} / 共 ${result.total} 条`;
    $('#message-more').hidden = state.messageOffset >= result.total;
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.messageLoading = false;
  }
}

const consoleTime = (seconds) => {
  const d = new Date(seconds * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
};

// One terminal-style line: time, sender, message. The spaces between the
// columns are real text, so a selection copies as "time  who  message".
function consoleLine(message, terms = []) {
  const who = senderName(message);
  let text = escapeHtml(message.content);
  for (const term of terms) {
    if (/[&<>"']/.test(term)) continue; // escaping would split the match
    const pattern = new RegExp(term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
    text = text.replace(pattern, (match) => `<mark>${match}</mark>`);
  }
  return `<div class="line" data-ts="${message.source_created_at}"><span class="t">${consoleTime(message.source_created_at)}</span>  <span class="w ${message.is_self ? 'self' : ''}" title="${escapeHtml(who)}">${escapeHtml(who)}</span>  <span class="m">${text}</span></div>`;
}

function senderName(message) {
  if (message.is_self) return '我';
  return message.sender_display_name || message.sender_source_id || '未知发送者';
}

// Message lines touched by the current text selection in the message window.
function selectedMessageRange() {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || !selection.rangeCount) return null;
  const range = selection.getRangeAt(0);
  const list = $('#message-list');
  if (!list.contains(range.commonAncestorContainer)) return null;
  const stamps = [...list.querySelectorAll('.line')]
    .filter((line) => range.intersectsNode(line))
    .map((line) => Number(line.dataset.ts));
  if (!stamps.length) return null;
  return {count: stamps.length, start: Math.min(...stamps), end: Math.max(...stamps)};
}

function updateMessageSelection() {
  state.messageSelection = selectedMessageRange();
  const selection = state.messageSelection;
  $('#message-summarize').hidden = !selection;
  $('#message-selection').textContent = selection
    ? `已选中 ${selection.count} 条：${consoleTime(selection.start)} – ${consoleTime(selection.end)}`
    : '';
}

async function summarizeSelection() {
  const selection = state.messageSelection;
  const conversationId = Number($('#message-conversation').value);
  if (!selection || !conversationId) return;
  const body = {conversation_id: conversationId, start_at: selection.start, end_at: selection.end};
  try {
    const preview = await api('/api/summaries/preview', {method: 'POST', body: JSON.stringify(body)});
    if (!preview.ai.configured) throw new Error('未配置 OpenAI API key，请先在“话题总结”页查看配置说明');
    if (preview.too_many) throw new Error(`消息超过 ${preview.max_messages} 条，请缩小选择范围`);
    const name = $('#message-conversation').selectedOptions[0].textContent;
    // The summary covers the whole time span, not only lines a keyword filter shows.
    const filtered = $('#message-query').value.trim() ? '\n注意：总结按这段时间内的全部消息计算，不只是关键词匹配的消息。' : '';
    if (!confirm(`用选中的消息生成话题总结？\n\n会话：${name}\n时间：${consoleTime(selection.start)} – ${consoleTime(selection.end)}\n共 ${preview.message_count} 条消息，将发送到 OpenAI（${preview.ai.model}）。${filtered}`)) return;
    const summary = await api('/api/summaries', {method: 'POST', body: JSON.stringify({...body, range_label: '选中消息'})});
    window.getSelection().removeAllRanges();
    updateMessageSelection();
    state.viewingSummary = summary.id;  // keep the summary page from opening another one
    document.querySelector('.nav[data-page="summary"]').click();
    viewSummary(summary.id);
  } catch (error) {
    toast(error.message, true);
  }
}

function clearMessageFilters(order) {
  $('#message-query').value = '';
  $('#message-start').value = '';
  $('#message-end').value = '';
  $('#message-order').value = order;
}

// ---------------------------------------------------------------------------
// 话题总结
// ---------------------------------------------------------------------------

const topicStatus = {resolved: '已有结论', ongoing: '仍在进行', inconclusive: '无明确结论'};
const summaryStatus = {queued: '排队中', running: '分析中', success: '已完成', failed: '失败', cancelled: '已取消', interrupted: '已中断'};
const shortTime = (value) => value ? new Date(value * 1000).toLocaleString([], {month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'}) : '—';
const fmtRange = (start, end) => `${shortTime(start)} – ${shortTime(end)}`;
const listHtml = (items) => items.length ? `<ul>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join('')}</ul>` : '<p class="muted">无</p>';

state.summaryPreset = '1';

function summaryRange() {
  if (state.summaryPreset === 'custom') {
    const start = epochStart($('#summary-start').value);
    const end = epochEnd($('#summary-end').value);
    return {start, end, label: start && end ? `${$('#summary-start').value} 至 ${$('#summary-end').value}` : ''};
  }
  // Presets are rolling windows ending now, so "最近 1 天" is never nearly empty in the morning.
  const days = Number(state.summaryPreset);
  const end = Math.floor(Date.now() / 1000);
  return {start: end - days * 86400, end, label: days === 7 ? '最近 1 周' : `最近 ${days} 天`};
}

async function loadAISettings() {
  try {
    const ai = await api('/api/ai/settings');
    const labels = {low: '低', mid: '中', high: '高'};
    $('#ai-tier').innerHTML = Object.entries(ai.tiers)
      .map(([tier, model]) => `<option value="${tier}" ${tier === ai.model_tier ? 'selected' : ''}>${labels[tier] || tier} · ${escapeHtml(model)}</option>`).join('');
    state.aiConfigured = ai.configured;
    $('#ai-note').innerHTML = ai.configured
      ? `生成时会把所选会话在该时间段内的文字消息发送到 OpenAI（${escapeHtml(ai.model)}，store=false，不保存会话状态）。配置来源：${escapeHtml(ai.source)}。总结由 AI 生成，可展开“查看原文”核对。`
      : '<span class="warn">未配置 OpenAI API key：可设置环境变量 OPENAI_API_KEY，或在数据目录放置 openai_config.json（见 README）。</span>';
  } catch (error) {
    toast(error.message, true);
  }
}

async function refreshSummaryPreview() {
  const conversationId = Number($('#summary-conversation').value);
  const range = summaryRange();
  const element = $('#summary-preview');
  const button = $('#summary-generate');
  if (!conversationId || !range.start || !range.end) {
    element.textContent = conversationId ? '请选择完整的起止日期' : '请先在“会话选择”中选择并同步会话';
    button.disabled = true;
    return;
  }
  try {
    const preview = await api('/api/summaries/preview', {
      method: 'POST', body: JSON.stringify({conversation_id: conversationId, start_at: range.start, end_at: range.end}),
    });
    if (!preview.message_count) {
      element.textContent = `${fmtRange(range.start, range.end)} 没有已归档的文字消息`;
      button.disabled = true;
      return;
    }
    const cached = preview.cached_windows ? `，其中 ${preview.cached_windows} 段已有结果可复用` : '';
    element.innerHTML = `${fmtRange(range.start, range.end)}：<strong>${preview.message_count}</strong> 条消息 · 约 ${preview.character_count} 字 · ${preview.participants} 位发送者 · 分 ${preview.window_count} 段分析${cached}`
      + (preview.too_many ? `<br><span class="warn">超过 ${preview.max_messages} 条上限，请缩短时间段</span>` : '');
    button.disabled = preview.too_many || !preview.ai.configured;
  } catch (error) {
    element.textContent = error.message;
    button.disabled = true;
  }
}

state.historyScope = 'all';

// "今天" / "昨天" / "10月5日" for the day a summary was generated.
function generatedDay(iso) {
  const date = new Date(iso);
  const today = localDate(new Date());
  const day = localDate(date);
  if (day === today) return '今天';
  if (day === localDate(daysAgo(1))) return '昨天';
  return date.toLocaleDateString([], {year: date.getFullYear() === new Date().getFullYear() ? undefined : 'numeric', month: 'long', day: 'numeric'});
}

async function loadSummaryHistory(openLatest = false) {
  const conversationId = Number($('#summary-conversation').value);
  const list = $('#summary-history');
  const all = state.historyScope === 'all';
  if (!all && !conversationId) { list.innerHTML = ''; return; }
  try {
    const items = await api(all ? '/api/summaries' : `/api/summaries?conversation_id=${conversationId}`);
    let lastDay = '';
    list.innerHTML = items.length ? items.map((item) => {
      const day = generatedDay(item.created_at);
      const header = day !== lastDay ? `<li class="day">${day}</li>` : '';
      lastDay = day;
      const generatedAt = new Date(item.created_at).toLocaleString([], {month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'});
      return `${header}
      <li data-id="${item.id}" class="${item.id === state.viewingSummary ? 'active' : ''}">
        <strong>${escapeHtml(item.conversation_name)}</strong>
        <small>范围：${fmtRange(item.range_start, item.range_end)}</small>
        <small>生成：${generatedAt} · ${summaryStatus[item.status] || item.status} · ${item.topic_count} 个话题${item.stale ? ' · <span class="badge">有新消息</span>' : ''}</small>
      </li>`;
    }).join('') : `<li class="muted">${all ? '还没有任何总结' : '这个会话还没有总结'}</li>`;
    list.querySelectorAll('li[data-id]').forEach((li) => { li.onclick = () => viewSummary(Number(li.dataset.id)); });
    if (openLatest && items.length) viewSummary(items[0].id);
    else if (openLatest) $('#summary-view').innerHTML = '<div class="card muted">还没有话题总结。选择会话和时间范围后点击“生成话题总结”。</div>';
  } catch (error) {
    toast(error.message, true);
  }
}

async function viewSummary(summaryId) {
  clearTimeout(state.summaryTimer);
  state.viewingSummary = summaryId;
  document.querySelectorAll('#summary-history li').forEach((li) => li.classList.toggle('active', Number(li.dataset.id) === summaryId));
  try {
    const summary = await api(`/api/summaries/${summaryId}`);
    if (state.viewingSummary !== summaryId) return;
    // Keep the controls on the conversation being viewed (history lists all conversations).
    const select = $('#summary-conversation');
    if (select.value !== String(summary.conversation_id) && [...select.options].some((o) => o.value === String(summary.conversation_id))) {
      select.value = String(summary.conversation_id);
      refreshSummaryPreview();
    }
    renderSummary(summary);
    if (['queued', 'running'].includes(summary.status)) {
      state.summaryTimer = setTimeout(() => viewSummary(summaryId), 1500);
    } else if (state.summaryWasRunning === summaryId) {
      state.summaryWasRunning = null;
      loadSummaryHistory();
      refreshSummaryPreview();
    }
  } catch (error) {
    toast(error.message, true);
  }
}

function renderSummary(summary) {
  const view = $('#summary-view');
  const running = ['queued', 'running'].includes(summary.status);
  if (running) state.summaryWasRunning = summary.id;
  const tokens = summary.input_tokens ? ` · tokens ${summary.input_tokens}/${summary.output_tokens}` : '';
  const head = `
    <div class="card summary-head">
      <div class="section-head">
        <div>
          <span class="eyebrow">TOPIC SUMMARY</span>
          <h2>${escapeHtml(summary.conversation_name)} · ${escapeHtml(summary.range_label || '自定义')}</h2>
          <div class="summary-meta">
            <span>${fmtTime(summary.created_at)} 生成</span>
            <span>范围 ${fmtRange(summary.range_start, summary.range_end)}</span>
            <span>${summary.message_count} 条消息</span>
            <span>${escapeHtml(summary.model)}${tokens}</span>
            <span>${summaryStatus[summary.status] || summary.status}</span>
            ${summary.stale ? '<span class="badge">此范围有新归档的消息，可重新生成</span>' : ''}
          </div>
        </div>
        <div class="summary-actions">
          ${running ? '<button class="quiet" id="summary-cancel">取消</button>' : '<button class="quiet" id="summary-rerun">按此范围重新生成</button>'}
          <button class="quiet" id="summary-delete">删除</button>
        </div>
      </div>
      ${running ? `<div class="progress-line"><span>正在分析第 ${Math.min(summary.done_windows + 1, summary.window_count || 1)} / ${summary.window_count || '?'} 段${summary.cached_windows ? `（${summary.cached_windows} 段复用已有结果）` : ''}</span></div>
        <div class="progress-track"><div class="progress-bar ${summary.window_count ? '' : 'indeterminate'}" style="width:${summary.window_count ? Math.round(summary.done_windows * 100 / summary.window_count) : 35}%"></div></div>` : ''}
      ${summary.status === 'failed' || summary.status === 'interrupted' ? `<p class="bad">${escapeHtml(summary.error_message || '未完成')}</p>` : ''}
      ${summary.overview ? `<p class="overview">${escapeHtml(summary.overview)}</p>` : ''}
    </div>`;
  const topics = summary.status === 'success' && !summary.topics.length
    ? '<div class="card muted">这段时间没有识别出有实质内容的话题。</div>'
    : summary.topics.map((topic) => `
      <article class="card topic">
        <div class="topic-head"><span class="status-pill ${topic.status}">${topicStatus[topic.status] || topic.status}</span><h3>${escapeHtml(topic.title)}</h3></div>
        <div class="topic-sub">${fmtRange(topic.first_message_at, topic.last_message_at)} · ${topic.source_count} 条消息 · ${escapeHtml(topic.participants.join('、'))}</div>
        <div class="topic-section"><h4>问题</h4><p>${escapeHtml(topic.question)}</p></div>
        <div class="topic-section"><h4>讨论</h4>${listHtml(topic.discussion)}</div>
        <div class="topic-section"><h4>结论</h4>${listHtml(topic.conclusions)}</div>
        <div class="topic-section"><h4>开放问题</h4>${listHtml(topic.open_questions)}</div>
        <div class="topic-sources"><button class="quiet" data-topic="${topic.id}">查看原文（${topic.source_count} 条）</button><div class="source-list console source-console" hidden></div></div>
      </article>`).join('');
  view.innerHTML = head + topics;

  view.querySelectorAll('button[data-topic]').forEach((button) => {
    button.onclick = async () => {
      const list = button.nextElementSibling;
      if (!list.hidden) { list.hidden = true; return; }
      try {
        const messages = await api(`/api/summary-topics/${button.dataset.topic}/messages`);
        list.innerHTML = messages.map((message) => consoleLine(message)).join('');
        list.hidden = false;
      } catch (error) {
        toast(error.message, true);
      }
    };
  });
  const cancel = $('#summary-cancel');
  if (cancel) cancel.onclick = () => api(`/api/summaries/${summary.id}/cancel`, {method: 'POST'}).catch((error) => toast(error.message, true));
  const rerun = $('#summary-rerun');
  // Regenerating keeps this summary; a new one is added to the history.
  if (rerun) rerun.onclick = () => generateSummary({start: summary.range_start, end: summary.range_end, label: summary.range_label}, summary.conversation_id);
  $('#summary-delete').onclick = async () => {
    if (!confirm('只删除这份话题总结。\n\n微信中的聊天记录和聊迹已归档的消息都不会被删除。确定删除？')) return;
    try {
      await api(`/api/summaries/${summary.id}`, {method: 'DELETE'});
      state.viewingSummary = null;
      $('#summary-view').innerHTML = '<div class="card muted">已删除。</div>';
      loadSummaryHistory();
    } catch (error) {
      toast(error.message, true);
    }
  };
}

async function generateSummary(range = summaryRange(), conversationId = Number($('#summary-conversation').value)) {
  const button = $('#summary-generate');
  button.disabled = true;
  try {
    const summary = await api('/api/summaries', {
      method: 'POST',
      body: JSON.stringify({conversation_id: conversationId, start_at: range.start, end_at: range.end, range_label: range.label}),
    });
    await loadSummaryHistory();
    viewSummary(summary.id);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function openSummaryPage() {
  loadAISettings();
  refreshSummaryPreview();
  loadSummaryHistory(!state.viewingSummary);
}

$('#scan-button').onclick = async () => {
  try {
    $('#scan-button').disabled = true;
    const result = await api('/api/conversations/scan', {method: 'POST'});
    toast(`已扫描 ${result.scanned} 个会话`);
    await loadConversations();
  } catch (error) {
    toast(error.message, true);
  } finally {
    $('#scan-button').disabled = false;
  }
};

$('#sync-button').onclick = async () => {
  try {
    const result = await api('/api/sync', {method: 'POST'});
    toast(`同步任务 #${result.job_id} 已开始`);
    await loadJobs();
  } catch (error) {
    toast(error.message, true);
  }
};

const orchSteps = [
  ['launching', '启动微信'], ['waiting_login', '等待登录'], ['wechat_writing', '微信写入中'], ['stable', '数据暂时稳定'],
  ['archiving', '聊迹归档'], ['settling', '等待补扫'], ['rescanning', '补偿扫描'], ['done', '归档完成'],
];
const clockTime = (value) => value ? new Date(value).toLocaleTimeString() : '—';

function renderOrchestration(run) {
  const order = orchSteps.map(([key]) => key);
  const ended = ['done', 'failed', 'cancelled'].includes(run.phase);
  // For failed/cancelled runs, ``reached`` is the step where the run stopped.
  const reached = order.indexOf(run.reached);
  $('#orch-steps').innerHTML = run.phase === 'idle' ? '' : orchSteps.map(([key, label], index) => {
    let css = '';
    if (run.was_running && (key === 'launching' || key === 'waiting_login')) css = 'skipped';
    else if (run.phase === key && !ended) css = 'current';
    else if (run.phase === 'done' || index < reached) css = 'done';
    else if (index === reached && ['failed', 'cancelled'].includes(run.phase)) css = 'bad';
    const hint = css === 'skipped' ? ' title="微信已在运行，已跳过"' : '';
    return `<li class="${css}"${hint}>${label}</li>`;
  }).join('');

  const detail = $('#orch-detail');
  let text = '';
  if (['wechat_writing', 'settling'].includes(run.phase) && run.quiet_remaining !== null) {
    text = `最近写入：${clockTime(run.last_write_at)} · 还需连续 ${run.quiet_remaining} 秒无新写入`;
  } else if (run.phase === 'waiting_login') {
    text = '请在微信窗口完成登录（扫码或手机确认）。检测到微信写入本地数据库后自动进入下一步。';
  } else if (['archiving', 'rescanning'].includes(run.phase)) {
    text = '正在归档，进度见下方同步卡片。';
  } else if (ended) {
    text = run.message;
  }
  detail.hidden = !text;
  detail.textContent = text;
  detail.classList.toggle('bad', run.phase === 'failed');

  $('#orch-log').hidden = !run.events.length;
  $('#orch-events').innerHTML = run.events.slice().reverse()
    .map((event) => `<li>${clockTime(event.at)} ${escapeHtml(event.text)}</li>`).join('');
  $('#orch-start').hidden = run.active;
  $('#orch-quiet').disabled = run.active;
  if (run.active && run.quiet_seconds) $('#orch-quiet').value = String(run.quiet_seconds);
  $('#orch-cancel').hidden = !run.active;
}

async function loadOrchestration() {
  clearTimeout(state.orchTimer);
  try {
    const run = await api('/api/orchestration');
    const wasActive = state.orchActive;
    state.orchActive = run.active;
    renderOrchestration(run);
    // Archiving runs a normal sync job; start the job poller so its progress card shows.
    if (['archiving', 'rescanning'].includes(run.phase) && $('#active-sync').hidden) loadJobs();
    if (run.active) state.orchTimer = setTimeout(loadOrchestration, 1000);
    else if (wasActive) loadJobs();
  } catch (error) {
    if (error.status === 404) {
      // Static files are read from disk, so a server started before this
      // feature existed still serves this page but lacks the endpoints.
      showOrchError(OUTDATED_SERVER);
      return;
    }
    toast(error.message, true);
    state.orchTimer = setTimeout(loadOrchestration, 3000);
  }
}

const OUTDATED_SERVER = '当前运行的聊迹服务是旧版本，不包含“启动微信并归档”功能。请关闭并重新启动聊迹，然后刷新本页面。';

function showOrchError(message) {
  const detail = $('#orch-detail');
  detail.hidden = false;
  detail.textContent = message;
  detail.classList.add('bad');
}

$('#orch-start').onclick = async () => {
  const button = $('#orch-start');
  button.disabled = true;
  try {
    await api('/api/orchestration/start', {method: 'POST', body: JSON.stringify({quiet_seconds: Number($('#orch-quiet').value)})});
    toast(state.wechatRunning ? '微信已在运行，等待数据稳定后归档' : '正在启动微信，请完成登录');
    loadOrchestration();
  } catch (error) {
    // Show the reason in the card; a toast alone is easy to miss.
    showOrchError(error.status === 404 ? OUTDATED_SERVER : error.message);
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
};

$('#orch-cancel').onclick = async () => {
  try {
    await api('/api/orchestration/cancel', {method: 'POST'});
    toast('已请求取消，微信保持当前状态');
    loadOrchestration();
  } catch (error) {
    toast(error.message, true);
  }
};

$('#sync-cancel').onclick = async () => {
  if (!state.activeJobId) return;
  try {
    await api(`/api/sync/${state.activeJobId}/cancel`, {method: 'POST'});
    toast('正在停止，当前批次写入完成后结束');
  } catch (error) {
    toast(error.message, true);
  }
};

$('#show-hidden').onchange = renderConversations;
$('#hide-inactive').onclick = async () => {
  const days = Number($('#inactive-days').value);
  const label = $('#inactive-days').selectedOptions[0].textContent;
  try {
    const {count} = await api(`/api/conversations/inactive?days=${days}`);
    if (!count) { toast(`没有${label}且未选择同步的会话`); return; }
    if (!confirm(`将不跟踪 ${count} 个${label}的会话（已选择同步的会话不受影响）。\n\n它们会从列表中隐藏，已归档的消息和话题总结都保留，可随时恢复。确定？`)) return;
    const result = await api('/api/conversations/hide-inactive', {method: 'POST', body: JSON.stringify({days})});
    toast(`已不跟踪 ${result.hidden} 个会话`);
    await loadConversations();
  } catch (error) {
    toast(error.message, true);
  }
};

$('#refresh-jobs').onclick = loadJobs;
$('#conversation-filter').oninput = renderConversations;
$('#message-search').onclick = () => searchMessages(false);
$('#message-more').onclick = () => searchMessages(true);
document.addEventListener('selectionchange', () => {
  // Coalesce the bursts fired while dragging.
  cancelAnimationFrame(state.selectionFrame);
  state.selectionFrame = requestAnimationFrame(updateMessageSelection);
});
// Keep the text selection alive when the button is pressed.
$('#message-summarize').onmousedown = (event) => event.preventDefault();
$('#message-summarize').onclick = summarizeSelection;
// Load the next page when the message window is scrolled to its end.
$('#message-list').addEventListener('scroll', () => {
  const list = $('#message-list');
  if (!state.messageLoading && state.messageOffset < state.messageTotal
      && list.scrollTop + list.clientHeight >= list.scrollHeight - 60) searchMessages(true);
});
$('#message-conversation').onchange = () => searchMessages(false);
$('#browse-latest').onclick = () => { clearMessageFilters('desc'); searchMessages(false); };
$('#browse-earliest').onclick = () => { clearMessageFilters('asc'); searchMessages(false); };
$('#browse-seven-days').onclick = () => {
  $('#message-end').value = localDate(new Date());
  $('#message-start').value = localDate(daysAgo(6));
  $('#message-query').value = '';
  $('#message-order').value = 'desc';
  searchMessages(false);
};
document.querySelectorAll('#summary-presets .preset').forEach((button) => {
  button.onclick = () => {
    state.summaryPreset = button.dataset.days;
    document.querySelectorAll('#summary-presets .preset').forEach((item) => item.classList.toggle('active', item === button));
    $('#summary-custom').hidden = state.summaryPreset !== 'custom';
    refreshSummaryPreview();
  };
});
$('#summary-start').onchange = refreshSummaryPreview;
$('#summary-end').onchange = refreshSummaryPreview;
$('#summary-conversation').onchange = () => {
  refreshSummaryPreview();
  // In the all-conversations list the current view stays; the per-conversation list opens its latest.
  if (state.historyScope === 'conversation') { state.viewingSummary = null; loadSummaryHistory(true); }
};
$('#summary-generate').onclick = () => generateSummary();
document.querySelectorAll('#history-scope button').forEach((button) => {
  button.onclick = () => {
    state.historyScope = button.dataset.scope;
    document.querySelectorAll('#history-scope button').forEach((item) => item.classList.toggle('active', item === button));
    loadSummaryHistory();
  };
});
$('#ai-tier').onchange = async () => {
  try {
    await api('/api/ai/settings', {method: 'PUT', body: JSON.stringify({model_tier: $('#ai-tier').value})});
    await loadAISettings();
    refreshSummaryPreview();
  } catch (error) {
    toast(error.message, true);
  }
};

$('#summary-end').value = localDate(new Date());
$('#summary-start').value = localDate(daysAgo(6));
$('#message-start').value = '';
$('#message-end').value = '';

loadStatus();
loadJobs();
loadOrchestration();
// Reopen the page named in the URL (#summary etc.) once conversations are loaded.
loadConversations().then(() => {
  const button = document.querySelector(`.nav[data-page="${location.hash.slice(1)}"]`);
  if (button) button.click();
});
