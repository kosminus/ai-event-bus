/**
 * AI Event Bus — Dashboard Application
 * Vanilla JS, no build step. WebSocket-driven real-time updates.
 */

const API = '/api/v1';

// --- State ---
const state = {
    events: [],
    agents: [],
    topics: [],
    rules: [],
    systemStatus: null,
    currentView: 'dashboard',
    selectedEvent: null,
    ws: null,
    producers: [],
    pendingActions: [],
    actionHistory: [],
    memories: [],
    memoryFilters: {
        q: '',
        scope: '',
        kind: '',
    },
    agentStreams: {},  // agent_id -> current streaming text
};

// --- WebSocket ---
function connectWebSocket() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    state.ws = new WebSocket(`${protocol}//${location.host}/ws`);

    state.ws.onopen = () => {
        state.ws.send(JSON.stringify({
            action: 'subscribe',
            channels: ['events:*', 'agents:*', 'system'],
        }));
        updateConnectionStatus(true);
    };

    state.ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        handleWSMessage(msg);
    };

    state.ws.onclose = () => {
        updateConnectionStatus(false);
        setTimeout(connectWebSocket, 3000);
    };

    state.ws.onerror = () => {
        state.ws.close();
    };
}

function handleWSMessage(msg) {
    const { channel, type, data } = msg;

    if (type === 'event.new') {
        state.events.unshift(data);
        if (state.events.length > 200) state.events.pop();
        renderEventStream();
        updateStats();
    } else if (type === 'event.deduped') {
        const evt = state.events.find(e => e.id === data.original_id);
        if (evt) {
            evt.dedupe_count = data.count;
            renderEventStream();
        }
    } else if (type === 'event.status') {
        const evt = state.events.find(e => e.id === data.id);
        if (evt) {
            evt.status = data.status;
            renderEventStream();
        }
    } else if (type === 'agent.status') {
        const agent = state.agents.find(a => a.id === data.agent_id);
        if (agent) {
            agent.status = data.status;
            renderAgents();
        }
    } else if (type === 'agent.stream') {
        if (!state.agentStreams[data.agent_id]) state.agentStreams[data.agent_id] = '';
        state.agentStreams[data.agent_id] += data.token;
        updateAgentStream(data.agent_id);
    } else if (type === 'agent.response') {
        state.agentStreams[data.agent_id] = '';
        refreshData();
    } else if (type === 'action.pending') {
        state.pendingActions.unshift(data);
        if (state.currentView === 'approvals') renderApprovalsView(document.getElementById('main-content'));
        render();
        showToast(`Action pending: ${data.action_type}`, 'info');
    } else if (type === 'action.executed') {
        refreshApprovals();
        showToast(`Auto-executed: ${data.action_type}`, 'success');
    } else if (type === 'action.approved') {
        state.pendingActions = state.pendingActions.filter(a => a.action_id !== data.action_id);
        if (state.currentView === 'approvals') renderApprovalsView(document.getElementById('main-content'));
        render();
    } else if (type === 'action.denied') {
        state.pendingActions = state.pendingActions.filter(a => a.action_id !== data.action_id);
        if (state.currentView === 'approvals') renderApprovalsView(document.getElementById('main-content'));
        render();
    } else if (type === 'system.alert') {
        showToast(data.message, 'error');
    }
}

function updateConnectionStatus(connected) {
    const dot = document.getElementById('ws-status');
    if (dot) {
        dot.className = `status-dot ${connected ? 'ok' : 'error'}`;
        dot.title = connected ? 'Connected' : 'Disconnected';
    }
}

// --- API ---
async function api(path, options = {}) {
    const resp = await fetch(`${API}${path}`, {
        headers: { 'Content-Type': 'application/json' },
        ...options,
    });
    if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: resp.statusText }));
        throw new Error(err.detail || 'API Error');
    }
    return resp.json();
}

async function refreshData() {
    try {
        const [events, agents, topics, rules, status] = await Promise.all([
            api('/events?limit=100'),
            api('/agents'),
            api('/topics'),
            api('/routing-rules'),
            api('/system/status'),
        ]);
        state.events = events;
        state.agents = agents;
        state.topics = topics;
        state.rules = rules;
        state.systemStatus = status;
        render();
    } catch (e) {
        console.error('Refresh failed:', e);
    }
    // Fetch producers and pending actions separately so a failure doesn't block the rest
    try {
        state.producers = await api('/producers');
        if (state.currentView === 'producers') renderProducers();
    } catch (e) {
        console.debug('Producers fetch failed:', e);
    }
    try {
        state.pendingActions = await api('/actions/pending');
        state.actionHistory = await api('/actions/history');
        if (state.currentView === 'approvals') renderApprovalsView(document.getElementById('main-content'));
    } catch (e) {
        console.debug('Pending actions fetch failed:', e);
    }
}

// --- Rendering ---
function render() {
    renderSidebar();
    renderMain();
    updateStats();
}

function renderSidebar() {
    const topicList = document.getElementById('topic-list');
    if (!topicList) return;
    topicList.innerHTML = state.topics.map(t => `
        <div class="topic-item" onclick="filterByTopic('${t.topic}')">
            <span class="dot"></span>
            <span>${t.topic}</span>
            <span class="topic-count">${t.event_count}</span>
        </div>
    `).join('');
}

function renderMain() {
    const main = document.getElementById('main-content');
    if (!main) return;

    switch (state.currentView) {
        case 'dashboard': renderDashboard(main); break;
        case 'events': renderEventsView(main); break;
        case 'agents': renderAgentsView(main); break;
        case 'approvals': renderApprovalsView(main); break;
        case 'memories': renderMemoriesView(main); break;
        case 'producers': renderProducersView(main); break;
        case 'config': renderConfigView(main); break;
        case 'event-detail': renderEventDetail(main); break;
    }
}

