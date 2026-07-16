export function initWorkspaceTabs(options = {}) {
  const root = options.root || document.querySelector("[data-workspace-tabs]");
  if (!root) return { activate() {} };
  const tabs = Array.from(root.querySelectorAll('[role="tab"]'));
  const panels = tabs
    .map((tab) => document.getElementById(tab.getAttribute("aria-controls") || ""))
    .filter(Boolean);

  function activate(target, { focus = false } = {}) {
    const index = typeof target === "number" ? target : tabs.indexOf(target);
    if (index < 0 || index >= tabs.length) return;
    tabs.forEach((tab, tabIndex) => {
      const selected = tabIndex === index;
      tab.classList.toggle("active", selected);
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
      const panel = document.getElementById(tab.getAttribute("aria-controls") || "");
      if (panel) panel.hidden = !selected;
    });
    if (focus) tabs[index].focus();
  }

  root.addEventListener("click", (event) => {
    const tab = event.target.closest('[role="tab"]');
    if (tab && root.contains(tab)) activate(tab);
  });
  root.addEventListener("keydown", (event) => {
    const tab = event.target.closest('[role="tab"]');
    const index = tabs.indexOf(tab);
    if (index < 0) return;
    let next = index;
    if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
    else if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = tabs.length - 1;
    else return;
    event.preventDefault();
    activate(next, { focus: true });
  });

  document.querySelectorAll("[data-workspace-action]").forEach((button) => {
    button.addEventListener("click", () => options.onAction?.(button.dataset.workspaceAction || ""));
  });
  activate(Math.max(0, tabs.findIndex((tab) => tab.getAttribute("aria-selected") === "true")));
  return { activate };
}
