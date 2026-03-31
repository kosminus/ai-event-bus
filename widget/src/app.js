const DAEMON = 'http://localhost:8420';
const WS_URL = 'ws://localhost:8420/ws';

const state = {
    events: [],
    pendingActions: [],
    activeTab: 'all',
    ws: null,
    connected: false,
    chatPending: null,
    chatResponse: null,
    chatStreaming: '',
};

// --- API ---

async function api(method, path, body) {
    const opts = { method, headers: { 'Content-Type': 'application/json' } };
    if (body) opts.body = JSON.stringify(body);
    const r = await fetch(`${DAEMON}${path}`, opts);
    return r.json();
}

// --- WebSocket ---

function connectWS() {
    if (state.ws && state.ws.readyState < 2) return;
    const ws = new WebSocket(WS_URL);

    ws.onopen = () => {
        state.ws = ws;
        state.connected = true;
        ws.send(JSON.stringify({ action: 'subscribe', channels: ['events:*', 'agents:*', 'system'] }));
        updateStatus();
    };

    ws.onmessage = (e) => {
        try { handleWS(JSON.parse(e.data)); } catch (_) {}
    };

    ws.onclose = () => {
        state.connected = false;
        updateStatus();
        setTimeout(connectWS, 3000);
    };

    ws.onerror = () => ws.close();
}

function handleWS(msg) {
    const { type, data } = msg;

    if (type === 'event.new') {
        state.events.unshift(data);
        if (state.events.length > 200) state.events.pop();

        // Critical notification
        const critical = ['security.', 'system.agent_failure', 'system.action_denied'];
        if (critical.some(p => data.topic.startsWith(p))) {
            notify('aiventbus Alert', `${data.topic}: ${payloadPreview(data.payload)}`);
        }
        renderFeed();
    }

    if (type === 'event.status') {
        const evt = state.events.find(e => e.id === data.id);
        if (evt) { evt.status = data.status; renderFeed(); }

        // If this is our chat event completing, fetch the response
        if (state.chatPending && data.id === state.chatPending.eventId && data.status === 'completed') {
            fetchChatResponse(data.id);
        }
    }

    if (type === 'agent.stream') {
        // If we're waiting for a chat response, show streaming tokens
        if (state.chatPending) {
            state.chatStreaming += data.token;
            showChatResponse(state.chatStreaming, true);
        }
    }

    if (type === 'agent.response') {
        if (state.chatPending) {
            fetchChatResponse(state.chatPending.eventId);
        }
    }

    if (type === 'action.pending') {
        state.pendingActions.push(data);
        updateBadge();
        notify('Action needs approval', `${data.action_type} from ${data.agent_id}`);
        if (state.activeTab === 'approvals') renderFeed();
    }

    if (type === 'action.approved' || type === 'action.denied') {
        state.pendingActions = state.pendingActions.filter(a => a.action_id !== data.action_id);
        updateBadge();
        if (state.activeTab === 'approvals') renderFeed();
    }
}

// --- Chat ---

async function submitChat() {
    const input = document.getElementById('chat-input');
    const text = input.value.trim();
    if (!text) return;

    input.value = '';
    state.chatStreaming = '';
    state.chatResponse = null;
    showChatResponse('Thinking', true);

    try {
        const event = await api('POST', '/api/v1/events', {
            topic: 'user.query',
            payload: { query: text },
            priority: 'high',
        });
        state.chatPending = { eventId: event.id, traceId: event.trace_id };
    } catch (e) {
        showChatResponse('Error: daemon not reachable', false);
        state.chatPending = null;
    }
}

async function fetchChatResponse(eventId) {
    try {
        const responses = await api('GET', `/api/v1/events/${eventId}/responses`);
        if (responses.length > 0) {
            const r = responses[0];
            const parsed = r.parsed_output;
            const summary = parsed ? parsed.summary : r.response_text.substring(0, 300);
            showChatResponse(summary, false);
        }
    } catch (_) {}
    state.chatPending = null;
}

function showChatResponse(text, streaming) {
    const el = document.getElementById('chat-response');
    const textEl = document.getElementById('chat-response-text');
    el.classList.remove('hidden');
    if (streaming && text === 'Thinking') {
        textEl.innerHTML = '<span class="thinking">Thinking</span>';
    } else {
        textEl.textContent = text;
    }
}

function closeChat() {
    document.getElementById('chat-response').classList.add('hidden');
    state.chatPending = null;
    state.chatStreaming = '';
}

// --- Tabs ---

function switchTab(tab) {
    state.activeTab = tab;
    document.querySelectorAll('.tab').forEach(t => {
        t.classList.toggle('active', t.dataset.tab === tab);
    });
    renderFeed();
}

// --- Rendering ---

