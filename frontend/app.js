/* ════════════════════════════════════════════════════════════
   Football AI Management System — Frontend JavaScript
   ════════════════════════════════════════════════════════════ */

'use strict';

// ── Agent display config ───────────────────────────────────────────────────────
const AGENT_CONFIG = {
  planner:    { icon: '🧠', label: 'Planner',   pipeId: 'pipe-planner' },
  financial:  { icon: '💰', label: 'Financial', pipeId: 'pipe-financial' },
  scouter:    { icon: '🔍', label: 'Scouter',   pipeId: 'pipe-scouter' },
  analysis:   { icon: '📊', label: 'Analysis',  pipeId: 'pipe-analysis' },
  tactical:   { icon: '⚔️', label: 'Tactical',  pipeId: 'pipe-tactical' },
  pdf_generator: { icon: '📄', label: 'PDF',    pipeId: 'pipe-pdf' },
};

// ── State ─────────────────────────────────────────────────────────────────────
let currentRunId = null;
let eventSource  = null;

// ── UI Helpers ────────────────────────────────────────────────────────────────

function setQuery(btn) {
  document.getElementById('queryInput').value = btn.textContent.trim();
}

function formatTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString('en-GB', {
    hour: '2-digit', minute: '2-digit', second: '2-digit'
  });
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.appendChild(document.createTextNode(String(str)));
  return div.innerHTML;
}

function clearActivity() {
  const feed = document.getElementById('activityFeed');
  feed.innerHTML = `
    <div class="feed-placeholder">
      <div class="feed-placeholder-icon">🤖</div>
      <div class="feed-placeholder-text">Run a scouting query to see the agents in action</div>
    </div>`;
  document.getElementById('downloadPanel').style.display = 'none';
  document.getElementById('debatePanel').style.display = 'none';
  const chatContainer = document.getElementById('debateChatContainer');
  if (chatContainer) chatContainer.innerHTML = '';
  const dirInput = document.getElementById('directiveInput');
  if (dirInput) { dirInput.value = ''; dirInput.disabled = false; }
  document.getElementById('clearBtn').style.display = 'none';
  document.getElementById('runIdDisplay').style.display = 'none';
  currentRunId = null;

  // Reset all pipeline agents
  Object.values(AGENT_CONFIG).forEach(cfg => {
    const el = document.getElementById(cfg.pipeId);
    if (el) el.className = el.className.replace(/\b(active|done|error)\b/g, '').trim();
  });
}

// ── Feed Event Renderer ───────────────────────────────────────────────────────

function renderEvent(event) {
  const feed = document.getElementById('activityFeed');

  // Remove placeholder if still there
  const placeholder = feed.querySelector('.feed-placeholder');
  if (placeholder) placeholder.remove();

  const agent = event.agent || 'system';
  const cfg   = AGENT_CONFIG[agent] || { icon: '🤖', label: agent };
  const type  = event.type || 'progress';

  const badgeClass = {
    agent_start:    'badge-start',
    progress:       'badge-progress',
    agent_complete: 'badge-complete',
    error:          'badge-error',
    connected:      'badge-system',
    pipeline_complete: 'badge-complete',
    pipeline_error: 'badge-error',
    stream_end:     'badge-system',
  }[type] || 'badge-progress';

  const badgeLabel = {
    agent_start:    'Starting',
    progress:       'Running',
    agent_complete: 'Done ✓',
    error:          'Error',
    connected:      'Connected',
    pipeline_complete: 'Complete ✓',
    pipeline_error: 'Failed',
    stream_end:     'Stream End',
  }[type] || type;

  // Format optional data preview
  let dataHtml = '';
  if (event.data && typeof event.data === 'object') {
    const preview = JSON.stringify(event.data, null, 2);
    if (preview.length < 2000) {
      dataHtml = `<div class="event-data">${escapeHtml(preview)}</div>`;
    }
  } else if (event.error) {
    dataHtml = `<div class="event-data" style="color: var(--red);">${escapeHtml(event.error)}</div>`;
  }

  const ts = event.timestamp ? formatTime(event.timestamp) : new Date().toLocaleTimeString();

  const el = document.createElement('div');
  el.className = 'feed-event';
  el.innerHTML = `
    <div class="event-agent-icon">${cfg.icon}</div>
    <div class="event-body">
      <div class="event-header">
        <span class="event-agent-name">${cfg.label || agent}</span>
        <span class="event-type-badge ${badgeClass}">${badgeLabel}</span>
        <span class="event-timestamp">${ts}</span>
      </div>
      <div class="event-message">${escapeHtml(event.message || '')}</div>
      ${dataHtml}
    </div>`;

  feed.appendChild(el);
  feed.scrollTop = feed.scrollHeight;
}

// ── Pipeline Status Updater ───────────────────────────────────────────────────

function updatePipelineAgent(agentName, status) {
  // status: 'active' | 'done' | 'error'
  const cfg = AGENT_CONFIG[agentName];
  if (!cfg?.pipeId) return;
  const el = document.getElementById(cfg.pipeId);
  if (!el) return;

  el.classList.remove('active', 'done', 'error');
  if (status) el.classList.add(status);
}

// ── Main Scouting Function ────────────────────────────────────────────────────

