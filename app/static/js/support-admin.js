(() => {
  const root = document.querySelector('[data-support-admin]');
  if (!root) return;

  const list = root.querySelector('[data-support-admin-conversations]');
  const empty = root.querySelector('[data-support-admin-empty]');
  const active = root.querySelector('[data-support-admin-active]');
  const messages = root.querySelector('[data-support-admin-messages]');
  const compose = root.querySelector('[data-support-admin-compose]');
  const textarea = compose.querySelector('textarea');
  const claimButton = root.querySelector('[data-support-admin-claim]');
  const closeButton = root.querySelector('[data-support-admin-close]');
  const title = root.querySelector('[data-support-admin-title]');
  const topic = root.querySelector('[data-support-admin-topic]');
  const status = root.querySelector('[data-support-admin-status]');
  const typing = root.querySelector('[data-support-admin-typing]');
  const notice = document.querySelector('[data-support-admin-notice]');
  const availability = document.querySelector('[data-support-agent-status]');
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const adminName = root.dataset.supportAdminName;
  const topicNames = {
    general: 'General help',
    orders: 'Orders',
    payments: 'Payments',
    delivery: 'Delivery and collection',
    returns: 'Returns',
    products: 'Products',
  };

  let conversations = [];
  let selected = null;
  let cursor = 0;
  let socket = null;
  let messageTimer = null;
  let typingTimer = null;
  let loading = false;
  let agentTyping = false;
  let pendingMessage = null;
  const renderedIds = new Set();

  async function request(url, options = {}) {
    const response = await fetch(url, {
      credentials: 'same-origin',
      cache: 'no-store',
      ...options,
      headers: {
        Accept: 'application/json',
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
        ...(options.method && options.method !== 'GET' ? { 'X-CSRFToken': csrfToken } : {}),
        ...options.headers,
      },
    });
    if (!response.headers.get('content-type')?.includes('application/json')) {
      throw new Error(response.redirected
        ? 'Your admin session expired. Sign in again to continue.'
        : 'Support inbox returned an unexpected response. Please try again.');
    }
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Support inbox could not be reached. Please try again.');
    return data;
  }

  function showNotice(message) {
    notice.textContent = message;
    notice.hidden = false;
  }

  function clearNotice() {
    notice.textContent = '';
    notice.hidden = true;
  }

  function messageId() {
    if (window.crypto?.randomUUID) return window.crypto.randomUUID();
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  function appendMessage(message) {
    if (!message || renderedIds.has(message.id)) return;
    renderedIds.add(message.id);
    const article = document.createElement('article');
    article.className = `support-admin-message${message.sender_type === 'agent' ? ' is-agent' : ''}`;
    const sender = document.createElement('strong');
    sender.textContent = message.sender_name || (message.sender_type === 'agent' ? 'Agent' : 'Customer');
    const body = document.createElement('p');
    body.textContent = message.body;
    const time = document.createElement('time');
    const date = new Date(message.created_at);
    time.dateTime = Number.isNaN(date.getTime()) ? '' : date.toISOString();
    time.textContent = Number.isNaN(date.getTime())
      ? ''
      : date.toLocaleString([], { dateStyle: 'short', timeStyle: 'short' });
    article.append(sender, body, time);
    messages.append(article);
    messages.scrollTop = messages.scrollHeight;
  }

  function updateSelected(conversation) {
    if (!conversation) return;
    selected = conversation;
    topic.textContent = topicNames[conversation.topic] || 'General help';
    title.textContent = conversation.customer_name || conversation.customer_email || 'Customer conversation';
    const assignedToMe = conversation.assigned === adminName;
    const isWaiting = conversation.status === 'waiting' && conversation.escalation_requested;
    status.textContent = conversation.status === 'assigned'
      ? `Assigned to ${conversation.assigned || 'an agent'}`
      : conversation.status === 'closed'
        ? 'Closed'
        : conversation.escalation_requested
          ? 'Waiting for an agent'
          : 'Customer is using self-service';
    claimButton.hidden = !isWaiting;
    closeButton.hidden = !assignedToMe;
    compose.hidden = !assignedToMe || conversation.status === 'closed';
  }

  function renderConversationList() {
    list.replaceChildren();
    if (!conversations.length) {
      const message = document.createElement('p');
      message.className = 'support-admin-empty-list';
      message.textContent = 'No open conversations right now.';
      list.append(message);
      return;
    }

    conversations.forEach((conversation) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'support-admin-conversation-row';
      button.setAttribute('aria-current', String(selected?.id === conversation.id));
      const name = document.createElement('strong');
      name.textContent = conversation.customer_name || conversation.customer_email || 'Customer';
      const excerpt = document.createElement('span');
      excerpt.textContent = conversation.last_message?.body || 'No messages yet';
      const details = document.createElement('small');
      const topicName = topicNames[conversation.topic] || 'General help';
      const state = conversation.status === 'assigned'
        ? `Assigned to ${conversation.assigned || 'an agent'}`
        : conversation.escalation_requested
          ? 'Waiting for an agent'
          : 'Self-service';
      details.textContent = `${topicName} · ${state}`;
      button.append(name, excerpt, details);
      button.addEventListener('click', () => selectConversation(conversation));
      list.append(button);
    });
  }

  async function refreshList() {
    try {
      const data = await request('/admin/support/api/conversations');
      conversations = data.conversations || [];
      if (selected) {
        const updated = conversations.find((item) => item.id === selected.id);
        if (updated) updateSelected(updated);
        else {
          selected = null;
          active.hidden = true;
          empty.hidden = false;
        }
      }
      renderConversationList();
    } catch (requestError) {
      showNotice(requestError.message);
    }
  }

  async function refreshMessages() {
    if (!selected || loading) return;
    loading = true;
    try {
      const data = await request(`/admin/support/api/conversations/${encodeURIComponent(selected.id)}/messages?after=${cursor}`);
      updateSelected(data.conversation);
      data.messages.forEach(appendMessage);
      if (data.messages.length) cursor = Math.max(cursor, ...data.messages.map((message) => message.id));
    } catch (requestError) {
      showNotice(requestError.message);
    } finally {
      loading = false;
    }
  }

  function joinSelectedConversation() {
    if (!socket?.connected || !selected) return;
    socket.emit('join_conversation', {
      conversation_id: selected.id,
      csrf_token: csrfToken,
    });
  }

  function sendHeartbeat() {
    if (!socket?.connected) return;
    socket.emit('agent_heartbeat', {
      status: availability.value,
      csrf_token: csrfToken,
    }, (result) => {
      if (result?.ok) clearNotice();
    });
  }

  function connectSocket() {
    if (!window.io || socket) return;
    socket = window.io('/support', {
      transports: ['websocket', 'polling'],
      reconnection: true,
      timeout: 8000,
    });
    socket.on('connect', () => {
      socket.emit('join_support_queue', { csrf_token: csrfToken });
      joinSelectedConversation();
      sendHeartbeat();
    });
    socket.on('queue_update', () => refreshList());
    socket.on('message', (message) => {
      if (selected && message) appendMessage(message);
      refreshList();
    });
    socket.on('typing', (data) => {
      if (data?.conversation_id === selected?.id && data.sender_type === 'customer') typing.hidden = !data.typing;
    });
    socket.on('closed', (data) => {
      if (data?.conversation?.id === selected?.id) updateSelected(data.conversation);
      refreshList();
    });
    socket.on('disconnect', () => {
      showNotice('Support chat is reconnecting. The inbox will keep refreshing.');
    });
  }

  async function selectConversation(conversation) {
    clearNotice();
    selected = conversation;
    cursor = 0;
    renderedIds.clear();
    messages.replaceChildren();
    empty.hidden = true;
    active.hidden = false;
    updateSelected(conversation);
    renderConversationList();
    connectSocket();
    joinSelectedConversation();
    await refreshMessages();
    if (messageTimer) window.clearInterval(messageTimer);
    messageTimer = window.setInterval(refreshMessages, 4000);
  }

  async function claimConversation() {
    if (!selected) return;
    claimButton.disabled = true;
    clearNotice();
    try {
      const data = await request(`/admin/support/api/conversations/${encodeURIComponent(selected.id)}/claim`, { method: 'POST' });
      updateSelected(data.conversation);
      conversations = conversations.map((item) => item.id === data.conversation.id ? { ...item, ...data.conversation } : item);
      renderConversationList();
      await refreshList();
    } catch (requestError) {
      showNotice(requestError.message);
      await refreshList();
    } finally {
      claimButton.disabled = false;
    }
  }

  async function sendReply(event) {
    event.preventDefault();
    if (!selected || !compose.checkValidity()) return;
    const body = textarea.value.trim();
    if (!body) return;
    if (!pendingMessage || pendingMessage.body !== body) {
      pendingMessage = { body, id: messageId() };
    }
    const original = selected;
    textarea.disabled = true;
    compose.querySelector('button[type="submit"]').disabled = true;
    clearNotice();
    try {
      const data = await request(`/admin/support/api/conversations/${encodeURIComponent(selected.id)}/messages`, {
        method: 'POST',
        body: JSON.stringify({ body, client_message_id: pendingMessage.id }),
      });
      appendMessage(data.message);
      textarea.value = '';
      pendingMessage = null;
      refreshList();
    } catch (requestError) {
      showNotice(requestError.message);
    } finally {
      textarea.disabled = false;
      compose.querySelector('button[type="submit"]').disabled = false;
      if (selected === original) textarea.focus();
    }
  }

  async function closeConversation() {
    if (!selected || !window.confirm('Close this support conversation?')) return;
    clearNotice();
    closeButton.disabled = true;
    try {
      const data = await request(`/admin/support/api/conversations/${encodeURIComponent(selected.id)}/close`, { method: 'POST' });
      updateSelected(data.conversation);
      await refreshList();
    } catch (requestError) {
      showNotice(requestError.message);
    } finally {
      closeButton.disabled = false;
    }
  }

  availability.addEventListener('change', sendHeartbeat);
  compose.addEventListener('submit', sendReply);
  textarea.addEventListener('input', () => {
    if (!socket?.connected || !selected || compose.hidden) return;
    if (!agentTyping) {
      agentTyping = true;
      socket.emit('typing', { conversation_id: selected.id, typing: true, csrf_token: csrfToken });
    }
    clearTimeout(typingTimer);
    typingTimer = window.setTimeout(() => {
      agentTyping = false;
      if (selected) socket.emit('typing', { conversation_id: selected.id, typing: false, csrf_token: csrfToken });
    }, 1400);
  });
  claimButton.addEventListener('click', claimConversation);
  closeButton.addEventListener('click', closeConversation);
  root.querySelector('[data-support-admin-refresh]').addEventListener('click', refreshList);

  connectSocket();
  const linkedConversation = new URLSearchParams(window.location.search).get('conversation');
  refreshList().then(() => {
    if (!linkedConversation) return;
    const conversation = conversations.find((item) => item.id === linkedConversation);
    if (conversation) {
      selectConversation(conversation);
    } else {
      showNotice('This conversation is not currently open in the support inbox.');
    }
  });
  window.setInterval(refreshList, 8000);
  window.setInterval(sendHeartbeat, 20000);
})();
