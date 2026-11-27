(() => {
  const defaults = {
    fit: "contain", zoom: 100, x: 50, y: 50,
    top: 0, right: 0, bottom: 0, left: 0, pan_x: 0, pan_y: 0,
  };

  document.querySelectorAll("[data-product-image-crop-editor]").forEach((editor) => {
    const profiles = Array.from(editor.querySelectorAll("[data-crop-profile]"));
    const sectionSelect = editor.querySelector("[data-crop-section-select]");
    const showSelectedProfile = () => {
      if (!sectionSelect) return;
      profiles.forEach((profile) => {
        profile.hidden = profile.dataset.cropProfile !== sectionSelect.value;
      });
    };
    if (sectionSelect) {
      sectionSelect.addEventListener("change", showSelectedProfile);
      showSelectedProfile();
    }

    profiles.forEach((profile) => {
      const fit = profile.querySelector("[data-crop-fit]");
      const preview = profile.querySelector("[data-crop-preview-image]");
      const settings = profile.querySelector("[data-crop-settings]");
      const previewArea = profile.querySelector("[data-crop-preview]");
      const values = {
        zoom: profile.querySelector('[data-crop-value="zoom"]'),
        x: profile.querySelector('[data-crop-value="x"]'),
        y: profile.querySelector('[data-crop-value="y"]'),
        top: profile.querySelector('[data-crop-value="top"]'),
        right: profile.querySelector('[data-crop-value="right"]'),
        bottom: profile.querySelector('[data-crop-value="bottom"]'),
        left: profile.querySelector('[data-crop-value="left"]'),
        pan_x: profile.querySelector('[data-crop-value="pan_x"]'),
        pan_y: profile.querySelector('[data-crop-value="pan_y"]'),
      };
      const frame = profile.querySelector("[data-crop-frame]");
      let drag = null;

      profile.querySelectorAll(".product-image-crop-preview-stage a").forEach((link) => {
        link.tabIndex = -1;
        link.addEventListener("click", (event) => {
          event.preventDefault();
          event.stopPropagation();
        });
      });
      profile.querySelectorAll(".product-image-crop-preview-stage button").forEach((button) => {
        button.type = "button";
        button.addEventListener("click", (event) => {
          event.preventDefault();
          event.stopPropagation();
        });
      });

      const update = () => {
        const crop = {
          fit: fit.value,
          zoom: Number(values.zoom.value),
          x: Number(values.x.value),
          y: Number(values.y.value),
          top: Number(values.top.value),
          right: Number(values.right.value),
          bottom: Number(values.bottom.value),
          left: Number(values.left.value),
          pan_x: Number(values.pan_x.value),
          pan_y: Number(values.pan_y.value),
        };
        preview.style.objectFit = crop.fit;
        preview.style.objectPosition = `${crop.x}% ${crop.y}%`;
        preview.style.translate = `${crop.pan_x}% ${crop.pan_y}%`;
        preview.style.scale = String(crop.zoom / 100);
        preview.style.transformOrigin = `${crop.x}% ${crop.y}%`;
        preview.style.clipPath = `inset(${crop.top}% ${crop.right}% ${crop.bottom}% ${crop.left}%)`;
        frame.style.inset = `${crop.top}% ${crop.right}% ${crop.bottom}% ${crop.left}%`;
        settings.value = JSON.stringify(crop);
        Object.entries(crop).forEach(([key, value]) => {
          const output = profile.querySelector(`[data-crop-readout="${key}"]`);
          if (output) output.value = `${value}${key === "fit" ? "" : "%"}`;
        });
      };

      profile.addEventListener("input", update);
      profile.addEventListener("change", update);
      previewArea.addEventListener("pointerdown", (event) => {
        if (event.button !== 0 || event.target.closest("button, [data-crop-edge], .home-card-quick-add")) return;
        event.preventDefault();
        const bounds = previewArea.getBoundingClientRect();
        if (!bounds.width || !bounds.height) return;
        drag = {
          pointerId: event.pointerId,
          startX: event.clientX,
          startY: event.clientY,
          panX: Number(values.pan_x.value),
          panY: Number(values.pan_y.value),
          width: bounds.width,
          height: bounds.height,
        };
        previewArea.setPointerCapture(event.pointerId);
        previewArea.classList.add("is-crop-dragging");
      });
      previewArea.addEventListener("pointermove", (event) => {
        if (!drag || drag.pointerId !== event.pointerId) return;
        values.pan_x.value = String(Math.max(-200, Math.min(200,
          Math.round(drag.panX + (event.clientX - drag.startX) / drag.width * 100),
        )));
        values.pan_y.value = String(Math.max(-200, Math.min(200,
          Math.round(drag.panY + (event.clientY - drag.startY) / drag.height * 100),
        )));
        update();
      });
      const finishDrag = (event) => {
        if (!drag || drag.pointerId !== event.pointerId) return;
        drag = null;
        previewArea.classList.remove("is-crop-dragging");
        if (previewArea.hasPointerCapture(event.pointerId)) {
          previewArea.releasePointerCapture(event.pointerId);
        }
      };
      previewArea.addEventListener("pointerup", finishDrag);
      previewArea.addEventListener("pointercancel", finishDrag);
      profile.querySelectorAll("[data-crop-edge]").forEach((handle) => {
        const edge = handle.dataset.cropEdge;
        handle.addEventListener("pointerdown", (event) => {
          event.preventDefault();
          handle.setPointerCapture(event.pointerId);
        });
        handle.addEventListener("pointermove", (event) => {
          if (!handle.hasPointerCapture(event.pointerId)) return;
          const bounds = profile.querySelector("[data-crop-preview]").getBoundingClientRect();
          const coordinate = edge === "left" || edge === "right"
            ? (event.clientX - bounds.left) / bounds.width
            : (event.clientY - bounds.top) / bounds.height;
          const next = Math.max(0, Math.min(45, Math.round(
            (edge === "right" || edge === "bottom" ? 1 - coordinate : coordinate) * 100,
          )));
          const opposite = values[
            { top: "bottom", right: "left", bottom: "top", left: "right" }[edge]
          ];
          values[edge].value = String(Math.min(next, 90 - Number(opposite.value)));
          update();
        });
      });
      profile.querySelector("[data-reset-crop]").addEventListener("click", () => {
        fit.value = defaults.fit;
        Object.entries(values).forEach(([key, input]) => {
          input.value = defaults[key];
        });
        update();
      });
      profile.querySelector("[data-center-image]").addEventListener("click", () => {
        values.pan_x.value = String(defaults.pan_x);
        values.pan_y.value = String(defaults.pan_y);
        update();
      });
      profile.querySelector("[data-fill-image-area]").addEventListener("click", () => {
        fit.value = "cover";
        Object.entries(values).forEach(([key, input]) => {
          input.value = defaults[key];
        });
        update();
      });
      update();
    });
  });
})();