function renderDashboard(el) {
    el.innerHTML = `
        <div class="stats-row" id="stats-row"></div>
        <div class="card">
            <div class="card-header">
                <span class="card-title">Live Event Stream</span>
                <button class="btn btn-sm" onclick="publishTestEvent()">Send Test Event</button>
            </div>
            <div class="event-stream" id="event-stream"></div>
        </div>
        <div class="card">
            <div class="card-header">
                <span class="card-title">Agents</span>
                <button class="btn btn-sm btn-primary" onclick="showCreateAgent()">+ New Agent</button>
            </div>
            <div class="agents-grid" id="agents-grid"></div>
        </div>
    `;
    updateStats();
    renderEventStream();
    renderAgents();
}

function updateStats() {
    const el = document.getElementById('stats-row');
    if (!el) return;
    const s = state.systemStatus || {};
    const activeAgents = state.agents.filter(a => a.status !== 'disabled').length;
    const processingAgents = state.agents.filter(a => a.status === 'processing').length;
    el.innerHTML = `
        <div class="stat-card">
            <div class="stat-value">${s.events_total || state.events.length}</div>
            <div class="stat-label">Total Events</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">${activeAgents}</div>
            <div class="stat-label">Active Agents</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">${processingAgents}</div>
            <div class="stat-label">Processing Now</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">${state.topics.length}</div>
            <div class="stat-label">Topics</div>
        </div>
        <div class="stat-card">
            <div class="stat-value" style="${(s.active_assignments || 0) > 0 ? 'color:var(--accent-orange)' : ''}">${s.active_assignments || 0}</div>
            <div class="stat-label">Queue Depth</div>
            ${(s.active_assignments || 0) > 0
                ? `<button class="btn btn-sm btn-danger" style="margin-top:6px" onclick="drainAssignments()" title="Cancel queued assignments + cascade-deny their pending approvals">Drain</button>`
                : ''}
        </div>
        <div class="stat-card" style="cursor:pointer" onclick="navigate('approvals')">
            <div class="stat-value" style="${state.pendingActions.length > 0 ? 'color:var(--accent-orange)' : ''}">${state.pendingActions.length}</div>
            <div class="stat-label">Pending Approvals</div>
        </div>
    `;
}

function renderEventStream() {
    const el = document.getElementById('event-stream');
    if (!el) return;

    el.innerHTML = state.events.slice(0, 50).map(evt => {
        const time = new Date(evt.timestamp).toLocaleTimeString();
        const payloadStr = JSON.stringify(evt.payload).slice(0, 80);
        return `
        <div class="event-item" onclick="showEventDetail('${evt.id}')">
            <span class="event-time">${time}</span>
            <span class="event-topic">${evt.topic}</span>
            <span class="event-payload">${payloadStr}</span>
            <span class="priority-badge ${evt.priority}">${evt.priority}</span>
            <span class="event-status ${evt.status}">${evt.status}</span>
        </div>`;
    }).join('');
}

function renderAgents() {
    const el = document.getElementById('agents-grid');
    if (!el) return;

    el.innerHTML = state.agents.map(agent => {
        const stream = state.agentStreams[agent.id] || '';
        const caps = agent.capabilities.map(c => `<span class="capability-tag">${c}</span>`).join('');
        const isReactive = agent.reactive !== false;
        const modeTag = isReactive
            ? `<span class="capability-tag" title="Full tool access: shell, files, HTTP, tool_call, etc.">reactive</span>`
            : `<span class="capability-tag" style="background:var(--accent-purple);color:#fff" title="Emits events only — tool_call / shell / files are blocked">passive</span>`;
        const modeBtn = isReactive
            ? `<button class="btn btn-sm" onclick="setAgentReactive('${agent.id}', false)" title="Block tool_call and OS actions — agent will only emit events">Make Passive</button>`
            : `<button class="btn btn-sm" onclick="setAgentReactive('${agent.id}', true)" title="Allow tool_call, shell_exec, file writes, etc.">Make Reactive</button>`;
        return `
        <div class="agent-card">
            <div class="agent-status-indicator ${agent.status}"></div>
            <div class="agent-name">${agent.name}</div>
            <div class="agent-model">${agent.model}</div>
            <div class="agent-capabilities">${modeTag}${caps || '<span class="capability-tag">general</span>'}</div>
            <div style="display:flex;gap:4px;margin-top:8px;flex-wrap:wrap">
                <button class="btn btn-sm" onclick="testAgent('${agent.id}')">Test</button>
                ${modeBtn}
                ${agent.status === 'disabled'
                    ? `<button class="btn btn-sm" onclick="enableAgent('${agent.id}')">Enable</button>`
                    : `<button class="btn btn-sm btn-danger" onclick="disableAgent('${agent.id}')">Disable</button>`
                }
            </div>
            ${stream ? `<div class="agent-stream">${escapeHtml(stream)}</div>` : ''}
        </div>`;
    }).join('');
}

function updateAgentStream(agentId) {
    const el = document.getElementById('agents-grid');
    if (!el) return;
    // Quick re-render of agents to show streaming
    renderAgents();
}

function renderEventsView(el) {
    el.innerHTML = `
        <div class="card">
            <div class="card-header">
                <span class="card-title">All Events</span>
                <button class="btn btn-sm btn-primary" onclick="showPublishEvent()">+ Publish Event</button>
            </div>
            <div class="event-stream" id="event-stream" style="max-height:calc(100vh - 150px)"></div>
        </div>
    `;
    renderEventStream();
}

