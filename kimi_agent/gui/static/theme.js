// Applies the light/dark theme before the first paint (the page has no inline scripts).
(function () {
  var root = document.documentElement;
  var media = window.matchMedia("(prefers-color-scheme: dark)");
  function apply() {
    var pref = root.getAttribute("data-theme-pref") || "system";
    root.setAttribute("data-theme", pref === "light" || pref === "dark" ? pref : (media.matches ? "dark" : "light"));
  }
  apply();
  media.addEventListener("change", apply);
  window.applyTheme = function (pref) { root.setAttribute("data-theme-pref", pref); apply(); };
})();
