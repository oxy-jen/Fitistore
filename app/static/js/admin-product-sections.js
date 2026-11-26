(() => {
  const rows = document.getElementById('product-content-section-rows');
  const template = document.getElementById('product-content-section-template');
  const addButton = document.getElementById('add-product-content-section');
  if (!rows || !template || !addButton) return;

  const refreshSections = () => {
    const items = [...rows.querySelectorAll('[data-product-content-row]')];
    items.forEach((item, index) => {
      const heading = item.querySelector('.product-content-section-heading strong');
      const moveUp = item.querySelector('[data-move-product-content-up]');
      const moveDown = item.querySelector('[data-move-product-content-down]');
      if (heading) heading.textContent = `Story section ${index + 1}`;
      if (moveUp) moveUp.disabled = index === 0;
      if (moveDown) moveDown.disabled = index === items.length - 1;
    });
  };

  addButton.addEventListener('click', () => {
    const item = template.content.firstElementChild.cloneNode(true);
    rows.append(item);
    refreshSections();
    item.querySelector('[name="content_section_title"]')?.focus();
  });

  rows.addEventListener('click', event => {
    const item = event.target.closest('[data-product-content-row]');
    if (!item) return;

    if (event.target.closest('[data-move-product-content-up]')) {
      const previous = item.previousElementSibling;
      if (previous) rows.insertBefore(item, previous);
      refreshSections();
      return;
    }

    if (event.target.closest('[data-move-product-content-down]')) {
      const next = item.nextElementSibling;
      if (next) rows.insertBefore(next, item);
      refreshSections();
      return;
    }

    if (!event.target.closest('[data-remove-product-content]')) return;
    if (rows.querySelectorAll('[data-product-content-row]').length > 1) {
      item.remove();
    } else {
      item.querySelectorAll('input,textarea').forEach(field => { field.value = ''; });
      item.querySelectorAll('select').forEach(select => { select.selectedIndex = 0; });
    }
    refreshSections();
  });

  refreshSections();
})();
