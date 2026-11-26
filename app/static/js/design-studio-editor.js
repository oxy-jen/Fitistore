(() => {
  const form = document.querySelector('[data-studio-editor]');
  if (!form) return;
  const html = form.querySelector('[data-studio-html]');
  const css = form.querySelector('[data-studio-css]');
  const templateKey = form.querySelector('[name="template_key"]');
  const templates = {
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
  form.querySelectorAll('[data-template]').forEach(button => button.addEventListener('click', () => {
    const key = button.dataset.template;
    const template = templates[key];
    if (!template) return;
    templateKey.value = key;
    html.value = template.html;
    css.value = template.css;
    ensureLottieTarget();
  }));
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
  });
  const previewSize = form.querySelector('[data-preview-size]');
  const preview = form.querySelector('[data-studio-preview]');
  if (previewSize && preview) {
    preview.style.maxWidth = previewSize.value;
    previewSize.addEventListener('change', () => { preview.style.maxWidth = previewSize.value; });
  }
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
    uploadResult.textContent = 'Asset inserted. Save the design to refresh its preview.';
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
      uploadResult.textContent = `${file.name} loaded. Save the design to validate and preview the animation.`;
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
      uploadResult.textContent = `${result.filename || file.name} uploaded. Markup inserted; save the design to refresh its preview.`;
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
