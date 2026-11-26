(() => {
  const editor = document.querySelector('[data-rich-editor]');
  if (!editor) return;
  const surface = editor.querySelector('[data-rich-editor-surface]');
  const stored = editor.querySelector('[data-rich-editor-value]');
  if (!surface || !stored) return;

  let savedRange = null;
  const rememberSelection = () => {
    const selection = window.getSelection();
    if (selection?.rangeCount && surface.contains(selection.anchorNode)) {
      savedRange = selection.getRangeAt(0).cloneRange();
    }
  };
  document.addEventListener('selectionchange', rememberSelection);

  const restoreSelection = () => {
    surface.focus();
    if (!savedRange) return;
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(savedRange);
  };

  editor.querySelectorAll('[data-rich-command]').forEach(button => {
    button.addEventListener('mousedown', event => event.preventDefault());
    button.addEventListener('click', () => {
      const command = button.dataset.richCommand;
      let value = button.dataset.richValue || null;
      if (command === 'createLink') {
        value = window.prompt('Enter a secure link URL');
        if (!value || !/^(https:\/\/|mailto:|\/(?!\/))/.test(value.trim())) return;
        value = value.trim();
      }
      restoreSelection();
      document.execCommand(command, false, value);
      rememberSelection();
    });
  });

  const insertImage = editor.querySelector('[data-rich-insert-image]');
  insertImage?.addEventListener('mousedown', event => event.preventDefault());
  insertImage?.addEventListener('click', () => {
    const picker = editor.querySelector('[data-rich-gallery-image]');
    const selected = picker?.selectedOptions?.[0];
    if (!selected?.value) return;
    const figure = document.createElement('figure');
    const image = document.createElement('img');
    image.src = selected.value;
    image.alt = selected.dataset.alt || '';
    figure.append(image);
    const caption = editor.querySelector('[data-rich-image-caption]')?.value.trim();
    if (caption) {
      const figcaption = document.createElement('figcaption');
      figcaption.textContent = caption;
      figure.append(figcaption);
    }
    restoreSelection();
    const selection = window.getSelection();
    if (!selection?.rangeCount) {
      surface.append(figure);
      return;
    }
    const range = selection.getRangeAt(0);
    range.deleteContents();
    range.insertNode(figure);
    range.setStartAfter(figure);
    range.collapse(true);
    selection.removeAllRanges();
    selection.addRange(range);
    savedRange = range.cloneRange();
    if (picker) picker.selectedIndex = 0;
    const captionInput = editor.querySelector('[data-rich-image-caption]');
    if (captionInput) captionInput.value = '';
  });

  editor.closest('form')?.addEventListener('submit', () => {
    stored.value = surface.innerHTML;
  });
})();
