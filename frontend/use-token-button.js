(() => {
  function addButton() {
    const wrap = document.querySelector('.token-input');
    if (!wrap || wrap.dataset.useTokenButton === '1') return;
    const input = wrap.querySelector('input');
    if (!input) return;
    wrap.dataset.useTokenButton = '1';

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = 'Use Token';
    btn.onclick = () => {
      if (!input.value.trim()) {
        alert('Paste your API token first.');
        return;
      }
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
      alert('Token is ready. Now use Pause/Resume or toggles.');
    };
    input.insertAdjacentElement('afterend', btn);
  }

  new MutationObserver(addButton).observe(document.documentElement, { childList: true, subtree: true });
  window.addEventListener('load', addButton);
  setInterval(addButton, 1000);
})();
