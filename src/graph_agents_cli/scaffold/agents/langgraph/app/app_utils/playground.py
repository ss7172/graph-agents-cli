# Copyright 2026 graph-agents-cli contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The dev-only chat page served at `/playground` when `APP_ENV=dev`.

Plain HTML and JS, no build step. It talks to `POST /chat` with the bearer key
typed into the page, through the same policy adapter every client uses, and
renders the SSE events as they arrive. A run that pauses for the approval of a
gated API call (`message.end` status `awaiting_approval`, or a 409
`approval_pending`) shows each approval (the call, the model's stated reason,
the approvers, the expiry) with Approve and Reject buttons; a decision is
`POST /threads/{thread_id}/approvals/{approval_id}` with the same key, and the
resumed run streams into the page. The server decides whether this key may
decide (403 otherwise). Every value is shown as text, never as HTML.
"""

PLAYGROUND_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Playground</title>
<style>
  :root { --bg: #f6f7f9; --fg: #1a1c1f; --muted: #6b7280; --card: #ffffff; --line: #e5e7eb; --accent: #2563eb; --tool: #fff7ed; --err: #fee2e2; --warn: #fef9c3; --ok: #16a34a; --no: #dc2626; }
  @media (prefers-color-scheme: dark) { :root { --bg: #0f1115; --fg: #e5e7eb; --muted: #9ca3af; --card: #181b21; --line: #2a2f3a; --accent: #60a5fa; --tool: #2a2216; --err: #3b1d1d; --warn: #3a3514; --ok: #22c55e; --no: #f87171; } }
  * { box-sizing: border-box; }
  body { margin: 0; font: 15px/1.5 system-ui, -apple-system, Segoe UI, sans-serif; background: var(--bg); color: var(--fg); }
  header { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; padding: 12px 16px; border-bottom: 1px solid var(--line); background: var(--card); }
  header h1 { font-size: 16px; margin: 0 auto 0 0; }
  header label { color: var(--muted); font-size: 13px; }
  header input { font: inherit; padding: 6px 8px; border: 1px solid var(--line); border-radius: 6px; background: var(--bg); color: var(--fg); min-width: 220px; }
  main { max-width: 860px; margin: 0 auto; padding: 16px; }
  #log { display: flex; flex-direction: column; gap: 10px; min-height: 50vh; }
  .msg { padding: 10px 14px; border-radius: 10px; background: var(--card); border: 1px solid var(--line); white-space: pre-wrap; word-break: break-word; }
  .user { border-color: var(--accent); }
  .tool { background: var(--tool); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }
  .error { background: var(--err); }
  .approval { background: var(--warn); white-space: normal; }
  .approval pre { margin: 6px 0; white-space: pre-wrap; word-break: break-word; font: 13px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; }
  .approval .row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 8px; }
  .approval input { flex: 1; min-width: 160px; font: inherit; padding: 6px 8px; border: 1px solid var(--line); border-radius: 6px; background: var(--card); color: var(--fg); }
  .approval .approve { background: var(--ok); }
  .approval .reject { background: var(--no); }
  .approval button { padding: 6px 14px; }
  .meta { color: var(--muted); font-size: 12px; }
  form { display: flex; gap: 8px; margin-top: 16px; }
  textarea { flex: 1; font: inherit; padding: 10px; border: 1px solid var(--line); border-radius: 8px; background: var(--card); color: var(--fg); resize: vertical; min-height: 56px; }
  button { font: inherit; padding: 0 18px; border: 0; border-radius: 8px; background: var(--accent); color: white; cursor: pointer; }
  button[disabled] { opacity: .5; cursor: default; }
  footer { color: var(--muted); font-size: 12px; margin-top: 12px; }
</style>
</head>
<body>
<header>
  <h1>Playground</h1>
  <label>Bearer key <input id="key" type="password" placeholder="API_KEY" autocomplete="off"></label>
  <label>Thread <input id="thread" placeholder="new thread" readonly></label>
  <button id="reset" type="button">New thread</button>
</header>
<main>
  <div id="log"></div>
  <form id="form">
    <textarea id="input" placeholder="Ask something... (Enter to send, Shift+Enter for a new line)" required></textarea>
    <button id="send" type="submit">Send</button>
  </form>
  <footer>Dev only: this page exists because APP_ENV=dev. It calls POST /chat with the key above, exactly as graph-agents-cli run and eval generate do. Actions that need approval show Approve and Reject; the server decides whether this key may decide.</footer>
</main>
<script>
(function () {
  var log = document.getElementById('log');
  var keyInput = document.getElementById('key');
  var threadInput = document.getElementById('thread');
  var input = document.getElementById('input');
  var send = document.getElementById('send');
  var form = document.getElementById('form');
  try { keyInput.value = localStorage.getItem('playground.key') || ''; } catch (e) {}
  keyInput.addEventListener('change', function () { try { localStorage.setItem('playground.key', keyInput.value); } catch (e) {} });
  document.getElementById('reset').addEventListener('click', function () { threadInput.value = ''; log.innerHTML = ''; });

  function add(cls, text) {
    var div = document.createElement('div');
    div.className = 'msg ' + cls;
    div.textContent = text;
    log.appendChild(div);
    div.scrollIntoView({ block: 'end' });
    return div;
  }

  function el(tag, text, cls) {
    var node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  }

  function headers() {
    return { 'Content-Type': 'application/json', 'Accept': 'text/event-stream', 'Authorization': 'Bearer ' + keyInput.value };
  }

  function parseSSE(buffer, onEvent) {
    var parts = buffer.split('\\n\\n');
    var rest = parts.pop();
    parts.forEach(function (block) {
      var event = 'message', data = '';
      block.split('\\n').forEach(function (line) {
        if (line.indexOf('event:') === 0) event = line.slice(6).trim();
        else if (line.indexOf('data:') === 0) data += line.slice(5).trim();
      });
      if (data) { try { onEvent(event, JSON.parse(data)); } catch (e) { onEvent('error', { code: 'parse', message: data }); } }
    });
    return rest;
  }

  function showApproval(approval) {
    var card = el('div', null, 'msg approval');
    card.appendChild(el('strong', 'Approval needed: ' + approval.method + ' ' + approval.path));
    var facts = [];
    if (approval.operation_id) facts.push('operation ' + approval.operation_id);
    facts.push('API ' + approval.api);
    facts.push('approvers ' + (approval.approvers || []).join(', '));
    facts.push('expires ' + approval.expires_at);
    card.appendChild(el('div', facts.join(' | '), 'meta'));
    if (approval.reason) card.appendChild(el('div', 'Stated reason: ' + approval.reason));
    if (approval.query && Object.keys(approval.query).length) card.appendChild(el('pre', 'query ' + JSON.stringify(approval.query, null, 2)));
    if (approval.body !== undefined && approval.body !== null) card.appendChild(el('pre', 'body ' + JSON.stringify(approval.body, null, 2)));
    var row = el('div', null, 'row');
    var comment = el('input');
    comment.placeholder = 'comment (optional)';
    var approve = el('button', 'Approve', 'approve');
    var reject = el('button', 'Reject', 'reject');
    approve.type = reject.type = 'button';
    row.appendChild(comment); row.appendChild(approve); row.appendChild(reject);
    card.appendChild(row);
    function decide(decision) {
      approve.disabled = reject.disabled = comment.disabled = true;
      var body = { decision: decision };
      if (comment.value.trim()) body.comment = comment.value.trim();
      var url = '/threads/' + encodeURIComponent(approval.thread_id) + '/approvals/' + encodeURIComponent(approval.approval_id);
      // Said once the server has accepted the decision; a refusal (403, 409, 410) shows its error instead.
      var note = el('div', (decision === 'approve' ? 'Approving' : 'Rejecting') + '...', 'meta');
      card.appendChild(note);
      run(url, body).then(function (ok) {
        if (ok) { note.textContent = (decision === 'approve' ? 'Approved' : 'Rejected') + ' by you.'; return; }
        note.remove();
        approve.disabled = reject.disabled = comment.disabled = false;
      });
    }
    approve.addEventListener('click', function () { decide('approve'); });
    reject.addEventListener('click', function () { decide('reject'); });
    log.appendChild(card);
    card.scrollIntoView({ block: 'end' });
  }

  async function run(url, body) {
    send.disabled = true;
    var reply = add('assistant', '');
    try {
      var res = await fetch(url, { method: 'POST', headers: headers(), body: JSON.stringify(body) });
      if (!res.ok) {
        var text = await res.text();
        var detail = null;
        try { detail = JSON.parse(text); } catch (e) {}
        reply.className = 'msg error';
        reply.textContent = 'HTTP ' + res.status + ': ' + (detail && detail.code ? detail.code + ': ' + detail.detail : text);
        if (detail && detail.code === 'approval_pending') { (detail.approvals || []).forEach(showApproval); }
        return false;
      }
      var reader = res.body.getReader();
      var decoder = new TextDecoder();
      var buffer = '';
      while (true) {
        var chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });
        buffer = parseSSE(buffer, function (event, data) {
          if (event === 'message.start') { threadInput.value = data.thread_id; }
          else if (event === 'message.delta') { reply.textContent += data.text; }
          else if (event === 'tool.call') { add('tool', 'tool.call ' + data.name + '(' + JSON.stringify(data.args || {}) + ')'); }
          else if (event === 'tool.result') { add('tool', 'tool.result ' + data.name + (data.is_error ? ' [error]' : '') + ': ' + data.result); }
          else if (event === 'message.end') {
            add('meta', 'run ' + data.run_id + ' | ' + data.status + ' | ' + data.latency_ms + ' ms | tokens in ' + data.usage.input_tokens + ' out ' + data.usage.output_tokens);
            if (data.status === 'awaiting_approval') { (data.approvals || [data.approval]).filter(Boolean).forEach(showApproval); }
          }
          else if (event === 'error') {
            add('error', data.code + ': ' + data.message + (data.detail ? '\\n' + data.detail : ''));
            if (data.code === 'approval_pending') { (data.approvals || []).forEach(showApproval); }
          }
        });
      }
      if (!reply.textContent) reply.remove();  // a run that only paused wrote no text
      return true;
    } catch (e) {
      add('error', String(e));
      return false;
    } finally {
      send.disabled = false;
      input.focus();
    }
  }

  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    var text = input.value.trim();
    if (!text) return;
    add('user', text);
    input.value = '';
    var body = { message: text, metadata: { source: 'playground' } };
    if (threadInput.value) body.thread_id = threadInput.value;
    run('/chat', body);
  });

  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
  });
})();
</script>
</body>
</html>
"""
