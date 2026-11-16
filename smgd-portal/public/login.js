"use strict";

(function () {
  const form = document.getElementById("login-form");
  const password = document.getElementById("password");
  const code = document.getElementById("code");
  const error = document.getElementById("login-error");
  const submit = document.getElementById("login-submit");
  const card = form.closest(".card");

  function showError(message) {
    error.textContent = message;
    error.hidden = false;
    card.classList.remove("shake");
    void card.offsetWidth; // starta om animationen
    card.classList.add("shake");
  }

  function setBusy(busy) {
    submit.disabled = busy;
    submit.setAttribute("aria-busy", busy ? "true" : "false");
  }

  // Bara siffror, med ett mellanrum i mitten för läsbarheten (123 456).
  code.addEventListener("input", function () {
    const digits = code.value.replace(/\D/g, "").slice(0, 6);
    code.value = digits.length > 3 ? digits.slice(0, 3) + " " + digits.slice(3) : digits;
    code.maxLength = 7;
    if (digits.length === 6 && password.value) form.requestSubmit();
  });

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    error.hidden = true;

    const digits = code.value.replace(/\D/g, "");
    if (!password.value) {
      showError("Ange ditt lösenord.");
      password.focus();
      return;
    }
    if (digits.length !== 6) {
      showError("Engångskoden består av sex siffror.");
      code.focus();
      return;
    }

    setBusy(true);
    try {
      const res = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        cache: "no-store",
        body: JSON.stringify({ password: password.value, code: digits }),
      });

      if (res.ok) {
        password.value = "";
        window.location.replace("/");
        return;
      }

      let message =
        res.status === 429 ? "För många försök. Vänta en stund och försök igen." : "Något gick fel. Försök igen.";
      try {
        const data = await res.json();
        if (data && typeof data.error === "string") message = data.error;
      } catch (_) {
        /* behåll standardtexten */
      }
      showError(message);
      code.value = "";
      code.focus();
    } catch (_) {
      showError("Kunde inte nå servern. Kontrollera anslutningen.");
    } finally {
      setBusy(false);
    }
  });
})();
