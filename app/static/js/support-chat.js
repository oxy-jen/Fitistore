/* From Uiverse.io by vinodjangid07 - https://uiverse.io/vinodjangid07/nervous-gecko-43
   MIT License, Copyright (c) vinodjangid07 (Vinod Jangid) */
(() => {
  const root = document.querySelector('[data-support-widget]');
  if (!root) return;

  const launcher = root.querySelector('[data-support-launcher]');
  const panel = root.querySelector('[data-support-panel]');
  const closeButton = root.querySelector('[data-support-close]');
  const settingsToggle = root.querySelector('[data-support-settings-toggle]');
  const settingsMenu = root.querySelector('[data-support-settings-menu]');
  const settingsStatus = root.querySelector('[data-support-settings-status]');
  const downloadTranscript = root.querySelector('[data-support-download]');
  const maximizeButton = root.querySelector('[data-support-maximize]');
  const notificationsToggle = root.querySelector('[data-support-notifications]');
  const presenceIndicator = root.querySelector('[data-support-presence]');
  const agentName = root.querySelector('[data-support-agent-name]');
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
  const botTyping = root.querySelector('[data-support-bot-typing]');
  const availability = root.querySelector('[data-support-availability]');
  const loaderMessage = root.querySelector('[data-support-loader-message]');
  const restartButton = root.querySelector('[data-support-restart]');
  const quickReplies = root.querySelector('[data-support-quick-replies]');
  const signInGate = root.querySelector('[data-support-signin]');
  const prechat = root.querySelector('[data-support-prechat]');
  const prechatForm = root.querySelector('[data-support-first-form]');
  const prechatError = root.querySelector('[data-support-prechat-error]');
  const prechatTyping = root.querySelector('[data-support-prechat-typing]');
  const topicPrompt = root.querySelector('[data-support-topic-prompt]');
  const suggestions = root.querySelector('[data-support-suggestions]');
  const fileInput = root.querySelector('[data-support-file]');
  const attachmentPreview = root.querySelector('[data-support-attachment-preview]');
  const attachmentName = root.querySelector('[data-support-attachment-name]');
  const removeAttachment = root.querySelector('[data-support-remove-attachment]');
  const firstNameInput = prechatForm.elements.first_name;
  const lastNameInput = prechatForm.elements.last_name;
  const firstMessageInput = prechatForm.elements.body;
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
  const topicPrompts = {
    general: {
      prompt: 'What would you like help with today?',
      suggestions: ['I need help with an order.', 'I have a question about FityStores.'],
    },
    orders: {
      prompt: 'What would you like to know about your order?',
      suggestions: ['I already placed an order.', 'I need help placing an order.'],
    },
    payments: {
      prompt: 'Is this about confirming a payment or choosing how to pay?',
      suggestions: ['I need help confirming my payment.', 'What payment methods can I use?'],
    },
    delivery: {
      prompt: 'Would you like delivery information or help with an order on its way?',
      suggestions: ['When will my order be delivered?', 'I need help with an existing delivery.'],
    },
    returns: {
      prompt: 'What can we help with for your return or refund?',
      suggestions: ['I need help returning an item.', 'I have a question about a refund.'],
    },
    products: {
      prompt: 'What would you like to know about a product?',
      suggestions: ['Is this product available?', 'I have a question about a product.'],
    },
  };

  const isAuthenticated = root.dataset.supportAuthenticated === 'true';
  let conversation = null;
  let selectedTopic = null;
  let cursor = 0;
  let socket = null;
  let messageTimer = null;
  let typingTimer = null;
  let customerTyping = false;
  let loading = false;
  let sendingMessage = false;
  let pendingMessage = null;
  let pendingInitialMessage = null;
  let notificationsEnabled = false;
  const renderedIds = new Set();
  const optimisticMessages = new Map();

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
    const isFormData = typeof FormData !== 'undefined' && options.body instanceof FormData;
    const response = await fetch(url, {
      credentials: 'same-origin',
      cache: 'no-store',
      ...options,
      headers: {
        Accept: 'application/json',
        ...(options.body && !isFormData ? { 'Content-Type': 'application/json' } : {}),
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
    if (!open) {
      settingsMenu.hidden = true;
      settingsToggle.setAttribute('aria-expanded', 'false');
    }
    launcher.setAttribute('aria-expanded', String(open));
    if (open) {
      closeButton.focus();
      refreshPresence();
      if (conversation) refreshMessages();
    } else {
      launcher.focus();
    }
    updateMessagePolling();
  }

  function appendMessage(message) {
    if (!message) return;
    const optimistic = message.client_message_id
      ? optimisticMessages.get(message.client_message_id)
      : null;
    if (optimistic) {
      optimistic.replaceWith(createMessageElement(message));
      optimisticMessages.delete(message.client_message_id);
      renderedIds.add(message.id);
      messages.scrollTop = messages.scrollHeight;
      return;
    }
    if (renderedIds.has(message.id)) return;
    renderedIds.add(message.id);
    messages.append(createMessageElement(message));
    messages.scrollTop = messages.scrollHeight;
    if (
      notificationsEnabled
      && document.visibilityState === 'hidden'
      && message.sender_type === 'agent'
      && message.sender_name !== 'FityStores Assistant'
      && 'Notification' in window
      && Notification.permission === 'granted'
    ) {
      try {
        new Notification(message.sender_name || 'FityStores customer care', {
          body: message.body,
          tag: `fity-support-${message.id}`,
        });
      } catch {
        notificationsEnabled = false;
        notificationsToggle.checked = false;
        settingsStatus.textContent = 'Your browser could not display this notification.';
        settingsStatus.hidden = false;
      }
    }
  }

  function createMessageElement(message) {
    const article = document.createElement('article');
    article.className = `fity-support-message${message.sender_type === 'customer' ? ' is-customer' : ''}`;
    const sender = document.createElement('strong');
    sender.textContent = message.sender_type === 'customer' ? 'You' : (message.sender_name || 'FityStores Agent');
    const body = document.createElement('p');
    body.textContent = message.body;
    if (message.attachment?.url) {
      const attachment = document.createElement('a');
      attachment.className = 'fity-support-message-attachment';
      attachment.href = message.attachment.url;
      attachment.textContent = `📎 ${message.attachment.filename}`;
      article.append(attachment);
    }
    const time = document.createElement('time');
    const date = new Date(message.created_at);
    time.dateTime = Number.isNaN(date.getTime()) ? '' : date.toISOString();
    time.textContent = Number.isNaN(date.getTime())
      ? ''
      : date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    article.append(sender, body, time);
    return article;
  }

  function appendOptimisticMessage(body, clientMessageId) {
    const article = createMessageElement({
      id: `pending-${clientMessageId}`,
      sender_type: 'customer',
      sender_name: 'You',
      body,
      created_at: new Date().toISOString(),
    });
    article.classList.add('is-pending');
    article.querySelector('time').textContent = 'Sending…';
    optimisticMessages.set(clientMessageId, article);
    messages.append(article);
    messages.scrollTop = messages.scrollHeight;
  }

  function removeOptimisticMessage(clientMessageId) {
    const article = optimisticMessages.get(clientMessageId);
    if (article) article.remove();
    optimisticMessages.delete(clientMessageId);
  }

  function updateConversation(next) {
    conversation = next;
    topicLabel.textContent = topicNames[next.topic] || 'General help';
    agentName.textContent = next.assigned || 'FityStores Support';
    presenceIndicator.classList.toggle('is-assigned', next.status === 'assigned');
    presenceIndicator.setAttribute(
      'aria-label',
      next.status === 'assigned' ? 'Customer care agent connected' : 'Customer care availability',
    );
    assignment.textContent = next.status === 'assigned'
      ? `Chatting with ${next.assigned || 'customer care'}`
      : next.status === 'closed'
        ? 'This conversation is closed'
        : next.escalation_requested
          ? 'Connecting you with customer care…'
          : 'FityStores virtual assistant';
    textarea.disabled = next.status === 'closed';
    form.querySelector('button[type="submit"]').disabled = next.status === 'closed';
    fileInput.disabled = next.status === 'closed';
    downloadTranscript.disabled = false;
    if (next.status === 'closed') {
      loader.hidden = true;
      quickReplies.replaceChildren();
      presenceIndicator.classList.add('is-assigned');
      showError('This chat is complete. Start another chat whenever you need us.');
      restartButton.hidden = false;
    }
    if (next.status === 'assigned') {
      loader.hidden = true;
      quickReplies.replaceChildren();
    } else if (next.escalation_requested) {
      loaderMessage.textContent = (
        'We’re finding an available customer care agent. Thanks for your patience; '
        + 'we’ll update this chat as soon as someone joins.'
      );
      loader.hidden = false;
      quickReplies.replaceChildren();
    } else {
      loader.hidden = true;
    }
  }

  async function refreshMessages() {
    const activeConversation = conversation;
    if (!activeConversation || loading) return;
    loading = true;
    try {
      const data = await request(`/support/conversations/${encodeURIComponent(activeConversation.id)}/messages?after=${cursor}`);
      if (conversation?.id !== activeConversation.id) return;
      updateConversation(data.conversation);
      data.messages.forEach(appendMessage);
      if (data.messages.length) cursor = Math.max(cursor, ...data.messages.map((message) => message.id));
    } catch (requestError) {
      if (conversation?.id === activeConversation.id) showError(requestError.message);
    } finally {
      loading = false;
    }
  }

  function updateMessagePolling() {
    if (messageTimer) window.clearInterval(messageTimer);
    messageTimer = null;
    if (!conversation || panel.hidden || socket?.connected) return;
    messageTimer = window.setInterval(() => {
      if (conversation && !panel.hidden && !socket?.connected) refreshMessages();
    }, 2500);
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
    socket.on('connect', () => {
      joinConversation();
      refreshMessages();
      updateMessagePolling();
    });
    socket.on('message', (message) => {
      if (conversation?.id === message?.conversation_id) appendMessage(message);
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
      if (conversation) {
        typing.hidden = true;
        customerTyping = false;
      }
      updateMessagePolling();
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
    if (!conversation || conversation.status !== 'assigned') {
      presenceIndicator.classList.toggle('is-available', available);
    }
  }

  function renderQuickReplies(replies) {
    quickReplies.replaceChildren();
    (replies || []).forEach((reply) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'fity-support-quick-reply';
      button.textContent = reply.label;
      button.addEventListener('click', () => {
        if (sendingMessage || button.disabled) return;
        if (reply.action === 'escalate') {
          requestCustomerCare();
        } else if (reply.action === 'resolve') {
          resolveConversation();
        } else if (reply.message) {
          sendCustomerMessage(reply.message);
        }
      });
      quickReplies.append(button);
    });
  }

  function resumedQuickReplies(topic) {
    const messagesByTopic = {
      general: ['I need help with an order', 'I have a payment question', 'I have a delivery question'],
      orders: ['I need an order confirmation', 'I need to track my order', 'I have a payment issue'],
      payments: ['I have already paid', 'What payment methods can I use?'],
      delivery: ['I have an existing delivery', 'What delivery options are available?'],
      returns: ['I need help with a return', 'I have a question about a refund'],
      products: ['Is this product available?', 'I have a product question'],
    };
    return [
      ...(messagesByTopic[topic] || messagesByTopic.general).map((message) => ({
        label: message,
        message,
        action: 'reply',
      })),
      { label: 'Connect me to customer care', action: 'escalate' },
    ];
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
    if (!isAuthenticated) return;
    selectedTopic = topic;
    topics.hidden = true;
    prechat.hidden = false;
    thread.hidden = true;
    signInGate.hidden = true;
    prechatError.hidden = true;
    prechatError.textContent = '';
    topicPrompt.textContent = topicPrompts[topic]?.prompt || topicPrompts.general.prompt;
    suggestions.replaceChildren();
    (topicPrompts[topic]?.suggestions || topicPrompts.general.suggestions).forEach((suggestion) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'fity-support-suggestion';
      button.textContent = suggestion;
      button.addEventListener('click', () => {
        firstMessageInput.value = suggestion;
        firstMessageInput.focus();
      });
      suggestions.append(button);
    });
    firstNameInput.focus();
  }

  async function submitInitialMessage(event) {
    event.preventDefault();
    if (!isAuthenticated || !selectedTopic || !prechatForm.reportValidity()) return;
    const body = firstMessageInput.value.trim();
    const firstName = firstNameInput.value.trim();
    const lastName = lastNameInput.value.trim();
    if (!body || !firstName || !lastName) return;

    const submitButton = prechatForm.querySelector('button[type="submit"]');
    const draftKey = JSON.stringify([selectedTopic, firstName, lastName, body]);
    if (!pendingInitialMessage || pendingInitialMessage.draftKey !== draftKey) {
      pendingInitialMessage = { draftKey, id: newMessageId() };
    }
    submitButton.disabled = true;
    prechatTyping.hidden = false;
    prechatError.hidden = true;
    try {
      const data = await request('/support/conversations', {
        method: 'POST',
        body: JSON.stringify({
          topic: selectedTopic,
          first_name: firstName,
          last_name: lastName,
          body,
          client_message_id: pendingInitialMessage.id,
        }),
      });
      conversation = data.conversation;
      updateConversation(conversation);
      connectSocket();
      joinConversation();
      topics.hidden = true;
      signInGate.hidden = true;
      prechat.hidden = true;
      thread.hidden = false;
      messages.replaceChildren();
      renderedIds.clear();
      cursor = 0;
      optimisticMessages.clear();
      appendMessage(data.message);
      appendMessage(data.bot_message);
      cursor = Math.max(data.message.id, data.bot_message.id);
      renderQuickReplies(data.quick_replies);
      updateMessagePolling();
      prechatForm.reset();
      pendingInitialMessage = null;
    } catch (requestError) {
      prechatError.textContent = requestError.message;
      prechatError.hidden = false;
    } finally {
      prechatTyping.hidden = true;
      submitButton.disabled = false;
    }
  }

  async function restoreConversation() {
    if (!isAuthenticated) return;
    try {
      const data = await request('/support/conversations');
      if (!data.conversations?.length) return;
      conversation = data.conversations[0];
      topics.hidden = true;
      thread.hidden = false;
      updateConversation(conversation);
      connectSocket();
      joinConversation();
      await refreshMessages();
      if (!conversation.escalation_requested && conversation.status !== 'closed') {
        renderQuickReplies(resumedQuickReplies(conversation.topic));
      }
    } catch {
      availability.textContent = 'Saved chat history could not be checked. Please try again.';
    }
  }

  async function sendCustomerMessage(body, file = null) {
    if (sendingMessage || !conversation || (!body && !file) || conversation.status === 'closed') return;
    const fileKey = file ? `${file.name}:${file.size}:${file.lastModified}` : '';
    if (!pendingMessage || pendingMessage.body !== body || pendingMessage.fileKey !== fileKey) {
      pendingMessage = { body, fileKey, id: newMessageId() };
    }
    const messageId = pendingMessage.id;
    sendingMessage = true;
    form.querySelector('button[type="submit"]').disabled = true;
    quickReplies.querySelectorAll('button').forEach((button) => { button.disabled = true; });
    botTyping.hidden = false;
    clearError();
    appendOptimisticMessage(body || 'Shared an attachment.', messageId);
    try {
      const requestBody = file
        ? (() => {
          const formData = new FormData();
          formData.append('body', body);
          formData.append('client_message_id', messageId);
          formData.append('attachment', file);
          return formData;
        })()
        : JSON.stringify({ body, client_message_id: messageId });
      const data = await request(`/support/conversations/${encodeURIComponent(conversation.id)}/messages`, {
        method: 'POST',
        body: requestBody,
      });
      appendMessage(data.message);
      appendMessage(data.bot_message);
      if (data.bot_message) renderQuickReplies(data.quick_replies);
      if (textarea.value.trim() === body) textarea.value = '';
      if (file) {
        fileInput.value = '';
        attachmentPreview.hidden = true;
        attachmentName.textContent = '';
      }
      pendingMessage = null;
      if (data.message.id) cursor = Math.max(cursor, data.message.id);
      if (data.bot_message?.id) cursor = Math.max(cursor, data.bot_message.id);
      clearTimeout(typingTimer);
      customerTyping = false;
      if (socket?.connected) socket.emit('typing', { conversation_id: conversation.id, typing: false, csrf_token: csrfToken });
    } catch (requestError) {
      removeOptimisticMessage(messageId);
      showError(requestError.message);
      if (!textarea.value.trim()) textarea.value = body;
    } finally {
      sendingMessage = false;
      botTyping.hidden = true;
      textarea.disabled = conversation?.status === 'closed';
      form.querySelector('button[type="submit"]').disabled = conversation?.status === 'closed';
      quickReplies.querySelectorAll('button').forEach((button) => { button.disabled = false; });
    }
  }

  async function requestCustomerCare() {
    if (!conversation || conversation.status === 'closed') return;
    clearError();
    loaderMessage.textContent = 'Contacting customer care…';
    loader.hidden = false;
    quickReplies.replaceChildren();
    try {
      const data = await request(
        `/support/conversations/${encodeURIComponent(conversation.id)}/escalate`,
        { method: 'POST' },
      );
      updateConversation(data.conversation);
      appendMessage(data.customer_message);
      appendMessage(data.bot_message);
      cursor = Math.max(cursor, data.customer_message?.id || 0, data.bot_message?.id || 0);
    } catch (requestError) {
      loader.hidden = true;
      showError(requestError.message);
      renderQuickReplies(resumedQuickReplies(conversation.topic));
    }
  }

  async function resolveConversation() {
    if (!conversation || conversation.status === 'closed') return;
    clearError();
    try {
      const data = await request(
        `/support/conversations/${encodeURIComponent(conversation.id)}/resolve`,
        { method: 'POST' },
      );
      appendMessage(data.customer_message);
      appendMessage(data.bot_message);
      updateConversation(data.conversation);
      cursor = Math.max(cursor, data.customer_message.id, data.bot_message.id);
    } catch (requestError) {
      showError(requestError.message);
    }
  }

  function sendMessage(event) {
    event.preventDefault();
    sendCustomerMessage(textarea.value.trim(), fileInput.files?.[0] || null);
  }

  async function downloadTranscriptFile() {
    if (!conversation) return;
    downloadTranscript.disabled = true;
    settingsStatus.textContent = 'Preparing your transcript…';
    settingsStatus.hidden = false;
    try {
      const response = await fetch(
        `/support/conversations/${encodeURIComponent(conversation.id)}/transcript`,
        { credentials: 'same-origin', cache: 'no-store' },
      );
      if (!response.ok) throw new Error('The transcript could not be downloaded. Please try again.');
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = objectUrl;
      link.download = `fitystores-chat-${conversation.id}.txt`;
      document.body.append(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(objectUrl);
      settingsStatus.textContent = 'Your chat transcript has been downloaded.';
    } catch (requestError) {
      settingsStatus.textContent = requestError.message;
    } finally {
      downloadTranscript.disabled = !conversation;
    }
  }

  async function setNotificationsEnabled(enabled) {
    settingsStatus.hidden = true;
    if (!enabled) {
      notificationsEnabled = false;
      try { window.localStorage.setItem('fity-support-notifications', 'false'); } catch { /* Storage can be disabled. */ }
      notificationsToggle.checked = false;
      return;
    }
    if (!('Notification' in window)) {
      notificationsToggle.checked = false;
      settingsStatus.textContent = 'This browser does not support chat notifications.';
      settingsStatus.hidden = false;
      return;
    }
    let permission = Notification.permission;
    if (permission === 'default') permission = await Notification.requestPermission();
    if (permission !== 'granted') {
      notificationsToggle.checked = false;
      settingsStatus.textContent = permission === 'denied'
        ? 'Allow notifications in your browser settings to turn this on.'
        : 'Notifications were not enabled.';
      settingsStatus.hidden = false;
      return;
    }
    notificationsEnabled = true;
    try { window.localStorage.setItem('fity-support-notifications', 'true'); } catch { /* Storage can be disabled. */ }
    notificationsToggle.checked = true;
    settingsStatus.textContent = 'You’ll be notified when customer care replies while this page is in the background.';
    settingsStatus.hidden = false;
  }

  function initializeSettings() {
    try {
      notificationsEnabled = window.localStorage.getItem('fity-support-notifications') === 'true';
    } catch {
      notificationsEnabled = false;
    }
    notificationsToggle.checked = notificationsEnabled;
    if (notificationsEnabled && (!('Notification' in window) || Notification.permission !== 'granted')) {
      notificationsEnabled = false;
      notificationsToggle.checked = false;
    }
  }

  launcher.addEventListener('click', () => setOpen(panel.hidden));
  closeButton.addEventListener('click', () => setOpen(false));
  settingsToggle.addEventListener('click', () => {
    settingsMenu.hidden = !settingsMenu.hidden;
    settingsToggle.setAttribute('aria-expanded', String(!settingsMenu.hidden));
  });
  downloadTranscript.addEventListener('click', downloadTranscriptFile);
  maximizeButton.addEventListener('click', () => {
    const maximized = panel.classList.toggle('is-maximized');
    maximizeButton.textContent = maximized ? 'Restore window' : 'Maximize window';
    maximizeButton.setAttribute('aria-pressed', String(maximized));
  });
  notificationsToggle.addEventListener('change', () => {
    setNotificationsEnabled(notificationsToggle.checked).catch(() => {
      notificationsToggle.checked = false;
      notificationsEnabled = false;
      settingsStatus.textContent = 'Notifications could not be enabled. Check your browser settings.';
      settingsStatus.hidden = false;
    });
  });
  fileInput.addEventListener('change', () => {
    const file = fileInput.files?.[0];
    if (!file) {
      attachmentPreview.hidden = true;
      attachmentName.textContent = '';
      return;
    }
    if (file.size > 3 * 1024 * 1024) {
      fileInput.value = '';
      attachmentPreview.hidden = true;
      showError('Attachments must be 3 MB or smaller.');
      return;
    }
    clearError();
    attachmentName.textContent = `${file.name} · ${(file.size / 1024 / 1024).toFixed(2)} MB`;
    attachmentPreview.hidden = false;
    textarea.focus();
  });
  removeAttachment.addEventListener('click', () => {
    fileInput.value = '';
    attachmentPreview.hidden = true;
    attachmentName.textContent = '';
    fileInput.focus();
  });
  root.querySelectorAll('[data-support-topic]').forEach((button) => {
    button.addEventListener('click', () => openConversation(button.dataset.supportTopic));
  });
  root.querySelector('[data-support-back]').addEventListener('click', () => {
    prechat.hidden = true;
    topics.hidden = false;
    prechatError.hidden = true;
    selectedTopic = null;
    pendingInitialMessage = null;
  });
  prechatForm.addEventListener('submit', submitInitialMessage);
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
    pendingMessage = null;
    renderedIds.clear();
    optimisticMessages.clear();
    messages.replaceChildren();
    quickReplies.replaceChildren();
    clearError();
    loader.hidden = true;
    restartButton.hidden = true;
    thread.hidden = true;
    prechat.hidden = true;
    topics.hidden = !isAuthenticated;
    signInGate.hidden = isAuthenticated;
    selectedTopic = null;
    updateMessagePolling();
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !panel.hidden) {
      if (!settingsMenu.hidden) {
        settingsMenu.hidden = true;
        settingsToggle.setAttribute('aria-expanded', 'false');
        settingsToggle.focus();
      } else {
        setOpen(false);
      }
    }
  });
  document.addEventListener('click', (event) => {
    if (!settingsMenu.hidden && !event.target.closest('.fity-support-header')) {
      settingsMenu.hidden = true;
      settingsToggle.setAttribute('aria-expanded', 'false');
    }
  });

  window.setInterval(() => {
    if (!panel.hidden) refreshPresence();
  }, 30000);
  avoidCookieNoticeOverlap();
  initializeSettings();
  refreshPresence();
  restoreConversation();
  if (new URLSearchParams(window.location.search).get('support_chat') === '1') {
    setOpen(true);
    if (isAuthenticated) topics.hidden = false;
  }
})();
