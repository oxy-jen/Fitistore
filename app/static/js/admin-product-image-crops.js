(() => {
  const defaults = { fit: "contain", zoom: 100, x: 50, y: 50 };

  document.querySelectorAll("[data-product-image-crop-editor]").forEach((editor) => {
    editor.querySelectorAll("[data-crop-profile]").forEach((profile) => {
      const fit = profile.querySelector("[data-crop-fit]");
      const preview = profile.querySelector("[data-crop-preview-image]");
      const settings = profile.querySelector("[data-crop-settings]");
      const values = {
        zoom: profile.querySelector('[data-crop-value="zoom"]'),
        x: profile.querySelector('[data-crop-value="x"]'),
        y: profile.querySelector('[data-crop-value="y"]'),
      };

      const update = () => {
        const crop = {
          fit: fit.value,
          zoom: Number(values.zoom.value),
          x: Number(values.x.value),
          y: Number(values.y.value),
        };
        preview.style.objectFit = crop.fit;
        preview.style.objectPosition = `${crop.x}% ${crop.y}%`;
        preview.style.scale = String(crop.zoom / 100);
        preview.style.transformOrigin = `${crop.x}% ${crop.y}%`;
        settings.value = JSON.stringify(crop);
        Object.entries(crop).forEach(([key, value]) => {
          const output = profile.querySelector(`[data-crop-readout="${key}"]`);
          if (output) output.value = `${value}${key === "fit" ? "" : "%"}`;
        });
      };

      profile.addEventListener("input", update);
      profile.addEventListener("change", update);
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
