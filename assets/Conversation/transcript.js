/* Offline transcript view. All side effects are explicit, user-initiated
 * messages to the native host; this document makes no network/file requests. */
(function () {
  'use strict';
  const markdown = window.VibeCueMarkdown;
  const transcript = document.getElementById('transcript');
  const emptyState = document.getElementById('empty-state');
  const emptyTitle = document.getElementById('empty-title');
  const emptyDetail = document.getElementById('empty-detail');
  const jumpButton = document.getElementById('jump-latest');
  const messageNodes = new Map(), groupNodes = new Map(), activityNodes = new Map();
  let current = { messages: [], activities: [], theme: 'dark', state: 'loading' };
  let initialRender = true, selectionRefresh = false, updateQueued = false;

  function post(value) {
    const bridge = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.vibecue;
    if (bridge && typeof bridge.postMessage === 'function') bridge.postMessage(value);
  }
  function create(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (typeof text === 'string') node.textContent = text;
    return node;
  }
  function hasSelection(node) {
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed || !selection.rangeCount) return false;
    try { return selection.getRangeAt(0).intersectsNode(node); }
    catch (_) { return false; }
  }
  function atBottom() {
    const element = document.scrollingElement || document.documentElement;
    return element.scrollHeight - element.scrollTop - window.innerHeight < 64;
  }
  function jumpLatest() {
    const element = document.scrollingElement || document.documentElement;
    element.scrollTop = element.scrollHeight;
    jumpButton.hidden = true;
  }
  function updateJump() { jumpButton.hidden = !transcript.childElementCount || atBottom(); }
  function readingAnchor() {
    for (const node of transcript.querySelectorAll('.message')) {
      const bounds = node.getBoundingClientRect();
      if (bounds.bottom > 0 && bounds.top < window.innerHeight) return { key: node.messageKey, top: bounds.top };
    }
    return null;
  }
  function restoreReadingAnchor(anchor, fallback) {
    const element = document.scrollingElement || document.documentElement;
    const node = anchor && messageNodes.get(anchor.key);
    if (node && node.isConnected) element.scrollTop += node.getBoundingClientRect().top - anchor.top;
    else element.scrollTop = fallback;
  }
  function selectionWouldBeRemoved(groups) {
    const wantedMessages = new Set(groups.flatMap(group => group.messages.map(row => row.key)));
    for (const [key, node] of messageNodes) if (!wantedMessages.has(key) && hasSelection(node)) return true;
    // Missing turn IDs use the nearest user message as a group boundary. If
    // that boundary is trimmed, moving a selected node also destroys its Range.
    for (const group of groups) {
      for (const row of group.messages) {
        const node = messageNodes.get(row.key);
        if (node && hasSelection(node) && node.parentElement !== groupNodes.get(group.key)) return true;
      }
    }
    const wantedActivities = new Set(groups.filter(group => group.activities.length).map(group => group.key));
    for (const [key, node] of activityNodes) if (!wantedActivities.has(key) && hasSelection(node)) return true;
    return false;
  }
  function reconcile(container, nodes) {
    const wanted = new Set(nodes);
    for (const child of Array.from(container.children)) if (!wanted.has(child)) child.remove();
    nodes.forEach((node, index) => {
      const currentNode = container.children[index];
      if (currentNode !== node) container.insertBefore(node, currentNode || null);
    });
  }
  function cleanMessages(values) {
    if (!Array.isArray(values)) return [];
    const result = [], seen = new Set();
    let remaining = 262144;
    for (const row of values.slice(-80)) {
      if (!row || !['user', 'assistant'].includes(row.role) || typeof row.text !== 'string') continue;
      const body = row.text.slice(-Math.min(131072, remaining));
      if (!body && !Array.isArray(row.attachments)) continue;
      remaining -= body.length;
      const id = typeof row.id === 'string' && row.id ? row.id.slice(0, 200) : 'fallback-' + result.length;
      const turn = typeof row.turn_id === 'string' ? row.turn_id.slice(0, 200) : '';
      const key = JSON.stringify([turn, id]);
      if (seen.has(key)) continue;
      seen.add(key);
      const attachments = Array.isArray(row.attachments) ? row.attachments.slice(0, 20).filter(file => file && typeof file.path === 'string' && file.path.startsWith('/') && !/[\u0000-\u001f]/.test(file.path) && typeof file.name === 'string').map(file => ({ name: file.name.slice(0, 180), path: file.path.slice(0, 8192), kind: file.kind === 'image' ? 'image' : 'file' })) : [];
      result.push({ id, key, role: row.role, text: body, phase: row.role === 'user' ? 'user' : row.phase === 'commentary' ? 'commentary' : 'final_answer', turn_id: turn, created_at: typeof row.created_at === 'number' ? row.created_at : null, attachments });
      if (remaining <= 0) break;
    }
    return result;
  }
  function cleanActivities(values) {
    if (!Array.isArray(values)) return [];
    return values.slice(-200).filter(row => row && typeof row.label === 'string' && typeof row.turn_id === 'string').map((row, index) => ({ id: typeof row.id === 'string' ? row.id.slice(0, 200) : 'activity-' + index, label: row.label.slice(0, 160), turn_id: row.turn_id.slice(0, 200), name: typeof row.name === 'string' ? row.name.slice(0, 100) : '', status: row.status === 'running' ? 'running' : 'complete' }));
  }
  function makeMessage(row) {
    const node = create('article', 'message');
    node.dataset.messageId = row.id;
    node.messageKey = row.key;
    const body = create('div', 'message-body');
    const attachments = create('div', 'attachments');
    const actions = create('div', 'message-actions');
    const copy = create('button', 'message-copy');
    copy.type = 'button'; copy.dataset.action = 'copy-message';
    copy.setAttribute('aria-label', row.role === 'user' ? '复制消息' : '复制回复');
    copy.title = row.role === 'user' ? '复制消息' : '复制回复'; copy.innerHTML = markdown.copyIcon;
    actions.append(copy); node.append(body, attachments, actions);
    node.body = body; node.attachments = attachments; node.renderedSignature = '';
    return node;
  }
  function updateMessage(node, row) {
    node.className = 'message message-' + row.role + (row.phase === 'commentary' ? ' message-commentary' : '');
    // Preserve actual reading/selection while live messages are updating. A
    // later selectionchange reapplies the latest payload after selection ends.
    const signature = JSON.stringify([row.text, row.attachments]);
    if (signature !== node.renderedSignature) {
      if (hasSelection(node)) { selectionRefresh = true; return; }
      node.body.innerHTML = markdown.render(row.text);
      node.copyText = row.text; node.renderedSignature = signature;
      const attachments = row.attachments.map(file => {
        const button = create('button', 'attachment');
        button.type = 'button'; button.dataset.action = 'open-attachment'; button.attachmentPath = file.path;
        button.title = file.path;
        const icon = create('span', 'attachment-type', file.kind === 'image' ? '图片' : '文件');
        const name = create('span', 'attachment-name', file.name);
        button.append(icon, name); return button;
      });
      reconcile(node.attachments, attachments);
      node.attachments.hidden = attachments.length === 0;
    }
  }
  function makeActivities() {
    const details = create('details', 'activities');
    const summary = create('summary');
    const chevron = create('span', 'activity-chevron');
    chevron.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="m6 3 5 5-5 5"/></svg>';
    details.caption = create('span'); details.runningIndicator = create('span', 'loading-indicator');
    summary.append(chevron, details.runningIndicator, details.caption);
    details.list = create('ul', 'activities-list'); details.append(summary, details.list);
    return details;
  }
  function updateActivities(details, rows) {
    const running = rows.filter(row => row.status === 'running').length;
    details.caption.textContent = running ? '正在执行 · ' + rows.length + ' 项操作' : '已执行 ' + rows.length + ' 项操作';
    details.runningIndicator.hidden = running === 0;
    const signature = JSON.stringify(rows);
    if (details.renderedSignature !== signature) {
      const existing = new Map(Array.from(details.list.children).map(node => [node.dataset.activityId, node]));
      const nodes = rows.map(row => {
        const node = existing.get(row.id) || create('li'); node.dataset.activityId = row.id;
        const label = node.labelNode || create('span'); label.textContent = row.label;
        const status = node.statusNode || create('span', 'activity-status'); status.textContent = row.status === 'running' ? '进行中' : '已完成';
        if (!node.labelNode) { node.append(label, status); node.labelNode = label; node.statusNode = status; }
        return node;
      });
      reconcile(details.list, nodes); details.renderedSignature = signature;
    }
  }
  function groupsFor(messages, activities) {
    const groups = [], byKey = new Map();
    let fallback = 'initial';
    for (const row of messages) {
      if (row.role === 'user' && !row.turn_id) fallback = row.id;
      const key = row.turn_id || 'unassigned:' + fallback;
      if (!byKey.has(key)) { const group = { key, messages: [], activities: [] }; groups.push(group); byKey.set(key, group); }
      byKey.get(key).messages.push(row);
    }
    for (const row of activities) {
      let group = byKey.get(row.turn_id);
      if (!group && groups.length && !row.turn_id) group = groups[groups.length - 1];
      if (group) group.activities.push(row);
    }
    return groups;
  }
  function showEmpty(payload, hasMessages) {
    emptyState.hidden = hasMessages;
    if (hasMessages) return;
    const state = ['loading', 'empty', 'error'].includes(payload.state) ? payload.state : 'empty';
    emptyState.dataset.state = state;
    emptyTitle.textContent = typeof payload.message === 'string' ? payload.message.slice(0, 300) : state === 'loading' ? '正在读取对话…' : state === 'error' ? '暂时无法读取对话' : '暂无可显示的对话';
    emptyDetail.textContent = typeof payload.detail === 'string' ? payload.detail.slice(0, 600) : state === 'loading' ? '' : '本窗口只显示已保留的用户消息与可见回复。';
  }
  function update(payload) {
    if (!payload || typeof payload !== 'object') return;
    const previous = current;
    current = { ...previous, ...payload, messages: Array.isArray(payload.messages) ? cleanMessages(payload.messages) : previous.messages, activities: Array.isArray(payload.activities) ? cleanActivities(payload.activities) : previous.activities };
    const groups = groupsFor(current.messages, current.activities);
    const selected = hasSelection(transcript);
    // A bounded reader may evict the selected message or its whole turn. Keep
    // the entire existing DOM until selection ends; never detach its Range.
    if (selected && selectionWouldBeRemoved(groups)) { selectionRefresh = true; return; }
    const follow = initialRender || (atBottom() && !selected);
    const anchor = follow ? null : readingAnchor();
    const scroll = (document.scrollingElement || document.documentElement).scrollTop;
    document.documentElement.dataset.theme = current.theme === 'light' ? 'light' : 'dark';
    const activeMessages = new Set(), activeGroups = new Set(), activeActivities = new Set();
    const nodes = groups.map(group => {
      activeGroups.add(group.key);
      const container = groupNodes.get(group.key) || create('section', 'turn'); groupNodes.set(group.key, container);
      const items = group.messages.map(row => {
        activeMessages.add(row.key);
        const node = messageNodes.get(row.key) || makeMessage(row); messageNodes.set(row.key, node); updateMessage(node, row); return node;
      });
      if (group.activities.length) {
        activeActivities.add(group.key);
        const details = activityNodes.get(group.key) || makeActivities(); activityNodes.set(group.key, details); updateActivities(details, group.activities);
        const finalIndex = group.messages.findIndex(row => row.role === 'assistant' && row.phase === 'final_answer');
        items.splice(finalIndex < 0 ? items.length : finalIndex, 0, details);
      }
      reconcile(container, items); return container;
    });
    reconcile(transcript, nodes);
    for (const [id] of messageNodes) if (!activeMessages.has(id)) messageNodes.delete(id);
    for (const [id] of groupNodes) if (!activeGroups.has(id)) groupNodes.delete(id);
    for (const [id] of activityNodes) if (!activeActivities.has(id)) activityNodes.delete(id);
    showEmpty(current, current.messages.length > 0);
    initialRender = current.messages.length === 0;
    if (follow) jumpLatest(); else restoreReadingAnchor(anchor, scroll);
    requestAnimationFrame(() => { if (follow && !hasSelection(transcript)) jumpLatest(); updateJump(); });
  }

  function showCopied(button) {
    const oldTitle = button.title;
    button.title = '已复制'; button.setAttribute('aria-label', '已复制');
    const label = button.querySelector('span'); if (label) label.textContent = '已复制';
    window.setTimeout(() => { button.title = oldTitle; button.setAttribute('aria-label', oldTitle || '复制代码'); if (label) label.textContent = '复制'; }, 1400);
  }
  function activateLink(link) {
    const url = markdown.safeURL(link.dataset.url);
    if (!url) return;
    if (url[0] === '#') {
      const target = document.getElementById(url.slice(1));
      if (target) (document.scrollingElement || document.documentElement).scrollTop = target.getBoundingClientRect().top + window.scrollY - 16;
      return;
    }
    post({ action: 'openLink', url });
  }
  document.addEventListener('click', event => {
    const link = event.target.closest('a');
    if (link) { event.preventDefault(); activateLink(link); return; }
    const button = event.target.closest('button');
    if (!button) return;
    const action = button.dataset.action;
    if (action === 'copy-code') {
      const code = button.closest('.code-block').querySelector('pre code');
      post({ action: 'copy', text: code.textContent }); showCopied(button);
    } else if (action === 'copy-message') {
      post({ action: 'copy', text: button.closest('.message').copyText || '' }); showCopied(button);
    } else if (action === 'open-attachment' && typeof button.attachmentPath === 'string') {
      post({ action: 'openAttachment', path: button.attachmentPath });
    }
  });
  document.addEventListener('keydown', event => {
    if ((event.key === 'Enter' || event.key === ' ') && event.target.matches('a[data-url]')) { event.preventDefault(); activateLink(event.target); }
  });
  document.addEventListener('selectionchange', () => {
    if (!selectionRefresh || hasSelection(transcript) || updateQueued) return;
    updateQueued = true;
    requestAnimationFrame(() => { updateQueued = false; selectionRefresh = false; update(current); });
  });
  window.addEventListener('scroll', updateJump, { passive: true });
  window.addEventListener('resize', updateJump);
  jumpButton.addEventListener('click', jumpLatest);
  window.VibeCueTranscript = Object.freeze({ update, jumpLatest, inspect: () => ({ messages: messageNodes.size, turns: groupNodes.size, activityGroups: activityNodes.size, theme: document.documentElement.dataset.theme, atBottom: atBottom(), jumpVisible: !jumpButton.hidden }) });
  post({ action: 'ready' });
})();
