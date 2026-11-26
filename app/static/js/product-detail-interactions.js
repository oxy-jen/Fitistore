(() => {
  const page = document.querySelector('.product-page');
  if (!page) return;

  const detailTabs = page.querySelector('[data-product-detail-tabs]');
  if (detailTabs) {
    const tabs = [...detailTabs.querySelectorAll('[role="tab"][data-product-tab]')];
    const panels = [...page.querySelectorAll('[data-product-panel]')];

    const activateTab = (tab, updateHistory = false) => {
      tabs.forEach(candidate => {
        const selected = candidate === tab;
        candidate.setAttribute('aria-selected', String(selected));
        candidate.tabIndex = selected ? 0 : -1;
      });
      panels.forEach(panel => {
        panel.hidden = panel.dataset.productPanel !== tab.dataset.productTab;
      });

      const panel = page.querySelector(`#${tab.getAttribute('aria-controls')}`);
      if (updateHistory && panel && window.location.hash !== `#${panel.id}`) {
        window.history.pushState({ productTab: tab.dataset.productTab }, '', `#${panel.id}`);
      }
    };

    const tabForCurrentLocation = () => {
      const target = document.getElementById(window.location.hash.slice(1));
      const panel = target && target.closest('[data-product-panel]');
      return tabs.find(tab => panel && tab.dataset.productTab === panel.dataset.productPanel) || tabs[0];
    };

    tabs.forEach(tab => {
      tab.addEventListener('click', () => activateTab(tab, true));
    });

    detailTabs.addEventListener('keydown', event => {
      const currentIndex = tabs.indexOf(document.activeElement);
      if (currentIndex < 0) return;

      let nextIndex;
      if (event.key === 'ArrowRight') nextIndex = (currentIndex + 1) % tabs.length;
      else if (event.key === 'ArrowLeft') nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
      else if (event.key === 'Home') nextIndex = 0;
      else if (event.key === 'End') nextIndex = tabs.length - 1;
      else return;

      event.preventDefault();
      tabs[nextIndex].focus();
      activateTab(tabs[nextIndex], true);
    });

    page.addEventListener('click', event => {
      const link = event.target.closest('a[href^="#"]');
      if (!link || !link.hash) return;

      const target = document.getElementById(link.hash.slice(1));
      const panel = target && target.closest('[data-product-panel]');
      if (!panel) return;

      const tab = tabs.find(candidate => candidate.dataset.productTab === panel.dataset.productPanel);
      if (!tab) return;

      event.preventDefault();
      activateTab(tab, true);
      window.requestAnimationFrame(() => target.scrollIntoView({ behavior: 'smooth', block: 'start' }));
    });

    const syncTabWithLocation = () => activateTab(tabForCurrentLocation());
    window.addEventListener('hashchange', syncTabWithLocation);
    window.addEventListener('popstate', syncTabWithLocation);
    syncTabWithLocation();
    if (window.location.hash) {
      window.requestAnimationFrame(() => {
        const target = document.getElementById(window.location.hash.slice(1));
        if (target) target.scrollIntoView({ block: 'start' });
      });
    }
  }

  const stickyBar = page.querySelector('[data-product-sticky-buy]');
  const primaryAction = page.querySelector('.product-detail-actions .btn.primary');
  const currentPrice = page.querySelector('#detail-current-price');
  const availability = page.querySelector('.product-availability');
  const quickPrices = page.querySelectorAll('[data-product-quick-price], [data-product-sticky-price]');
  const quickStocks = page.querySelectorAll('[data-product-quick-stock], [data-product-sticky-stock]');
  const quickBuyButtons = page.querySelectorAll('[data-product-quick-buy]');

  const syncQuickBuy = () => {
    if (currentPrice) {
      quickPrices.forEach(price => { price.textContent = currentPrice.textContent.trim(); });
    }
    if (availability) {
      quickStocks.forEach(stock => {
        stock.textContent = availability.textContent.trim();
        stock.className = availability.classList.contains('bad')
          ? 'product-quick-stock bad'
          : availability.classList.contains('warn')
            ? 'product-quick-stock warn'
            : 'product-quick-stock';
      });
    }
    quickBuyButtons.forEach(button => {
      button.disabled = !primaryAction || primaryAction.disabled;
    });
  };

  quickBuyButtons.forEach(button => {
    button.addEventListener('click', () => {
      if (primaryAction && !primaryAction.disabled) primaryAction.click();
    });
  });
  syncQuickBuy();

  if ('MutationObserver' in window) {
    const purchaseObserver = new MutationObserver(syncQuickBuy);
    [currentPrice, availability, primaryAction].filter(Boolean).forEach(element => {
      purchaseObserver.observe(element, {
        attributes: true,
        childList: true,
        characterData: true,
        subtree: true,
      });
    });
  }

  if (stickyBar && primaryAction && 'IntersectionObserver' in window) {
    const actionObserver = new IntersectionObserver(entries => {
      stickyBar.hidden = entries[0].isIntersecting;
    }, { threshold: 0.01 });
    actionObserver.observe(primaryAction);
  }
})();
