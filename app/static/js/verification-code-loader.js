/* Verification-code input, automatic submission, and loading feedback. */
(() => {
  const forms = Array.from(document.querySelectorAll('[data-code-loading-form]'));

  forms.forEach(form => {
    if (form.matches('[data-otp-form]')) {
      const inputs = Array.from(form.querySelectorAll('[data-otp-digit]'));
      const combinedCode = form.querySelector('[data-otp-code]');

      if (inputs.length === 6 && combinedCode) {
        const syncCode = () => {
          combinedCode.value = inputs.map(input => input.value).join('');
        };

        const submitWhenComplete = () => {
          syncCode();
          if (inputs.every(input => /^[0-9]$/.test(input.value)) && form.dataset.codeSubmitting !== 'true') {
            form.requestSubmit();
          }
        };

        inputs.forEach((input, index) => {
          input.addEventListener('input', () => {
            const entered = input.value.replace(/[^0-9]/g, '');
            if (entered.length > 1) {
              const digits = entered.slice(0, inputs.length);
              inputs.forEach((digitInput, digitIndex) => {
                digitInput.value = digits[digitIndex] || '';
              });
              inputs[Math.min(digits.length, inputs.length - 1)].focus();
            } else {
              input.value = entered.slice(-1);
              if (input.value && inputs[index + 1]) inputs[index + 1].focus();
            }
            submitWhenComplete();
          });

          input.addEventListener('paste', event => {
            const pastedText = event.clipboardData ? event.clipboardData.getData('text') : '';
            const digits = pastedText.replace(/[^0-9]/g, '').slice(0, inputs.length);
            if (!digits) return;

            event.preventDefault();
            inputs.forEach((digitInput, digitIndex) => {
              digitInput.value = digits[digitIndex] || '';
            });
            inputs[Math.min(digits.length, inputs.length - 1)].focus();
            submitWhenComplete();
          });

          input.addEventListener('keydown', event => {
            if (event.key === 'ArrowLeft' && inputs[index - 1]) {
              event.preventDefault();
              inputs[index - 1].focus();
            } else if (event.key === 'ArrowRight' && inputs[index + 1]) {
              event.preventDefault();
              inputs[index + 1].focus();
            } else if (event.key === 'Backspace' && !input.value && inputs[index - 1]) {
              inputs[index - 1].focus();
            }
          });
        });
      }
    }

    form.addEventListener('submit', event => {
      if (form.dataset.codeSubmitting === 'true') {
        event.preventDefault();
        return;
      }
      form.dataset.codeSubmitting = 'true';

      const loader = form.querySelector('[data-code-loader]');
      const message = loader && loader.querySelector('[data-code-loading-message]');
      if (!loader || !message) return;

      message.textContent = form.dataset.loadingMessage || 'Please wait...';
      loader.hidden = false;
      form.setAttribute('aria-busy', 'true');

      const submitButton = form.querySelector('button[type="submit"], input[type="submit"]');
      if (submitButton) {
        submitButton.disabled = true;
        submitButton.hidden = true;
      }
    });
  });

  window.addEventListener('pageshow', () => {
    forms.forEach(form => {
      form.dataset.codeSubmitting = 'false';
      form.removeAttribute('aria-busy');
      const loader = form.querySelector('[data-code-loader]');
      if (loader) loader.hidden = true;
      const submitButton = form.querySelector('button[type="submit"], input[type="submit"]');
      if (submitButton) {
        submitButton.disabled = false;
        submitButton.hidden = false;
      }
    });
  });
})();
