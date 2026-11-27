(() => {
  const form = document.querySelector('[data-studio-editor]');
  if (!form) return;
  const html = form.querySelector('[data-studio-html]');
  const css = form.querySelector('[data-studio-css]');
  const templateKey = form.querySelector('[name="template_key"]');
  const defaultTemplate = JSON.parse(form.querySelector('[data-studio-default-template]').textContent);
  const productGridStyles = `
.studio-product-grid{display:contents}
.studio-product-rail{display:flex;width:100%;min-width:0;align-items:stretch;gap:14px;overflow-x:auto;overflow-y:hidden;overscroll-behavior-x:contain;scroll-snap-type:x mandatory;scrollbar-width:thin;-webkit-overflow-scrolling:touch;padding:8px 4px 14px}
.studio-product-card{display:grid;flex:0 0 clamp(175px,20vw,245px);min-width:0;min-height:300px;grid-template-rows:auto minmax(0,1fr) auto;gap:10px;padding:12px;border:1px solid #eadfe0;border-radius:16px;background:#fff;color:#252525;scroll-snap-align:start}
.studio-product-card a{display:grid;gap:10px;color:inherit;text-decoration:none}
.studio-product-image{position:relative;display:grid;min-height:170px;place-items:center;overflow:hidden;border-radius:12px;background:radial-gradient(circle,rgba(113,77,164,.18),transparent 72%)}
.studio-product-image img{position:relative;z-index:2;width:100%;height:154px;padding-bottom:12px;object-fit:contain;filter:drop-shadow(0 4px 4px rgba(0,0,0,.16))}
.studio-product-pedestal{position:absolute;z-index:1;right:9%;bottom:10px;left:9%;height:28px;filter:drop-shadow(0 5px 6px rgba(35,21,55,.2))}
.studio-product-pedestal-base{position:absolute;top:8px;right:3%;bottom:0;left:3%;border-radius:0 0 48% 48%/0 0 34% 34%;background:linear-gradient(180deg,#c8a9ff 0%,#a879ec 58%,#8050c8 100%)}
.studio-product-pedestal-top{position:absolute;top:0;right:0;left:0;height:13px;border-radius:50%;background:linear-gradient(180deg,#fff 0%,#f5f0ff 62%,#ddd0f8 100%);box-shadow:inset 0 2px 2px rgba(255,255,255,.95),0 1px 2px rgba(31,16,54,.24)}
.studio-product-price{display:flex;flex-wrap:wrap;align-items:center;gap:5px 8px}
.studio-product-current-price{color:#252525;font-size:1rem;font-weight:850}
.studio-product-previous-price{color:#777;font-size:.86rem}
.studio-product-discount{padding:4px 8px;border-radius:999px;background:#fff0e8;color:#c5441a;font-size:.72rem;font-weight:850}
.studio-product-card button{padding:10px 14px;border:0;border-radius:999px;background:#e52335;color:#fff}
@media(max-width:720px){.studio-product-rail{gap:10px}.studio-product-card{flex-basis:72vw;max-width:260px;min-height:258px;grid-template-rows:auto minmax(0,1fr) auto;padding:9px}.studio-product-image{min-height:120px}.studio-product-image img{height:132px}}
`;
  const templates = {
    offers: {html: defaultTemplate.html, css: defaultTemplate.css},
    editorial: {
      html: '<section class="ds-promo"><div class="ds-promo-copy"><p class="ds-kicker">FityStores finds</p><h2>Everyday essentials, better together</h2><p>Fresh picks for your everyday style.</p><a class="ds-cta" href="/shop">Explore the collection</a></div><div class="studio-product-rail" data-studio-product-canvas>%%FITYSTORES_PRODUCTS%%</div></section>',
      css: '.ds-promo{box-sizing:border-box;position:relative;display:grid;grid-template-columns:minmax(190px,.8fr) minmax(0,2fr);align-items:center;gap:20px;width:100%;height:400px;min-height:400px;max-height:400px;overflow:hidden;padding:clamp(16px,3vw,34px);border-radius:24px;background:linear-gradient(120deg,#fff1f4,#f8e7d2);color:#252525}.ds-promo-copy{min-width:0}.ds-kicker{color:#e52335;text-transform:uppercase;letter-spacing:.12em;font-weight:700}.ds-promo h2{max-width:15ch;font-size:clamp(1.7rem,3vw,3rem);line-height:1.04}.ds-cta{display:inline-block;padding:12px 20px;border-radius:999px;background:#e52335;color:white;text-decoration:none}@media(max-width:720px){.ds-promo{grid-template-columns:minmax(115px,.72fr) minmax(0,1.5fr);gap:10px;height:351px;min-height:351px;max-height:351px;padding:12px}.ds-promo h2{font-size:clamp(1.25rem,5vw,2rem)}.ds-promo-copy>p:not(.ds-kicker){font-size:.8rem}.ds-promo .ds-cta{padding:8px 10px;font-size:.75rem}}' + productGridStyles
    },
    'product-grid': {
      html: '<section class="ds-products"><div class="ds-products-copy"><p class="ds-kicker">Shop the edit</p><h2>Everyday favourites</h2><p>Current picks, straight from the FityStores catalogue.</p></div><div class="studio-product-rail" data-studio-product-canvas>%%FITYSTORES_PRODUCTS%%</div></section>',
      css: '.ds-products{box-sizing:border-box;position:relative;display:grid;grid-template-columns:minmax(190px,.8fr) minmax(0,2fr);align-items:center;gap:20px;width:100%;height:400px;min-height:400px;max-height:400px;overflow:hidden;padding:clamp(16px,3vw,34px);background:#fff8f4;border-radius:20px;color:#252525}.ds-products-copy{min-width:0}.ds-products h2{font-size:clamp(1.7rem,3vw,3rem)}@media(max-width:720px){.ds-products{grid-template-columns:minmax(115px,.72fr) minmax(0,1.5fr);gap:10px;height:351px;min-height:351px;max-height:351px;padding:12px}.ds-products h2{font-size:clamp(1.25rem,5vw,2rem)}.ds-products-copy>p:not(.ds-kicker){font-size:.8rem}}' + productGridStyles
    },
    seasonal: {
      html: '<section class="ds-season"><div class="ds-season-copy"><p class="ds-kicker">Your next everyday favourite</p><h2>Small details. Big feel-good energy.</h2><p>Discover comfort-first finds picked for the season.</p><a class="ds-cta" href="/shop">Shop the edit</a><span class="ds-season-mark" aria-hidden="true">F</span></div><div class="studio-product-rail" data-studio-product-canvas>%%FITYSTORES_PRODUCTS%%</div></section>',
      css: '.ds-season{box-sizing:border-box;position:relative;display:grid;grid-template-columns:minmax(190px,.8fr) minmax(0,2fr);align-items:center;gap:20px;width:100%;height:400px;min-height:400px;max-height:400px;overflow:hidden;padding:clamp(16px,3vw,34px);border-radius:24px;background:#252525;color:white}.ds-season-copy{position:relative;min-width:0}.ds-season h2{font-size:clamp(1.7rem,3vw,3rem);line-height:1.05}.ds-kicker{color:#ffb5bd;text-transform:uppercase;letter-spacing:.12em;font-weight:700}.ds-cta{display:inline-block;padding:12px 20px;border-radius:999px;background:#e52335;color:white;text-decoration:none}.ds-season-mark{position:absolute;right:0;bottom:-.25em;color:#e52335;font-size:clamp(3rem,7vw,6rem);font-weight:900;opacity:.5;pointer-events:none}@media(max-width:720px){.ds-season{grid-template-columns:minmax(115px,.72fr) minmax(0,1.5fr);gap:10px;height:351px;min-height:351px;max-height:351px;padding:12px}.ds-season h2{font-size:clamp(1.25rem,5vw,2rem)}.ds-season-copy>p:not(.ds-kicker){font-size:.8rem}.ds-season .ds-cta{padding:8px 10px;font-size:.75rem}}' + productGridStyles
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
  const layoutStart = '/* fity-studio-card-layout:start */';
  const layoutEnd = '/* fity-studio-card-layout:end */';
  let customCardSequence = 0;
  function createCustomCardKey() {
    const random = globalThis.crypto?.randomUUID?.().replace(/[^a-z0-9-]/gi, '').toLowerCase() || '';
    return `c-${random || `${Date.now().toString(36)}-${(++customCardSequence).toString(36)}`}`;
  }
  function ensureEditableMarkup(markup) {
    const parsed = new DOMParser().parseFromString(markup || '', 'text/html');
    let nextEditId = [...parsed.body.querySelectorAll('[data-studio-edit-id]')]
      .reduce((current, element) => Math.max(current, Number(element.dataset.studioEditId.slice(1)) || 0), 0);
    parsed.body.querySelectorAll('.offers-product-item,.studio-product-card').forEach(card => {
      if (!card.dataset.studioCardKey) card.dataset.studioCardKey = createCustomCardKey();
    });
    parsed.body.querySelectorAll('h1,h2,h3,h4,p,a,button,strong,em,b,small,span,li,figcaption').forEach(element => {
      if (element.dataset.studioEditId || element.closest('[aria-hidden="true"]')) return;
      const hasEditableText = [...element.childNodes].some(node =>
        node.nodeType === Node.TEXT_NODE
        && node.nodeValue.trim()
        && !node.nodeValue.includes('%%FITYSTORES_')
      );
      if (hasEditableText) {
        nextEditId += 1;
        element.dataset.studioEditId = `e${nextEditId}`;
      }
    });
    const result = parsed.body.innerHTML;
    return result.length <= 20000 ? result : markup;
  }
  function studioLayoutBlock(value = css.value) {
    const start = value.indexOf(layoutStart);
    const end = value.indexOf(layoutEnd, start + layoutStart.length);
    return start < 0 || end < 0 ? '' : value.slice(start, end + layoutEnd.length);
  }
  function readStudioLayout(value = css.value) {
    const block = studioLayoutBlock(value);
    const rules = new Map();
    const rulePattern = /\[data-studio-card-key="([a-z0-9-]+)"\]\s*\{([^}]*)\}/gi;
    let match;
    while ((match = rulePattern.exec(block))) {
      const key = match[1];
      if (!/^(?:p-\d+|c-[a-z0-9-]{1,70})$/.test(key)) continue;
      const properties = {};
      match[2].split(';').forEach(declaration => {
        const [name, ...parts] = declaration.split(':');
        const property = name.trim().toLowerCase();
        const content = parts.join(':').trim();
        if (property === 'order' && /^-?\d{1,5}$/.test(content)) properties.order = content;
        if (property === 'transform' && /^translate\(\s*-?(?:\d+(?:\.\d+)?|\.\d+)%\s*,\s*-?(?:\d+(?:\.\d+)?|\.\d+)%\s*\)$/.test(content)) {
          properties.transform = content;
        }
      });
      if (Object.keys(properties).length) rules.set(key, properties);
    }
    return rules;
  }
  function writeStudioLayout(rules) {
    const block = studioLayoutBlock();
    const baseCss = css.value.replace(block, '').trimEnd();
    const entries = [...rules.entries()]
      .filter(([key, properties]) => /^(?:p-\d+|c-[a-z0-9-]{1,70})$/.test(key) && Object.keys(properties).length)
      .map(([key, properties]) => `[data-studio-card-key="${key}"]{${Object.entries(properties).map(([name, value]) => `${name}:${value}`).join(';')}}`);
    const nextBlock = entries.length ? `${layoutStart}\n${entries.join('\n')}\n${layoutEnd}` : '';
    const nextCss = [baseCss, nextBlock].filter(Boolean).join('\n');
    if (nextCss.length > 20000) {
      previewStatus.textContent = 'The card layout cannot be saved because the style limit has been reached.';
      return false;
    }
    css.value = nextCss;
    previewStatus.textContent = 'Card layout updated in the preview. Save the draft to keep these changes.';
    return true;
  }
  function updateStudioLayout(updates) {
    const rules = readStudioLayout();
    updates.forEach(([key, properties]) => {
      if (!/^(?:p-\d+|c-[a-z0-9-]{1,70})$/.test(key)) return;
      const next = {...(rules.get(key) || {})};
      Object.entries(properties).forEach(([name, value]) => {
        if (value === null) delete next[name];
        else next[name] = value;
      });
      rules.set(key, next);
    });
    return writeStudioLayout(rules);
  }
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
.ds-campaign{--ds-campaign-background:${value.background};--ds-campaign-accent:${value.accent};--ds-campaign-platform:${value.platform};box-sizing:border-box;position:relative;isolation:isolate;display:grid;grid-template-columns:minmax(210px,.82fr) minmax(0,2.4fr);align-items:center;gap:clamp(14px,2.4vw,34px);width:100%;height:400px;min-height:400px;max-height:400px;overflow:hidden;padding:clamp(20px,3.3vw,42px);border-radius:20px;background:linear-gradient(112deg,#08070b 0%,#100c18 42%,var(--ds-campaign-background) 100%);color:#fff;font-family:system-ui,sans-serif}
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
.ds-campaign .offers-product-pedestal{position:absolute;z-index:1;right:9%;bottom:3px;left:9%;height:27px;pointer-events:none;filter:drop-shadow(0 7px 8px rgba(18,8,39,.32))}
.ds-campaign .offers-product-pedestal-base{position:absolute;top:8px;right:3%;bottom:0;left:3%;border-radius:0 0 48% 48%/0 0 34% 34%;background:linear-gradient(180deg,var(--ds-campaign-platform) 0%,#a879ec 58%,#8050c8 100%);box-shadow:inset 0 -2px 3px rgba(67,33,125,.32)}
.ds-campaign .offers-product-pedestal-top{position:absolute;z-index:1;top:0;right:0;left:0;height:13px;border:1px solid rgba(255,255,255,.92);border-radius:50%;background:linear-gradient(180deg,#fff 0%,#f5f0ff 62%,#ddd0f8 100%);box-shadow:inset 0 2px 2px rgba(255,255,255,.95),0 1px 2px rgba(31,16,54,.3)}
.ds-campaign .offers-product-image-link{position:relative;z-index:2;display:grid;width:100%;height:100%;place-items:end center;padding-bottom:10px}
.ds-campaign .offers-product-image img{position:relative;z-index:3;display:block;width:100%;height:clamp(118px,15vw,190px);object-fit:contain;filter:drop-shadow(0 5px 4px rgba(0,0,0,.24))}
.ds-campaign .home-quick-add-card .home-card-quick-add{position:absolute;z-index:3;top:38%;right:.1rem;display:grid;width:30px;height:30px;place-items:center;border:0;border-radius:50%;background:#fff;color:#21172b;box-shadow:0 2px 8px rgba(0,0,0,.22);font-size:21px;line-height:1;text-decoration:none}
.ds-campaign .offers-price-group{display:flex;flex-wrap:wrap;align-items:center;justify-content:center;gap:.14rem .34rem}
.ds-campaign .offers-current-price{display:inline-flex;min-height:27px;align-items:center;justify-content:center;padding:.3rem .6rem;border-radius:999px;background:#fff;color:#17121e;font-size:clamp(.62rem,.95vw,.82rem);font-weight:900;line-height:1;text-decoration:none;white-space:nowrap}
.ds-campaign .offers-original-price{color:#e6e1ea;font-size:clamp(.56rem,.78vw,.69rem);line-height:1.1}
.ds-campaign .offers-discount-note{flex-basis:100%;color:#ffe0a2;font-size:.58rem;font-weight:750;line-height:1.1}
.ds-campaign:not(.ds-campaign--cutouts) .offers-product-image::before{top:12%;width:92%;height:72%;aspect-ratio:auto;border-radius:15px;background:rgba(255,255,255,.1)}
.ds-campaign:not(.ds-campaign--cutouts) .offers-product-image img{filter:none}
.ds-campaign--black-friday-feature .ds-campaign-products::before{position:absolute;z-index:-1;inset:4% 5%;border-radius:50%;background:radial-gradient(circle,var(--ds-campaign-accent),transparent 72%);opacity:.48;content:""}
.ds-campaign--deal-of-day .ds-campaign-product-track{gap:clamp(0px,.5vw,8px)}
@media(max-width:760px){.ds-campaign{grid-template-columns:minmax(145px,.72fr) minmax(0,1.65fr);gap:10px;height:351px;min-height:351px;max-height:351px;padding:18px 14px}.ds-campaign h2{font-size:clamp(1.7rem,5vw,2.8rem)}.ds-campaign .offers-product-item{min-height:258px}.ds-campaign .offers-product-card{min-height:258px;grid-template-rows:auto minmax(100px,1fr) auto}.ds-campaign .offers-product-image{min-height:100px}.ds-campaign .offers-product-image img{height:clamp(100px,20vw,145px)}.ds-campaign .offers-current-price{white-space:normal}}
@media(max-width:520px){.ds-campaign{grid-template-columns:minmax(105px,.72fr) minmax(0,1.65fr);gap:7px;height:351px;min-height:351px;max-height:351px;padding:12px 9px}.ds-campaign h2{font-size:clamp(1.2rem,6vw,1.8rem)}.ds-campaign-brand{font-size:.75rem}.ds-campaign-kicker{font-size:.95rem}.ds-campaign-badge{font-size:.48rem}.ds-campaign-copyline{font-size:.69rem}.ds-campaign-cta{min-height:30px;padding:.35rem .55rem;font-size:.68rem}.ds-campaign-product-track{gap:7px}.ds-campaign .offers-product-item{flex-basis:clamp(112px,29vw,150px);min-height:258px}.ds-campaign .offers-product-card h3{font-size:.72rem}.ds-campaign .offers-product-image img{height:126px}}
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
      ? `<span class="ds-campaign-heading-highlight" data-studio-edit-field="highlight">${escapeMarkup(value.highlight)}</span>` : '';
    const arrow = ' →';
    const currentMarkup = new DOMParser().parseFromString(html.value || '', 'text/html');
    const savedLottieTarget = currentMarkup.body.querySelector('[data-studio-lottie-target]')?.dataset.studioLottieTarget;
    const lottieTargetAttribute = savedLottieTarget
      && (savedLottieTarget === 'section' || /^(?:p-\d+|c-[a-z0-9-]{1,70})$/.test(savedLottieTarget))
      ? ` data-studio-lottie-target="${escapeMarkup(savedLottieTarget)}"`
      : '';
    const customCards = [...currentMarkup.querySelectorAll('[data-studio-card-key^="c-"]')]
      .map(card => card.outerHTML).join('');
    const savedLayout = studioLayoutBlock();
    const campaignMarkup = `<section class="ds-campaign ds-campaign--${key}${cutoutClass}"${lottieTargetAttribute} data-template-key="${key}" data-template-brand="${attr('brand')}" data-template-kicker="${attr('kicker')}" data-template-badge="${attr('badge')}" data-template-headline="${attr('headline')}" data-template-highlight="${attr('highlight')}" data-template-copy="${attr('copy')}" data-template-cta="${attr('cta')}" data-template-url="${attr('url')}" data-template-background="${attr('background')}" data-template-accent="${attr('accent')}" data-template-platform="${attr('platform')}" data-template-cutouts="${value.cutouts ? 'true' : 'false'}"><div class="ds-campaign-copy"><p class="ds-campaign-brand" data-studio-edit-field="brand">${escapeMarkup(value.brand)}</p><p class="ds-campaign-kicker" data-studio-edit-field="kicker">${escapeMarkup(value.kicker)}</p>${badge ? badge.replace('<p class="ds-campaign-badge">', '<p class="ds-campaign-badge" data-studio-edit-field="badge">') : ''}<h2><span class="ds-campaign-heading-main" data-studio-edit-field="headline">${escapeMarkup(value.headline)}</span>${highlight}</h2><p class="ds-campaign-copyline" data-studio-edit-field="copy">${escapeMarkup(value.copy)}</p><a class="ds-campaign-cta" href="${attr('url')}"><span data-studio-edit-field="cta">${escapeMarkup(value.cta)}</span><span aria-hidden="true">${arrow}</span></a></div><div class="ds-campaign-products"><div class="ds-campaign-product-track" data-studio-product-canvas>%%FITYSTORES_OFFERS%%${customCards}</div></div></section>`;
    html.value = ensureEditableMarkup(campaignMarkup);
    css.value = `${campaignCss(key, value)}${savedLayout ? `\n${savedLayout}` : ''}`;
  }
  form.querySelectorAll('[data-template]').forEach(button => button.addEventListener('click', () => {
    const key = button.dataset.template;
    const template = templates[key];
    if (!template && !campaignKeys.has(key)) return;
    html.value = ensureEditableMarkup(html.value);
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
    html.value = ensureEditableMarkup(html.value);
    syncLottieTargetOptions();
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
  html.value = ensureEditableMarkup(html.value);
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
  const productPicker = form.querySelector('[data-studio-product-picker]');
  const selectedProducts = form.querySelector('[data-studio-selected-products]');
  const productInputs = form.querySelector('[data-studio-product-inputs]');
  const addProductButton = form.querySelector('[data-studio-add-product]');
  const productStatus = form.querySelector('[data-studio-product-status]');
  const lottieTargetSelect = form.querySelector('[data-studio-lottie-target]');
  const lottieTargetStatus = form.querySelector('[data-studio-lottie-target-status]');
  const selectedProductIds = new Set();
  let unavailableSavedProductCount = 0;
  let selectionLoadFailed = false;
  function lottieProductChoices() {
    const choices = new Map();
    [...productPicker.options].slice(1).forEach(option => {
      choices.set(`p-${option.value}`, option.dataset.name || option.textContent.trim());
    });
    const markup = new DOMParser().parseFromString(html.value || '', 'text/html');
    markup.body.querySelectorAll('[data-studio-card-key^="c-"]').forEach(card => {
      const key = card.dataset.studioCardKey;
      const title = card.querySelector('.offers-product-card h3,h3,strong')?.textContent.trim();
      if (key && title) choices.set(key, title);
    });
    return choices;
  }
  function syncLottieTargetOptions() {
    const markup = new DOMParser().parseFromString(html.value || '', 'text/html');
    const root = markup.body.querySelector('[data-studio-lottie-target]') || markup.body.firstElementChild;
    const savedTarget = root?.dataset.studioLottieTarget || lottieTargetSelect.value || 'section';
    const choices = lottieProductChoices();
    const savedProductId = /^p-(\d+)$/.exec(savedTarget)?.[1];
    lottieTargetSelect.replaceChildren(new Option('Section background', 'section'));
    if (choices.size) {
      const group = document.createElement('optgroup');
      group.label = 'Product displayed over Lottie background';
      choices.forEach((name, key) => {
        group.append(new Option(name.slice(0, 120), key));
      });
      lottieTargetSelect.append(group);
    }
    if (savedTarget === 'section' || (choices.has(savedTarget)
      && (!savedProductId || selectedProductIds.has(savedProductId)))) {
      lottieTargetSelect.value = savedTarget;
      lottieTargetStatus.textContent = savedTarget === 'section'
        ? 'The animation will preview automatically behind the selected section.'
        : 'This product card will render over the Lottie animation.';
      return;
    }
    lottieTargetSelect.value = 'section';
    lottieTargetStatus.textContent = 'The selected product is no longer available; the animation is set to the section background.';
    if (root) {
      root.dataset.studioLottieTarget = 'section';
      html.value = ensureEditableMarkup(markup.body.innerHTML);
    }
  }
  function persistLottieTarget(target) {
    const availableTargets = new Set([...lottieTargetSelect.options].map(option => option.value));
    if (!availableTargets.has(target)) return false;
    const markup = new DOMParser().parseFromString(html.value || '', 'text/html');
    const root = markup.body.querySelector('[data-studio-lottie-target]') || markup.body.firstElementChild;
    if (!root) return false;
    root.dataset.studioLottieTarget = target;
    markup.body.querySelectorAll('[data-studio-lottie]').forEach(container => container.remove());
    html.value = ensureEditableMarkup(markup.body.innerHTML);
    return true;
  }
  lottieTargetSelect.addEventListener('change', () => {
    const target = lottieTargetSelect.value;
    const productId = /^p-(\d+)$/.exec(target)?.[1];
    if (productId && !selectedProductIds.has(productId)) {
      if (selectedProductIds.size >= 30) {
        syncLottieTargetOptions();
        lottieTargetStatus.textContent = 'Remove a product from the design before choosing another animation product (maximum 30).';
        return;
      }
      selectedProductIds.add(productId);
      renderSelectedProducts();
    }
    if (!persistLottieTarget(target)) {
      syncLottieTargetOptions();
      lottieTargetStatus.textContent = 'Choose a section or an available product card as the animation target.';
      return;
    }
    syncLottieTargetOptions();
    schedulePreview();
  });
  function renderSelectedProducts(updatePreview = false) {
    selectedProducts.replaceChildren();
    productInputs.replaceChildren();
    const selected = [...selectedProductIds]
      .map(id => [...productPicker.options].find(option => option.value === id))
      .filter(Boolean)
      .slice(0, 30);
    selectedProductIds.clear();
    selected.forEach(option => {
      const id = option.value;
      selectedProductIds.add(id);
      const card = document.createElement('article');
      card.className = 'ds-selected-product';
      const imageUrl = option.dataset.image || '';
      if (imageUrl.startsWith('/static/') || imageUrl.startsWith('https://res.cloudinary.com/')) {
        const image = document.createElement('img');
        image.className = 'ds-selected-product-image';
        image.src = imageUrl;
        image.alt = '';
        image.loading = 'lazy';
        card.append(image);
      } else {
        const placeholder = document.createElement('span');
        placeholder.className = 'ds-selected-product-image ds-selected-product-placeholder';
        placeholder.setAttribute('aria-hidden', 'true');
        placeholder.textContent = 'F';
        card.append(placeholder);
      }
      const copy = document.createElement('div');
      copy.className = 'ds-selected-product-copy';
      const name = document.createElement('strong');
      name.textContent = option.dataset.name || option.textContent.trim();
      copy.append(name);
      const prices = document.createElement('div');
      prices.className = 'ds-selected-product-prices';
      const currentPrice = document.createElement('span');
      currentPrice.className = 'ds-selected-current-price';
      currentPrice.textContent = option.dataset.currentPrice || 'See current price';
      prices.append(currentPrice);
      if (option.dataset.previousPrice) {
        const previousPrice = document.createElement('s');
        previousPrice.textContent = option.dataset.previousPrice;
        prices.append(previousPrice);
      }
      if (option.dataset.discountPercent && Number(option.dataset.discountPercent) > 0) {
        const discount = document.createElement('span');
        discount.className = 'ds-product-discount-badge';
        discount.textContent = `${option.dataset.discountPercent}% OFF`;
        prices.append(discount);
      }
      copy.append(prices);
      card.append(copy);
      const removeButton = document.createElement('button');
      removeButton.type = 'button';
      removeButton.className = 'ds-selected-product-remove';
      removeButton.dataset.removeProduct = id;
      removeButton.setAttribute('aria-label', `Remove ${name.textContent} from this design`);
      removeButton.title = 'Remove product';
      removeButton.textContent = '×';
      card.append(removeButton);
      selectedProducts.append(card);
      const input = document.createElement('input');
      input.type = 'hidden';
      input.name = 'product_ids';
      input.value = id;
      productInputs.append(input);
      option.disabled = true;
    });
    [...productPicker.options].slice(1).forEach(option => {
      option.disabled = selectedProductIds.has(option.value);
    });
    productPicker.value = '';
    addProductButton.disabled = selectedProductIds.size >= 30 || !productPicker.value;
    productStatus.textContent = `${selectedProductIds.size} of 30 products selected.`
      + (selectionLoadFailed ? ' Saved product choices could not be read; select them again.' : '')
      + (unavailableSavedProductCount ? ` ${unavailableSavedProductCount} saved product(s) are no longer active.` : '');
    syncLottieTargetOptions();
    if (updatePreview) schedulePreview();
  }
  try {
    const selectedIds = JSON.parse(form.querySelector('[data-studio-products]').dataset.selectedProducts || '[]');
    if (!Array.isArray(selectedIds)) throw new Error('Invalid saved product list');
    const activeProductIds = new Set([...productPicker.options].slice(1).map(option => option.value));
    selectedIds.map(String).slice(0, 30).forEach(id => {
      if (activeProductIds.has(id)) selectedProductIds.add(id);
      else unavailableSavedProductCount += 1;
    });
  } catch {
    selectionLoadFailed = true;
  }
  renderSelectedProducts();
  productPicker.addEventListener('change', () => {
    addProductButton.disabled = !productPicker.value || selectedProductIds.size >= 30;
  });
  addProductButton.addEventListener('click', () => {
    if (!productPicker.value || selectedProductIds.size >= 30) return;
    selectedProductIds.add(productPicker.value);
    renderSelectedProducts(true);
  });
  selectedProducts.addEventListener('click', event => {
    const button = event.target.closest('[data-remove-product]');
    if (!button || !selectedProductIds.has(button.dataset.removeProduct)) return;
    selectedProductIds.delete(button.dataset.removeProduct);
    renderSelectedProducts(true);
  });
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
      setPreviewEditorMode();
      return;
    }
    previewBusy = false;
    previewStatus.textContent = 'Live preview updated.';
    setPreviewEditorMode();
    if (previewQueued) submitPreview();
  });
  preview.contentWindow.postMessage({type: 'fity-studio-command', action: 'measure'}, '*');
  const cardMode = form.querySelector('[data-studio-card-mode]');
  function setPreviewEditorMode() {
    if (!preview.contentWindow) return;
    preview.contentWindow.postMessage({
      type: 'fity-studio-command',
      action: 'enable-visual-editor',
      cardMode: cardMode.value
    }, '*');
  }
  cardMode.addEventListener('change', setPreviewEditorMode);
  window.addEventListener('message', event => {
    if (event.source !== preview.contentWindow || !event.data || event.data.type !== 'fity-studio') return;
    const data = event.data;
    if (data.action === 'lottie-target-missing') {
      previewStatus.textContent = 'The selected product card is unavailable; the animation is previewing as the section background.';
      return;
    }
    if (data.action === 'lottie-render-error') {
      previewStatus.textContent = 'The Lottie player could not render this animation. Check the JSON and local player availability.';
      return;
    }
    if (data.action === 'resize') {
      const height = Number(event.data.height);
      if (Number.isFinite(height)) preview.style.height = `${Math.max(80, Math.min(1200, Math.round(height)))}px`;
      return;
    }
    if (data.action === 'text-edit') {
      const id = String(data.id || '');
      if (!/^e\d{1,5}$/.test(id) || typeof data.html !== 'string' || data.html.length > 5000) return;
      const parsed = new DOMParser().parseFromString(html.value, 'text/html');
      const source = parsed.body.querySelector(`[data-studio-edit-id="${id}"]`);
      if (!source) return;
      source.innerHTML = data.html;
      const fieldName = source.dataset.studioEditField;
      if (fieldName && campaignFields[fieldName]) {
        const value = source.textContent.trim().replace(/\s+/g, ' ')
          .slice(0, {brand: 60, kicker: 70, badge: 40, headline: 60, highlight: 40, copy: 180, cta: 30}[fieldName]);
        const campaignRoot = source.closest('[data-template-key]');
        if (campaignRoot && campaignKeys.has(campaignRoot.dataset.templateKey)) {
          source.textContent = value;
          campaignRoot.setAttribute(`data-template-${fieldName}`, value);
          campaignFields[fieldName].value = value;
        }
      }
      html.value = ensureEditableMarkup(parsed.body.innerHTML);
      previewStatus.textContent = 'Text updated in the preview. Save the draft to keep this change.';
      return;
    }
    if (data.action === 'card-order' && Array.isArray(data.keys) && data.keys.length <= 60) {
      const keys = data.keys.map(String);
      if (new Set(keys).size !== keys.length
          || keys.some(key => !/^(?:p-\d+|c-[a-z0-9-]{1,70})$/.test(key))) return;
      updateStudioLayout(keys.map((key, index) => [key, {order: String(index + 1)}]));
      return;
    }
    if (data.action === 'card-position') {
      const key = String(data.key || '');
      const x = Number(data.x);
      const y = Number(data.y);
      if (!/^(?:p-\d+|c-[a-z0-9-]{1,70})$/.test(key)
          || !Number.isFinite(x) || !Number.isFinite(y)
          || Math.abs(x) > 500 || Math.abs(y) > 500) return;
      updateStudioLayout([[key, {transform: `translate(${x.toFixed(2)}%, ${y.toFixed(2)}%)`}]]);
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
  form.querySelectorAll('[data-studio-html], [data-studio-css], [data-studio-js], [data-studio-lottie-json], [name="motion_preset"]').forEach(field => {
    field.addEventListener('input', schedulePreview);
    field.addEventListener('change', schedulePreview);
  });
  html.addEventListener('change', () => {
    const previous = html.value;
    html.value = ensureEditableMarkup(html.value);
    syncLottieTargetOptions();
    ensureLottieTarget();
    if (html.value !== previous) schedulePreview();
  });
  if (html.value) {
    const updated = ensureEditableMarkup(html.value);
    html.value = updated;
    schedulePreview();
  }
  const fileInput = form.querySelector('[data-studio-upload]');
  const uploadButton = form.querySelector('[data-studio-upload-button]');
  const loadLibraryButton = form.querySelector('[data-studio-load-library]');
  const uploadResult = form.querySelector('[data-studio-upload-result]');
  const library = form.querySelector('[data-studio-library]');
  const cardBuilder = form.querySelector('[data-studio-card-builder]');
  const cardImagePreview = form.querySelector('[data-studio-card-image-preview]');
  const cardImage = form.querySelector('[data-studio-card-image]');
  const cardImageName = form.querySelector('[data-studio-card-image-name]');
  const cardName = form.querySelector('[data-studio-card-name]');
  const cardCurrentPrice = form.querySelector('[data-studio-card-current-price]');
  const cardPreviousPrice = form.querySelector('[data-studio-card-previous-price]');
  const cardLink = form.querySelector('[data-studio-card-link]');
  const addProductCard = form.querySelector('[data-studio-add-product-card]');
  let selectedCardImageUrl = '';
  function selectProductImage(url, name) {
    if ((!url.startsWith('/static/') || url.startsWith('//') || url.includes('..'))
        && !url.startsWith('https://res.cloudinary.com/')) {
      uploadResult.textContent = 'Choose an image from the approved local or Cloudinary media library.';
      return;
    }
    selectedCardImageUrl = url;
    cardImage.src = url;
    cardImage.alt = name || 'Uploaded product';
    cardImageName.textContent = name || 'Selected product image';
    cardImagePreview.hidden = false;
    addProductCard.disabled = false;
    uploadResult.textContent = 'Image selected. Complete the product details and add it to the template.';
  }
  function currencyAmount(value) {
    const amount = Number(value);
    if (!Number.isFinite(amount) || amount <= 0) return '';
    const [whole, fraction] = amount.toFixed(2).split('.');
    const currency = cardBuilder.dataset.currency || '';
    return `${currency} ${Number(whole).toLocaleString('en')}${fraction === '00' ? '' : `.${fraction}`}`.trim();
  }
  function customProductCardMarkup(cardKey, url, name, currentPrice, previousPrice, destination) {
    const safeUrl = escapeMarkup(url);
    const safeName = escapeMarkup(name);
    const safeDestination = escapeMarkup(destination);
    const discountPercent = previousPrice ? Math.floor(((previousPrice - currentPrice) / previousPrice) * 100) : 0;
    if (templateKey.value === 'offers' || campaignKeys.has(templateKey.value)) {
      return `<div class="offers-product-item" data-studio-card-key="${cardKey}"><article class="offers-product-card"><div class="offers-product-copy"><p class="offers-product-category">Featured</p><h3><a href="${safeDestination}">${safeName}</a></h3></div><div class="offers-product-image"><span class="offers-product-pedestal" aria-hidden="true"><span class="offers-product-pedestal-base"></span><span class="offers-product-pedestal-top"></span></span><a class="offers-product-image-link" href="${safeDestination}" aria-label="View ${safeName}"><img src="${safeUrl}" alt="${safeName}" loading="lazy" width="540" height="540"></a></div><div class="offers-price-group"><span class="offers-current-price">${currencyAmount(currentPrice)}</span>${previousPrice ? `<s class="offers-original-price">${currencyAmount(previousPrice)}</s><span class="offers-discount-note">${discountPercent}% off</span>` : '<span class="offers-discount-note">Available now</span>'}</div></article></div>`;
    }
    return `<article class="studio-product-card" data-studio-card-key="${cardKey}"><a href="${safeDestination}" aria-label="View ${safeName}"><span class="studio-product-image"><img src="${safeUrl}" alt="${safeName}" loading="lazy" width="540" height="540"><span class="studio-product-pedestal" aria-hidden="true"><span class="studio-product-pedestal-base"></span><span class="studio-product-pedestal-top"></span></span></span><strong>${safeName}</strong></a><div class="studio-product-price"><strong class="studio-product-current-price">${currencyAmount(currentPrice)}</strong>${previousPrice ? `<s class="studio-product-previous-price">${currencyAmount(previousPrice)}</s><span class="studio-product-discount">${discountPercent}% OFF</span>` : ''}</div></article>`;
  }
  cardLink.addEventListener('change', () => {
    const option = cardLink.selectedOptions[0];
    if (!option || !option.value) return;
    cardName.value = option.dataset.productName || '';
    const currentCents = Number(option.dataset.currentCents);
    const previousCents = Number(option.dataset.previousCents);
    cardCurrentPrice.value = Number.isFinite(currentCents) && currentCents > 0 ? (currentCents / 100).toFixed(2) : '';
    cardPreviousPrice.value = Number.isFinite(previousCents) && previousCents > 0 ? (previousCents / 100).toFixed(2) : '';
  });
  addProductCard.addEventListener('click', () => {
    const name = cardName.value.trim();
    const currentPrice = Number(cardCurrentPrice.value);
    const previousPrice = cardPreviousPrice.value ? Number(cardPreviousPrice.value) : null;
    const destination = cardLink.value;
    if (!selectedCardImageUrl || !name || !Number.isFinite(currentPrice) || currentPrice <= 0
        || (previousPrice !== null && (!Number.isFinite(previousPrice) || previousPrice <= currentPrice))
        || !/^\/product\/[A-Za-z0-9_-]{1,170}$/.test(destination)) {
      uploadResult.textContent = 'Add an image, product name, valid current price, and product destination. Previous price must exceed current price.';
      return;
    }
    const placeholder = templateKey.value === 'offers' || campaignKeys.has(templateKey.value)
      ? '%%FITYSTORES_OFFERS%%' : '%%FITYSTORES_PRODUCTS%%';
    if (!html.value.includes(placeholder)) {
      uploadResult.textContent = 'This template has no product rail. Choose an editable product template before adding a product card.';
      return;
    }
    const markup = customProductCardMarkup(
      createCustomCardKey(), selectedCardImageUrl, name, currentPrice, previousPrice, destination
    );
    html.value = ensureEditableMarkup(html.value.replace(placeholder, `${placeholder}${markup}`));
    syncLottieTargetOptions();
    html.dispatchEvent(new Event('input', {bubbles: true}));
    selectedCardImageUrl = '';
    cardImage.removeAttribute('src');
    cardImage.alt = '';
    cardImageName.textContent = '';
    cardImagePreview.hidden = true;
    cardName.value = '';
    cardCurrentPrice.value = '';
    cardPreviousPrice.value = '';
    cardLink.value = '';
    addProductCard.disabled = true;
    uploadResult.textContent = 'Product card added to the template rail. Preview updated automatically.';
  });
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
      uploadResult.textContent = `${library.childElementCount} approved assets available. Select an image for a product card; videos insert into the markup.`;
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
      insertAtCursor(html, `<video controls muted playsinline src="${escapeMarkup(url)}"></video>`);
      uploadResult.textContent = 'Video inserted. The preview is updating automatically.';
      schedulePreview();
    } else {
      selectProductImage(url, button.querySelector('span:last-child')?.textContent || '');
    }
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
        insertAtCursor(html, `<video controls muted playsinline src="${escapeMarkup(safeUrl)}"></video>`);
        uploadResult.textContent = `${result.filename || file.name} uploaded and inserted. The preview is updating automatically.`;
        schedulePreview();
      } else {
        selectProductImage(safeUrl, result.filename || file.name);
        fileInput.value = '';
      }
    } catch (error) {
      uploadResult.textContent = error.message || 'The upload could not be completed.';
    } finally {
      uploadButton.disabled = false;
    }
  });
  function ensureLottieTarget() {
    if (lottieJson.value.trim() && !persistLottieTarget(lottieTargetSelect.value || 'section')) {
      lottieTargetStatus.textContent = 'Add a section to the design before placing its animation.';
      return false;
    }
    return true;
  }
  function insertAtCursor(field, value) {
    const start = field.selectionStart;
    const end = field.selectionEnd;
    field.setRangeText(value, start, end, 'end');
    field.focus();
  }
})();