function renderAgentsView(el) {
    el.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
            <h2 style="font-size:16px">Agents</h2>
            <button class="btn btn-primary" onclick="showCreateAgent()">+ New Agent</button>
        </div>
        <div class="agents-grid" id="agents-grid"></div>
    `;
    renderAgents();
}

function renderMemoriesView(el) {
    const { q, scope, kind } = state.memoryFilters;
    el.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;gap:12px;flex-wrap:wrap">
            <h2 style="font-size:16px">Memories</h2>
            <div style="font-size:12px;color:var(--text-muted)">
                Recalled experience is searchable and user-reviewable here.
            </div>
        </div>
        <div class="card">
            <div class="memory-toolbar">
                <input id="memory-search" placeholder="Search recalled experience..." value="${escapeHtml(q)}" />
                <select id="memory-scope">
                    <option value="">All scopes</option>
                    <option value="user" ${scope === 'user' ? 'selected' : ''}>user</option>
                    <option value="global" ${scope === 'global' ? 'selected' : ''}>global</option>
                    ${state.agents.map(a => `<option value="agent:${escapeAttr(a.id)}" ${scope === `agent:${a.id}` ? 'selected' : ''}>agent:${escapeHtml(a.id)}</option>`).join('')}
                </select>
                <select id="memory-kind">
                    <option value="">All kinds</option>
                    <option value="episodic" ${kind === 'episodic' ? 'selected' : ''}>episodic</option>
                    <option value="semantic" ${kind === 'semantic' ? 'selected' : ''}>semantic</option>
                    <option value="procedural" ${kind === 'procedural' ? 'selected' : ''}>procedural</option>
                </select>
                <button class="btn btn-primary" onclick="applyMemoryFilters()">Search</button>
                <button class="btn" onclick="resetMemoryFilters()">Reset</button>
            </div>
        </div>
        <div id="memories-list">
            ${renderMemoriesList()}
        </div>
    `;
}

function renderMemoriesList() {
    if (state.memories.length === 0) {
        return '<div class="card" style="text-align:center;padding:40px;color:var(--text-muted)">No memories matched this query.</div>';
    }
    return state.memories.map(memory => {
        const when = memory.created_at ? new Date(memory.created_at).toLocaleString() : '';
        const tags = (memory.tags || []).map(tag => `<span class="capability-tag">${escapeHtml(tag)}</span>`).join('');
        const summary = memory.summary || memory.content;
        return `
        <div class="memory-card">
            <div class="memory-card-header">
                <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                    <span class="memory-kind">${escapeHtml(memory.kind)}</span>
                    <span class="memory-scope">scope:${escapeHtml(memory.scope)}</span>
                    <span style="font-size:11px;color:var(--text-muted)">${when}</span>
                </div>
                <div style="display:flex;gap:8px;align-items:center">
                    <span style="font-size:11px;color:var(--text-muted)">importance ${Number(memory.importance || 0).toFixed(2)}</span>
                    <button class="btn btn-sm btn-danger" onclick="deleteMemory('${escapeAttr(memory.id)}')">Delete</button>
                </div>
            </div>
            <div class="memory-summary">${escapeHtml(summary)}</div>
            ${memory.summary && memory.content !== memory.summary
                ? `<div class="memory-body">${escapeHtml(memory.content)}</div>`
                : ''}
            <div class="memory-meta">
                <span>id: <span style="color:var(--accent-blue)">${escapeHtml(memory.id)}</span></span>
                ${memory.source_event_id ? `<span>event: <span style="color:var(--accent-purple)">${escapeHtml(memory.source_event_id)}</span></span>` : ''}
                <span>accesses: ${memory.access_count ?? 0}</span>
                ${tags}
            </div>
        </div>`;
    }).join('');
}

function renderConfigView(el) {
    el.innerHTML = `
        <div class="card">
            <div class="card-header">
                <span class="card-title">Routing Rules</span>
                <button class="btn btn-sm btn-primary" onclick="showCreateRule()">+ New Rule</button>
            </div>
            <div id="rules-list"></div>
        </div>
        <div class="card">
            <div class="card-header"><span class="card-title">System Config</span></div>
            <div class="json-block" id="system-config"></div>
        </div>
    `;
    renderRules();
    const cfg = document.getElementById('system-config');
    if (cfg && state.systemStatus) {
        cfg.textContent = JSON.stringify(state.systemStatus, null, 2);
    }
}

function renderRules() {
    const el = document.getElementById('rules-list');
    if (!el) return;
    if (state.rules.length === 0) {
        el.innerHTML = '<div style="padding:12px;color:var(--text-muted);font-size:13px">No routing rules configured</div>';
        return;
    }
    el.innerHTML = state.rules.map(rule => `
        <div class="event-item" style="grid-template-columns: 1fr 1fr 1fr auto">
            <span style="font-weight:600">${rule.name}</span>
            <span class="event-topic">${rule.topic_pattern || '*'} ${rule.semantic_type_pattern ? '/ ' + rule.semantic_type_pattern : ''}</span>
            <span style="color:var(--accent-green)">${rule.consumer_id}</span>
            <button class="btn btn-sm btn-danger" onclick="deleteRule('${rule.id}')">Delete</button>
        </div>
    `).join('');
}

async function showEventDetail(eventId) {
    try {
        const [event, assignments, responses] = await Promise.all([
            api(`/events/${eventId}`),
            api(`/events/${eventId}/assignments`),
            api(`/events/${eventId}/responses`),
        ]);
        state.selectedEvent = { event, assignments, responses };
        state.currentView = 'event-detail';
        renderMain();
    } catch (e) {
        showToast(e.message, 'error');
    }
}

