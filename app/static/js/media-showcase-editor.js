(function () {
  const form = document.querySelector('.showcase-editor');
  if (!form) return;
  const list = form.querySelector('[data-showcase-item-list]');
  const jsonField = form.querySelector('[name="items_json"]');
  const addButton = form.querySelector('[data-add-showcase-item]');
  const dataNode = document.getElementById('showcase-items-data');
  const products = JSON.parse(document.getElementById('showcase-products-data')?.textContent || '[]');
  const categories = JSON.parse(document.getElementById('showcase-categories-data')?.textContent || '[]');
  const dialog = document.querySelector('[data-media-library]');
  const libraryGrid = dialog?.querySelector('[data-library-grid]');
  const libraryNotice = dialog?.querySelector('[data-library-notice]');
  const libraryStatus = form.querySelector('[data-media-library-status]');
  const bulkUpload = form.querySelector('[data-bulk-upload]');
  const bulkUploadStatus = form.querySelector('[data-bulk-upload-status]');
  const bulkLinks = form.querySelector('[data-bulk-links]');
  const preview = document.querySelector('[data-live-preview]');
  const presetOptions = [
    ['', 'No preset link'], ['/shop', 'Shop all products'], ['/about', 'About FityStores'],
    ['/upcoming', 'Upcoming merchandise'], ['/#mamba-collection', 'Mamba collection'],
    ...categories.map((category) => [`/shop?category=${encodeURIComponent(category.slug)}`, `Category: ${category.name}`]),
  ];
  const choices = (parent, labelText, key, value, options, className = 'item-field') => {
    const label = document.createElement('label'); label.className = className; label.append(document.createTextNode(labelText));
    const select = document.createElement('select'); select.dataset.itemKey = key;
    options.forEach(([optionValue, caption]) => { const option = new Option(caption, optionValue); select.append(option); });
    select.value = value || options[0]?.[0] || ''; label.append(select); parent.append(label); return select;
  };
  const textField = (parent, labelText, key, value = '', type = 'text', className = 'item-field') => {
    const label = document.createElement('label'); label.className = className; label.append(document.createTextNode(labelText));
    const input = document.createElement(type === 'textarea' ? 'textarea' : 'input');
    if (type !== 'textarea') input.type = (key === 'url' || key === 'poster' || key === 'destination') ? 'text' : type;
    input.dataset.itemKey = key; input.value = value == null ? '' : value;
    if (key === 'url') input.placeholder = 'Paste a direct media URL or YouTube/Vimeo link';
    if (key === 'aspect_ratio') input.placeholder = 'Auto, 9:16, 16:9, 1:1, 1.85:1';
    if (key === 'poster') input.placeholder = 'Optional thumbnail/poster URL';
    label.append(input); parent.append(label); return input;
  };
  const rangeField = (parent, labelText, key, value = 50) => {
    const label = document.createElement('label'); label.append(document.createTextNode(labelText));
    const input = document.createElement('input'); input.type = 'range'; input.min = '0'; input.max = '100'; input.step = '1'; input.dataset.itemKey = key; input.value = String(value ?? 50);
    const output = document.createElement('output'); output.value = `${input.value}%`; output.textContent = output.value;
    label.append(input, output); parent.append(label); return input;
  };
  const formatBytes = (value) => value == null || !Number.isFinite(Number(value)) ? 'Size unavailable' : `${(Number(value) / 1048576).toFixed(2)} MB`;
  const sourceNames = { library: 'Existing media library', upload: 'New local upload', cloudinary: 'Cloudinary', external_url: 'External URL', video_embed: 'YouTube/Vimeo embed' };
  const esc = (value) => String(value || '');
  let items = [];
  let activeLibraryIndex = null;
  let libraryItems = [];
  const selectedLibraryAssets = new Map();
  let dirty = false;

  function safePreviewUrl(value) {
    const raw = (value || '').trim();
    return raw.startsWith('/static/') || /^https?:\/\//i.test(raw) ? raw : '';
  }

  function platformEmbed(value) {
    try {
      const url = new URL(value);
      const path = url.pathname.split('/').filter(Boolean);
      if (['youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtube-nocookie.com'].includes(url.hostname)) {
        const id = url.pathname === '/watch' ? url.searchParams.get('v') : (['embed', 'shorts', 'live'].includes(path[0]) ? path[1] : '');
        return /^[\w-]{11}$/.test(id || '') ? `https://www.youtube-nocookie.com/embed/${id}` : '';
      }
      if (url.hostname === 'youtu.be' && /^[\w-]{11}$/.test(path[0] || '')) return `https://www.youtube-nocookie.com/embed/${path[0]}`;
      if (['vimeo.com', 'www.vimeo.com', 'player.vimeo.com'].includes(url.hostname)) {
        const id = url.hostname === 'player.vimeo.com' && path[0] === 'video' ? path[1] : path[0];
        if (!/^\d{5,12}$/.test(id || '')) return '';
        const hash = url.searchParams.get('h') || '';
        return `https://player.vimeo.com/video/${id}${/^[A-Za-z0-9]{6,64}$/.test(hash) ? `?h=${hash}` : ''}`;
      }
    } catch (_) { return ''; }
    return '';
  }

  function sourceForUrl(value) {
    const embed = platformEmbed(value);
    if (embed) return { type: 'video', source: 'video_embed', embed };
    let path = '';
    try { path = new URL(value, window.location.origin).pathname.toLowerCase(); } catch (_) { return null; }
    if (/\.(gif)$/.test(path)) return { type: 'gif', source: value.includes('res.cloudinary.com') ? 'cloudinary' : 'external_url' };
    if (/\.(mp4|m4v|mov|webm)$/.test(path) || /cloudinary\.com\/video\/upload\//.test(value)) return { type: 'video', source: value.includes('cloudinary.com') ? 'cloudinary' : 'external_url' };
    if (/\.(jpe?g|png|webp)$/.test(path) || /cloudinary\.com\/image\/upload\//.test(value)) return { type: 'image', source: value.includes('cloudinary.com') ? 'cloudinary' : 'external_url' };
    return null;
  }

  function collect() {
    return [...list.querySelectorAll('.media-item-card')].map((card, index) => {
      const item = { type: 'image', active: true, autoplay: true, muted: true, loop: true, controls: false };
      card.querySelectorAll('[data-item-key]').forEach((input) => {
        const key = input.dataset.itemKey;
        if (input.type === 'checkbox') item[key] = input.checked;
        else if (key === 'product_id') item[key] = input.value || null;
        else item[key] = input.value.trim();
      });
      item.source = card.dataset.source || 'external_url';
      item.filename = card.dataset.filename || '';
      item.size = Number(card.dataset.size) || null;
      item.width = Number(card.dataset.width) || null;
      item.height = Number(card.dataset.height) || null;
      item.duration = Number(card.dataset.duration) || null;
      return item;
    });
  }

  function colorValue(name, fallback) { return form.querySelector(`[data-color-value][name="${name}"]`)?.value || fallback; }
  function recordMetadata(index, width, height, duration) {
    const editorCard = list.querySelectorAll('.media-item-card')[index];
    if (!editorCard) return;
    if (width > 0) editorCard.dataset.width = width;
    if (height > 0) editorCard.dataset.height = height;
    if (Number.isFinite(duration) && duration > 0) editorCard.dataset.duration = duration;
    const meta = editorCard.querySelector('[data-media-metadata]');
    if (meta) meta.textContent = [editorCard.dataset.filename, editorCard.dataset.width && editorCard.dataset.height ? `${editorCard.dataset.width} × ${editorCard.dataset.height}` : '', formatBytes(editorCard.dataset.size || null), editorCard.dataset.duration ? `${Number(editorCard.dataset.duration).toFixed(1)} s` : ''].filter(Boolean).join(' · ');
  }
  function renderPreview() {
    if (!preview) return;
    const entries = collect();
    const section = document.createElement('section');
    const density = form.elements.density?.value || 'compact'; const layout = form.elements.layout?.value || 'carousel';
    section.className = `media-showcase media-layout-${layout} media-density-${density}`;
    section.classList.add(`media-count-mobile-${form.elements.visible_cards_mobile?.value || 2}`, `media-count-tablet-${form.elements.visible_cards_tablet?.value || 3}`, `media-count-desktop-${form.elements.visible_cards_desktop?.value || 4}`);
    const background = colorValue('background_color', '#ffffff');
    section.style.setProperty('--showcase-bg', background === 'transparent' ? 'transparent' : background);
    if (form.elements.section_padding_px?.value) section.style.setProperty('--showcase-padding', `${Number(form.elements.section_padding_px.value)}px`);
    if (form.elements.card_gap_px?.value) section.style.setProperty('--showcase-gap', `${Number(form.elements.card_gap_px.value)}px`);
    section.style.setProperty('--showcase-heading', colorValue('heading_color', '#252525'));
    section.style.setProperty('--showcase-description', colorValue('description_color', '#666666'));
    section.style.setProperty('--showcase-accent', colorValue('accent_color', '#e52335'));
    section.style.setProperty('--showcase-card-bg', colorValue('card_background_color', '#ffffff'));
    section.style.setProperty('--showcase-border', colorValue('border_color', '#e8e8eb'));
    section.style.setProperty('--showcase-button-bg', colorValue('button_background_color', '#e52335'));
    section.style.setProperty('--showcase-button-text', colorValue('button_text_color', '#ffffff'));
    const title = form.elements.title?.value || form.elements.name?.value || 'Showcase heading';
    const subtitle = form.elements.subtitle?.value || '';
    const description = form.elements.description?.value || '';
    const heading = document.createElement('header'); heading.className = 'media-showcase-heading';
    const h2 = document.createElement('h2'); h2.textContent = title; heading.append(h2);
    if (subtitle) { const p = document.createElement('p'); p.className = 'media-showcase-subtitle'; p.textContent = subtitle; heading.append(p); }
    if (description) { const p = document.createElement('p'); p.textContent = description; heading.append(p); }
    section.append(heading);
    const rail = document.createElement('div'); rail.className = 'media-showcase-items';
    if (layout === 'carousel' && entries.filter((item) => item.active !== false).length === 1) rail.classList.add('media-showcase-items-single');
    const cachedMedia = new Map([...preview.querySelectorAll('[data-preview-key]')].map((media) => [media.dataset.previewKey, media]));
    entries.forEach((item, index) => {
      if (item.active === false) return;
      const card = document.createElement('article'); card.className = 'media-showcase-card';
      const src = safePreviewUrl(item.url);
      if (item.type === 'video' && item.source === 'video_embed') {
        const embed = platformEmbed(item.url);
        if (embed) { const old = cachedMedia.get(String(index)); const frame = old?.tagName === 'IFRAME' && old.dataset.previewSource === embed ? old : document.createElement('iframe'); frame.dataset.previewKey = index; frame.dataset.previewSource = embed; frame.src = embed; frame.title = item.title || 'Embedded video preview'; frame.loading = 'lazy'; frame.allow = 'fullscreen; picture-in-picture'; frame.sandbox = 'allow-scripts allow-same-origin allow-presentation'; frame.className = 'media-embed-player'; frame.dataset.aspectRatio = item.aspect_ratio || ''; card.append(frame); }
      } else if (src && item.type === 'video') {
        const old = cachedMedia.get(String(index)); const video = old?.tagName === 'VIDEO' && old.dataset.previewSource === src ? old : document.createElement('video'); video.dataset.previewKey = index; video.dataset.previewSource = src; if (!video.src || old?.dataset.previewSource !== src) video.src = src; video.poster = safePreviewUrl(item.poster); video.controls = true; video.muted = true; video.preload = 'metadata'; video.className = `media-presentation-${item.presentation || 'original'}`; video.dataset.presentationMode = item.presentation || 'original'; video.dataset.aspectRatio = item.aspect_ratio || ''; video.style.setProperty('--media-position-x', `${item.preview_position_x ?? 50}%`); video.style.setProperty('--media-position-y', `${item.preview_position_y ?? 50}%`);
        if (item.width && item.height) { const ratio = Number(item.width) / Number(item.height) < .9 ? .8 : Number(item.width) / Number(item.height); video.style.setProperty('--media-item-ratio', ratio === .8 ? '4 / 5' : `${item.width} / ${item.height}`); video.style.maxWidth = `${360 * ratio}px`; }
        if (video !== old) video.addEventListener('loadedmetadata', () => { const ratio = video.videoWidth / video.videoHeight < .9 ? .8 : video.videoWidth / video.videoHeight; video.style.setProperty('--media-item-ratio', ratio === .8 ? '4 / 5' : `${video.videoWidth} / ${video.videoHeight}`); video.style.maxWidth = `${360 * ratio}px`; recordMetadata(index, video.videoWidth, video.videoHeight, video.duration); });
        video.addEventListener('error', () => { if (!card.querySelector('.media-preview-error')) { const issue = document.createElement('small'); issue.className = 'media-preview-error'; issue.textContent = 'Video preview unavailable. Check the direct media URL or storage access.'; card.append(issue); } });
        if (video.readyState >= 1) recordMetadata(index, video.videoWidth, video.videoHeight, video.duration);
        card.append(video);
      } else if (src && ['image', 'gif'].includes(item.type)) {
        const old = cachedMedia.get(String(index)); const image = old?.tagName === 'IMG' && old.dataset.previewSource === src ? old : document.createElement('img'); image.dataset.previewKey = index; image.dataset.previewSource = src; if (!image.src || old?.dataset.previewSource !== src) image.src = src; image.alt = item.alt || item.title || ''; image.loading = 'lazy'; image.className = `media-presentation-${item.presentation || 'original'}`; image.dataset.presentationMode = item.presentation || 'original'; image.dataset.aspectRatio = item.aspect_ratio || ''; if (image !== old) { image.addEventListener('load', () => { card.dataset.dimensions = `${image.naturalWidth} × ${image.naturalHeight}`; recordMetadata(index, image.naturalWidth, image.naturalHeight, 0); }); image.addEventListener('error', () => { const issue = document.createElement('small'); issue.className = 'media-preview-error'; issue.textContent = 'This URL did not return a previewable image.'; card.append(issue); }); } else if (image.complete && image.naturalWidth) recordMetadata(index, image.naturalWidth, image.naturalHeight, 0); card.append(image);
      } else { const placeholder = document.createElement('div'); placeholder.className = 'media-showcase-text-card'; placeholder.textContent = item.type === 'link' ? 'Linked content' : 'Preview available after choosing a supported media URL'; card.append(placeholder); }
      if (item.title || item.description) { const copy = document.createElement('div'); copy.className = 'media-showcase-copy'; if (item.title) { const h3 = document.createElement('h3'); h3.textContent = item.title; copy.append(h3); } if (item.description) { const p = document.createElement('p'); p.textContent = item.description; copy.append(p); } card.append(copy); }
      rail.append(card);
    });
    section.append(rail); preview.replaceChildren(section);
    if (!entries.length) { const empty = document.createElement('p'); empty.className = 'media-preview-empty'; empty.textContent = 'Add media to preview your showcase.'; preview.append(empty); }
  }

  function checkbox(parent, labelText, key, checked) {
    const label = document.createElement('label'); label.className = 'item-checkbox';
    const input = document.createElement('input'); input.type = 'checkbox'; input.checked = checked !== false; input.dataset.itemKey = key;
    label.append(input, document.createTextNode(labelText)); parent.append(label);
  }

  function draw() {
    list.replaceChildren();
    const links = [['', 'Choose a category, page or collection…'], ['/shop', 'Shop all products'], ['/about', 'About FityStores'], ['/upcoming', 'Upcoming merchandise'], ['/#mamba-collection', 'Mamba collection'], ...categories.map((entry) => [`/shop?category=${encodeURIComponent(entry.slug)}`, `Category: ${entry.name}`])];
    items.forEach((item, index) => {
      const card = document.createElement('fieldset'); card.className = 'media-item-card';
      card.dataset.source = item.source || 'external_url'; card.dataset.filename = item.filename || '';
      card.dataset.size = item.size || ''; card.dataset.width = item.width || ''; card.dataset.height = item.height || ''; card.dataset.duration = item.duration || '';
      const legend = document.createElement('legend'); legend.textContent = `Media item ${index + 1}`; card.append(legend);
      const actions = document.createElement('div'); actions.className = 'item-edit-actions';
      [[-1, 'Move up'], [1, 'Move down']].forEach(([direction, caption]) => { const button = document.createElement('button'); button.type = 'button'; button.className = 'btn'; button.textContent = caption; button.dataset.itemMove = direction; button.disabled = index + direction < 0 || index + direction >= items.length; actions.append(button); });
      const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'btn item-remove'; remove.textContent = 'Remove item'; remove.dataset.itemRemove = '1'; actions.append(remove);
      const source = document.createElement('span'); source.className = 'media-source-badge'; source.dataset.sourceLabel = '1'; source.textContent = sourceNames[card.dataset.source] || sourceNames.external_url; actions.append(source);
      const assetMeta = document.createElement('small'); assetMeta.className = 'media-item-meta'; assetMeta.dataset.mediaMetadata = '1';
      assetMeta.textContent = [card.dataset.filename, card.dataset.width && card.dataset.height ? `${card.dataset.width} × ${card.dataset.height}` : '', formatBytes(card.dataset.size || null), card.dataset.duration ? `${Number(card.dataset.duration).toFixed(1)} s` : ''].filter(Boolean).join(' · '); actions.append(assetMeta); card.append(actions);
      const fields = document.createElement('div'); fields.className = 'item-fields-grid';
      const typeSelect = choices(fields, 'Media type', 'type', item.type || 'image', [['video', 'Video'], ['gif', 'Animated GIF'], ['image', 'Image'], ['link', 'Link card']]);
      textField(fields, 'Title', 'title', item.title || '');
      const url = textField(fields, 'Media URL', 'url', item.url || '', 'url');
      textField(fields, 'Poster / thumbnail URL', 'poster', item.poster || '', 'url');
      const destination = textField(fields, 'Destination URL', 'destination', item.destination || '', 'url');
      choices(fields, 'Link to existing content', 'destination_preset', '', links);
      const productOptions = [['', 'No linked product'], ...products.map((entry) => [`${entry.id}`, entry.name])];
      choices(fields, 'Linked product', 'product_id', item.product_id == null ? '' : String(item.product_id), productOptions);
      textField(fields, 'Button label', 'cta', item.cta || ''); textField(fields, 'Alternative text', 'alt', item.alt || '');
      textField(fields, 'Description', 'description', item.description || '', 'textarea');
      choices(fields, 'Media fit', 'presentation', item.presentation || 'original', [['original', 'Original / natural ratio'], ['contain', 'Contain the full media'], ['cover', 'Cover the frame (crop edges)']]);
      textField(fields, 'Frame ratio (optional)', 'aspect_ratio', item.aspect_ratio || '');
      card.append(fields);
      const crop = document.createElement('div'); crop.className = 'media-crop-controls'; crop.dataset.videoCropControls = '1'; crop.hidden = typeSelect.value !== 'video';
      const cropHeading = document.createElement('strong'); cropHeading.textContent = 'Video preview crop'; crop.append(cropHeading);
      const cropHelp = document.createElement('p'); cropHelp.textContent = 'Adjust the preview only. The original video stays unchanged.'; crop.append(cropHelp);
      rangeField(crop, 'Horizontal position', 'preview_position_x', item.preview_position_x);
      rangeField(crop, 'Vertical position', 'preview_position_y', item.preview_position_y);
      const cropReset = document.createElement('button'); cropReset.type = 'button'; cropReset.className = 'btn media-crop-reset'; cropReset.textContent = 'Reset position'; crop.append(cropReset);
      card.append(crop);
      const sourceActions = document.createElement('div'); sourceActions.className = 'media-item-source-actions';
      const browse = document.createElement('button'); browse.type = 'button'; browse.className = 'btn'; browse.textContent = 'Choose existing media'; browse.dataset.itemLibrary = '1'; sourceActions.append(browse);
      const uploadLabel = document.createElement('label'); uploadLabel.className = 'btn media-upload-button'; uploadLabel.textContent = 'Upload file';
      const upload = document.createElement('input'); upload.type = 'file'; upload.accept = '.jpg,.jpeg,.png,.webp,.gif,.mp4,.m4v,.mov,.webm'; upload.className = 'media-file-input'; upload.setAttribute('aria-label', 'Choose media upload file'); upload.dataset.itemUpload = '1'; uploadLabel.append(upload); sourceActions.append(uploadLabel);
      const progress = document.createElement('progress'); progress.max = 100; progress.value = 0; progress.hidden = true; progress.dataset.uploadProgress = '1'; sourceActions.append(progress);
      const uploadStatus = document.createElement('span'); uploadStatus.className = 'media-upload-status'; uploadStatus.dataset.uploadStatus = '1'; uploadStatus.setAttribute('role', 'status'); sourceActions.append(uploadStatus);
      card.append(sourceActions);
      const toggles = document.createElement('div'); toggles.className = 'item-toggles';
      checkbox(toggles, 'Active', 'active', item.active); checkbox(toggles, 'Autoplay direct videos', 'autoplay', item.autoplay);
      checkbox(toggles, 'Muted', 'muted', item.muted); checkbox(toggles, 'Loop', 'loop', item.loop); checkbox(toggles, 'Show video controls', 'controls', item.controls);
      card.append(toggles); list.append(card);
      url.addEventListener('change', () => {
        const detected = sourceForUrl(url.value);
        if (detected) { card.querySelector('[data-item-key="type"]').value = detected.type; card.dataset.source = detected.source; source.textContent = sourceNames[detected.source]; if (detected.embed?.includes('youtube-nocookie.com') && !card.querySelector('[data-item-key="poster"]').value) card.querySelector('[data-item-key="poster"]').value = `https://img.youtube.com/vi/${detected.embed.split('/').pop()}/hqdefault.jpg`; }
        else if (url.value) { uploadStatus.textContent = 'Preview supports direct media files plus YouTube and Vimeo links.'; }
        renderPreview();
      });
      typeSelect.addEventListener('change', () => { crop.hidden = typeSelect.value !== 'video'; renderPreview(); });
      cropReset.addEventListener('click', () => {
        crop.querySelector('[data-item-key="preview_position_x"]').value = '50';
        crop.querySelector('[data-item-key="preview_position_y"]').value = '50';
        crop.querySelectorAll('output').forEach((output) => { output.value = '50%'; output.textContent = '50%'; });
        dirty = true; renderPreview();
      });
      destination.querySelector?.('input');
      const destinationPreset = card.querySelector('[data-item-key="destination_preset"]');
      destinationPreset.addEventListener('change', () => { if (destinationPreset.value) { destination.value = destinationPreset.value; card.querySelector('[data-item-key="product_id"]').value = ''; renderPreview(); } });
      card.querySelector('[data-item-key="product_id"]').addEventListener('change', (event) => { if (event.target.value) destination.value = ''; renderPreview(); });
      browse.addEventListener('click', () => openLibrary(index));
      upload.addEventListener('change', () => { if (upload.files[0]) uploadFile(upload.files[0], card); });
    });
    renderPreview();
  }

  function applyAsset(asset, index) {
    items = collect();
    if (index == null) {
      if (items.length >= 30) { libraryStatus.textContent = 'This showcase already has the maximum of 30 items.'; return; }
      items.push({ type: asset.type, title: asset.filename || 'Media item', url: asset.url, source: asset.source, filename: asset.filename, size: asset.size, width: asset.width, height: asset.height, duration: asset.duration, active: true, autoplay: false, muted: true, loop: true });
      index = items.length - 1;
    } else {
      items[index] = { ...items[index], type: asset.type, title: items[index].title || asset.filename || 'Media item', url: asset.url, source: asset.source, filename: asset.filename, size: asset.size, width: asset.width, height: asset.height, duration: asset.duration };
    }
    draw();
    if (dialog?.open) dialog.close();
    dirty = true;
  }

  function assetItem(asset) {
    return { type: asset.type, title: asset.filename || new URL(asset.url, window.location.origin).pathname.split('/').pop() || 'Media item', url: asset.url, source: asset.source, filename: asset.filename || '', size: asset.size, width: asset.width, height: asset.height, duration: asset.duration, active: true, autoplay: false, muted: true, loop: true, presentation: 'original' };
  }

  async function uploadBulk(files) {
    items = collect();
    let added = 0, failed = 0;
    for (const file of files) {
      if (items.length >= 30) { failed += files.length - added - failed; break; }
      if (file.size > 3_500_000) { failed++; continue; }
      const data = new FormData(); data.append('media', file);
      try {
        const response = await fetch(form.dataset.uploadUrl, { method: 'POST', body: data, headers: { 'X-CSRFToken': form.querySelector('[name="csrf_token"]').value, Accept: 'application/json' } });
        const result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.error || 'Upload failed');
        items.push({ ...assetItem({ ...result, filename: result.filename || file.name }), title: file.name.replace(/\.[^.]+$/, '') }); added++;
        bulkUploadStatus.textContent = `Uploaded ${added} of ${files.length}${failed ? `; ${failed} skipped or failed` : ''}.`;
        draw(); dirty = true;
      } catch (error) { failed++; bulkUploadStatus.textContent = `${added} uploaded; ${failed} failed or skipped. ${error.message}`; }
    }
    bulkUploadStatus.textContent = `${added} media file${added === 1 ? '' : 's'} added${failed ? `; ${failed} failed or exceeded the 3.5 MB per-file limit` : ''}.`;
  }

  function addBulkLinks() {
    items = collect();
    const links = bulkLinks.value.split(/[\r\n,;]+/).map((value) => value.trim()).filter(Boolean);
    let added = 0, skipped = 0;
    for (const url of links) {
      if (items.length >= 30) { skipped += links.length - added - skipped; break; }
      const detected = sourceForUrl(url);
      if (!detected) { skipped++; continue; }
      const filename = (() => { try { return new URL(url).pathname.split('/').pop().split('?')[0]; } catch (_) { return ''; } })();
      items.push(assetItem({ url, type: detected.type, source: detected.source, filename })); added++;
    }
    draw(); dirty = true;
    libraryStatus.textContent = `${added} media link${added === 1 ? '' : 's'} added${skipped ? `; ${skipped} unsupported or over the 30-item limit` : ''}.`;
    bulkLinks.value = '';
  }

  async function openLibrary(index = null) {
    if (!dialog) return;
    activeLibraryIndex = index;
    libraryNotice.textContent = 'Loading available media…'; libraryGrid.replaceChildren(); dialog.showModal();
    try {
      const response = await fetch(form.dataset.libraryUrl, { headers: { Accept: 'application/json' } });
      const contentType = response.headers.get('content-type') || '';
      if (!contentType.includes('application/json')) throw new Error(response.redirected ? 'Your admin session may have expired. Reload the page and sign in again.' : `The media library returned an unexpected response (HTTP ${response.status}).`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `The media library could not be loaded (HTTP ${response.status}).`);
      libraryItems = data.items || [];
      const broken = data.broken_local_references?.length ? ` ${data.broken_local_references.length} local media reference(s) are missing; they remain in their showcases until replaced.` : '';
      libraryNotice.textContent = (data.cloudinary === 'not_configured' ? `Showing local media and recorded Cloudinary references. This environment has no Cloudinary account configured. Browse limited to ${data.limit} items.` : data.cloudinary === 'unavailable' ? 'Cloudinary could not be reached. Showing local media and recorded references.' : `Choose a file already stored by the site. Reusing an item does not make another copy. Showing up to ${data.limit} assets.`) + broken + ' Remote URLs are not fetched to verify availability.';
      drawLibrary(libraryItems);
    } catch (error) { libraryNotice.textContent = error.message || 'The media library could not be loaded.'; }
  }

  function drawLibrary(assets) {
    libraryGrid.replaceChildren();
    if (!assets.length) { const empty = document.createElement('p'); empty.textContent = 'No media files found. Upload a new file or paste a direct URL instead.'; libraryGrid.append(empty); return; }
    assets.forEach((asset) => {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'media-library-asset';
      if (['image', 'gif'].includes(asset.type)) { const image = document.createElement('img'); image.src = asset.url; image.alt = ''; image.loading = 'lazy'; button.append(image); }
      else { const previewIcon = document.createElement('span'); previewIcon.className = 'media-library-kind-icon'; previewIcon.textContent = asset.type === 'video' ? 'VIDEO' : 'LINK'; button.append(previewIcon); }
      const details = document.createElement('span'); details.className = 'media-library-asset-details';
      const name = document.createElement('strong'); name.textContent = asset.filename || asset.url; details.append(name);
      const meta = document.createElement('small'); meta.textContent = `${asset.type.toUpperCase()} · ${sourceNames[asset.source] || asset.source} · ${asset.width && asset.height ? `${asset.width} × ${asset.height} · ` : ''}${formatBytes(asset.size)}${asset.duration ? ` · ${Number(asset.duration).toFixed(1)} s` : ''} · ${asset.usage_count ? `Used ${asset.usage_count}×` : 'No known record reference'}`; details.append(meta);
      button.append(details);
      if (activeLibraryIndex == null) button.setAttribute('aria-pressed', selectedLibraryAssets.has(asset.url) ? 'true' : 'false');
      button.addEventListener('click', () => {
        if (activeLibraryIndex != null) { applyAsset(asset, activeLibraryIndex); return; }
        if (selectedLibraryAssets.has(asset.url)) selectedLibraryAssets.delete(asset.url); else selectedLibraryAssets.set(asset.url, asset);
        button.setAttribute('aria-pressed', selectedLibraryAssets.has(asset.url) ? 'true' : 'false');
        const count = dialog.querySelector('[data-library-selection-count]'); if (count) count.textContent = `${selectedLibraryAssets.size} selected`;
      }); libraryGrid.append(button);
    });
  }

  function uploadFile(file, card) {
    const status = card.querySelector('[data-upload-status]'); const progress = card.querySelector('[data-upload-progress]');
    if (file.size > 3_500_000) { status.textContent = 'File is too large. Limit is 3.5 MB per file.'; return; }
    const data = new FormData(); data.append('media', file);
    const xhr = new XMLHttpRequest(); xhr.open('POST', form.dataset.uploadUrl); xhr.setRequestHeader('X-CSRFToken', form.querySelector('[name="csrf_token"]').value); xhr.setRequestHeader('Accept', 'application/json');
    progress.hidden = false; progress.value = 0; status.textContent = 'Uploading…';
    xhr.upload.addEventListener('progress', (event) => { if (event.lengthComputable) progress.value = Math.round(event.loaded / event.total * 100); });
    xhr.addEventListener('load', () => {
      let result = {}; try { result = JSON.parse(xhr.responseText || '{}'); } catch (_) { /* handled below */ }
      if (xhr.status < 200 || xhr.status >= 300 || !result.ok) { status.textContent = result.error || 'Upload failed. Try a direct URL or another file.'; progress.hidden = true; return; }
      card.querySelector('[data-item-key="url"]').value = result.url;
      card.querySelector('[data-item-key="type"]').value = result.type;
      card.querySelector('[data-item-key="title"]').value ||= file.name.replace(/\.[^.]+$/, '');
      card.dataset.source = result.source; card.dataset.filename = result.filename; card.dataset.size = result.size || file.size;
      card.dataset.width = result.width || ''; card.dataset.height = result.height || ''; card.dataset.duration = result.duration || '';
      card.querySelector('[data-source-label]').textContent = sourceNames[result.source] || result.source;
      card.querySelector('[data-media-metadata]').textContent = [card.dataset.filename, card.dataset.width && card.dataset.height ? `${card.dataset.width} × ${card.dataset.height}` : '', formatBytes(card.dataset.size), card.dataset.duration ? `${Number(card.dataset.duration).toFixed(1)} s` : ''].filter(Boolean).join(' · ');
      status.textContent = `${result.source === 'cloudinary' ? 'Saved to Cloudinary' : 'Saved locally for development'} · ${formatBytes(result.size || file.size)}${result.width ? ` · ${result.width} × ${result.height}` : ''}`;
      progress.value = 100; dirty = true; renderPreview();
    });
    xhr.addEventListener('error', () => { status.textContent = 'Upload connection failed. No media reference was saved.'; progress.hidden = true; });
    xhr.send(data);
  }

  function initColours() {
    form.querySelectorAll('[data-color-control]').forEach((control) => {
      const select = control.querySelector('[data-color-preset]'); const picker = control.querySelector('[data-color-picker]');
      const value = control.querySelector('[data-color-value]'); const swatch = control.querySelector('[data-color-swatch]');
      const update = (next) => { value.value = next; swatch.style.background = next; picker.hidden = next === 'transparent'; if (next !== 'transparent' && /^#[\da-f]{6}$/i.test(next)) picker.value = next; dirty = true; renderPreview(); checkContrast(); };
      const match = [...select.options].find((option) => option.value.toLowerCase() === value.value.toLowerCase());
      select.value = match ? match.value : 'custom'; picker.hidden = value.value === 'transparent'; if (/^#[\da-f]{6}$/i.test(value.value)) picker.value = value.value;
      swatch.style.background = value.value || '#ffffff';
      select.addEventListener('change', () => { if (select.value !== 'custom') update(select.value); else { picker.hidden = false; update(picker.value); } });
      picker.addEventListener('input', () => { select.value = 'custom'; update(picker.value); });
    });
  }

  function contrast(a, b) {
    const luminance = (hex) => {
      let normal = hex === 'transparent' ? '#ffffff' : hex;
      if (/^#[\da-f]{3}$/i.test(normal)) normal = `#${normal[1]}${normal[1]}${normal[2]}${normal[2]}${normal[3]}${normal[3]}`;
      const rgb = (normal.match(/[\da-f]{2}/gi) || ['ff', 'ff', 'ff']).map((part) => parseInt(part, 16) / 255).map((v) => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4);
      return .2126 * rgb[0] + .7152 * rgb[1] + .0722 * rgb[2];
    };
    const l1 = luminance(a === 'transparent' ? '#ffffff' : a), l2 = luminance(b === 'transparent' ? '#ffffff' : b);
    return (Math.max(l1, l2) + .05) / (Math.min(l1, l2) + .05);
  }
  function checkContrast() {
    const warning = document.querySelector('[data-colour-warning]'); if (!warning) return;
    const pairs = [['heading_color', 'background_color', 'Heading'], ['heading_color', 'card_background_color', 'Card heading'], ['description_color', 'background_color', 'Description'], ['description_color', 'card_background_color', 'Card description'], ['button_text_color', 'button_background_color', 'Button text']];
    const weak = pairs.filter(([a, b]) => contrast(colorValue(a, '#ffffff'), colorValue(b, '#ffffff')) < 4.5).map(([, , label]) => label);
    warning.textContent = weak.length ? `Contrast may be difficult to read for: ${weak.join(', ')}. Choose a darker text or lighter background.` : 'Text contrast looks readable for the selected colours.';
  }

  const encodedItems = JSON.parse(dataNode.textContent || '[]');
  items = Array.isArray(encodedItems) ? encodedItems : JSON.parse(encodedItems || '[]');
  draw(); initColours(); checkContrast();
  addButton?.addEventListener('click', () => { items = collect(); if (items.length >= 30) { libraryStatus.textContent = 'This showcase already has the maximum of 30 items.'; return; } items.push({ type: 'image', title: '', url: '', active: true, autoplay: false, muted: true, loop: true, source: 'external_url', presentation: 'original' }); draw(); dirty = true; });
  bulkUpload?.addEventListener('change', () => { if (bulkUpload.files.length) uploadBulk([...bulkUpload.files]); bulkUpload.value = ''; });
  form.querySelector('[data-add-bulk-links]')?.addEventListener('click', addBulkLinks);
  form.addEventListener('click', (event) => {
    const button = event.target.closest('[data-item-remove],[data-item-move],[data-library-open]');
    if (!button) return;
    if (button.hasAttribute('data-library-open')) { openLibrary(); return; }
    items = collect();
    const card = button.closest('.media-item-card'); const index = [...list.children].indexOf(card);
    if (button.hasAttribute('data-item-remove')) { if (!window.confirm('Remove this media item from the showcase? The stored media asset will not be deleted.')) return; items.splice(index, 1); }
    else { const direction = Number(button.dataset.itemMove); const next = index + direction; if (next < 0 || next >= items.length) return; [items[index], items[next]] = [items[next], items[index]]; }
    draw(); dirty = true;
  });
  dialog?.querySelector('[data-library-close]')?.addEventListener('click', () => dialog.close());
  dialog?.querySelector('[data-library-add-selected]')?.addEventListener('click', () => {
    items = collect(); const available = Math.max(0, 30 - items.length); const selected = [...selectedLibraryAssets.values()];
    items.push(...selected.slice(0, available).map(assetItem));
    const skipped = selected.length - Math.min(selected.length, available);
    selectedLibraryAssets.clear(); draw(); dirty = true; dialog.close();
    libraryStatus.textContent = `${Math.min(selected.length, available)} library item${Math.min(selected.length, available) === 1 ? '' : 's'} added${skipped ? `; ${skipped} skipped because the showcase limit is 30` : ''}.`;
    dialog.querySelector('[data-library-selection-count]').textContent = '0 selected';
  });
  dialog?.querySelector('[data-library-search]')?.addEventListener('input', (event) => {
    const query = event.target.value.toLowerCase(); drawLibrary(libraryItems.filter((asset) => `${asset.filename} ${asset.type} ${asset.source}`.toLowerCase().includes(query)));
  });
  form.addEventListener('input', (event) => { if (!event.target.matches('[data-color-picker]')) dirty = true; const output = event.target.parentElement?.querySelector('output'); if (output && event.target.type === 'range') { output.value = `${event.target.value}%`; output.textContent = output.value; } renderPreview(); });
  form.addEventListener('change', (event) => { if (event.target.matches('[data-item-key="type"]')) renderPreview(); });
  document.querySelectorAll('[data-live-width]').forEach((button) => button.addEventListener('click', () => { preview.style.maxWidth = `${button.dataset.liveWidth}px`; }));
  form.addEventListener('submit', (event) => {
    const collected = collect();
    if (collected.some((item) => item.url && !item.title.trim())) { event.preventDefault(); libraryStatus.textContent = 'Give every media item a title before saving.'; return; }
    jsonField.value = JSON.stringify(collected, null, 2); dirty = false;
  });
  window.addEventListener('beforeunload', (event) => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
})();
