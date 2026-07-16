(function bootstrapTheme() {
  const root = document.documentElement;
  try {
    let style =
      localStorage.getItem("deepseek-infra.theme-style") ||
      localStorage.getItem("deepseek-mobile.theme-style") ||
      "chatgpt";
    let mode =
      localStorage.getItem("deepseek-infra.theme-mode") ||
      localStorage.getItem("deepseek-mobile.theme-mode");
    if (!mode) {
      const legacy =
        localStorage.getItem("deepseek-infra.theme") ||
        localStorage.getItem("deepseek-mobile.theme");
      mode = ["light", "dark", "system"].includes(legacy) ? legacy : "system";
    }
    if (!["chatgpt", "linear", "notion", "arc"].includes(style)) style = "chatgpt";
    if (!["system", "light", "dark"].includes(mode)) mode = "system";
    root.dataset.theme = style;
    root.dataset.mode = mode;
  } catch {
    root.dataset.theme = "chatgpt";
    root.dataset.mode = "system";
  }
})();