function renderEventDetail(el) {
    if (!state.selectedEvent) return;
    const { event, assignments, responses } = state.selectedEvent;
    el.innerHTML = `
        <button class="btn btn-sm" onclick="navigate('dashboard')" style="margin-bottom:12px">&larr; Back</button>
        <div class="event-detail">
            <h2>${event.id}</h2>
            <div class="detail-section">
                <h3>Event</h3>
                <div style="display:flex;gap:8px;margin-bottom:8px">
                    <span class="event-topic" style="font-size:14px">${event.topic}</span>
                    <span class="priority-badge ${event.priority}">${event.priority}</span>
                    <span class="event-status ${event.status}">${event.status}</span>
                    ${event.semantic_type ? `<span style="color:var(--accent-purple);font-size:12px">${event.semantic_type}</span>` : ''}
                </div>
                <div class="json-block">${JSON.stringify(event.payload, null, 2)}</div>
            </div>
            ${assignments.length > 0 ? `
            <div class="detail-section">
                <h3>Assignments (${assignments.length})</h3>
                ${assignments.map(a => `
                    <div class="card" style="font-size:12px">
                        <div><strong>Agent:</strong> ${a.agent_id}</div>
                        <div><strong>Status:</strong> <span class="event-status ${a.status}">${a.status}</span></div>
                        ${a.model_used ? `<div><strong>Model:</strong> ${a.model_used}</div>` : ''}
                        ${a.started_at ? `<div><strong>Started:</strong> ${new Date(a.started_at).toLocaleTimeString()}</div>` : ''}
                        ${a.completed_at ? `<div><strong>Completed:</strong> ${new Date(a.completed_at).toLocaleTimeString()}</div>` : ''}
                        ${a.error_message ? `<div style="color:var(--accent-red)"><strong>Error:</strong> ${a.error_message}</div>` : ''}
                    </div>
                `).join('')}
            </div>` : ''}
            ${responses.length > 0 ? `
            <div class="detail-section">
                <h3>Agent Responses (${responses.length})</h3>
                ${responses.map(r => `
                    <div class="card">
                        <div style="font-size:12px;margin-bottom:8px">
                            <strong>${r.agent_id}</strong> via <span style="color:var(--accent-purple)">${r.model_used}</span>
                            in ${r.duration_ms}ms
                        </div>
                        ${r.parsed_output ? `
                            <div style="margin-bottom:8px">
                                <span class="event-status ${r.parsed_output.type}">${r.parsed_output.type}</span>
                                ${r.parsed_output.confidence != null ? `<span style="color:var(--text-muted);font-size:11px;margin-left:8px">confidence: ${r.parsed_output.confidence}</span>` : ''}
                            </div>
                            <div style="font-size:13px;margin-bottom:8px">${escapeHtml(r.parsed_output.summary)}</div>
                            ${r.parsed_output.proposed_actions.length > 0 ? `
                                <h3 style="font-size:11px;color:var(--text-muted);margin-bottom:4px">PROPOSED ACTIONS</h3>
                                <div class="json-block">${JSON.stringify(r.parsed_output.proposed_actions, null, 2)}</div>
                            ` : ''}
                        ` : `
                            <div class="json-block">${escapeHtml(r.response_text)}</div>
                        `}
                    </div>
                `).join('')}
            </div>` : ''}
        </div>
    `;
}

// --- Actions ---
async function publishTestEvent() {
    try {
        await api('/events', {
            method: 'POST',
            body: JSON.stringify({
                topic: 'test.hello',
                payload: { message: 'Test event from dashboard', timestamp: new Date().toISOString() },
                priority: 'medium',
            }),
        });
        showToast('Test event published', 'success');
    } catch (e) {
        showToast(e.message, 'error');
    }
}

function showPublishEvent() {
    showModal('Publish Event', `
        <div class="form-group">
            <label>Topic</label>
            <input id="evt-topic" placeholder="log.error" />
        </div>
        <div class="form-group">
            <label>Payload (JSON)</label>
            <textarea id="evt-payload" placeholder='{"message": "something happened"}'></textarea>
        </div>
        <div class="form-group">
            <label>Priority</label>
            <select id="evt-priority">
                <option value="low">Low</option>
                <option value="medium" selected>Medium</option>
                <option value="high">High</option>
                <option value="critical">Critical</option>
            </select>
        </div>
        <div class="form-group">
            <label>Semantic Type (optional)</label>
            <input id="evt-semantic" placeholder="security.auth_failure" />
        </div>
        <div class="form-group">
            <label>Output Topic (optional, for chain reactions)</label>
            <input id="evt-output-topic" placeholder="agent.response" />
        </div>
    `, async () => {
        const topic = document.getElementById('evt-topic').value;
        let payload;
        try { payload = JSON.parse(document.getElementById('evt-payload').value); }
        catch { showToast('Invalid JSON payload', 'error'); return; }
        const body = { topic, payload, priority: document.getElementById('evt-priority').value };
        const sem = document.getElementById('evt-semantic').value;
        const out = document.getElementById('evt-output-topic').value;
        if (sem) body.semantic_type = sem;
        if (out) body.output_topic = out;
        await api('/events', { method: 'POST', body: JSON.stringify(body) });
        closeModal();
        showToast('Event published', 'success');
        refreshData();
    });
}

