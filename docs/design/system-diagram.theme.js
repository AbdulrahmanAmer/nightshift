/* design-farm · recipe switcher for the Nightshift System Map.
   Optional: the page themes itself without this (light by default, dark from
   prefers-color-scheme or the host's data-theme). This adds Auto / Light / Dark / Soft.
   Recipes mirror design-farm/registry.json. */
(function () {
  var RECIPES = {
    light: { palette: "violet", depth: "lifted", shape: "rounded", type: "urbanist", density: "comfortable" },
    dark: { palette: "violet-night", depth: "lifted", shape: "rounded", type: "urbanist", density: "comfortable" },
    soft: { palette: "cobalt-signal", depth: "neumorphic", shape: "soft", type: "manrope", density: "comfortable" }
  };
  var AXES = ["palette", "depth", "shape", "type", "density"];
  var KEY = "ns-system-map-recipe";
  var root = document.documentElement;

  function apply(name) {
    var r = RECIPES[name];
    AXES.forEach(function (a) {
      if (r) root.setAttribute("data-" + a, r[a]);
      else root.removeAttribute("data-" + a); // auto: CSS follows system / host theme
    });
  }

  var saved = "auto";
  try { saved = localStorage.getItem(KEY) || "auto"; } catch (e) { /* storage blocked: stay on auto */ }
  if (!RECIPES[saved]) saved = "auto";
  apply(saved);

  var bar = document.createElement("div");
  bar.className = "ds-recipes";
  bar.setAttribute("role", "group");
  bar.setAttribute("aria-label", "Page theme");
  ["auto", "light", "dark", "soft"].forEach(function (name) {
    var b = document.createElement("button");
    b.type = "button";
    b.textContent = name.charAt(0).toUpperCase() + name.slice(1);
    b.setAttribute("aria-pressed", String(name === saved));
    b.addEventListener("click", function () {
      apply(name);
      try { localStorage.setItem(KEY, name); } catch (e) { /* ignore */ }
      Array.prototype.forEach.call(bar.children, function (c) { c.setAttribute("aria-pressed", String(c === b)); });
    });
    bar.appendChild(b);
  });
  document.body.appendChild(bar);
})();
