(() => {
  const rows = document.getElementById('product-variant-rows');
  const template = document.getElementById('product-variant-row-template');
  const addButton = document.getElementById('add-product-variant');
  if (!rows || !template || !addButton) return;

  const refreshLabels = () => rows.querySelectorAll('[data-product-variant-row]').forEach((row, index) => {
    const heading = row.querySelector('.admin-variant-row-heading strong');
    if (heading) heading.textContent = `Variant ${index + 1}`;
  });

  const appendRow = source => {
    const row = source ? source.cloneNode(true) : template.content.firstElementChild.cloneNode(true);
    row.querySelectorAll('input[type="file"]').forEach(input => { input.value = ''; });
    if (source) {
      const sku = row.querySelector('[name="variant_sku"]');
      if (sku) sku.value = '';
    }
    const existingImages = rows.querySelector('[name="variant_image_id"]');
    const newImageSelect = row.querySelector('[name="variant_image_id"]');
    if (existingImages && newImageSelect) newImageSelect.innerHTML = existingImages.innerHTML;
    rows.append(row);
    refreshLabels();
    row.querySelector('input:not([type="hidden"]):not([type="checkbox"]):not([type="file"]),select')?.focus();
    return row;
  };

  addButton.addEventListener('click', () => {
    appendRow();
  });

  rows.addEventListener('change', event => {
    const toggle = event.target.closest('[data-variant-active-toggle]');
    if (!toggle) return;
    const hidden = toggle.closest('[data-product-variant-row]')?.querySelector('[name="variant_active"]');
    if (hidden) hidden.value = toggle.checked ? '1' : '0';
  });

  rows.addEventListener('click', event => {
    const duplicate = event.target.closest('[data-duplicate-product-variant]');
    if (duplicate) {
      appendRow(duplicate.closest('[data-product-variant-row]'));
      return;
    }
    const remove = event.target.closest('[data-remove-product-variant]');
    if (!remove) return;
    const row = remove.closest('[data-product-variant-row]');
    if (rows.querySelectorAll('[data-product-variant-row]').length > 1) {
      row.remove();
    } else {
      row.querySelectorAll('input:not([type="hidden"]):not([type="checkbox"]),select').forEach(field => {
        if (field.type === 'file') field.value = '';
        else field.value = '';
      });
      const active = row.querySelector('[data-variant-active-toggle]');
      const hidden = row.querySelector('[name="variant_active"]');
      if (active) active.checked = true;
      if (hidden) hidden.value = '1';
    }
    refreshLabels();
  });
})();