function showCreateAgent() {
    showModal('Create Agent', `
        <div class="form-group">
            <label>Name</label>
            <input id="agent-name" placeholder="security scanner" />
        </div>
        <div class="form-group">
            <label>Model</label>
            <input id="agent-model" placeholder="gemma4:latest" />
        </div>
        <div class="form-group">
            <label>System Prompt</label>
            <textarea id="agent-prompt" placeholder="You analyze security events..."></textarea>
        </div>
        <div class="form-group">
            <label>Capabilities (comma-separated)</label>
            <input id="agent-caps" placeholder="security, code" />
        </div>
        <div class="form-group">
            <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
                <input id="agent-reactive" type="checkbox" checked style="width:auto" />
                <span>Reactive (allow tool_call, shell, file, HTTP actions)</span>
            </label>
            <div style="color:var(--text-muted);font-size:12px;margin-top:4px">
                Unchecked = passive: agent may only emit events / log / alert. Tool calls and OS actions are blocked.
            </div>
        </div>
    `, async () => {
        const caps = document.getElementById('agent-caps').value
            .split(',').map(s => s.trim()).filter(Boolean);
        await api('/agents', {
            method: 'POST',
            body: JSON.stringify({
                name: document.getElementById('agent-name').value,
                model: document.getElementById('agent-model').value,
                system_prompt: document.getElementById('agent-prompt').value || undefined,
                capabilities: caps,
                reactive: document.getElementById('agent-reactive').checked,
            }),
        });
        closeModal();
        showToast('Agent created', 'success');
        refreshData();
    });
}

async function setAgentReactive(id, reactive) {
    await api(`/agents/${id}`, {
        method: 'PUT',
        body: JSON.stringify({ reactive }),
    });
    showToast(`Agent set to ${reactive ? 'reactive' : 'passive'}`, 'success');
    refreshData();
}

function showCreateRule() {
    const agentOptions = state.agents.map(a =>
        `<option value="${a.id}">${a.name} (${a.id})</option>`
    ).join('');
    showModal('Create Routing Rule', `
        <div class="form-group">
            <label>Name</label>
            <input id="rule-name" placeholder="route errors to scanner" />
        </div>
        <div class="form-group">
            <label>Topic Pattern (glob)</label>
            <input id="rule-topic" placeholder="log.*" />
        </div>
        <div class="form-group">
            <label>Semantic Type Pattern (glob, optional)</label>
            <input id="rule-semantic" placeholder="security.*" />
        </div>
        <div class="form-group">
            <label>Target Agent</label>
            <select id="rule-consumer">${agentOptions}</select>
        </div>
    `, async () => {
        const body = {
            name: document.getElementById('rule-name').value,
            topic_pattern: document.getElementById('rule-topic').value || null,
            semantic_type_pattern: document.getElementById('rule-semantic').value || null,
            consumer_id: document.getElementById('rule-consumer').value,
        };
        await api('/routing-rules', { method: 'POST', body: JSON.stringify(body) });
        closeModal();
        showToast('Rule created', 'success');
        refreshData();
    });
}

async function enableAgent(id) {
    await api(`/agents/${id}/enable`, { method: 'POST' });
    refreshData();
}

async function disableAgent(id) {
    await api(`/agents/${id}/disable`, { method: 'POST' });
    refreshData();
}

async function deleteRule(id) {
    await api(`/routing-rules/${id}`, { method: 'DELETE' });
    refreshData();
}

async function testAgent(agentId) {
    const agent = state.agents.find(a => a.id === agentId);
    if (!agent) return;
    // Find a rule that routes to this agent to determine topic
    const rule = state.rules.find(r => r.consumer_id === agentId);
    const topic = rule?.topic_pattern?.replace('*', 'test') || 'test.event';
    state.agentStreams[agentId] = '';
    await api('/events', {
        method: 'POST',
        body: JSON.stringify({
            topic,
            payload: { message: `Test event for ${agent.name}`, test: true },
            priority: 'medium',
        }),
    });
    showToast(`Test event sent to ${agent.name}`, 'success');
}

