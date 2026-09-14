// Apply the saved theme before first paint to avoid a flash of the default.
// Shared via localStorage('jiraffe-theme') across every page.
(function () {
  try {
    var t = localStorage.getItem('jiraffe-theme');
    if (t) document.documentElement.setAttribute('data-theme', t);
  } catch (e) {}
})();