async function startScouting() {
  const query = document.getElementById('queryInput').value.trim();
  if (!query) {
    document.getElementById('queryInput').focus();
    return;
  }

  // Close any existing stream
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }

  // UI reset & loading state
  clearActivity();
  const scoutBtn = document.getElementById('scoutBtn');
  scoutBtn.disabled = true;
  scoutBtn.classList.add('running');
  scoutBtn.querySelector('.btn-icon').textContent = '⏳';
  scoutBtn.querySelector('.btn-text').textContent = 'Pipeline Running...';

  document.getElementById('clearBtn').style.display = 'inline-flex';

  // POST to /api/scout — we use fetch + ReadableStream for SSE
  let response;
  try {
    response = await fetch('/api/scout', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query }),
    });
  } catch (err) {
    renderEvent({
      type: 'error',
      agent: 'system',
      message: `Failed to connect to API: ${err.message}`,
      timestamp: Date.now() / 1000,
    });
    resetScoutBtn();
    return;
  }

  if (!response.ok) {
    renderEvent({
      type: 'error',
      agent: 'system',
      message: `API error ${response.status}: ${response.statusText}`,
      timestamp: Date.now() / 1000,
    });
    resetScoutBtn();
    return;
  }

  readSSEStream(response).then(() => {
    // Stream complete
  });
}

// Extract the SSE reading logic into a helper so we can reuse it
async function readSSEStream(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop(); // Keep incomplete last line

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const jsonStr = line.slice(6).trim();
        if (!jsonStr) continue;

        let event;
        try { event = JSON.parse(jsonStr); }
        catch { continue; }

        handleEvent(event);
      }
    }
  } catch (err) {
    renderEvent({
      type: 'error',
      agent: 'system',
      message: `Stream error: ${err.message}`,
      timestamp: Date.now() / 1000,
    });
  }
}

// ── Event Handler ─────────────────────────────────────────────────────────────

function handleEvent(event) {
  const type = event.type;

  // Update run ID display
  if (event.run_id && !currentRunId) {
    currentRunId = event.run_id;
    const runDisplay = document.getElementById('runIdDisplay');
    runDisplay.textContent = `Run: ${event.run_id.slice(0, 8)}`;
    runDisplay.style.display = 'inline-flex';
  }

  // Update pipeline agent status
  if (event.agent && type === 'agent_start') {
    updatePipelineAgent(event.agent, 'active');
  } else if (event.agent && type === 'agent_complete') {
    updatePipelineAgent(event.agent, 'done');
  } else if (event.agent && type === 'error') {
    updatePipelineAgent(event.agent, 'error');
  }

  // Render feed event
  renderEvent(event);

  // Handle Debate states
  if (type === 'draft_ready' || type === 'revision_ready') {
    appendChatBubble('system', `I've found the following candidates based on your requirements:<div class="chat-shortlist">${JSON.stringify(event.ranked_players, null, 2)}</div>`);
  } else if (type === 'chat_message') {
    appendChatBubble(event.sender || 'system', event.message);
  } else if (type === 'awaiting_review') {
    document.getElementById('debatePanel').style.display = 'block';
    document.getElementById('debatePanel').scrollIntoView({ behavior: 'smooth', block: 'center' });
    resetScoutBtn();
    document.getElementById('directiveBtn').disabled = false;
    document.getElementById('directiveInput').disabled = false;
  } else if (type === 'finalizing_start') {
    appendChatBubble('system', `Understood. Finalizing report for: ${event.selected ? event.selected.join(', ') : 'selected players'}...`);
    document.getElementById('directiveBtn').disabled = true;
    document.getElementById('directiveInput').disabled = true;
  } else if (type === 'final_ready') {
    showDownloadPanel(event);
  }

  // Pipeline complete → show download panel
  if (type === 'pipeline_complete' && event.run_id) {
    showDownloadPanel(event);
  }

  // Stream end
  if (type === 'stream_end' || type === 'timeout') {
    resetScoutBtn();
  }
}

// ── Debate Handlers ───────────────────────────────────────────────────────────

function appendChatBubble(role, htmlContent) {
  const container = document.getElementById('debateChatContainer');
  if (!container) return;
  const bubble = document.createElement('div');
  bubble.className = `chat-bubble ${role}`;
  bubble.innerHTML = htmlContent;
  container.appendChild(bubble);
  container.scrollTop = container.scrollHeight;
}

async function sendDirective() {
  const directiveInput = document.getElementById('directiveInput');
  const message = directiveInput.value.trim();
  if (!message || !currentRunId) return;

  // Add bubble to history
  appendChatBubble('user', message);

  directiveInput.value = '';
  document.getElementById('directiveBtn').disabled = true;
  document.getElementById('directiveInput').disabled = true;

  try {
    const response = await fetch(`/api/scout/${currentRunId}/directive`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message }),
    });
    if (!response.ok) throw new Error(response.statusText);
    readSSEStream(response);
  } catch (err) {
    renderEvent({ type: 'error', message: `Directive error: ${err.message}`, agent: 'system' });
    document.getElementById('directiveBtn').disabled = false;
    document.getElementById('directiveInput').disabled = false;
  }
}

// ── Download Panel ────────────────────────────────────────────────────────────

function showDownloadPanel(event) {
  const panel = document.getElementById('downloadPanel');
  const btn   = document.getElementById('downloadBtn');
  const sub   = document.getElementById('downloadSubtitle');

  const runId  = event.run_id;
  const count  = event.ranked_count || '3+';

  btn.href = `/api/download/${runId}`;
  sub.textContent = `${count} players ranked · Scouting report with charts and tactical analysis ready.`;
  panel.style.display = 'flex';

  panel.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

// ── Reset Button ──────────────────────────────────────────────────────────────

function resetScoutBtn() {
  const scoutBtn = document.getElementById('scoutBtn');
  scoutBtn.disabled = false;
  scoutBtn.classList.remove('running');
  scoutBtn.querySelector('.btn-icon').textContent = '🚀';
  scoutBtn.querySelector('.btn-text').textContent = 'Run Scouting Pipeline';
}

// ── Keyboard Shortcut: Ctrl+Enter to submit ───────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('queryInput').addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      startScouting();
    }
  });
});
