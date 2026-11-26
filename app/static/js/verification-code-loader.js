/*
 * From Uiverse.io by dovatgabriel - https://uiverse.io/dovatgabriel/tidy-walrus-92
 * MIT License, Copyright (c) dovatgabriel (Gabriel)
 */
(() => {
  const forms = document.querySelectorAll('[data-code-loading-form]');

  forms.forEach(form => {
    form.addEventListener('submit', () => {
      const loader = form.querySelector('[data-code-loader]');
      const message = loader?.querySelector('[data-code-loading-message]');
      if (!loader || !message) return;

      message.textContent = form.dataset.loadingMessage || 'Please wait...';
      loader.hidden = false;
      form.setAttribute('aria-busy', 'true');

      const submitButton = form.querySelector('button[type="submit"], input[type="submit"]');
      if (submitButton) submitButton.disabled = true;
    });
  });

  window.addEventListener('pageshow', () => {
    forms.forEach(form => {
      form.removeAttribute('aria-busy');
      const loader = form.querySelector('[data-code-loader]');
      if (loader) loader.hidden = true;
      const submitButton = form.querySelector('button[type="submit"], input[type="submit"]');
      if (submitButton) submitButton.disabled = false;
    });
  });
})();
