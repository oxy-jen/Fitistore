(() => {
  const root = document.querySelector('[data-support-admin]');
  if (!root) return;

  const list = root.querySelector('[data-support-admin-conversations]');
  const searchInput = root.querySelector('[data-support-admin-search]');
  const sortSelect = root.querySelector('[data-support-admin-sort]');
  const countLabel = root.querySelector('[data-support-admin-count]');
  const filterButtons = [...root.querySelectorAll('[data-support-admin-filter]')];
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
  const claimBanner = root.querySelector('[data-support-admin-claim-banner]');
  const fileInput = root.querySelector('[data-support-admin-file]');
  const attachmentPreview = root.querySelector('[data-support-admin-attachment-preview]');
  const attachmentName = root.querySelector('[data-support-admin-attachment-name]');
  const removeAttachment = root.querySelector('[data-support-admin-remove-attachment]');
  const avatar = root.querySelector('[data-support-admin-avatar]');
  const emailLabel = root.querySelector('[data-support-admin-email]');
  const liveDot = root.querySelector('[data-support-admin-live-dot]');
  const detailsPanel = root.querySelector('[data-support-admin-details]');
  const detailsToggle = root.querySelector('[data-support-admin-details-toggle]');
  const notificationsToggle = root.querySelector('[data-support-admin-notifications]');
  const menuToggle = root.querySelector('[data-support-admin-menu-toggle]');
  const moreMenu = root.querySelector('[data-support-admin-more-menu]');
  const downloadButton = root.querySelector('[data-support-admin-download]');
  const copyLinkButton = root.querySelector('[data-support-admin-copy-link]');
  const emojiToggle = root.querySelector('[data-support-admin-emoji-toggle]');
  const emojiPicker = root.querySelector('[data-support-admin-emoji-picker]');
  const detailsClose = root.querySelector('[data-support-admin-details-close]');
  const mobileBack = root.querySelector('[data-support-admin-mobile-back]');
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
  let socketClientLoading = false;
  let socketRetryAt = 0;
  let messageTimer = null;
  let listRefreshTimer = null;
  let typingTimer = null;
  let loadingConversationId = null;
  let agentTyping = false;
  let pendingMessage = null;
  let selectedFilter = 'all';
  let notificationsEnabled = false;
  const renderedIds = new Set();

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
    article.append(sender, body);
    if (message.attachment?.url) {
      const attachment = document.createElement('a');
      attachment.className = 'support-admin-message-attachment';
      attachment.href = message.attachment.url;
      attachment.textContent = `📎 ${message.attachment.filename}`;
      article.append(attachment);
    }
    article.append(time);
    messages.append(article);
    messages.scrollTop = messages.scrollHeight;
  }

  function initials(name) {
    return String(name || 'Customer')
      .trim()
      .split(/\s+/)
      .slice(0, 2)
      .map((part) => part.charAt(0).toUpperCase())
      .join('') || 'C';
  }

  function relativeTime(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '';
    const seconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
    if (seconds < 60) return 'now';
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
    if (seconds < 604800) return `${Math.floor(seconds / 86400)}d`;
    return date.toLocaleDateString([], { month: 'short', day: 'numeric' });
  }

  function statusLabel(conversation) {
    if (conversation.status === 'assigned') return `Assigned to ${conversation.assigned || 'an agent'}`;
    if (conversation.escalation_requested) return 'Needs agent';
    return 'Self-service';
  }

  function filteredConversations() {
    const query = searchInput.value.trim().toLocaleLowerCase();
    const filtered = conversations.filter((conversation) => {
      if (selectedFilter === 'needs-agent'
          && !(conversation.status === 'waiting' && conversation.escalation_requested)) return false;
      if (selectedFilter === 'assigned' && conversation.status !== 'assigned') return false;
      if (selectedFilter === 'self-service'
          && !(conversation.status === 'waiting' && !conversation.escalation_requested)) return false;
      if (!query) return true;
      const lastMessage = conversation.last_message?.body || '';
      return [
        conversation.customer_name,
        conversation.customer_email,
        topicNames[conversation.topic],
        lastMessage,
      ].some((value) => String(value || '').toLocaleLowerCase().includes(query));
    });
    const direction = sortSelect.value === 'oldest' ? 1 : -1;
    return filtered.sort((left, right) => (
      direction * (
        new Date(left.last_message_at || left.created_at).getTime()
        - new Date(right.last_message_at || right.created_at).getTime()
      )
    ));
  }

  function updateFilterCounts() {
    const counts = {
      all: conversations.length,
      'needs-agent': conversations.filter((item) => item.status === 'waiting' && item.escalation_requested).length,
      assigned: conversations.filter((item) => item.status === 'assigned').length,
      'self-service': conversations.filter((item) => item.status === 'waiting' && !item.escalation_requested).length,
    };
    Object.entries(counts).forEach(([key, count]) => {
      const counter = root.querySelector(`[data-support-admin-filter-count="${key}"]`);
      if (counter) counter.textContent = String(count);
    });
  }

  function updateSelected(conversation) {
    if (!conversation) return;
    if (selected?.id === conversation.id) conversation = { ...selected, ...conversation };
    selected = conversation;
    topic.textContent = topicNames[conversation.topic] || 'General help';
    const customerName = conversation.customer_name || conversation.customer_email || 'Customer conversation';
    const customerEmail = conversation.customer_email || '';
    title.textContent = customerName;
    emailLabel.textContent = customerEmail || 'Email unavailable';
    avatar.textContent = initials(customerName);
    root.querySelector('[data-support-admin-details-avatar]').textContent = initials(customerName);
    root.querySelector('[data-support-admin-details-name]').textContent = customerName;
    const detailsEmail = root.querySelector('[data-support-admin-details-email]');
    detailsEmail.textContent = customerEmail || 'Email unavailable';
    if (customerEmail) detailsEmail.href = `mailto:${customerEmail}`;
    else detailsEmail.removeAttribute('href');
    const assignedToMe = conversation.assigned === adminName;
    const isWaiting = conversation.status === 'waiting' && conversation.escalation_requested;
    status.textContent = conversation.status === 'assigned'
      ? `Assigned to ${conversation.assigned || 'an agent'}`
      : conversation.status === 'closed'
        ? 'Closed'
        : conversation.escalation_requested
          ? 'Waiting for an agent'
          : 'Customer is using self-service';
    claimBanner.hidden = !isWaiting;
    closeButton.hidden = !assignedToMe;
    compose.hidden = !assignedToMe || conversation.status === 'closed';
    liveDot.classList.toggle('is-online', conversation.status === 'assigned');
    liveDot.classList.toggle('is-closed', conversation.status === 'closed');
    detailsToggle.disabled = false;
    root.querySelector('[data-support-admin-details-topic]').textContent = topicNames[conversation.topic] || 'General help';
    root.querySelector('[data-support-admin-details-status]').textContent = statusLabel(conversation);
    root.querySelector('[data-support-admin-details-assigned]').textContent = conversation.assigned || 'Unassigned';
    const created = new Date(conversation.created_at);
    root.querySelector('[data-support-admin-details-created]').textContent = Number.isNaN(created.getTime())
      ? '—'
      : created.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
    root.querySelector('[data-support-admin-details-notification]').textContent = conversation.notification_status || 'Not recorded';
    fileInput.disabled = compose.hidden;
    if (compose.hidden) {
      attachmentPreview.hidden = true;
      attachmentName.textContent = '';
      fileInput.value = '';
    }
  }

  function renderConversationList() {
    list.replaceChildren();
    updateFilterCounts();
    const visibleConversations = filteredConversations();
    countLabel.textContent = `${visibleConversations.length} of ${conversations.length} conversations`;
    if (!visibleConversations.length) {
      const message = document.createElement('p');
      message.className = 'support-admin-empty-list';
      message.textContent = conversations.length
        ? 'No conversations match this search or filter.'
        : 'No open conversations right now.';
      list.append(message);
      return;
    }

    visibleConversations.forEach((conversation) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'support-admin-conversation-row';
      button.setAttribute('aria-current', String(selected?.id === conversation.id));
      const conversationAvatar = document.createElement('span');
      conversationAvatar.className = 'support-admin-row-avatar';
      const palette = ['is-coral', 'is-violet', 'is-teal', 'is-gold'];
      conversationAvatar.classList.add(palette[(conversation.id.length + conversation.id.charCodeAt(0)) % palette.length]);
      conversationAvatar.textContent = initials(conversation.customer_name || conversation.customer_email);
      if (conversation.status === 'assigned') {
        const online = document.createElement('i');
        online.setAttribute('aria-hidden', 'true');
        conversationAvatar.append(online);
      }
      const heading = document.createElement('div');
      heading.className = 'support-admin-row-heading';
      const name = document.createElement('strong');
      name.textContent = conversation.customer_name || conversation.customer_email || 'Customer';
      const time = document.createElement('time');
      time.textContent = relativeTime(conversation.last_message_at || conversation.created_at);
      heading.append(name, time);
      const excerpt = document.createElement('span');
      excerpt.className = 'support-admin-row-preview';
      excerpt.textContent = conversation.last_message?.body || 'No messages yet';
      if (conversation.last_message?.attachment) {
        excerpt.textContent = `Attachment · ${conversation.last_message.attachment.filename}`;
      }
      const details = document.createElement('div');
      details.className = 'support-admin-row-meta';
      const topicName = topicNames[conversation.topic] || 'General help';
      const topicBadge = document.createElement('span');
      topicBadge.textContent = topicName;
      const state = document.createElement('span');
      state.className = 'support-admin-row-state';
      if (conversation.escalation_requested && conversation.status === 'waiting') {
        state.classList.add('is-needs-agent');
      } else if (conversation.status === 'assigned') {
        state.classList.add('is-assigned');
      }
      state.textContent = statusLabel(conversation);
      details.append(topicBadge, document.createTextNode('·'), state);
      button.append(conversationAvatar, heading, excerpt, details);
      button.addEventListener('click', () => selectConversation(conversation));
      list.append(button);
    });
  }

  function scheduleListRefresh() {
    if (listRefreshTimer) window.clearTimeout(listRefreshTimer);
    listRefreshTimer = window.setTimeout(() => {
      listRefreshTimer = null;
      refreshList();
    }, 120);
  }

  function applyQueueUpdate(data) {
    if (!data?.conversation_id || !data.last_message) {
      scheduleListRefresh();
      return;
    }
    const index = conversations.findIndex((item) => item.id === data.conversation_id);
    if (index < 0) {
      scheduleListRefresh();
      return;
    }
    const updated = {
      ...conversations[index],
      status: data.status || conversations[index].status,
      escalation_requested: typeof data.escalation_requested === 'boolean'
        ? data.escalation_requested
        : conversations[index].escalation_requested,
      last_message: data.last_message,
      last_message_at: data.last_message.created_at || conversations[index].last_message_at,
    };
    conversations[index] = updated;
    if (selected?.id === updated.id) selected = { ...selected, ...updated };
    renderConversationList();
  }

  async function refreshList() {
    try {
      const data = await request('/admin/support/api/conversations');
      conversations = data.conversations || [];
      if (selected) {
        const updated = conversations.find((item) => item.id === selected.id);
        if (updated) updateSelected(updated);
      }
      renderConversationList();
    } catch (requestError) {
      showNotice(requestError.message);
    }
  }

  async function refreshMessages() {
    const conversation = selected;
    if (!conversation || loadingConversationId === conversation.id) return;
    loadingConversationId = conversation.id;
    try {
      const data = await request(`/admin/support/api/conversations/${encodeURIComponent(conversation.id)}/messages?after=${cursor}`);
      if (selected?.id !== conversation.id) return;
      updateSelected(data.conversation);
      data.messages.forEach(appendMessage);
      if (data.messages.length) cursor = Math.max(cursor, ...data.messages.map((message) => message.id));
    } catch (requestError) {
      if (selected?.id === conversation.id) showNotice(requestError.message);
    } finally {
      if (loadingConversationId === conversation.id) loadingConversationId = null;
    }
  }

  function updateMessagePolling() {
    if (messageTimer) window.clearInterval(messageTimer);
    messageTimer = null;
    if (!selected || socket?.connected) return;
    messageTimer = window.setInterval(() => {
      if (selected && !socket?.connected) refreshMessages();
    }, 4000);
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
      refreshList();
      if (selected) refreshMessages();
      updateMessagePolling();
    });
    socket.on('queue_update', applyQueueUpdate);
    socket.on('message', (message) => {
      if (selected?.id === message?.conversation_id) {
        const isNewMessage = !renderedIds.has(message.id);
        appendMessage(message);
        if (isNewMessage) showCustomerNotification(message);
      }
    });
    socket.on('typing', (data) => {
      if (data?.conversation_id === selected?.id && data.sender_type === 'customer') typing.hidden = !data.typing;
    });
    socket.on('closed', (data) => {
      if (data?.conversation?.id === selected?.id) updateSelected(data.conversation);
      scheduleListRefresh();
    });
    socket.on('disconnect', () => {
      showNotice('Support chat is reconnecting. The inbox will keep refreshing.');
      updateMessagePolling();
    });
  }

  function ensureSocketClient() {
    if (window.io) {
      connectSocket();
      return;
    }
    if (socketClientLoading || Date.now() < socketRetryAt) return;
    socketClientLoading = true;
    const script = document.createElement('script');
    script.src = 'https://cdn.socket.io/4.8.1/socket.io.min.js';
    script.integrity = 'sha384-mkQ3/7FUtcGyoppY6bz/PORYgOqOl7/aSUMn2ymDOJcapfS6PHqxhRTMh1RR0Q6+';
    script.crossOrigin = 'anonymous';
    script.onload = () => {
      socketClientLoading = false;
      if (!window.io) {
        socketRetryAt = Date.now() + 30000;
        showNotice('Live updates are unavailable; the inbox will keep refreshing.');
        return;
      }
      connectSocket();
    };
    script.onerror = () => {
      socketClientLoading = false;
      socketRetryAt = Date.now() + 30000;
      showNotice('Live updates are unavailable; the inbox will keep refreshing.');
    };
    document.head.append(script);
  }

  async function selectConversation(conversation) {
    clearNotice();
    moreMenu.hidden = true;
    emojiPicker.hidden = true;
    menuToggle.setAttribute('aria-expanded', 'false');
    emojiToggle.setAttribute('aria-expanded', 'false');
    selected = conversation;
    root.classList.add('is-chat-open');
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
    updateMessagePolling();
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
    if (!selected) return;
    const body = textarea.value.trim();
    const file = fileInput.files?.[0] || null;
    if (!body && !file) return;
    const fileKey = file ? `${file.name}:${file.size}:${file.lastModified}` : '';
    if (!pendingMessage || pendingMessage.body !== body || pendingMessage.fileKey !== fileKey) {
      pendingMessage = { body, fileKey, id: messageId() };
    }
    const original = selected;
    textarea.disabled = true;
    compose.querySelector('button[type="submit"]').disabled = true;
    clearNotice();
    try {
      const requestBody = file
        ? (() => {
          const formData = new FormData();
          formData.append('body', body);
          formData.append('client_message_id', pendingMessage.id);
          formData.append('attachment', file);
          return formData;
        })()
        : JSON.stringify({ body, client_message_id: pendingMessage.id });
      const data = await request(`/admin/support/api/conversations/${encodeURIComponent(selected.id)}/messages`, {
        method: 'POST',
        body: requestBody,
      });
      appendMessage(data.message);
      textarea.value = '';
      fileInput.value = '';
      attachmentPreview.hidden = true;
      attachmentName.textContent = '';
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

  function setDetailsVisible(visible) {
    detailsPanel.hidden = !visible;
    root.classList.toggle('has-details', visible);
    detailsToggle.setAttribute('aria-expanded', String(visible));
  }

  async function downloadTranscript() {
    if (!selected) return;
    downloadButton.disabled = true;
    try {
      const response = await fetch(
        `/admin/support/api/conversations/${encodeURIComponent(selected.id)}/transcript`,
        { credentials: 'same-origin', cache: 'no-store' },
      );
      if (!response.ok) throw new Error('The transcript could not be downloaded. Please try again.');
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = objectUrl;
      link.download = `fitystores-chat-${selected.id}.txt`;
      document.body.append(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
      moreMenu.hidden = true;
      menuToggle.setAttribute('aria-expanded', 'false');
    } catch (requestError) {
      showNotice(requestError.message);
    } finally {
      downloadButton.disabled = !selected;
    }
  }

  async function copyConversationLink() {
    if (!selected) return;
    const url = new URL(window.location.href);
    url.searchParams.set('conversation', selected.id);
    try {
      await navigator.clipboard.writeText(url.href);
      showNotice('Conversation link copied.');
      moreMenu.hidden = true;
      menuToggle.setAttribute('aria-expanded', 'false');
    } catch {
      showNotice('Could not copy the link. Copy it from the browser address bar instead.');
    }
  }

  async function toggleNotifications() {
    if (notificationsEnabled) {
      notificationsEnabled = false;
      notificationsToggle.setAttribute('aria-pressed', 'false');
      notificationsToggle.setAttribute('aria-label', 'Enable browser notifications');
      try { window.localStorage.setItem('fity-support-admin-notifications', 'false'); } catch { /* Storage can be disabled. */ }
      return;
    }
    if (!('Notification' in window)) {
      showNotice('This browser does not support desktop notifications.');
      return;
    }
    let permission = Notification.permission;
    if (permission === 'default') permission = await Notification.requestPermission();
    if (permission !== 'granted') {
      showNotice(permission === 'denied'
        ? 'Allow notifications in your browser settings to enable chat alerts.'
        : 'Browser notifications were not enabled.');
      return;
    }
    notificationsEnabled = true;
    notificationsToggle.setAttribute('aria-pressed', 'true');
    notificationsToggle.setAttribute('aria-label', 'Disable browser notifications');
    try { window.localStorage.setItem('fity-support-admin-notifications', 'true'); } catch { /* Storage can be disabled. */ }
    clearNotice();
  }

  function showCustomerNotification(message) {
    if (
      !notificationsEnabled
      || document.visibilityState !== 'hidden'
      || message.sender_type !== 'customer'
      || !('Notification' in window)
      || Notification.permission !== 'granted'
    ) return;
    try {
      new Notification(message.sender_name || 'FityStores customer', {
        body: message.body || 'Sent an attachment.',
        tag: `fity-support-admin-${message.id}`,
      });
    } catch {
      notificationsEnabled = false;
      notificationsToggle.setAttribute('aria-pressed', 'false');
      showNotice('Your browser could not display the chat notification.');
    }
  }

  function initializeNotifications() {
    try {
      notificationsEnabled = window.localStorage.getItem('fity-support-admin-notifications') === 'true';
    } catch {
      notificationsEnabled = false;
    }
    if (!('Notification' in window) || Notification.permission !== 'granted') {
      notificationsEnabled = false;
    }
    notificationsToggle.setAttribute('aria-pressed', String(notificationsEnabled));
    notificationsToggle.setAttribute(
      'aria-label',
      notificationsEnabled ? 'Disable browser notifications' : 'Enable browser notifications',
    );
  }

  function insertEmoji(emoji) {
    const start = textarea.selectionStart;
    const end = textarea.selectionEnd;
    textarea.setRangeText(emoji, start, end, 'end');
    textarea.focus();
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    emojiPicker.hidden = true;
    emojiToggle.setAttribute('aria-expanded', 'false');
  }

  availability.addEventListener('change', sendHeartbeat);
  compose.addEventListener('submit', sendReply);
  searchInput.addEventListener('input', renderConversationList);
  sortSelect.addEventListener('change', renderConversationList);
  filterButtons.forEach((button) => {
    button.addEventListener('click', () => {
      selectedFilter = button.dataset.supportAdminFilter;
      filterButtons.forEach((item) => {
        item.setAttribute('aria-pressed', String(item === button));
      });
      renderConversationList();
    });
  });
  detailsToggle.addEventListener('click', () => setDetailsVisible(detailsPanel.hidden));
  detailsClose.addEventListener('click', () => setDetailsVisible(false));
  notificationsToggle.addEventListener('click', () => {
    toggleNotifications().catch(() => showNotice('Browser notifications could not be changed.'));
  });
  menuToggle.addEventListener('click', () => {
    moreMenu.hidden = !moreMenu.hidden;
    menuToggle.setAttribute('aria-expanded', String(!moreMenu.hidden));
  });
  downloadButton.addEventListener('click', downloadTranscript);
  copyLinkButton.addEventListener('click', copyConversationLink);
  emojiToggle.addEventListener('click', () => {
    emojiPicker.hidden = !emojiPicker.hidden;
    emojiToggle.setAttribute('aria-expanded', String(!emojiPicker.hidden));
  });
  emojiPicker.querySelectorAll('button').forEach((button) => {
    button.addEventListener('click', () => insertEmoji(button.textContent));
  });
  mobileBack.addEventListener('click', () => {
    setDetailsVisible(false);
    root.classList.remove('is-chat-open');
  });
  root.querySelector('[data-support-admin-focus-availability]').addEventListener('click', () => {
    availability.focus();
    availability.scrollIntoView({ behavior: 'smooth', block: 'center' });
  });
  root.querySelector('[data-support-admin-refresh]').addEventListener('click', refreshList);
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
      showNotice('Attachments must be 3 MB or smaller.');
      return;
    }
    clearNotice();
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
  textarea.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      compose.requestSubmit();
    }
  });
  claimButton.addEventListener('click', claimConversation);
  closeButton.addEventListener('click', closeConversation);
  document.addEventListener('keydown', (event) => {
    if (event.key === '/' && !event.ctrlKey && !event.metaKey && !event.altKey) {
      const target = event.target;
      if (target instanceof HTMLElement && !['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)) {
        event.preventDefault();
        searchInput.focus();
      }
    }
    if (event.key === 'Escape') {
      moreMenu.hidden = true;
      emojiPicker.hidden = true;
      menuToggle.setAttribute('aria-expanded', 'false');
      emojiToggle.setAttribute('aria-expanded', 'false');
    }
  });
  document.addEventListener('click', (event) => {
    if (!(event.target instanceof Element)) return;
    if (!event.target.closest('.support-admin-actions')) {
      moreMenu.hidden = true;
      menuToggle.setAttribute('aria-expanded', 'false');
    }
    if (!event.target.closest('.support-admin-emoji-wrap')) {
      emojiPicker.hidden = true;
      emojiToggle.setAttribute('aria-expanded', 'false');
    }
  });

  initializeNotifications();
  ensureSocketClient();
  const linkedConversation = new URLSearchParams(window.location.search).get('conversation');
  refreshList().then(async () => {
    if (!linkedConversation) return;
    let conversation = conversations.find((item) => item.id === linkedConversation);
    if (!conversation) {
      try {
        const data = await request(
          `/admin/support/api/conversations/${encodeURIComponent(linkedConversation)}`,
        );
        conversation = data.conversation;
      } catch (requestError) {
        showNotice(requestError.message);
        return;
      }
    }
    if (conversation) selectConversation(conversation);
  });
  window.setInterval(() => {
    if (!socket?.connected) {
      ensureSocketClient();
      refreshList();
    }
  }, 8000);
  window.setInterval(sendHeartbeat, 20000);
})();
