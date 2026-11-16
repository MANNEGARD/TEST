"use strict";

(function () {
  // Engångsnyckeln ligger efter # i länken, så den skickas aldrig i adressen till servern
  // eller hamnar i loggar. Ta bort den ur adressfältet direkt.
  const token = decodeURIComponent(location.hash.slice(1));
  history.replaceState(null, "", location.pathname);
  // Klistras länken in i en redan öppen flik ändras bara #-delen – läs in sidan på nytt då.
  window.addEventListener("hashchange", function () {
    location.reload();
  });

  const loading = document.getElementById("setup-loading");
  const form = document.getElementById("setup-form");
  const pw = document.getElementById("password");
  const pw2 = document.getElementById("password2");
  const code = document.getElementById("code");
  const error = document.getElementById("setup-error");
  const submit = document.getElementById("setup-submit");
  const card = form.closest(".card");

  function showError(message) {
    error.textContent = message;
    error.hidden = false;
    card.classList.remove("shake");
    void card.offsetWidth;
    card.classList.add("shake");
  }

  async function post(path, body) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      cache: "no-store",
      body: JSON.stringify(Object.assign({ token: token }, body)),
    });
    let data = {};
    try {
      data = await res.json();
    } catch (_) {
      /* tomt svar */
    }
    if (res.status === 429) data.error = "För många försök. Vänta en minut.";
    if (res.status === 404) data.error = "Inloggningen är redan installerad.";
    return { ok: res.ok, data: data };
  }

  async function start() {
    if (!token) {
      loading.textContent = "Länken saknar installationsnyckel. Använd hela länken du fick.";
      return;
    }
    const r = await post("/api/setup/start", {});
    if (!r.ok) {
      loading.textContent = r.data.error || "Installationen kunde inte startas.";
      return;
    }
    // SVG:n kommer från vår egen server och innehåller bara former (inga skript).
    const doc = new DOMParser().parseFromString(r.data.qrSvg, "image/svg+xml");
    const svg = doc.documentElement;
    if (svg && svg.nodeName.toLowerCase() === "svg" && !svg.querySelector("script")) {
      document.getElementById("qr").appendChild(document.importNode(svg, true));
    }
    document.getElementById("secret").textContent = r.data.secret;
    loading.hidden = true;
    form.hidden = false;
    pw.focus();
  }

  code.addEventListener("input", function () {
    const d = code.value.replace(/\D/g, "").slice(0, 6);
    code.value = d.length > 3 ? d.slice(0, 3) + " " + d.slice(3) : d;
  });

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    error.hidden = true;
    if (pw.value.length < 12) return showError("Lösenordet måste vara minst 12 tecken.");
    if (pw.value !== pw2.value) return showError("Lösenorden är inte likadana.");
    const digits = code.value.replace(/\D/g, "");
    if (digits.length !== 6) return showError("Skriv den sexsiffriga koden från appen.");

    submit.disabled = true;
    submit.setAttribute("aria-busy", "true");
    try {
      const r = await post("/api/setup/finish", { password: pw.value, code: digits });
      if (r.ok) {
        pw.value = pw2.value = "";
        location.replace("/");
        return;
      }
      showError(r.data.error || "Något gick fel. Försök igen.");
      code.value = "";
      code.focus();
    } catch (_) {
      showError("Kunde inte nå servern.");
    } finally {
      submit.disabled = false;
      submit.setAttribute("aria-busy", "false");
    }
  });

  start().catch(function () {
    loading.textContent = "Kunde inte nå servern.";
  });
})();
