document.addEventListener('DOMContentLoaded', () => {
  const nav = document.getElementById('nav');
  const more = nav?.querySelector('[data-nav-more]');
  const menu = nav?.querySelector('[data-nav-overflow-menu]');
  if (!nav || !more || !menu) return;

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
  window.addEventListener('fity:cart-count-updated', scheduleLayout);
  scheduleLayout();
});