// --- Approvals ---
function renderApprovalsView(el) {
    const pending = state.pendingActions;
    const resolved = (state.actionHistory || []).filter(a => a.status !== 'waiting_confirmation');

    // Group pending by agent so we can offer "Deny all from <agent>" when
    // more than one agent is spamming the queue.
    const byAgent = {};
    for (const a of pending) {
        const id = a.agent_id || 'unknown';
        byAgent[id] = (byAgent[id] || 0) + 1;
    }
    const agentChips = Object.entries(byAgent)
        .sort((a, b) => b[1] - a[1])
        .map(([agentId, count]) => `
            <button class="btn btn-sm" onclick="denyAllPending('${escapeAttr(agentId)}')"
                    title="Deny the ${count} pending action${count !== 1 ? 's' : ''} from ${escapeAttr(agentId)}">
                Deny ${count} from ${escapeHtml(agentId)}
            </button>
        `).join(' ');

    el.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;gap:12px;flex-wrap:wrap">
            <h2 style="font-size:16px">Pending Approvals</h2>
            <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                <span style="color:var(--text-muted);font-size:12px">
                    ${pending.length} action${pending.length !== 1 ? 's' : ''} awaiting review
                </span>
                ${pending.length > 1 && Object.keys(byAgent).length > 1 ? agentChips : ''}
                ${pending.length > 0
                    ? `<button class="btn btn-sm btn-danger" onclick="denyAllPending()">Deny all (${pending.length})</button>`
                    : ''}
            </div>
        </div>
        <div id="approvals-list">
            ${pending.length === 0
                ? '<div class="card" style="text-align:center;padding:40px;color:var(--text-muted)">No pending actions. Agents will queue actions here when they need approval.</div>'
                : pending.map(a => renderActionCard(a)).join('')}
        </div>
        ${resolved.length > 0 ? `
        <div style="margin-top:24px;margin-bottom:12px">
            <h2 style="font-size:16px;color:var(--text-secondary)">History</h2>
        </div>
        <div id="approvals-history">
            ${resolved.map(a => renderHistoryCard(a)).join('')}
        </div>` : ''}
    `;
}

async function drainAssignments(agentId) {
    const s = state.systemStatus || {};
    const queueDepth = s.active_assignments || 0;
    const label = agentId ? ` for ${agentId}` : '';
    if (queueDepth === 0 && !agentId) {
        showToast('Queue is already empty.', 'info');
        return;
    }
    if (!confirm(
        `Cancel all queued assignments${label} (~${queueDepth}) and cascade-deny any pending approvals?\n\n` +
        `Running assignments are NOT cancelled (an agent is actively using them). This cannot be undone.`
    )) return;
    try {
        const qs = agentId ? `?agent_id=${encodeURIComponent(agentId)}` : '';
        const res = await api(`/assignments/cancel-pending${qs}`, { method: 'POST' });
        const cancelled = (res && res.cancelled_assignments) ? res.cancelled_assignments.length : 0;
        const cascaded = (res && res.cascaded_actions) ? res.cascaded_actions.length : 0;
        showToast(`Drained ${cancelled} assignments, denied ${cascaded} approvals`, 'success');
        // Refresh the stats + approvals since they're now empty/smaller.
        refreshData();
    } catch (e) {
        showToast(e.message || String(e), 'error');
    }
}

async function denyAllPending(agentId) {
    const scope = agentId
        ? state.pendingActions.filter(a => (a.agent_id || '') === agentId).length
        : state.pendingActions.length;
    if (scope === 0) return;
    const label = agentId ? ` from ${agentId}` : '';
    if (!confirm(`Deny ${scope} pending action${scope !== 1 ? 's' : ''}${label}? This cannot be undone.`)) return;
    try {
        const qs = agentId ? `?agent_id=${encodeURIComponent(agentId)}` : '';
        const res = await api(`/actions/deny-pending${qs}`, { method: 'POST' });
        const denied = (res && res.denied) ? res.denied.length : 0;
        state.pendingActions = agentId
            ? state.pendingActions.filter(a => (a.agent_id || '') !== agentId)
            : [];
        if (state.currentView === 'approvals') renderApprovalsView(document.getElementById('main-content'));
        showToast(`Denied ${denied} action${denied !== 1 ? 's' : ''}`, 'success');
    } catch (e) {
        showToast(e.message || String(e), 'error');
    }
}

function renderActionCard(action) {
    const actionData = action.action_data || action;
    const actionType = action.action_type || actionData.action_type || 'unknown';
    const agentId = action.agent_id || '';
    const eventId = action.event_id || '';
    const actionId = action.id || action.action_id || '';
    const reason = action.policy_reason || '';
    const created = action.created_at ? new Date(action.created_at).toLocaleString() : '';

    // Build a readable description of what the action wants to do
    let detail = '';
    if (actionType === 'shell_exec') {
        detail = `<div class="action-command"><span style="color:var(--accent-orange)">$</span> ${escapeHtml(actionData.command || '')}</div>`;
    } else if (actionType === 'file_read') {
        detail = `<div style="font-size:12px;color:var(--text-secondary)">Read: <code>${escapeHtml(actionData.path || '')}</code></div>`;
    } else if (actionType === 'file_write') {
        detail = `<div style="font-size:12px;color:var(--text-secondary)">Write to: <code>${escapeHtml(actionData.path || '')}</code></div>`;
    } else if (actionType === 'file_delete') {
        detail = `<div style="font-size:12px;color:var(--accent-red)">Delete: <code>${escapeHtml(actionData.path || '')}</code></div>`;
    } else if (actionType === 'notify') {
        detail = `<div style="font-size:12px;color:var(--text-secondary)">${escapeHtml(actionData.message || '')}</div>`;
    } else if (actionType === 'open_app') {
        detail = `<div style="font-size:12px;color:var(--text-secondary)">Open: ${escapeHtml(actionData.app || actionData.command || '')}</div>`;
    } else {
        detail = `<div class="json-block" style="max-height:150px">${JSON.stringify(actionData, null, 2)}</div>`;
    }

    return `
    <div class="action-card">
        <div class="action-card-header">
            <div>
                <span class="action-type-badge ${actionType}">${actionType}</span>
                <span style="font-size:11px;color:var(--text-muted);margin-left:8px">${actionId}</span>
            </div>
            <div style="font-size:11px;color:var(--text-muted)">${created}</div>
        </div>
        ${detail}
        <div class="action-meta">
            <span>Agent: <span style="color:var(--accent-purple)">${escapeHtml(agentId)}</span></span>
            <span>Event: <span style="color:var(--accent-blue);cursor:pointer" onclick="showEventDetail('${eventId}')">${escapeHtml(eventId)}</span></span>
            ${reason ? `<span>Reason: <span style="color:var(--text-secondary)">${escapeHtml(reason)}</span></span>` : ''}
        </div>
        <div class="action-buttons">
            <button class="btn btn-primary" onclick="approveAction('${actionId}')">Approve</button>
            <button class="btn btn-danger" onclick="denyAction('${actionId}')">Deny</button>
        </div>
    </div>`;
}

function renderHistoryCard(action) {
    const actionType = action.action_type || 'unknown';
    const actionId = action.id || '';
    const status = action.status || '';
    const resolved = action.resolved_at ? new Date(action.resolved_at).toLocaleString() : '';
    const result = action.result;

    let statusColor = 'var(--text-muted)';
    let statusLabel = status;
    if (status === 'completed') { statusColor = 'var(--accent-green)'; statusLabel = 'approved'; }
    else if (status === 'denied') { statusColor = 'var(--accent-red)'; }
    else if (status === 'approved') { statusColor = 'var(--accent-green)'; }

    // Brief summary of what happened
    let summary = '';
    if (actionType === 'shell_exec') {
        const cmd = (action.action_data || {}).command || '';
        summary = `<span style="font-family:var(--font-mono);font-size:12px;color:var(--text-secondary)">$ ${escapeHtml(cmd)}</span>`;
        if (result && result.returncode !== undefined) {
            summary += ` <span style="font-size:11px;color:${result.returncode === 0 ? 'var(--accent-green)' : 'var(--accent-red)'}">exit ${result.returncode}</span>`;
        }
    } else {
        summary = `<span style="font-size:12px;color:var(--text-secondary)">${escapeHtml(JSON.stringify(action.action_data || {}).slice(0, 80))}</span>`;
    }

    const hasResult = result && Object.keys(result).length > 0;

    return `
    <div class="action-card" style="opacity:0.8">
        <div style="display:flex;justify-content:space-between;align-items:center">
            <div style="display:flex;align-items:center;gap:8px">
                <span class="action-type-badge ${actionType}">${actionType}</span>
                <span style="font-size:10px;padding:2px 8px;border-radius:10px;font-weight:500;text-transform:uppercase;color:${statusColor}">${statusLabel}</span>
                ${summary}
            </div>
            <div style="display:flex;align-items:center;gap:8px">
                <span style="font-size:11px;color:var(--text-muted)">${resolved}</span>
                ${hasResult ? `<button class="btn btn-sm" onclick="viewActionResult('${actionId}')">View Result</button>` : ''}
            </div>
        </div>
    </div>`;
}

async function viewActionResult(actionId) {
    try {
        const action = await api(`/actions/${actionId}`);
        showActionResult(actionId, { result: action.result || {} });
    } catch (e) {
        showToast(e.message, 'error');
    }
}

async function approveAction(actionId) {
    try {
        const result = await api(`/actions/${actionId}/approve`, { method: 'POST' });
        state.pendingActions = state.pendingActions.filter(a => (a.id || a.action_id) !== actionId);
        showToast('Action approved', 'success');
        // Show the execution result in a modal
        showActionResult(actionId, result);
        refreshApprovals();
        render();
    } catch (e) {
        showToast(e.message, 'error');
    }
}

async function denyAction(actionId) {
    try {
        await api(`/actions/${actionId}/deny`, { method: 'POST' });
        state.pendingActions = state.pendingActions.filter(a => (a.id || a.action_id) !== actionId);
        showToast('Action denied', 'success');
        refreshApprovals();
        render();
    } catch (e) {
        showToast(e.message, 'error');
    }
}

function showActionResult(actionId, result) {
    const r = result.result || {};
    let body = '';
    if (r.stdout !== undefined) {
        // shell_exec result
        body = `
            ${r.returncode !== undefined ? `<div style="margin-bottom:8px;font-size:12px">Exit code: <span style="color:${r.returncode === 0 ? 'var(--accent-green)' : 'var(--accent-red)'}">${r.returncode}</span></div>` : ''}
            ${r.stdout ? `<div style="margin-bottom:4px;font-size:11px;color:var(--text-muted)">STDOUT</div><div class="json-block" style="max-height:400px;white-space:pre">${escapeHtml(r.stdout)}</div>` : ''}
            ${r.stderr ? `<div style="margin-top:8px;margin-bottom:4px;font-size:11px;color:var(--accent-red)">STDERR</div><div class="json-block" style="max-height:200px;white-space:pre;border-color:var(--accent-red)">${escapeHtml(r.stderr)}</div>` : ''}
        `;
    } else if (r.error) {
        body = `<div style="color:var(--accent-red)">${escapeHtml(r.error)}</div>`;
    } else {
        body = `<div class="json-block">${JSON.stringify(r, null, 2)}</div>`;
    }

    const overlay = document.getElementById('modal-overlay');
    overlay.innerHTML = `
        <div class="modal" style="max-width:700px">
            <h2>Execution Result</h2>
            <div style="font-size:12px;color:var(--text-muted);margin-bottom:12px">${actionId}</div>
            ${body}
            <div class="modal-actions">
                <button class="btn btn-primary" onclick="closeModal()">Close</button>
            </div>
        </div>
    `;
    overlay.classList.add('active');
}

async function refreshApprovals() {
    try {
        state.pendingActions = await api('/actions/pending');
        state.actionHistory = await api('/actions/history');
        if (state.currentView === 'approvals') renderApprovalsView(document.getElementById('main-content'));
    } catch (e) {
        console.debug('Approvals refresh failed:', e);
    }
}

async function refreshMemories() {
    try {
        const params = new URLSearchParams({ limit: '100' });
        if (state.memoryFilters.q) params.set('q', state.memoryFilters.q);
        if (state.memoryFilters.scope) params.set('scope', state.memoryFilters.scope);
        if (state.memoryFilters.kind) params.set('kind', state.memoryFilters.kind);
        state.memories = await api(`/memories?${params.toString()}`);
        if (state.currentView === 'memories') {
            const list = document.getElementById('memories-list');
            if (list) list.innerHTML = renderMemoriesList();
        }
        render();
    } catch (e) {
        console.debug('Memories refresh failed:', e);
    }
}

// --- Producers ---
function renderProducersView(el) {
    el.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
            <h2 style="font-size:16px">Producers</h2>
        </div>
        <div class="producers-grid" id="producers-grid"></div>
    `;
    renderProducers();
}