function renderFeed() {
    const feed = document.getElementById('feed');

    if (state.activeTab === 'approvals') {
        renderApprovals(feed);
        return;
    }

    const filtered = filterEvents();
    if (filtered.length === 0) {
        feed.innerHTML = '<div class="empty-state">No events yet</div>';
        return;
    }

    feed.innerHTML = filtered.slice(0, 100).map(e => {
        const time = timeAgo(e.created_at || e.timestamp);
        const preview = payloadPreview(e.payload);
        const statusClass = `status-${e.status}`;
        return `
            <div class="event-row" onclick="openDashboard()">
                <div class="event-header">
                    <span class="event-time">${time}</span>
                    <span class="event-topic">${escHtml(e.topic)}</span>
                    <span class="event-status ${statusClass}">${e.status}</span>
                </div>
                ${preview ? `<div class="event-payload">${escHtml(preview)}</div>` : ''}
            </div>`;
    }).join('');
}

function filterEvents() {
    const tab = state.activeTab;
    if (tab === 'all') return state.events;
    if (tab === 'files') return state.events.filter(e => e.topic.startsWith('fs.') || e.topic.startsWith('clipboard.'));
    if (tab === 'security') return state.events.filter(e =>
        e.topic.startsWith('security.') || e.topic === 'system.agent_failure' || e.topic === 'system.action_denied'
    );
    return state.events;
}

function renderApprovals(feed) {
    if (state.pendingActions.length === 0) {
        feed.innerHTML = '<div class="empty-state">No pending approvals</div>';
        return;
    }

    feed.innerHTML = state.pendingActions.map(a => {
        const detail = a.action_data ?
            (a.action_data.command || a.action_data.path || JSON.stringify(a.action_data).substring(0, 100)) : '';
        return `
            <div class="action-row">
                <div class="action-type">${escHtml(a.action_type)}</div>
                <div class="action-detail">${escHtml(detail)}</div>
                <div class="action-agent">from ${escHtml(a.agent_id)}</div>
                <div class="action-buttons">
                    <button class="btn btn-approve" onclick="approveAction('${a.action_id || a.id}')">Approve</button>
                    <button class="btn btn-deny" onclick="denyAction('${a.action_id || a.id}')">Deny</button>
                </div>
            </div>`;
    }).join('');
}

// --- Actions ---

async function approveAction(id) {
    await api('POST', `/api/v1/actions/${id}/approve`);
    state.pendingActions = state.pendingActions.filter(a => (a.action_id || a.id) !== id);
    updateBadge();
    renderFeed();
}

async function denyAction(id) {
    await api('POST', `/api/v1/actions/${id}/deny`);
    state.pendingActions = state.pendingActions.filter(a => (a.action_id || a.id) !== id);
    updateBadge();
    renderFeed();
}

// --- Tray + Notifications ---

function updateBadge() {
    const count = state.pendingActions.length;
    const badge = document.getElementById('approvals-badge');
    if (count > 0) {
        badge.textContent = count;
        badge.classList.remove('hidden');
    } else {
        badge.classList.add('hidden');
    }
    // Update tray icon
    if (window.__TAURI__) {
        window.__TAURI__.core.invoke('set_tray_badge', { count });
    }
}

function notify(title, body) {
    if (window.__TAURI__) {
        window.__TAURI__.core.invoke('send_notification', { title, body });
    }
}

function openDashboard() {
    if (window.__TAURI__) {
        window.__TAURI__.shell.open(DAEMON);
    } else {
        window.open(DAEMON, '_blank');
    }
}

// --- Helpers ---

function updateStatus() {
    const dot = document.getElementById('status-dot');
    const text = document.getElementById('status-text');
    dot.className = 'dot ' + (state.connected ? 'connected' : 'disconnected');
    text.textContent = state.connected ? 'connected' : 'disconnected';
}

function timeAgo(iso) {
    if (!iso) return '';
    const diff = (Date.now() - new Date(iso).getTime()) / 1000;
    if (diff < 60) return `${Math.floor(diff)}s`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
    return `${Math.floor(diff / 86400)}d`;
}

function payloadPreview(payload) {
    if (!payload) return '';
    if (payload.query) return payload.query;
    if (payload.content) return payload.content.substring(0, 80);
    if (payload.message) return payload.message;
    if (payload.command) return payload.command;
    if (payload.path) return payload.path;
    const str = JSON.stringify(payload);
    return str.length > 80 ? str.substring(0, 77) + '...' : str;
}

function escHtml(s) {
    if (!s) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// --- Init ---

async function init() {
    // Load initial data
    try {
        state.events = await api('GET', '/api/v1/events?limit=50');
        state.pendingActions = await api('GET', '/api/v1/actions/pending');
    } catch (e) {
        console.warn('Daemon not reachable:', e);
    }

    updateBadge();
    renderFeed();
    connectWS();

    // Chat input handler
    document.getElementById('chat-input').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') submitChat();
    });

    // Tauri global shortcut listener
    if (window.__TAURI__) {
        window.__TAURI__.event.listen('focus-chat', () => {
            document.getElementById('chat-input').focus();
        });
    }
}

document.addEventListener('DOMContentLoaded', init);
