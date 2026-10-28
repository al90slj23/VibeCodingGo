#!/usr/bin/env node
/* Optional development-only DOM regression. Run with jsdom available through
 * NODE_PATH. The shipped app has no Node/jsdom dependency. No network is used. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM, VirtualConsole } = require('jsdom');
const root = path.resolve(__dirname, '../assets/Conversation');
const errors = [], bridge = [];
const virtualConsole = new VirtualConsole();
virtualConsole.on('jsdomError', error => errors.push(error.message));
const dom = new JSDOM(fs.readFileSync(path.join(root, 'index.html'), 'utf8'), {
  url: 'file:///private/tmp/Conversation/index.html', runScripts: 'outside-only', pretendToBeVisual: true, virtualConsole
});
const w = dom.window, d = w.document;
w.webkit = { messageHandlers: { vibecue: { postMessage: value => bridge.push(JSON.parse(JSON.stringify(value))) } } };
w.requestAnimationFrame = callback => { callback(); return 1; };
w.setTimeout = () => 1;
Object.defineProperty(w, 'innerHeight', { value: 600, configurable: true });
Object.defineProperty(d.documentElement, 'scrollHeight', { get: () => 2000, configurable: true });
for (const asset of ['vendor/markdown-it.min.js', 'vendor/highlight.min.js', 'markdown.js', 'transcript.js']) w.eval(fs.readFileSync(path.join(root, asset), 'utf8'));
let checks = 0;
function check(condition, message) { assert.ok(condition, message); checks++; }
const user = { id: 'user', turn_id: 'one', role: 'user', text: '请检查 **这段代码**。', phase: 'user', attachments: [{ name: '参考.png', path: '/private/tmp/reference.png', kind: 'image' }] };
const assistant = { id: 'reply', turn_id: 'one', role: 'assistant', phase: 'final_answer', text: '### 修改结果\n\n已完成 **检查**。\n\n```js\nconst value = "safe";\n```\n\n[说明](https://example.com/doc)\n\n| 左 | 右 |\n| :--- | ---: |\n| a | b |' };
const activities = [{ id: 'tool', turn_id: 'one', label: '已运行检查', status: 'complete' }];
const update = value => w.VibeCueTranscript.update(value);
check(bridge.length === 1 && bridge[0].action === 'ready', 'view signals readiness without input');
update({ messages: [user, assistant], activities, theme: 'dark', state: 'ready' });
check(d.querySelectorAll('.message').length === 2, 'one node per actual visible message');
check(d.querySelectorAll('h3').length === 1 && d.querySelectorAll('table').length === 1, 'headings and tables render');
check(d.querySelector('code.hljs.language-js .hljs-keyword') && d.querySelector('code.hljs.language-js').textContent === 'const value = "safe";\n', 'explicit code language has offline syntax colors while preserving exact code');
check(d.querySelectorAll('[style]').length === 0, 'table alignment does not violate strict style CSP');
check(d.querySelectorAll('a[href],img,iframe,object').length === 0, 'content never gets navigating or loading elements');
check(d.querySelector('.message-user').textContent.includes('这段代码'), 'user text renders without a name/timestamp label');
check(!d.querySelector('.message-assistant').textContent.includes('Codex'), 'assistant content does not get a QQ-style role heading');
const node = d.querySelector('[data-message-id="reply"]'), body = node.querySelector('.message-body');
const details = d.querySelector('details.activities');
check(details && !details.open && details.querySelector('li').textContent.includes('已运行检查'), 'tool activity is collapsed, separate from answer');
details.open = true;
update({ messages: [user, assistant], activities, theme: 'light' });
check(node === d.querySelector('[data-message-id="reply"]') && body === node.querySelector('.message-body'), 'unchanged live data preserves row and body identity');
check(details === d.querySelector('details.activities') && details.open, 'activity expansion is preserved');
check(d.documentElement.dataset.theme === 'light', 'native theme updates without rebuilding messages');

const copyCode = d.querySelector('.code-copy'); copyCode.click();
check(bridge.at(-1).action === 'copy' && bridge.at(-1).text === 'const value = "safe";\n', 'code copy sends exact raw code to native');
node.querySelector('.message-copy').click();
check(bridge.at(-1).action === 'copy' && bridge.at(-1).text === assistant.text, 'message copy sends original Markdown');
d.querySelector('a[data-url]').click();
check(bridge.at(-1).action === 'openLink' && bridge.at(-1).url === 'https://example.com/doc' && w.location.protocol === 'file:', 'link click delegates to native without navigation');
d.querySelector('.attachment').click();
check(bridge.at(-1).action === 'openAttachment' && bridge.at(-1).path === '/private/tmp/reference.png', 'explicit attachment click delegates its path to native');

const paragraph = body.querySelector('p'), originalBody = body.innerHTML;
const range = d.createRange(); range.selectNodeContents(paragraph); w.getSelection().addRange(range);
update({ messages: [user, { ...assistant, text: assistant.text + '\n\n新增的进展。' }] });
check(body.innerHTML === originalBody, 'stream refresh preserves a selected passage');
w.getSelection().removeAllRanges(); d.dispatchEvent(new w.Event('selectionchange'));
check(body.textContent.includes('新增的进展。'), 'deferred streaming text appears after selection ends');

d.documentElement.scrollTop = 120;
update({ messages: [user, assistant, { id: 'progress', role: 'assistant', phase: 'commentary', turn_id: 'one', text: '还有一项检查。' }] });
check(d.documentElement.scrollTop === 120 && w.VibeCueTranscript.inspect().jumpVisible, 'reading older text preserves viewport and exposes jump to latest');
d.getElementById('jump-latest').click();
check(d.documentElement.scrollTop === 2000 && !w.VibeCueTranscript.inspect().jumpVisible, 'jump control follows the latest content');

update({ messages: [{ id: 'same', role: 'assistant', turn_id: 'first', text: '重复的真实回答' }, { id: 'same', role: 'assistant', turn_id: 'second', text: '重复的真实回答' }], activities: [] });
check(d.querySelectorAll('.message').length === 2, 'same source ID in distinct turns is not lost');

const selectedOld = d.querySelector('.message'), selectedText = selectedOld.textContent;
const oldRange = d.createRange(); oldRange.selectNodeContents(selectedOld.querySelector('.message-body')); w.getSelection().addRange(oldRange);
const beforeTrim = Array.from(d.querySelectorAll('.message'));
update({ messages: [{ id: 'new-tail', role: 'assistant', turn_id: 'third', text: '后台读取到的新尾部。' }], activities: [], theme: 'dark' });
check(selectedOld.isConnected && d.querySelectorAll('.message').length === beforeTrim.length && w.getSelection().toString().includes('重复的真实回答'), 'evicting a selected message or whole turn defers the entire DOM update');
check(!d.querySelector('[data-message-id="new-tail"]') && selectedOld.textContent === selectedText, 'new tail does not silently detach the selected reading range');
w.getSelection().removeAllRanges(); d.dispatchEvent(new w.Event('selectionchange'));
check(!selectedOld.isConnected && d.querySelector('[data-message-id="new-tail"]'), 'deferred tail eviction applies only after selection ends');

const readingMessages = [
  { id: 'older', role: 'assistant', turn_id: 'old-turn', text: '之前的轮次。' },
  { id: 'anchor', role: 'assistant', turn_id: 'reading-turn', text: '正在阅读的段落。' },
  { id: 'tail', role: 'assistant', turn_id: 'tail-turn', text: '后面的段落。' }
];
update({ messages: readingMessages });
const messageRect = function () {
  const index = Array.from(d.querySelectorAll('.message')).indexOf(this), top = index * 400 - d.documentElement.scrollTop;
  return { top, bottom: top + 400, left: 0, right: 768, width: 768, height: 400 };
};
for (const message of d.querySelectorAll('.message')) message.getBoundingClientRect = messageRect;
d.documentElement.scrollTop = 450;
const readingNode = d.querySelector('[data-message-id="anchor"]'), relativeTop = readingNode.getBoundingClientRect().top;
update({ messages: readingMessages.slice(1) });
check(readingNode === d.querySelector('[data-message-id="anchor"]') && readingNode.getBoundingClientRect().top === relativeTop && d.documentElement.scrollTop === 50, 'removing an earlier turn restores the visible message ID and relative viewport offset');
const appended = { id: 'extra', role: 'assistant', turn_id: 'extra-turn', text: '新消息。' };
update({ messages: [...readingMessages.slice(1), appended] });
check(readingNode.getBoundingClientRect().top === relativeTop && d.documentElement.scrollTop === 50, 'appending streamed content keeps the same reading anchor');

update({ messages: [{ id: 'attack', role: 'assistant', text: '<script>window.pwned=1</script>\n\n![secret](https://example.com/secret)\n\n[bad](javascript:alert(1))' }] });
check(!w.pwned && d.querySelectorAll('#transcript script,#transcript img,#transcript a[data-url]').length === 0, 'untrusted message HTML and image/link payloads remain inert');
update({ messages: [], activities: [], state: 'error', message: '日志暂不可读', detail: '可以稍后刷新。' });
check(!d.getElementById('empty-state').hidden && d.getElementById('empty-title').textContent === '日志暂不可读' && w.VibeCueTranscript.inspect().messages === 0, 'controlled empty/error state replaces stale content only when native asks');
check(errors.length === 0, 'no JavaScript or DOM console errors');
console.log(JSON.stringify({ checks, passed: true, errors }));
dom.window.close();
