"use strict";

(function () {
  // Utloggning
  const logout = document.getElementById("logout");
  logout.addEventListener("click", async function () {
    logout.disabled = true;
    try {
      await fetch("/api/logout", { method: "POST", credentials: "same-origin", cache: "no-store" });
    } finally {
      window.location.replace("/");
    }
  });

  // När sessionen går ut
  const info = document.getElementById("session-info");
  const expires = Number(info.dataset.expires) * 1000;
  if (expires) {
    const time = new Date(expires).toLocaleTimeString("sv-SE", { hour: "2-digit", minute: "2-digit" });
    info.textContent = "Inloggad till kl. " + time;
    const ms = expires - Date.now();
    if (ms > 0 && ms < 2 ** 31) setTimeout(() => window.location.replace("/"), ms + 1000);
  }

  // Status för varje subdomän
  const labels = { up: "Svarar", down: "Svarar inte", unknown: "Status okänd" };
  fetch("/api/status", { credentials: "same-origin", cache: "no-store" })
    .then((res) => {
      if (res.status === 401) window.location.replace("/");
      return res.ok ? res.json() : {};
    })
    .then((data) => {
      document.querySelectorAll(".site").forEach((el) => {
        const state = data[el.dataset.site] || "unknown";
        const dot = el.querySelector(".status");
        dot.dataset.status = state;
        dot.title = labels[state];
        dot.firstElementChild.textContent = labels[state];
      });
    })
    .catch(() => {});
})();
