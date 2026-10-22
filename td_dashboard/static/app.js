(() => {
  const tip = document.createElement("div");
  tip.className = "tip";
  tip.hidden = true;
  document.body.append(tip);
  const show = (el, x, y) => {
    const value = document.createElement("strong");
    value.textContent = el.dataset.tipValue;
    const label = document.createElement("span");
    label.textContent = el.dataset.tipLabel;
    tip.replaceChildren(value, label);
    tip.hidden = false;
    const box = tip.getBoundingClientRect();
    tip.style.left = Math.max(8, Math.min(innerWidth - box.width - 8, x - box.width / 2)) + "px";
    tip.style.top = Math.max(8, y - box.height - 12) + "px";
  };
  const hide = () => { tip.hidden = true; };
  for (const el of document.querySelectorAll("[data-tip-value]")) {
    el.addEventListener("pointermove", (event) => show(el, event.clientX, event.clientY));
    el.addEventListener("pointerleave", hide);
    el.addEventListener("focus", () => {
      const box = el.getBoundingClientRect();
      show(el, box.left + box.width / 2, box.top);
    });
    el.addEventListener("blur", hide);
  }
  const every = Number(document.body.dataset.refresh || 0) * 1000;
  if (every) {
    const loaded = Date.now();
    const check = () => {
      const typing = document.activeElement && document.activeElement.matches("input, select, textarea");
      if (!document.hidden && !typing && Date.now() - loaded >= every) location.reload();
    };
    setInterval(check, 5000);
    document.addEventListener("visibilitychange", check);
  }
})();
