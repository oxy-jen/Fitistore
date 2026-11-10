(() => {
  const installButton = document.querySelector("[data-pwa-install]");
  const dialogInstallButton = document.querySelector("[data-pwa-dialog-install]");
  const helpButton = document.querySelector("[data-pwa-help]");
  const dialog = document.querySelector("[data-pwa-dialog]");
  const instruction = document.querySelector("[data-pwa-instructions]");
  if (!installButton || !helpButton || !dialog || !instruction) return;

  const installed = () => window.matchMedia("(display-mode: standalone)").matches ||
    window.navigator.standalone === true;
  const dismissKey = "fity-pwa-install-dismissed-at";
  const dismissedRecently = () => {
    try {
      const at = Number(window.localStorage.getItem(dismissKey));
      return at > 0 && Date.now() - at < 14 * 24 * 60 * 60 * 1000;
    } catch (_) { return false; }
  };

  if ("serviceWorker" in navigator && window.isSecureContext) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/service-worker.js", { scope: "/" }).catch(() => {});
    }, { once: true });
  }

  let installPrompt = null;
  const showPromptButton = () => {
    const canShow = !installed() && !!installPrompt && !dismissedRecently();
    installButton.hidden = !canShow;
    if (dialogInstallButton) dialogInstallButton.hidden = !canShow;
  };

  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    installPrompt = event;
    showPromptButton();
  });

  async function promptInstall() {
    if (!installPrompt) return;
    const prompt = installPrompt;
    installPrompt = null;
    showPromptButton();
    try {
      await prompt.prompt();
      const result = await prompt.userChoice;
      if (result?.outcome === "dismissed") {
        try { window.localStorage.setItem(dismissKey, String(Date.now())); } catch (_) { /* storage may be blocked */ }
      }
      if (result?.outcome === "accepted" && dialog.open) dialog.close();
    } catch (_) { /* Browser controls whether installation is available. */ }
  }

  installButton.addEventListener("click", promptInstall);
  dialogInstallButton?.addEventListener("click", promptInstall);
  window.addEventListener("appinstalled", () => {
    installPrompt = null;
    installButton.hidden = true;
    helpButton.hidden = true;
    if (dialogInstallButton) dialogInstallButton.hidden = true;
    if (dialog.open) dialog.close();
  });

  helpButton.addEventListener("click", () => {
    const ua = navigator.userAgent;
    const isIOS = /iPad|iPhone|iPod/.test(ua) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    const isAndroid = /Android/i.test(ua);
    if (installed()) {
      instruction.textContent = "FityStores is already installed on this device.";
    } else if (installPrompt) {
      instruction.textContent = "Choose Install FityStores below to open your browser's installation prompt.";
    } else if (isIOS) {
      instruction.textContent = "In Safari, tap Share, then choose Add to Home Screen. Other iPhone browsers may not offer this option.";
    } else if (isAndroid) {
      instruction.textContent = "In Chrome, open the browser menu and choose Install app or Add to Home screen. If it is not listed, update Chrome and try again later.";
    } else {
      instruction.textContent = "In a supported browser such as Chrome or Edge, open the browser menu and choose Install FityStores or Install this site as an app.";
    }
    showPromptButton();
    if (typeof dialog.showModal === "function") dialog.showModal();
  });

  if (installed()) helpButton.hidden = true;
  showPromptButton();
})();
