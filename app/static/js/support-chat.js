/* From Uiverse.io by vinodjangid07 - https://uiverse.io/vinodjangid07/nervous-gecko-43
   MIT License, Copyright (c) vinodjangid07 (Vinod Jangid) */
(() => {
  const root = document.querySelector('[data-support-widget]');
  if (!root) return;

  const launcher = root.querySelector('[data-support-launcher]');
  const panel = root.querySelector('[data-support-panel]');
  const closeButton = root.querySelector('[data-support-close]');
  const topics = root.querySelector('[data-support-topics]');
  const thread = root.querySelector('[data-support-thread]');
  const topicLabel = root.querySelector('[data-support-topic-label]');
  const assignment = root.querySelector('[data-support-assignment]');
  const messages = root.querySelector('[data-support-messages]');
  const form = root.querySelector('[data-support-compose]');
  const textarea = form.querySelector('textarea');
  const loader = root.querySelector('[data-support-loader]');
  const error = root.querySelector('[data-support-error]');
  const typing = root.querySelector('[data-support-typing]');
  const availability = root.querySelector('[data-support-availability]');
  const greeting = root.querySelector('[data-support-greeting]');
  const loaderMessage = root.querySelector('[data-support-loader-message]');
  const restartButton = root.querySelector('[data-support-restart]');
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const cookieNotice = document.querySelector('[data-cookie-notice]');
  const topicNames = {
    general: 'General help',
    orders: 'Orders',
    payments: 'Payments',
    delivery: 'Delivery and collection',
    returns: 'Returns',
    products: 'Products',
  };

  let conversation = null;
  let cursor = 0;
  let socket = null;
  let typingTimer = null;
  let customerTyping = false;
  let loading = false;
  let pendingMessage = null;
  let handoffShown = false;
  let handoffAnimationTimer = null;
  const renderedIds = new Set();

  const customerName = root.dataset.supportCustomerName?.trim();
  greeting.textContent = `Hi ${customerName || 'there'}, welcome to FityStores! How can we help you today?`;

  function avoidCookieNoticeOverlap() {
    root.style.removeProperty('--support-launcher-bottom');
    root.style.removeProperty('--support-launcher-left');
    root.style.removeProperty('--support-launcher-right');
    root.style.removeProperty('--support-panel-left');
    root.style.removeProperty('--support-panel-right');
    if (!cookieNotice || !document.body.contains(cookieNotice)) return;
    const cookieBounds = cookieNotice.getBoundingClientRect();
    const launcherBounds = launcher.getBoundingClientRect();
    const overlaps = launcherBounds.right > cookieBounds.left
      && launcherBounds.left < cookieBounds.right
      && launcherBounds.bottom > cookieBounds.top
      && launcherBounds.top < cookieBounds.bottom;
    if (!overlaps) return;
    if (cookieBounds.left >= launcherBounds.width + 32) {
      root.style.setProperty('--support-launcher-left', '16px');
      root.style.setProperty('--support-launcher-right', 'auto');
      root.style.setProperty('--support-panel-left', '16px');
      root.style.setProperty('--support-panel-right', 'auto');
      return;
    }
    root.style.setProperty(
      '--support-launcher-bottom',
      `${Math.ceil(window.innerHeight - cookieBounds.top + 12)}px`,
    );
  }

  window.addEventListener('resize', avoidCookieNoticeOverlap, { passive: true });
  if (cookieNotice) {
    new MutationObserver(avoidCookieNoticeOverlap).observe(document.body, { childList: true });
    window.addEventListener('load', avoidCookieNoticeOverlap, { once: true });
    window.requestAnimationFrame(avoidCookieNoticeOverlap);
  }

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
        ? 'Your session changed. Refresh the page before using support chat.'
        : 'Support chat returned an unexpected response. Please try again.');
    }
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Support chat could not be reached. Please try again.');
    return data;
  }

  function showError(message) {
    error.textContent = message;
    error.hidden = false;
  }

  function clearError() {
    error.textContent = '';
    error.hidden = true;
  }

  function setOpen(open) {
    panel.hidden = !open;
    launcher.setAttribute('aria-expanded', String(open));
    if (open) {
      closeButton.focus();
      refreshPresence();
      if (conversation) refreshMessages();
    } else {
      launcher.focus();
    }
  }

  function appendMessage(message) {
    if (!message || renderedIds.has(message.id)) return;
    renderedIds.add(message.id);
    if (message.sender_type === 'agent' && message.sender_name === 'FityStores') handoffShown = true;
    const article = document.createElement('article');
    article.className = `fity-support-message${message.sender_type === 'customer' ? ' is-customer' : ''}`;
    const sender = document.createElement('strong');
    sender.textContent = message.sender_type === 'customer' ? 'You' : (message.sender_name || 'FityStores Agent');
    const body = document.createElement('p');
    body.textContent = message.body;
    const time = document.createElement('time');
    const date = new Date(message.created_at);
    time.dateTime = Number.isNaN(date.getTime()) ? '' : date.toISOString();
    time.textContent = Number.isNaN(date.getTime())
      ? ''
      : date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    article.append(sender, body, time);
    messages.append(article);
    messages.scrollTop = messages.scrollHeight;
  }

  function updateConversation(next) {
    conversation = next;
    topicLabel.textContent = topicNames[next.topic] || 'General help';
    assignment.textContent = next.status === 'assigned'
      ? `Chatting with ${next.assigned || 'an agent'}`
      : next.status === 'closed'
        ? 'This conversation is closed'
        : 'Waiting for an agent';
    textarea.disabled = next.status === 'closed';
    form.querySelector('button[type="submit"]').disabled = next.status === 'closed';
    if (next.status === 'closed') {
      showError('This conversation has been closed. Choose a topic to start a new one.');
      restartButton.hidden = false;
    }
    if (next.status === 'assigned') hideHandoffAnimation();
  }

  function showHandoffAnimation() {
    loaderMessage.textContent = 'Connecting you with customer care…';
    loader.hidden = false;
    clearTimeout(handoffAnimationTimer);
    handoffAnimationTimer = window.setTimeout(() => {
      if (conversation?.status !== 'assigned') loader.hidden = true;
    }, 1800);
  }

  function hideHandoffAnimation() {
    clearTimeout(handoffAnimationTimer);
    loader.hidden = true;
  }

  async function refreshMessages() {
    if (!conversation || loading) return;
    loading = true;
    try {
      const data = await request(`/support/conversations/${encodeURIComponent(conversation.id)}/messages?after=${cursor}`);
      updateConversation(data.conversation);
      data.messages.forEach(appendMessage);
      if (data.messages.length) cursor = Math.max(cursor, ...data.messages.map((message) => message.id));
    } catch (requestError) {
      showError(requestError.message);
      restartButton.hidden = false;
    } finally {
      loading = false;
    }
  }

  function joinConversation() {
    if (!socket || !conversation) return;
    socket.emit('join_conversation', {
      conversation_id: conversation.id,
      csrf_token: csrfToken,
    }, (result) => {
      if (result?.ok && result.conversation) updateConversation(result.conversation);
    });
  }

  function connectSocket() {
    if (!window.io || socket) return;
    socket = window.io('/support', {
      transports: ['websocket', 'polling'],
      reconnection: true,
      timeout: 8000,
    });
    socket.on('connect', joinConversation);
    socket.on('message', (message) => {
      if (conversation && message) appendMessage(message);
    });
    socket.on('assigned', (data) => {
      if (data?.conversation && data.conversation.id === conversation?.id) updateConversation(data.conversation);
    });
    socket.on('closed', (data) => {
      if (data?.conversation && data.conversation.id === conversation?.id) updateConversation(data.conversation);
    });
    socket.on('typing', (data) => {
      if (data?.conversation_id !== conversation?.id || data.sender_type === 'customer') return;
      typing.hidden = !data.typing;
    });
    socket.on('presence', (data) => updateAvailability(Boolean(data?.available)));
    socket.on('disconnect', () => {
      if (conversation) assignment.textContent = 'Reconnecting…';
    });
  }

  async function refreshPresence() {
    try {
      const data = await request('/support/presence');
      updateAvailability(Boolean(data.available));
    } catch {
      availability.textContent = 'Support status is temporarily unavailable';
    }
  }

  function updateAvailability(available) {
    availability.textContent = available ? 'An agent is available' : 'We’ll reply as soon as an agent is available';
  }

  function newMessageId() {
    if (window.crypto?.randomUUID) return window.crypto.randomUUID();
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  async function openConversation(topic) {
    clearError();
    topics.hidden = true;
    thread.hidden = false;
    loader.hidden = false;
    messages.replaceChildren();
    renderedIds.clear();
    cursor = 0;
    handoffShown = false;
    hideHandoffAnimation();
    try {
      const data = await request('/support/conversations', {
        method: 'POST',
        body: JSON.stringify({ topic }),
      });
      updateConversation(data.conversation);
      connectSocket();
      joinConversation();
      await refreshMessages();
    } catch (requestError) {
      showError(requestError.message);
      restartButton.hidden = false;
    } finally {
      loader.hidden = true;
    }
  }

  async function restoreConversation() {
    try {
      const data = await request('/support/conversations');
      if (!data.conversations?.length) return;
      conversation = data.conversations[0];
      topics.hidden = true;
      thread.hidden = false;
      updateConversation(conversation);
      loader.hidden = false;
      connectSocket();
      joinConversation();
      await refreshMessages();
    } catch {
      availability.textContent = 'Saved chat history could not be checked. Please try again.';
    } finally {
      loader.hidden = true;
    }
  }

  async function sendMessage(event) {
    event.preventDefault();
    const body = textarea.value.trim();
    if (!conversation || !body || conversation.status === 'closed') return;
    const showHandoff = !handoffShown && conversation.status !== 'assigned';
    if (!pendingMessage || pendingMessage.body !== body) {
      pendingMessage = { body, id: newMessageId() };
    }
    textarea.disabled = true;
    form.querySelector('button[type="submit"]').disabled = true;
    clearError();
    try {
      const data = await request(`/support/conversations/${encodeURIComponent(conversation.id)}/messages`, {
        method: 'POST',
        body: JSON.stringify({ body, client_message_id: pendingMessage.id }),
      });
      appendMessage(data.message);
      if (data.handoff_message) appendMessage(data.handoff_message);
      textarea.value = '';
      pendingMessage = null;
      if (showHandoff && data.handoff_message) showHandoffAnimation();
      if (socket?.connected) socket.emit('typing', { conversation_id: conversation.id, typing: false, csrf_token: csrfToken });
    } catch (requestError) {
      showError(requestError.message);
      restartButton.hidden = false;
    } finally {
      textarea.disabled = conversation?.status === 'closed';
      form.querySelector('button[type="submit"]').disabled = conversation?.status === 'closed';
      textarea.focus();
    }
  }

  launcher.addEventListener('click', () => setOpen(panel.hidden));
  closeButton.addEventListener('click', () => setOpen(false));
  root.querySelectorAll('[data-support-topic]').forEach((button) => {
    button.addEventListener('click', () => openConversation(button.dataset.supportTopic));
  });
  form.addEventListener('submit', sendMessage);

  textarea.addEventListener('input', () => {
    if (!socket?.connected || !conversation) return;
    if (!customerTyping) {
      customerTyping = true;
      socket.emit('typing', { conversation_id: conversation.id, typing: true, csrf_token: csrfToken });
    }
    clearTimeout(typingTimer);
    typingTimer = setTimeout(() => {
      customerTyping = false;
      if (conversation) socket.emit('typing', { conversation_id: conversation.id, typing: false, csrf_token: csrfToken });
    }, 1400);
  });

  restartButton.addEventListener('click', () => {
    conversation = null;
    cursor = 0;
    handoffShown = false;
    pendingMessage = null;
    renderedIds.clear();
    messages.replaceChildren();
    clearError();
    hideHandoffAnimation();
    restartButton.hidden = true;
    thread.hidden = true;
    topics.hidden = false;
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !panel.hidden) setOpen(false);
  });

  window.setInterval(() => {
    if (conversation && !panel.hidden) refreshMessages();
  }, 5000);
  window.setInterval(() => {
    if (!panel.hidden) refreshPresence();
  }, 30000);
  avoidCookieNoticeOverlap();
  refreshPresence();
  restoreConversation();
})();
