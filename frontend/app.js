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

// The example buttons carry a short label but a full brief in data-q, so the
// chips stay readable without shipping a half-written query to the planner.
function setQuery(btn) {
  const input = document.getElementById('queryInput');
  input.value = btn.dataset.q || btn.textContent.trim();
  input.focus();
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

function formatMessageWithCards(msg) {
  if (!msg) return '';
  
  const jsonStart = msg.indexOf('[');
  const jsonEnd = msg.lastIndexOf(']');
  if (jsonStart !== -1 && jsonEnd !== -1 && jsonEnd > jsonStart) {
    try {
      const potentialJson = msg.substring(jsonStart, jsonEnd + 1);
      const parsed = JSON.parse(potentialJson);
      if (Array.isArray(parsed) && parsed.length > 0 && (parsed[0].rank !== undefined || parsed[0].tactical_fit_score !== undefined || parsed[0].name !== undefined)) {
        let textBefore = msg.substring(0, jsonStart).replace(/```json\s*/g, '').replace(/```\s*/g, '');
        let textAfter = msg.substring(jsonEnd + 1).replace(/```\s*/g, '');
        return escapeHtml(textBefore) + `<div class="chat-shortlist" style="margin-top: 12px; margin-bottom: 12px;">${renderPlayerCardsHtml(parsed)}</div>` + escapeHtml(textAfter);
      }
    } catch (e) {
      // Fallback
    }
  }
  
  return escapeHtml(msg);
}

function clearActivity() {
  const feed = document.getElementById('activityFeed');
  feed.innerHTML = `
    <div class="feed-placeholder">
      <div class="feed-placeholder-icon">⚽</div>
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
    if (Array.isArray(event.data) && event.data.length > 0 && (event.data[0].rank !== undefined || event.data[0].tactical_fit_score !== undefined || event.data[0].name !== undefined)) {
      dataHtml = `<div style="margin-top: 12px;">${renderPlayerCardsHtml(event.data)}</div>`;
    } else {
      const preview = JSON.stringify(event.data, null, 2);
      if (preview.length < 2000) {
        dataHtml = `<div class="event-data">${escapeHtml(preview)}</div>`;
      }
    }
  } else if (event.error) {
    dataHtml = `<div class="event-data" style="color: var(--bad);">${escapeHtml(event.error)}</div>`;
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
      <div class="event-message">${formatMessageWithCards(
        // A failure carries its detail in event.error and often no message at
        // all — without a fallback the row renders as a blank line.
        event.message || (event.error ? 'The agent stopped with an error.' : '')
      )}</div>
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
    const club = event.financial_decision?.team_name || '';
    const funnel = renderFunnelHtml(event.selection_funnel, club);
    const lead = (event.ranked_players || []).length
      ? "I've found the following candidates based on your requirements:"
      : 'No candidate met this brief.';
    appendChatBubble('system',
      `${lead}${funnel}<div class="chat-shortlist">${renderPlayerCardsHtml(event.ranked_players)}</div>`);
  } else if (type === 'chat_message') {
    appendChatBubble(event.sender || 'system', formatMessageWithCards(event.message));
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
  scoutBtn.querySelector('.btn-icon').textContent = '⚽';
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

// ── Player Card Renderer ──────────────────────────────────────────────────────

// ── Formatting helpers ────────────────────────────────────────────────────────
// These mirror the PDF report's rules on purpose. A metric that was never
// measured must read as "—", never as 0 — a real zero and a missing measurement
// must not look identical.

function metricCell(rm, key, digits = 2) {
  const m = rm?.[key];
  if (!m || m.per90 === undefined || m.per90 === null) {
    return '<span class="na" title="not measured">—</span>';
  }
  return Number(m.per90).toFixed(digits);
}

function money(v) {
  if (!v) return '—';
  return v >= 1_000_000
    ? `€${Math.round(v / 1_000_000)}M`
    : `€${Number(v).toLocaleString()}`;
}

function wageText(p) {
  if (p.wage_low && p.wage_high) {
    return `€${Number(p.wage_low).toLocaleString()}–€${Number(p.wage_high).toLocaleString()}/wk`;
  }
  return p.wage_eur ? `€${Number(p.wage_eur).toLocaleString()}/wk` : '—';
}

// Wage sites the scouter is allowed to quote. A figure from one of them is a
// published number, but only the offline Capology cache (wage_estimated false)
// is contract data — the web cross-check can still misattribute.
const WAGE_SITES = ['capology.com', 'salarysport.com', 'spotrac.com', 'footballbenchmark.com'];

function wageBasis(p) {
  if (!p.wage_eur) return 'not available';
  if (p.wage_estimated === false) return 'contract data';
  if (WAGE_SITES.includes(p.wage_source)) return `published figure · ${p.wage_source}`;
  if (p.wage_source === 'market_value') return 'modelled from market value';
  return 'estimated from league, minutes and age';
}

// Explains a short shortlist instead of leaving the reader to guess.
function renderFunnelHtml(fn, club) {
  if (!fn || !fn.cohort) return '';
  const bits = [];
  if (fn.already_at_club) bits.push(`${fn.already_at_club} already at ${escapeHtml(club || 'the club')}`);
  if (fn.wrong_primary_position) bits.push(`${fn.wrong_primary_position} play the position only as a secondary role`);
  if (fn.over_fee_ceiling) {
    bits.push(`<b>${fn.over_fee_ceiling} priced above the fee ceiling</b>` +
      (fn.cheapest_excluded_fee_eur ? ` (cheapest excluded: ${money(fn.cheapest_excluded_fee_eur)})` : ''));
  }
  if (fn.over_wage_ceiling) {
    bits.push(`<b>${fn.over_wage_ceiling} above the wage ceiling</b>` +
      (fn.lowest_excluded_wage_eur ? ` (lowest excluded: €${Number(fn.lowest_excluded_wage_eur).toLocaleString()}/wk)` : ''));
  }
  if (fn.over_wage_on_confirmation) bits.push(`${fn.over_wage_on_confirmation} dropped once a confirmed wage exceeded the ceiling`);

  // A failed selection call is not accounted for by any filter above. Saying so
  // beats blaming the budget for a shortlist the budget did not produce.
  if (fn.selection_failed) {
    const fallback = fn.selection_fallback || 0;
    return `<div class="funnel-note">
      <b>Candidate selection failed${fn.selection_error ? ` (${escapeHtml(fn.selection_error)})` : ''}.</b>
      ${fallback
        ? `These ${fallback} come from the deterministic fit ranking over ${fn.cohort} players,
           which is how the shortlist is ordered anyway — but without the model's
           scout note. Re-run for the written assessment.`
        : `${fn.cohort} players were in the cohort and the budget filters were applied,
           but the selection step did not return. This is a run failure, not a
           verdict on the brief — try again.`}
    </div>`;
  }

  if (!bits.length) return '';
  const n = fn.shortlisted ?? 0;
  return `<div class="funnel-note">
    <b>${n === 0 ? 'No candidate met this brief.' : `Only ${n} candidate${n === 1 ? '' : 's'} met this brief.`}</b>
    Of ${fn.cohort} players considered: ${bits.join('; ')}; leaving ${n}.
    Raising whichever ceiling did most of the cutting is the fastest way to widen the field.
  </div>`;
}

function renderPlayerCardsHtml(players) {
  if (!players || players.length === 0) return '<p>No players found.</p>';

  let html = '<div class="player-cards-container">';
  players.forEach((p, idx) => {
    const rm = p.raw_metrics || {};

    // demonstrated_fit is the ranking number: it counts requirements that could
    // not be measured as unmet. measurable_fit normalises over measured traits
    // only, so it runs higher and would disagree with the ranking and the PDF.
    const rawFit = p.demonstrated_fit ?? p.tactical_fit_score ?? p.measurable_fit;
    const hasFit = rawFit !== undefined && rawFit !== null;
    const fitLabel = hasFit ? `Fit ${Math.round(rawFit)}/100` : 'Insufficient data';

    const cov = p.trait_coverage;
    const conf = p.confidence;
    const chips = [];
    if (cov !== undefined && cov !== null) {
      const cls = cov >= 0.6 ? 'good' : cov >= 0.4 ? 'warn' : 'bad';
      chips.push(`<span class="chip ${cls}">${Math.round(cov * 100)}% of requirement measured</span>`);
    }
    if (conf !== undefined && conf !== null) {
      const cls = conf >= 0.6 ? 'good' : conf >= 0.35 ? 'warn' : 'bad';
      chips.push(`<span class="chip ${cls}">confidence ${Math.round(conf * 100)}%</span>`);
    }
    if (p.used_position_key) {
      chips.push(`<span class="chip">scored as ${escapeHtml(p.used_position_key.replace(/_/g, ' '))}</span>`);
    }
    if (p.contract_expires) {
      chips.push(`<span class="chip">contract to ${escapeHtml(p.contract_expires)}</span>`);
    }

    const gaps = (p.qualitative_gaps || []).map(g => g.trait);
    const scored = (p.per_trait_breakdown || []);

    html += `
      <details class="player-card">
        <summary class="player-card-header">
          <div class="player-card-title">
            <span class="player-rank">#${p.rank || idx + 1}</span>
            <span class="player-name">${escapeHtml(p.name || 'Unknown')}</span>
            <span class="player-club">${escapeHtml(p.squad || 'Unknown')} (${p.age || '?'})</span>
          </div>
          <div class="player-card-score">${fitLabel}</div>
        </summary>
        <div class="player-card-body">
          ${chips.length ? `<div class="chip-row">${chips.join(' ')}</div>` : ''}

          ${p.abstain ? `<div class="funnel-note"><b>No verdict issued.</b> ${escapeHtml(p.abstain_reason || 'Evidence too thin to support a recommendation.')}</div>` : ''}

          <div class="player-stats-grid">
            <div class="stat-box"><strong>Goals/90</strong> ${metricCell(rm, 'goals')}</div>
            <div class="stat-box"><strong>Assists/90</strong> ${metricCell(rm, 'assists')}</div>
            <div class="stat-box"><strong>Tackles/90</strong> ${metricCell(rm, 'tackles')}</div>
            <div class="stat-box"><strong>Pass %</strong> ${rm.pass_cmp_pct ? Number(rm.pass_cmp_pct.value).toFixed(1) : '<span class="na">—</span>'}</div>
            <div class="stat-box"><strong>Minutes</strong> ${p.minutes || 0}</div>
            <div class="stat-box"><strong>Wage</strong> ${wageText(p)}<br><small>${wageBasis(p)}</small></div>
            <div class="stat-box"><strong>Value</strong> ${money(p.value_eur)}${p.value_as_of ? `<br><small>Transfermarkt, ${escapeHtml(p.value_as_of)}</small>` : ''}</div>
            <div class="stat-box"><strong>Cost/yr</strong> ${money(p.annual_book_cost_eur)}</div>
          </div>

          <div class="player-analysis">
            ${p.economic_verdict ? `<h4>Cost of the deal</h4><p>${escapeHtml(p.economic_verdict)}</p>` : ''}

            ${scored.length ? `<h4>Requirement match</h4><ul>${
              scored.map(t => `<li>${escapeHtml(String(t.trait).replace(/_/g, ' '))} — ${Math.round(t.score * 100)} <small>(${escapeHtml(t.source)})</small></li>`).join('')
            }</ul>` : ''}

            ${gaps.length ? `<h4>Not measured</h4><p><small>${
              gaps.map(g => escapeHtml(String(g).replace(/_/g, ' '))).join(' · ')
            }<br>These count as unmet in the fit score above; they are not evidence against the player.</small></p>` : ''}

            ${p.tactical_verdict ? `<h4>Tactical Verdict</h4><p>${escapeHtml(p.tactical_verdict)}</p>` : ''}

            ${(p.strengths || []).length || (p.weaknesses || []).length ? `
            <div class="sw-grid">
              ${(p.strengths || []).length ? `<div class="strengths"><h5>Strengths</h5><ul>${
                p.strengths.map(s => `<li>${escapeHtml(s)}</li>`).join('')}</ul></div>` : ''}
              ${(p.weaknesses || []).length ? `<div class="weaknesses"><h5>Reservations</h5><ul>${
                p.weaknesses.map(w => `<li>${escapeHtml(w)}</li>`).join('')}</ul></div>` : ''}
            </div>` : ''}

            ${p.scout_note ? `<h4>Scout Note</h4><p>${escapeHtml(p.scout_note)}</p>` : ''}
            ${p.news ? `<h4>Latest News</h4><p><small>${escapeHtml(p.news)}</small></p>` : ''}
          </div>
        </div>
      </details>
    `;
  });
  html += '</div>';
  return html;
}
