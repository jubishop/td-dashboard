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
  // A notice shows once; drop its token so reloads and shared links do not repeat it.
  const url = new URL(location.href);
  if (url.searchParams.has("notice")) {
    url.searchParams.delete("notice");
    history.replaceState(history.state, "", url.pathname + url.search + url.hash);
  }
  // Pin an open row menu beside its button, outside the table's scroll box, and keep one open at a time.
  const menus = [...document.querySelectorAll("details.menu")];
  const closeMenus = (except) => { for (const menu of menus) if (menu !== except) menu.open = false; };
  const place = (menu) => {
    const panel = menu.querySelector(".menu-panel");
    const button = menu.querySelector("summary").getBoundingClientRect();
    const size = panel.getBoundingClientRect();
    const below = button.bottom + 4 + size.height <= innerHeight - 8;
    panel.style.top = (below ? button.bottom + 4 : Math.max(8, button.top - 4 - size.height)) + "px";
    panel.style.left = Math.max(8, Math.min(innerWidth - size.width - 8, button.right - size.width)) + "px";
  };
  for (const menu of menus) {
    menu.addEventListener("toggle", () => {
      if (menu.open) {
        closeMenus(menu);
        place(menu);
      }
    });
  }
  document.addEventListener("click", (event) => { if (!event.target.closest("details.menu")) closeMenus(); });
  document.addEventListener("keydown", (event) => {
    const open = menus.find((menu) => menu.open);
    if (event.key === "Escape" && open) {
      closeMenus();
      open.querySelector("summary").focus();
    }
  });
  addEventListener("scroll", () => closeMenus(), { passive: true, capture: true });
  addEventListener("resize", () => closeMenus());
  // Disable a form's buttons once it is sent, after the browser has read which one was clicked.
  document.addEventListener("submit", (event) => {
    if (event.target.matches("form.act")) {
      setTimeout(() => { for (const button of event.target.querySelectorAll("button")) button.disabled = true; });
    }
  });
  const every = Number(document.body.dataset.refresh || 0) * 1000;
  if (every) {
    const loaded = Date.now();
    const check = () => {
      const typing = document.activeElement && document.activeElement.matches("input, select, textarea");
      const busy = typing || document.querySelector("details.menu[open], form.act button:disabled");
      if (!document.hidden && !busy && Date.now() - loaded >= every) location.reload();
    };
    setInterval(check, 5000);
    document.addEventListener("visibilitychange", check);
  }
})();
