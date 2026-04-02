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
    // Fetch producers separately so a failure doesn't block the rest
    try {
        state.producers = await api('/producers');
        if (state.currentView === 'producers') renderProducers();
    } catch (e) {
        console.debug('Producers fetch failed:', e);
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
            <div class="stat-value">${s.active_assignments || 0}</div>
            <div class="stat-label">Queue Depth</div>
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
        return `
        <div class="agent-card">
            <div class="agent-status-indicator ${agent.status}"></div>
            <div class="agent-name">${agent.name}</div>
            <div class="agent-model">${agent.model}</div>
            <div class="agent-capabilities">${caps || '<span class="capability-tag">general</span>'}</div>
            <div style="display:flex;gap:4px;margin-top:8px">
                <button class="btn btn-sm" onclick="testAgent('${agent.id}')">Test</button>
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
            <input id="agent-model" placeholder="gemma3:4b" />
        </div>
        <div class="form-group">
            <label>System Prompt</label>
            <textarea id="agent-prompt" placeholder="You analyze security events..."></textarea>
        </div>
        <div class="form-group">
            <label>Capabilities (comma-separated)</label>
            <input id="agent-caps" placeholder="security, code" />
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
            }),
        });
        closeModal();
        showToast('Agent created', 'success');
        refreshData();
    });
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

    el.innerHTML = state.producers.map(p => `
        <div class="producer-card">
            <div class="agent-status-indicator ${p.running ? 'idle' : 'disabled'}"></div>
            <div class="agent-name">${p.name}</div>
            <div style="font-size:12px;color:var(--text-secondary);margin-bottom:8px">${escapeHtml(p.description)}</div>
            <div style="display:flex;gap:6px;align-items:center;margin-bottom:8px">
                <span class="event-status ${p.running ? 'completed' : 'expired'}">${p.running ? 'running' : 'stopped'}</span>
                <span class="capability-tag">${p.type}</span>
            </div>
            <div style="margin-top:8px">
                ${p.running
                    ? `<button class="btn btn-sm btn-danger" onclick="toggleProducer('${p.name}', false)">Disable</button>`
                    : `<button class="btn btn-sm btn-primary" onclick="toggleProducer('${p.name}', true)">Enable</button>`
                }
            </div>
        </div>
    `).join('');
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

// --- Init ---
document.addEventListener('DOMContentLoaded', () => {
    connectWebSocket();
    refreshData();
    // Auto-refresh every 10s
    setInterval(refreshData, 10000);
});
