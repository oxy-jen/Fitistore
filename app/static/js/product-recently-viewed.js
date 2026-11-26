(() => {
  const section = document.querySelector('[data-recently-viewed]');
  const grid = section?.querySelector('[data-recently-viewed-grid]');
  if (!section || !grid) return;

  let productIds;
  try {
    const saved = JSON.parse(localStorage.getItem('fity-recently-viewed-products') || '[]');
    const currentId = Number(section.dataset.currentProductId);
    productIds = Array.isArray(saved)
      ? [...new Set(saved.map(Number).filter(id => Number.isInteger(id) && id > 0 && id !== currentId))].slice(0, 8)
      : [];
  } catch (error) {
    console.warn('Recently viewed products are unavailable in this browser.', error);
    return;
  }
  if (!productIds.length) return;

  fetch(`/api/products/recently-viewed?ids=${encodeURIComponent(productIds.join(','))}`, {
    headers: { Accept: 'application/json' }
  })
    .then(response => {
      if (!response.ok) throw new Error('Could not load recently viewed products.');
      return response.json();
    })
    .then(data => {
      if (!Array.isArray(data.products) || !data.products.length) return;
      data.products.forEach(product => {
        const card = document.createElement('a');
        card.className = 'recently-viewed-card';
        card.href = product.url;

        const image = document.createElement('img');
        image.src = product.image_url;
        image.alt = product.image_alt || '';
        image.loading = 'lazy';
        card.append(image);

        const copy = document.createElement('span');
        copy.className = 'recently-viewed-copy';
        const name = document.createElement('strong');
        name.textContent = product.name;
        const price = document.createElement('span');
        price.textContent = product.price_high
          ? `${product.price} – ${product.price_high}`
          : product.price;
        copy.append(name, price);
        card.append(copy);
        grid.append(card);
      });
      section.hidden = !grid.childElementCount;
    })
    .catch(error => console.warn('Recently viewed products could not be loaded.', error));
})();