function renderProducers() {
    const el = document.getElementById('producers-grid');
    if (!el) return;

    if (state.producers.length === 0) {
        el.innerHTML = '<div style="padding:12px;color:var(--text-muted);font-size:13px">No producers available</div>';
        return;
    }

    el.innerHTML = state.producers.map(p => {
        const caps = p.capabilities || {};
        const capRows = Object.keys(caps).map(name => {
            const c = caps[name];
            const cls = c.available ? 'completed' : 'expired';
            const backend = c.backend ? ` · ${escapeHtml(c.backend)}` : '';
            const reason = !c.available && c.reason ? ` — ${escapeHtml(c.reason)}` : '';
            return `<div style="font-size:11px;color:var(--text-secondary);margin-top:2px">
                <span class="event-status ${cls}" style="margin-right:4px">${escapeHtml(name)}</span>${backend}${reason}
            </div>`;
        }).join('');
        const unavailable = !p.available;
        const unavailableNote = unavailable && p.unavailable_reason
            ? `<div style="font-size:11px;color:var(--text-muted);margin-top:6px">${escapeHtml(p.unavailable_reason)}</div>`
            : '';
        const actionBtn = unavailable
            ? `<button class="btn btn-sm" disabled title="${escapeHtml(p.unavailable_reason || 'unavailable')}">Unavailable</button>`
            : p.running
                ? `<button class="btn btn-sm btn-danger" onclick="toggleProducer('${p.name}', false)">Disable</button>`
                : `<button class="btn btn-sm btn-primary" onclick="toggleProducer('${p.name}', true)">Enable</button>`;
        return `
        <div class="producer-card">
            <div class="agent-status-indicator ${p.running ? 'idle' : (unavailable ? 'error' : 'disabled')}"></div>
            <div class="agent-name">${p.name}</div>
            <div style="font-size:12px;color:var(--text-secondary);margin-bottom:8px">${escapeHtml(p.description)}</div>
            <div style="display:flex;gap:6px;align-items:center;margin-bottom:8px;flex-wrap:wrap">
                <span class="event-status ${p.running ? 'completed' : 'expired'}">${p.running ? 'running' : (unavailable ? 'unavailable' : 'stopped')}</span>
                ${(p.topics || []).map(t => `<span class="capability-tag">${escapeHtml(t)}</span>`).join('')}
            </div>
            <div>${capRows}</div>
            ${unavailableNote}
            <div style="margin-top:8px">${actionBtn}</div>
        </div>`;
    }).join('');
}

