(() => {
  const variantSelect = document.getElementById('variant-choice');
  const description = document.getElementById('product-variant-description');
  if (!variantSelect || !description) return;

  const updateVariantDescription = () => {
    const option = variantSelect.selectedOptions[0];
    const html = option?.dataset.description || '';
    description.innerHTML = html;
    description.hidden = !html;
  };

  variantSelect.addEventListener('change', updateVariantDescription);
  updateVariantDescription();
})();
