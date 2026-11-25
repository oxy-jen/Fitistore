(() => {
  const gallery = document.querySelector('.product-gallery');
  const stage = gallery?.querySelector('[data-product-gallery]');
  const image = stage?.querySelector('#detail-product-image');
  if (!gallery || !stage || !image) return;

  const thumbs = () => [...gallery.querySelectorAll('[data-product-thumb]')];
  const counter = gallery.querySelector('[data-product-image-count]');
  const dialog = gallery.querySelector('[data-product-image-dialog]');
  const lightboxImage = dialog?.querySelector('[data-product-lightbox-image]');
  let currentIndex = thumbs().findIndex(thumb => thumb.dataset.productThumb === image.getAttribute('src'));

  const setImage = (source, alt, filename = '', scroll = false) => {
    if (!source) return;
    const items = thumbs();
    let index = items.findIndex(thumb => thumb.dataset.productThumb === source);
    if (index < 0 && filename) {
      index = items.findIndex(thumb => thumb.dataset.productThumbName === filename);
    }

    if (index >= 0) {
      const selected = items[index];
      source = selected.dataset.productThumb;
      alt = selected.dataset.productThumbAlt || alt;
      currentIndex = index;
    } else {
      currentIndex = -1;
    }

    image.src = source;
    image.alt = alt || image.dataset.productDefaultAlt || 'Product photo';
    items.forEach((thumb, thumbIndex) => {
      const active = thumbIndex === currentIndex;
      thumb.classList.toggle('active', active);
      if (active) thumb.setAttribute('aria-current', 'true');
      else thumb.removeAttribute('aria-current');
    });

    if (counter) {
      counter.textContent = currentIndex >= 0
        ? `Photo ${currentIndex + 1} of ${items.length}`
        : 'Selected option photo';
    }
    if (scroll && currentIndex >= 0) {
      items[currentIndex].scrollIntoView({
        block: 'nearest',
        inline: 'nearest',
        behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
      });
    }
    if (dialog?.open && lightboxImage) {
      lightboxImage.src = image.src;
      lightboxImage.alt = image.alt;
    }
  };

  const stepImage = direction => {
    const items = thumbs();
    if (items.length < 2) return;
    const baseIndex = currentIndex >= 0 ? currentIndex : (direction > 0 ? -1 : 0);
    const nextIndex = (baseIndex + direction + items.length) % items.length;
    setImage(items[nextIndex].dataset.productThumb, items[nextIndex].dataset.productThumbAlt, '', true);
  };

  const syncSelectedVariant = () => {
    const option = document.querySelector('#variant-choice')?.selectedOptions[0];
    if (option?.value && option.dataset.image) {
      setImage(option.dataset.image, option.dataset.imageAlt, option.dataset.imageName || '');
    }
  };

  gallery.addEventListener('click', event => {
    const step = event.target.closest('[data-product-gallery-step]');
    if (step) {
      stepImage(Number(step.dataset.productGalleryStep));
      return;
    }

    const thumb = event.target.closest('[data-product-thumb]');
    if (thumb) {
      setImage(thumb.dataset.productThumb, thumb.dataset.productThumbAlt, thumb.dataset.productThumbName || '');
      return;
    }

    if (event.target.closest('[data-product-image-zoom]') && dialog) {
      if (lightboxImage) {
        lightboxImage.src = image.src;
        lightboxImage.alt = image.alt;
      }
      if (typeof dialog.showModal === 'function') dialog.showModal();
      else dialog.setAttribute('open', '');
    }

    if (event.target.closest('[data-product-image-close]') && dialog?.open) {
      dialog.close();
    }
  });

  gallery.addEventListener('keydown', event => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    stepImage(event.key === 'ArrowRight' ? 1 : -1);
  });

  let touchStart = null;
  stage.addEventListener('touchstart', event => {
    touchStart = event.changedTouches.length === 1
      ? { x: event.changedTouches[0].clientX, y: event.changedTouches[0].clientY }
      : null;
  }, { passive: true });
  stage.addEventListener('touchend', event => {
    if (!touchStart || event.changedTouches.length !== 1) return;
    const deltaX = event.changedTouches[0].clientX - touchStart.x;
    const deltaY = event.changedTouches[0].clientY - touchStart.y;
    touchStart = null;
    if (Math.abs(deltaX) > 48 && Math.abs(deltaX) > Math.abs(deltaY) * 1.3) {
      stepImage(deltaX < 0 ? 1 : -1);
    }
  }, { passive: true });

  dialog?.addEventListener('click', event => {
    if (event.target === dialog) dialog.close();
  });

  document.addEventListener('change', event => {
    if (event.target?.id === 'variant-choice') syncSelectedVariant();
  });
  document.addEventListener('click', event => {
    if (event.target.closest('[data-variant-card], [data-variant-dimension]')) {
      window.requestAnimationFrame(syncSelectedVariant);
    }
  });
  document.addEventListener('DOMContentLoaded', syncSelectedVariant, { once: true });
})();
