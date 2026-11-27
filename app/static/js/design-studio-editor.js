(() => {
  const form = document.querySelector('[data-studio-editor]');
  if (!form) return;
  const html = form.querySelector('[data-studio-html]');
  const css = form.querySelector('[data-studio-css]');
  const templateKey = form.querySelector('[name="template_key"]');
  const defaultTemplate = JSON.parse(form.querySelector('[data-studio-default-template]').textContent);
  const templates = {
    offers: {html: defaultTemplate.html, css: defaultTemplate.css},
    editorial: {
      html: '<section class="ds-promo"><p class="ds-kicker">FityStores finds</p><h2>Everyday essentials, better together</h2><p>Fresh picks for your everyday style.</p><a class="ds-cta" href="/shop">Explore the collection</a></section>',
      css: '.ds-promo{padding:clamp(24px,6vw,64px);border-radius:24px;background:linear-gradient(120deg,#fff1f4,#f8e7d2);color:#252525}.ds-kicker{color:#e52335;text-transform:uppercase;letter-spacing:.12em;font-weight:700}.ds-promo h2{max-width:15ch;font-size:clamp(2rem,6vw,4rem);line-height:1.04}.ds-cta{display:inline-block;padding:12px 20px;border-radius:999px;background:#e52335;color:white;text-decoration:none}'
    },
    'product-grid': {
      html: '<section class="ds-products"><p class="ds-kicker">Shop the edit</p><h2>Everyday favourites</h2><p>Current picks, straight from the FityStores catalogue.</p>%%FITYSTORES_PRODUCTS%%</section>',
      css: '.ds-products{padding:clamp(20px,4vw,48px);background:#fff8f4;border-radius:20px;color:#252525}.ds-products h2{font-size:clamp(1.7rem,4vw,3rem)}.studio-product-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,170px),1fr));gap:16px}.studio-product-card{padding:14px;border:1px solid #eadfe0;border-radius:16px;background:white}.studio-product-card img{width:100%;aspect-ratio:1;object-fit:cover;border-radius:12px}.studio-product-card a{display:grid;gap:10px;color:inherit;text-decoration:none}.studio-product-card p{font-weight:700}.studio-product-card button{padding:10px 14px;border:0;border-radius:999px;background:#e52335;color:white}'
    },
    seasonal: {
      html: '<section class="ds-season"><div><p class="ds-kicker">Your next everyday favourite</p><h2>Small details. Big feel-good energy.</h2><p>Discover comfort-first finds picked for the season.</p><a class="ds-cta" href="/shop">Shop the edit</a></div><span class="ds-season-mark" aria-hidden="true">F</span></section>',
      css: '.ds-season{display:flex;align-items:center;justify-content:space-between;gap:20px;padding:clamp(24px,6vw,64px);border-radius:24px;background:#252525;color:white}.ds-season>div{max-width:620px}.ds-season h2{font-size:clamp(2rem,5vw,3.8rem);line-height:1.05}.ds-kicker{color:#ffb5bd;text-transform:uppercase;letter-spacing:.12em;font-weight:700}.ds-cta{display:inline-block;padding:12px 20px;border-radius:999px;background:#e52335;color:white;text-decoration:none}.ds-season-mark{font-size:clamp(5rem,15vw,12rem);color:#e52335;font-weight:900}'
    }
  };
  const campaignTemplates = {
    'deal-of-day': {
      brand: 'FityStores', kicker: 'Black Friday', badge: '20 units only',
      headline: 'Deal of', highlight: 'the day', copy: 'Today, 2PM',
      cta: 'Discover', url: '/shop', background: '#321d68',
      accent: '#ff5b24', platform: '#c7a6ff', cutouts: true
    },
    'black-friday-feature': {
      brand: 'FityStores', kicker: 'Black Friday', badge: 'Limited-time offers',
      headline: 'Black Friday', highlight: 'Offers', copy: 'Up to 70% off · Fast delivery · Limited quantity',
      cta: 'Shop now', url: '/shop', background: '#21132f',
      accent: '#f45124', platform: '#d8c5ff', cutouts: true
    },
    smartphones: {
      brand: 'FityStores', kicker: 'Black Friday', badge: 'Few days left',
      headline: 'Smartphones', highlight: '', copy: 'Use code TBF350 for KSh 350 off orders above KSh 12,000.',
      cta: 'Shop now', url: '/shop', background: '#38206e',
      accent: '#ff5b24', platform: '#c6a6ff', cutouts: true
    },
    beauty: {
      brand: 'FityStores', kicker: 'Black Friday', badge: 'Few days left',
      headline: 'Beauty', highlight: '', copy: 'Use code TBFB100 for KSh 100 off orders above KSh 1,500.',
      cta: 'Shop now', url: '/shop', background: '#36206c',
      accent: '#ff5b24', platform: '#e4d5ff', cutouts: true
    },
    home: {
      brand: 'FityStores', kicker: 'Black Friday', badge: 'Few days left',
      headline: 'Home', highlight: '', copy: 'Useful everyday pieces, selected for your home.',
      cta: 'Shop now', url: '/shop', background: '#38216e',
      accent: '#ff5b24', platform: '#d1b6ff', cutouts: true
    }
  };
  const campaignKeys = new Set(Object.keys(campaignTemplates));
  const campaignSettings = form.querySelector('[data-campaign-settings]');
  const campaignFields = {
    brand: form.querySelector('[data-template-brand]'),
    kicker: form.querySelector('[data-template-kicker]'),
    badge: form.querySelector('[data-template-badge]'),
    headline: form.querySelector('[data-template-headline]'),
    highlight: form.querySelector('[data-template-highlight]'),
    copy: form.querySelector('[data-template-copy]'),
    cta: form.querySelector('[data-template-cta]'),
    url: form.querySelector('[data-template-url]'),
    background: form.querySelector('[data-template-background]'),
    accent: form.querySelector('[data-template-accent]'),
    platform: form.querySelector('[data-template-platform]'),
    cutouts: form.querySelector('[data-template-cutouts]')
  };
  const escapeMarkup = value => String(value).replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[character]);
  const colorValue = (value, fallback) => /^#[0-9a-f]{6}$/i.test(value) ? value : fallback;
  const campaignValues = defaults => ({
    brand: campaignFields.brand.value.trim().slice(0, 60) || defaults.brand,
    kicker: campaignFields.kicker.value.trim().slice(0, 70),
    badge: campaignFields.badge.value.trim().slice(0, 40),
    headline: campaignFields.headline.value.trim().slice(0, 60) || defaults.headline,
    highlight: campaignFields.highlight.value.trim().slice(0, 40),
    copy: campaignFields.copy.value.trim().slice(0, 180),
    cta: campaignFields.cta.value.trim().slice(0, 30) || defaults.cta,
    url: ['/shop', '/upcoming', '/cart'].includes(campaignFields.url.value) ? campaignFields.url.value : '/shop',
    background: colorValue(campaignFields.background.value, defaults.background),
    accent: colorValue(campaignFields.accent.value, defaults.accent),
    platform: colorValue(campaignFields.platform.value, defaults.platform),
    cutouts: campaignFields.cutouts.checked
  });
  const campaignCss = (key, value) => `
.ds-campaign{--ds-campaign-background:${value.background};--ds-campaign-accent:${value.accent};--ds-campaign-platform:${value.platform};box-sizing:border-box;position:relative;isolation:isolate;display:grid;grid-template-columns:minmax(210px,.82fr) minmax(0,2.4fr);align-items:center;gap:clamp(14px,2.4vw,34px);min-height:320px;overflow:hidden;padding:clamp(20px,3.3vw,42px);border-radius:20px;background:linear-gradient(112deg,#08070b 0%,#100c18 42%,var(--ds-campaign-background) 100%);color:#fff;font-family:system-ui,sans-serif}
.ds-campaign,.ds-campaign *,.ds-campaign *::before,.ds-campaign *::after{box-sizing:border-box}
.ds-campaign::before{position:absolute;z-index:-1;inset:0;background:radial-gradient(ellipse at 76% 54%,rgba(255,255,255,.12),transparent 57%);content:""}
.ds-campaign-copy{position:relative;z-index:2;display:flex;min-width:0;flex-direction:column;align-items:flex-start;justify-content:center}
.ds-campaign-brand{margin:0 0 3px;color:#fff;font-size:clamp(.9rem,1.65vw,1.55rem);font-weight:900;letter-spacing:.02em;line-height:1;text-transform:uppercase}
.ds-campaign-kicker{margin:0;color:var(--ds-campaign-accent);font-size:clamp(1.05rem,2.4vw,2rem);font-weight:900;letter-spacing:-.035em;line-height:1.05}
.ds-campaign-badge{display:inline-flex;align-items:center;margin:.22rem 0 .45rem;padding:.13rem .5rem;border-radius:999px;background:var(--ds-campaign-accent);color:#fff;font-size:.58rem;font-weight:900;letter-spacing:.12em;line-height:1.15;text-transform:uppercase}
.ds-campaign h2{display:grid;gap:0;margin:.42rem 0 .6rem;color:#fff;font-size:clamp(2rem,4.2vw,4.25rem);font-weight:900;letter-spacing:-.055em;line-height:.98}
.ds-campaign-heading-highlight{color:var(--ds-campaign-accent)}
.ds-campaign-copyline{max-width:35rem;margin:0 0 .85rem;color:#f0edf4;font-size:clamp(.82rem,1.55vw,1.28rem);line-height:1.35}
.ds-campaign-cta{display:inline-flex;min-height:38px;align-items:center;gap:.45rem;padding:.5rem .95rem;border-radius:999px;background:#fff;color:#16131a;font-size:.84rem;font-weight:800;text-decoration:none}
.ds-campaign-products{position:relative;display:grid;min-width:0;align-items:center;overflow-x:auto;overscroll-behavior-x:contain;scroll-snap-type:x proximity;scrollbar-width:thin}
.ds-campaign-product-track{display:flex;width:max-content;min-width:100%;align-items:stretch;gap:clamp(2px,1vw,14px)}
.ds-campaign .offers-product-item{display:flex;flex:0 0 ${key === 'deal-of-day' ? 'clamp(125px,13vw,185px)' : 'clamp(175px,20vw,260px)'};min-width:0;min-height:230px;align-items:stretch;scroll-snap-align:start}
.ds-campaign .offers-product-card{position:relative;display:grid;width:100%;min-width:0;grid-template-rows:auto minmax(118px,1fr) auto;justify-items:center;align-content:stretch;gap:.35rem;padding:.15rem .2rem .45rem;text-align:center}
.ds-campaign .offers-product-category{display:none}
.ds-campaign .offers-product-card h3{display:-webkit-box;overflow:hidden;min-height:2.35em;margin:0;color:#fff;font-size:clamp(.65rem,1.2vw,.98rem);font-weight:750;line-height:1.18;-webkit-box-orient:vertical;-webkit-line-clamp:2}
.ds-campaign .offers-product-card h3 a{color:inherit;text-decoration:none}
.ds-campaign .offers-product-image{position:relative;display:grid;width:100%;min-height:118px;place-items:center}
.ds-campaign .offers-product-image::before{position:absolute;z-index:0;top:7%;left:50%;width:min(82%,160px);aspect-ratio:1;transform:translateX(-50%);border-radius:50%;background:radial-gradient(circle,var(--ds-campaign-accent) 0%,rgba(255,255,255,.18) 57%,transparent 77%);content:""}
.ds-campaign .offers-product-image::after{position:absolute;z-index:0;right:9%;bottom:3px;left:9%;height:17px;border-radius:50%/80%;background:linear-gradient(180deg,#fff 0%,var(--ds-campaign-platform) 100%);box-shadow:0 8px 15px rgba(0,0,0,.32);content:""}
.ds-campaign .offers-product-image-link{position:relative;z-index:1;display:grid;width:100%;height:100%;place-items:center}
.ds-campaign .offers-product-image img{position:relative;z-index:1;display:block;width:100%;height:clamp(118px,15vw,190px);object-fit:contain;filter:drop-shadow(0 5px 4px rgba(0,0,0,.24))}
.ds-campaign .home-quick-add-card .home-card-quick-add{position:absolute;z-index:3;top:38%;right:.1rem;display:grid;width:30px;height:30px;place-items:center;border:0;border-radius:50%;background:#fff;color:#21172b;box-shadow:0 2px 8px rgba(0,0,0,.22);font-size:21px;line-height:1;text-decoration:none}
.ds-campaign .offers-price-group{display:flex;flex-wrap:wrap;align-items:center;justify-content:center;gap:.14rem .34rem}
.ds-campaign .offers-current-price{display:inline-flex;min-height:27px;align-items:center;justify-content:center;padding:.3rem .6rem;border-radius:999px;background:#fff;color:#17121e;font-size:clamp(.62rem,.95vw,.82rem);font-weight:900;line-height:1;text-decoration:none;white-space:nowrap}
.ds-campaign .offers-original-price{color:#e6e1ea;font-size:clamp(.56rem,.78vw,.69rem);line-height:1.1}
.ds-campaign .offers-discount-note{flex-basis:100%;color:#ffe0a2;font-size:.58rem;font-weight:750;line-height:1.1}
.ds-campaign:not(.ds-campaign--cutouts) .offers-product-image::before{top:12%;width:92%;height:72%;aspect-ratio:auto;border-radius:15px;background:rgba(255,255,255,.1)}
.ds-campaign:not(.ds-campaign--cutouts) .offers-product-image img{filter:none}
.ds-campaign--black-friday-feature .ds-campaign-products::before{position:absolute;z-index:-1;inset:4% 5%;border-radius:50%;background:radial-gradient(circle,var(--ds-campaign-accent),transparent 72%);opacity:.48;content:""}
.ds-campaign--deal-of-day .ds-campaign-product-track{gap:clamp(0px,.5vw,8px)}
@media(max-width:760px){.ds-campaign{grid-template-columns:minmax(145px,.72fr) minmax(0,1.65fr);gap:10px;min-height:285px;padding:18px 14px}.ds-campaign h2{font-size:clamp(1.7rem,5vw,2.8rem)}.ds-campaign .offers-product-item{min-height:190px}.ds-campaign .offers-product-card{grid-template-rows:auto minmax(100px,1fr) auto}.ds-campaign .offers-product-image{min-height:100px}.ds-campaign .offers-product-image img{height:clamp(100px,20vw,145px)}.ds-campaign .offers-current-price{white-space:normal}}
@media(max-width:520px){.ds-campaign{grid-template-columns:1fr;gap:8px;min-height:0;padding:17px 13px}.ds-campaign-copy{display:grid;grid-template-columns:1fr auto;column-gap:8px;align-items:center}.ds-campaign-brand,.ds-campaign-kicker,.ds-campaign-badge,.ds-campaign h2,.ds-campaign-copyline,.ds-campaign-cta{grid-column:1}.ds-campaign-brand{font-size:.86rem}.ds-campaign-kicker{font-size:1.1rem}.ds-campaign-badge{justify-self:start;margin:.2rem 0}.ds-campaign h2{margin:.25rem 0;font-size:2rem}.ds-campaign-copyline{margin:0 0 .45rem;font-size:.82rem}.ds-campaign-cta{grid-row:1/5;grid-column:2;align-self:center;white-space:nowrap}.ds-campaign-product-track{gap:7px}.ds-campaign .offers-product-item{flex-basis:clamp(112px,29vw,150px);min-height:190px}.ds-campaign .offers-product-card h3{font-size:.72rem}.ds-campaign .offers-product-image img{height:126px}}
@media(prefers-reduced-motion:reduce){.ds-campaign *{scroll-behavior:auto!important}}
`;
  function applyCampaignFields(values) {
    Object.entries(values).forEach(([name, value]) => {
      if (name === 'cutouts') campaignFields[name].checked = Boolean(value);
      else if (campaignFields[name]) campaignFields[name].value = value;
    });
  }
  function readCampaignFields() {
    const parsed = new DOMParser().parseFromString(html.value, 'text/html');
    const root = parsed.querySelector('[data-template-key]');
    if (!root || !campaignKeys.has(root.dataset.templateKey)) return;
    const defaults = campaignTemplates[root.dataset.templateKey];
    applyCampaignFields({
      brand: root.dataset.templateBrand ?? defaults.brand,
      kicker: root.dataset.templateKicker ?? defaults.kicker,
      badge: root.dataset.templateBadge ?? '',
      headline: root.dataset.templateHeadline ?? defaults.headline,
      highlight: root.dataset.templateHighlight ?? '',
      copy: root.dataset.templateCopy ?? '',
      cta: root.dataset.templateCta ?? defaults.cta,
      url: root.dataset.templateUrl ?? '/shop',
      background: colorValue(root.dataset.templateBackground || '', defaults.background),
      accent: colorValue(root.dataset.templateAccent || '', defaults.accent),
      platform: colorValue(root.dataset.templatePlatform || '', defaults.platform),
      cutouts: root.dataset.templateCutouts !== 'false'
    });
  }
  function buildCampaign(key) {
    const defaults = campaignTemplates[key];
    const value = campaignValues(defaults);
    const attr = name => escapeMarkup(value[name]);
    const cutoutClass = value.cutouts ? ' ds-campaign--cutouts' : '';
    const badge = value.badge ? `<p class="ds-campaign-badge">${escapeMarkup(value.badge)}</p>` : '';
    const highlight = value.highlight
      ? `<span class="ds-campaign-heading-highlight">${escapeMarkup(value.highlight)}</span>` : '';
    const arrow = ' →';
    html.value = `<section class="ds-campaign ds-campaign--${key}${cutoutClass}" data-template-key="${key}" data-template-brand="${attr('brand')}" data-template-kicker="${attr('kicker')}" data-template-badge="${attr('badge')}" data-template-headline="${attr('headline')}" data-template-highlight="${attr('highlight')}" data-template-copy="${attr('copy')}" data-template-cta="${attr('cta')}" data-template-url="${attr('url')}" data-template-background="${attr('background')}" data-template-accent="${attr('accent')}" data-template-platform="${attr('platform')}" data-template-cutouts="${value.cutouts ? 'true' : 'false'}"><div class="ds-campaign-copy"><p class="ds-campaign-brand">${escapeMarkup(value.brand)}</p><p class="ds-campaign-kicker">${escapeMarkup(value.kicker)}</p>${badge}<h2><span class="ds-campaign-heading-main">${escapeMarkup(value.headline)}</span>${highlight}</h2><p class="ds-campaign-copyline">${escapeMarkup(value.copy)}</p><a class="ds-campaign-cta" href="${attr('url')}">${escapeMarkup(value.cta)}${arrow}</a></div><div class="ds-campaign-products"><div class="ds-campaign-product-track">%%FITYSTORES_OFFERS%%</div></div></section>`;
    css.value = campaignCss(key, value);
  }
  form.querySelectorAll('[data-template]').forEach(button => button.addEventListener('click', () => {
    const key = button.dataset.template;
    const template = templates[key];
    if (!template && !campaignKeys.has(key)) return;
    templateKey.value = key;
    if (campaignKeys.has(key)) {
      applyCampaignFields(campaignTemplates[key]);
      campaignSettings.hidden = false;
      buildCampaign(key);
    } else {
      campaignSettings.hidden = true;
      html.value = template.html;
      css.value = template.css;
    }
    form.querySelectorAll('[data-template]').forEach(item => {
      item.setAttribute('aria-pressed', String(item === button));
    });
    ensureLottieTarget();
    schedulePreview();
  }));
  form.querySelectorAll('[data-template]').forEach(button => {
    button.setAttribute('aria-pressed', String(button.dataset.template === templateKey.value));
  });
  if (campaignKeys.has(templateKey.value)) {
    campaignSettings.hidden = false;
    readCampaignFields();
  }
  Object.entries(campaignFields).forEach(([name, field]) => {
    field.addEventListener('input', () => {
      const key = templateKey.value;
      if (campaignKeys.has(key)) buildCampaign(key);
      schedulePreview();
    });
    field.addEventListener('change', () => {
      const key = templateKey.value;
      if (campaignKeys.has(key)) buildCampaign(key);
      schedulePreview();
    });
  });
  const productSelect = form.querySelector('[data-studio-products]');
  try {
    const selectedIds = JSON.parse(productSelect.dataset.selectedProducts || '[]').map(String);
    [...productSelect.options].forEach(option => { option.selected = selectedIds.includes(option.value); });
  } catch {
    [...productSelect.options].forEach(option => { option.selected = false; });
  }
  const motionDuration = form.querySelector('[data-motion-duration]');
  const motionConfig = form.querySelector('[data-motion-config]');
  try {
    const config = JSON.parse(motionConfig.value || '{}');
    motionDuration.value = String(Math.max(100, Math.min(3000, Number(config.duration || 600))));
  } catch {
    motionDuration.value = '600';
  }
  motionDuration.addEventListener('input', () => {
    motionConfig.value = JSON.stringify({duration: Number(motionDuration.value || 600)});
    schedulePreview();
  });
  const previewSize = form.querySelector('[data-preview-size]');
  const preview = form.querySelector('[data-studio-preview]');
  const previewStatus = form.querySelector('[data-studio-preview-status]');
  const previewRequest = document.createElement('form');
  previewRequest.method = 'post';
  previewRequest.action = form.dataset.previewUrl;
  previewRequest.target = preview.name;
  previewRequest.hidden = true;
  document.body.append(previewRequest);
  let previewTimer = null;
  let previewSizeTimer = null;
  let previewQueued = false;
  let previewBusy = false;
  function submitPreview() {
    if (!previewQueued || previewBusy) return;
    previewQueued = false;
    previewBusy = true;
    previewStatus.textContent = 'Updating preview…';
    previewRequest.replaceChildren();
    for (const [name, value] of new FormData(form).entries()) {
      if (typeof value !== 'string') continue;
      const input = document.createElement('input');
      input.type = 'hidden';
      input.name = name;
      input.value = value;
      previewRequest.append(input);
    }
    previewRequest.submit();
  }
  function schedulePreview() {
    previewQueued = true;
    previewStatus.textContent = 'Previewing your latest changes…';
    clearTimeout(previewTimer);
    previewTimer = setTimeout(submitPreview, 250);
  }
  preview.addEventListener('load', () => {
    if (!previewBusy) {
      preview.contentWindow.postMessage({type: 'fity-studio-command', action: 'measure'}, '*');
      return;
    }
    previewBusy = false;
    previewStatus.textContent = 'Live preview updated.';
    if (previewQueued) submitPreview();
  });
  preview.contentWindow.postMessage({type: 'fity-studio-command', action: 'measure'}, '*');
  window.addEventListener('message', event => {
    if (event.source !== preview.contentWindow || !event.data || event.data.type !== 'fity-studio') return;
    if (event.data.action === 'resize') {
      const height = Number(event.data.height);
      if (Number.isFinite(height)) preview.style.height = `${Math.max(80, Math.min(1200, Math.round(height)))}px`;
    }
  });
  if (previewSize && preview) {
    preview.style.maxWidth = previewSize.value;
    previewSize.addEventListener('change', () => {
      preview.style.maxWidth = previewSize.value;
      clearTimeout(previewSizeTimer);
      previewSizeTimer = setTimeout(() => {
        preview.contentWindow.postMessage({type: 'fity-studio-command', action: 'measure'}, '*');
      }, 250);
    });
  }
  form.querySelectorAll('[data-studio-html], [data-studio-css], [data-studio-js], [data-studio-lottie-json], [name="product_ids"], [name="motion_preset"]').forEach(field => {
    field.addEventListener('input', schedulePreview);
    field.addEventListener('change', schedulePreview);
  });
  const fileInput = form.querySelector('[data-studio-upload]');
  const uploadButton = form.querySelector('[data-studio-upload-button]');
  const loadLibraryButton = form.querySelector('[data-studio-load-library]');
  const uploadResult = form.querySelector('[data-studio-upload-result]');
  const library = form.querySelector('[data-studio-library]');
  loadLibraryButton.addEventListener('click', async () => {
    loadLibraryButton.disabled = true;
    uploadResult.textContent = 'Loading media library…';
    try {
      const response = await fetch('/admin/media-showcases/library', {headers: {'Accept': 'application/json'}});
      const result = await response.json();
      if (!response.ok || !Array.isArray(result.items)) throw new Error('The shared media library is unavailable.');
      library.replaceChildren();
      result.items.slice(0, 60).forEach(asset => {
        const url = String(asset.url || '');
        if (!url.startsWith('/static/') && !url.startsWith('https://res.cloudinary.com/')) return;
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'ds-asset-item';
        button.dataset.assetUrl = url;
        button.dataset.assetType = asset.type === 'video' ? 'video' : 'image';
        if (asset.type !== 'video') {
          const image = document.createElement('img');
          image.src = url;
          image.alt = '';
          image.loading = 'lazy';
          button.append(image);
        } else {
          const marker = document.createElement('span');
          marker.textContent = 'VIDEO';
          button.append(marker);
        }
        const label = document.createElement('span');
        label.textContent = asset.filename || 'Store asset';
        button.append(label);
        library.append(button);
      });
      uploadResult.textContent = `${library.childElementCount} approved assets available. Select one to insert it.`;
    } catch (error) {
      uploadResult.textContent = error.message || 'The shared media library is unavailable.';
    } finally {
      loadLibraryButton.disabled = false;
    }
  });
  library.addEventListener('click', event => {
    const button = event.target.closest('[data-asset-url]');
    if (!button) return;
    const url = button.dataset.assetUrl;
    if (button.dataset.assetType === 'video') {
      insertAtCursor(html, `<video controls muted playsinline src="${url}"></video>`);
    } else {
      insertAtCursor(html, `<img src="${url}" alt="">`);
    }
    uploadResult.textContent = 'Asset inserted. The preview is updating automatically.';
    schedulePreview();
  });
  const lottieUpload = form.querySelector('[data-studio-lottie-upload]');
  const lottieJson = form.querySelector('[data-studio-lottie-json]');
  lottieUpload.addEventListener('change', async () => {
    const file = lottieUpload.files?.[0];
    if (!file) return;
    if (file.size > 300000) {
      uploadResult.textContent = 'Lottie JSON must be no larger than 300 KB.';
      lottieUpload.value = '';
      return;
    }
    try {
      const value = await file.text();
      JSON.parse(value);
      lottieJson.value = value;
      ensureLottieTarget();
      uploadResult.textContent = `${file.name} loaded. The preview is updating automatically.`;
      schedulePreview();
    } catch {
      uploadResult.textContent = 'Choose a valid Lottie JSON file.';
    }
  });
  uploadButton.addEventListener('click', async () => {
    const file = fileInput.files?.[0];
    if (!file) {
      uploadResult.textContent = 'Choose an asset first.';
      return;
    }
    const body = new FormData();
    body.append('media', file);
    uploadButton.disabled = true;
    uploadResult.textContent = 'Uploading…';
    try {
      const response = await fetch('/admin/design-studio/assets/upload', {
        method: 'POST',
        headers: {'X-CSRFToken': document.querySelector('meta[name="csrf-token"]')?.content || ''},
        body
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.description || result.error || 'Upload unavailable.');
      const safeUrl = String(result.url || '');
      if (!safeUrl.startsWith('/static/') && !safeUrl.startsWith('https://res.cloudinary.com/')) {
        throw new Error('The media URL did not match the approved storage locations.');
      }
      if (result.type === 'video') {
        insertAtCursor(html, `<video controls muted playsinline src="${safeUrl}"></video>`);
      } else {
        insertAtCursor(html, `<img src="${safeUrl}" alt="">`);
      }
      uploadResult.textContent = `${result.filename || file.name} uploaded. The preview is updating automatically.`;
      schedulePreview();
    } catch (error) {
      uploadResult.textContent = error.message || 'The upload could not be completed.';
    } finally {
      uploadButton.disabled = false;
    }
  });
  function ensureLottieTarget() {
    if (lottieJson.value.trim() && !html.value.includes('data-studio-lottie')) {
      html.value += '\n<div data-studio-lottie aria-hidden="true"></div>';
    }
  }
  function insertAtCursor(field, value) {
    const start = field.selectionStart;
    const end = field.selectionEnd;
    field.setRangeText(value, start, end, 'end');
    field.focus();
  }
})();
