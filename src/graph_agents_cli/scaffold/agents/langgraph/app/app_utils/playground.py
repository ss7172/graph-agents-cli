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
renders the SSE events as they arrive.
"""

PLAYGROUND_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Playground</title>
<style>
  :root { --bg: #f6f7f9; --fg: #1a1c1f; --muted: #6b7280; --card: #ffffff; --line: #e5e7eb; --accent: #2563eb; --tool: #fff7ed; --err: #fee2e2; }
  @media (prefers-color-scheme: dark) { :root { --bg: #0f1115; --fg: #e5e7eb; --muted: #9ca3af; --card: #181b21; --line: #2a2f3a; --accent: #60a5fa; --tool: #2a2216; --err: #3b1d1d; } }
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
  <footer>Dev only: this page exists because APP_ENV=dev. It calls POST /chat with the key above, exactly as graph-agents-cli run and eval generate do.</footer>
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

  form.addEventListener('submit', async function (ev) {
    ev.preventDefault();
    var text = input.value.trim();
    if (!text) return;
    add('user', text);
    input.value = '';
    send.disabled = true;
    var reply = add('assistant', '');
    var body = { message: text, metadata: { source: 'playground' } };
    if (threadInput.value) body.thread_id = threadInput.value;
    try {
      var res = await fetch('/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream', 'Authorization': 'Bearer ' + keyInput.value },
        body: JSON.stringify(body)
      });
      if (!res.ok) { reply.className = 'msg error'; reply.textContent = 'HTTP ' + res.status + ': ' + (await res.text()); return; }
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
          else if (event === 'message.end') { add('meta', 'run ' + data.run_id + ' | ' + data.latency_ms + ' ms | tokens in ' + data.usage.input_tokens + ' out ' + data.usage.output_tokens); }
          else if (event === 'error') { add('error', data.code + ': ' + data.message + (data.detail ? '\\n' + data.detail : '')); }
        });
      }
    } catch (e) {
      add('error', String(e));
    } finally {
      send.disabled = false;
      input.focus();
    }
  });

  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
  });
})();
</script>
</body>
</html>
"""
