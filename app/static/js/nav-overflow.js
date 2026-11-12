document.addEventListener('DOMContentLoaded', () => {
  const nav = document.getElementById('nav');
  const more = nav?.querySelector('[data-nav-more]');
  const menu = nav?.querySelector('[data-nav-overflow-menu]');
  const mobileSheet = more?.querySelector('[data-nav-mobile-sheet]');
  const mobileLinks = more?.querySelector('[data-nav-mobile-links]');
  const backdrop = more?.querySelector('[data-nav-menu-backdrop]');
  const closeButton = more?.querySelector('[data-nav-menu-close]');
  const siteHeader = nav?.closest('.site-header');
  if (siteHeader) {
    const syncHeaderHeight = () => {
      const height = Math.ceil(siteHeader.getBoundingClientRect().height);
      document.documentElement.style.setProperty('--site-header-height', `${height}px`);
      document.body.classList.add('has-fixed-site-header');
    };
    syncHeaderHeight();
    if ('ResizeObserver' in window) new ResizeObserver(syncHeaderHeight).observe(siteHeader);
    window.addEventListener('resize', syncHeaderHeight, { passive: true });
  }
  if (!nav || !more || !menu) return;

  const mobileNavigation = window.matchMedia('(max-width: 760px)');
  function refreshMobileMenu() {
    const showSheet = more.open && mobileNavigation.matches && mobileSheet && mobileLinks;
    if (mobileSheet) mobileSheet.hidden = !showSheet;
    if (backdrop) backdrop.hidden = !showSheet;
    if (menu) menu.hidden = Boolean(showSheet);
    if (!showSheet) return;

    mobileLinks.replaceChildren();
    for (const item of originalItems) {
      const source = item.matches('a') ? item : item.querySelector('.nav-profile-account-link');
      if (!source) continue;
      const link = source.cloneNode(true);
      link.removeAttribute('id');
      link.removeAttribute('data-nav-overflow-item');
      link.removeAttribute('data-nav-priority');
      link.classList.add('nav-mobile-sheet-link');
      link.querySelectorAll('[id]').forEach((element) => element.removeAttribute('id'));
      mobileLinks.append(link);
    }
  }

  more.addEventListener('toggle', () => {
    nav.classList.toggle('nav-more-open', more.open);
    refreshMobileMenu();
  });
  document.addEventListener('pointerdown', (event) => {
    if (more.open && !more.contains(event.target)) more.open = false;
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && more.open) {
      more.open = false;
      more.querySelector('summary')?.focus();
    }
  });
  backdrop?.addEventListener('click', () => { more.open = false; });
  closeButton?.addEventListener('click', () => {
    more.open = false;
    more.querySelector('summary')?.focus();
  });
  mobileLinks?.addEventListener('click', (event) => {
    if (event.target.closest('a')) more.open = false;
  });
  menu.addEventListener('click', (event) => {
    if (event.target.closest('a')) more.open = false;
  });

  const originalItems = Array.from(nav.children).filter((item) => item !== more);
  const candidates = originalItems.filter((item) => item.hasAttribute('data-nav-overflow-item'));
  const overflowOrder = [...candidates].reverse().sort((a, b) =>
    Number(a.dataset.navPriority || 50) - Number(b.dataset.navPriority || 50)
  );
  let scheduled = false;

  function scheduleLayout() {
    if (scheduled) return;
    scheduled = true;
    window.requestAnimationFrame(() => {
      scheduled = false;
      layoutNavigation();
    });
  }

  function layoutNavigation() {
    // Restore the full navigation in its original order, then move only the
    // trailing links that exceed the space available in the header.
    nav.classList.add('nav-overflow-ready');
    let nextItem = more;
    for (const item of [...originalItems].reverse()) {
      nav.insertBefore(item, nextItem);
      nextItem = item;
    }
    more.hidden = false;

    if (!nav.clientWidth) {
      more.hidden = menu.children.length === 0;
      return;
    }
    while (nav.scrollWidth > nav.clientWidth + 1) {
      const overflowItem = overflowOrder.find((item) =>
        item.parentElement === nav && !item.classList.contains('cart-link')
      );
      if (!overflowItem) break;
      const nextOverflow = originalItems.slice(originalItems.indexOf(overflowItem) + 1)
        .find((item) => item.parentElement === menu);
      menu.insertBefore(overflowItem, nextOverflow || null);
    }

    const hasOverflow = menu.children.length > 0;
    more.hidden = !hasOverflow;
    if (!hasOverflow) more.open = false;
  }

  if ('ResizeObserver' in window) {
    const observer = new ResizeObserver(scheduleLayout);
    observer.observe(nav);
    if (nav.closest('.site-header')) observer.observe(nav.closest('.site-header'));
  }
  window.addEventListener('resize', scheduleLayout, { passive: true });
  mobileNavigation.addEventListener?.('change', refreshMobileMenu);
  window.addEventListener('fity:cart-count-updated', scheduleLayout);
  scheduleLayout();
});
