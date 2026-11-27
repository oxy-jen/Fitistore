(() => {
  const defaults = {
    fit: "contain", zoom: 100, x: 50, y: 50,
    top: 0, right: 0, bottom: 0, left: 0,
  };

  document.querySelectorAll("[data-product-image-crop-editor]").forEach((editor) => {
    editor.querySelectorAll("[data-crop-profile]").forEach((profile) => {
      const fit = profile.querySelector("[data-crop-fit]");
      const preview = profile.querySelector("[data-crop-preview-image]");
      const settings = profile.querySelector("[data-crop-settings]");
      const values = {
        zoom: profile.querySelector('[data-crop-value="zoom"]'),
        x: profile.querySelector('[data-crop-value="x"]'),
        y: profile.querySelector('[data-crop-value="y"]'),
        top: profile.querySelector('[data-crop-value="top"]'),
        right: profile.querySelector('[data-crop-value="right"]'),
        bottom: profile.querySelector('[data-crop-value="bottom"]'),
        left: profile.querySelector('[data-crop-value="left"]'),
      };
      const frame = profile.querySelector("[data-crop-frame]");

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
        };
        preview.style.objectFit = crop.fit;
        preview.style.objectPosition = `${crop.x}% ${crop.y}%`;
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
      update();
    });
  });
})();
