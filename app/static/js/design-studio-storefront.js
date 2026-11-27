(() => {
  const allowedPaths = new Set(['/shop', '/upcoming', '/cart']);
  const isAllowedPath = path => allowedPaths.has(path) || /^\/product\/[A-Za-z0-9_-]{1,170}$/.test(path);
  let lastMessageAt = 0;
  window.addEventListener('message', async event => {
    if (event.origin !== 'null' || !event.data || event.data.type !== 'fity-studio') return;
    const frame = [...document.querySelectorAll('[data-fity-studio-frame]')]
      .find(item => item.contentWindow === event.source);
    if (!frame) return;
    const now = Date.now();
    if (now - lastMessageAt < 80) return;
    lastMessageAt = now;
    const data = event.data;
    if (data.action === 'resize') {
      const height = Number(data.height);
      if (Number.isFinite(height)) frame.style.height = `${Math.max(80, Math.min(1200, Math.round(height)))}px`;
      return;
    }
    if (data.action === 'navigate') {
      if (typeof data.path === 'string' && isAllowedPath(data.path)) window.location.assign(data.path);
      return;
    }
    const products = JSON.parse(frame.dataset.products || '[]');
    const productId = Number(data.productId);
    const product = products.find(item => item.id === productId);
    if (!product) return;
    if (data.action === 'open-product') {
      window.location.assign(product.url);
      return;
    }
    if (data.action !== 'add-to-cart') return;
    const quantity = Number(data.quantity);
    const variantId = data.variantId === null ? null : Number(data.variantId);
    if (!Number.isInteger(quantity) || quantity < 1 || quantity > 5) return;
    if (variantId !== null && (!Number.isInteger(variantId) || !product.variant_ids.includes(variantId))) return;
    const token = document.querySelector('meta[name="csrf-token"]')?.content || '';
    try {
      const response = await fetch('/api/design-studio/validate-product', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': token},
        body: JSON.stringify({product_id: productId, variant_id: variantId, quantity})
      });
      const result = await response.json();
      if (!response.ok || result.ok !== true) {
        window.dispatchEvent(new CustomEvent('fity:studio-add-to-cart-error', {
          detail: {message: result.message || 'This item is no longer available in the requested quantity.'}
        }));
        return;
      }
      window.dispatchEvent(new CustomEvent('fity:studio-add-to-cart', {
        detail: {productId, variantId, quantity}
      }));
    } catch {
      window.dispatchEvent(new CustomEvent('fity:studio-add-to-cart-error', {
        detail: {message: 'We could not verify this item. Please try again.'}
      }));
    }
  });
})();