async function toggleProducer(name, enable) {
    try {
        const action = enable ? 'enable' : 'disable';
        await api(`/producers/${name}/${action}`, { method: 'POST' });
        showToast(`Producer ${name} ${enable ? 'enabled' : 'disabled'}`, 'success');
        refreshData();
    } catch (e) {
        showToast(e.message, 'error');
    }
}

// --- Navigation ---
function navigate(view) {
    state.currentView = view;
    document.querySelectorAll('.nav-item').forEach(el => {
        el.classList.toggle('active', el.dataset.view === view);
    });
    if (view === 'memories') refreshMemories();
    renderMain();
}

function filterByTopic(topic) {
    state.currentView = 'events';
    renderMain();
    // TODO: filter by topic
}

// --- Modal ---
function showModal(title, content, onSubmit) {
    const overlay = document.getElementById('modal-overlay');
    overlay.innerHTML = `
        <div class="modal">
            <h2>${title}</h2>
            ${content}
            <div class="modal-actions">
                <button class="btn" onclick="closeModal()">Cancel</button>
                <button class="btn btn-primary" id="modal-submit">Create</button>
            </div>
        </div>
    `;
    overlay.classList.add('active');
    document.getElementById('modal-submit').onclick = async () => {
        try { await onSubmit(); } catch (e) { showToast(e.message, 'error'); }
    };
}

function closeModal() {
    document.getElementById('modal-overlay').classList.remove('active');
}

// --- Toast ---
function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => toast.remove(), 4000);
}

// --- Utilities ---
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function escapeAttr(text) {
    // Safe for use inside single-quoted HTML attributes in inline handlers.
    return String(text ?? '').replace(/\\/g, '\\\\').replace(/'/g, "\\'");
}

function applyMemoryFilters() {
    state.memoryFilters.q = document.getElementById('memory-search')?.value.trim() || '';
    state.memoryFilters.scope = document.getElementById('memory-scope')?.value || '';
    state.memoryFilters.kind = document.getElementById('memory-kind')?.value || '';
    refreshMemories();
}

function resetMemoryFilters() {
    state.memoryFilters = { q: '', scope: '', kind: '' };
    refreshMemories();
    if (state.currentView === 'memories') renderMain();
}

async function deleteMemory(memoryId) {
    if (!confirm(`Delete memory ${memoryId}? This cannot be undone.`)) return;
    try {
        await api(`/memories/${memoryId}`, { method: 'DELETE' });
        state.memories = state.memories.filter(m => m.id !== memoryId);
        if (state.currentView === 'memories') {
            const list = document.getElementById('memories-list');
            if (list) list.innerHTML = renderMemoriesList();
        }
        render();
        showToast('Memory deleted', 'success');
    } catch (e) {
        showToast(e.message, 'error');
    }
}

// --- Init ---
document.addEventListener('DOMContentLoaded', () => {
    connectWebSocket();
    refreshData();
    refreshMemories();
    // Auto-refresh every 10s
    setInterval(refreshData, 10000);
    setInterval(refreshMemories, 15000);
});
