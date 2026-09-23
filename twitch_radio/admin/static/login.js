(function () {
  var form = document.getElementById("login-form");
  var pw = document.getElementById("password");
  var reveal = document.getElementById("reveal");
  var go = document.getElementById("go");
  var eq = document.getElementById("eq");
  if (!form || !pw || !go) return;

  function setLive(on) {
    if (eq) eq.classList.toggle("live", on);
  }

  if (reveal) {
    reveal.addEventListener("click", function () {
      var showing = pw.type === "text";
      pw.type = showing ? "password" : "text";
      reveal.textContent = showing ? "Show" : "Hide";
      reveal.setAttribute("aria-pressed", showing ? "false" : "true");
      pw.focus();
    });
  }

  pw.addEventListener("focus", function () { setLive(true); });
  pw.addEventListener("blur", function () { setLive(false); });

  form.addEventListener("submit", function () {
    if (pw.type === "text") pw.type = "password";
    setLive(true);
    go.textContent = "Signing in\u2026";
    setTimeout(function () { go.disabled = true; }, 0);
  });

  window.addEventListener("pageshow", function (ev) {
    if (!ev.persisted) return;
    go.disabled = false;
    go.textContent = "Sign in";
    setLive(false);
  });

  var remaining = parseInt(go.getAttribute("data-retry") || "0", 10);
  var counter = document.getElementById("retry-left");
  if (remaining > 0 && counter) {
    var fmt = function (s) {
      var m = Math.floor(s / 60), sec = s % 60;
      return m + ":" + (sec < 10 ? "0" : "") + sec;
    };
    counter.textContent = fmt(remaining);
    var timer = setInterval(function () {
      remaining -= 1;
      if (remaining <= 0) {
        clearInterval(timer);
        pw.disabled = false;
        go.disabled = false;
        var notice = document.getElementById("lockout");
        if (notice) notice.textContent = "You can try again now.";
        pw.focus();
        return;
      }
      counter.textContent = fmt(remaining);
    }, 1000);
  }
})();
