const parameters = new URLSearchParams(location.search);
const invite = new URLSearchParams(location.hash.slice(1)).get("invite") || parameters.get("invite");
const form = document.querySelector("#auth-form");
const title = document.querySelector("#auth-title");
const description = document.querySelector("#auth-description");
const emailField = document.querySelector("#email-field");
const password = form.elements.namedItem("password");
const submit = document.querySelector("#auth-submit");
const feedback = document.querySelector("#auth-feedback");

if (invite) {
  title.textContent = "Создание аккаунта";
  description.textContent = "Задайте пароль для аккаунта из приглашения.";
  emailField.hidden = true;
  form.elements.namedItem("email").required = false;
  password.autocomplete = "new-password";
  password.minLength = 12;
  submit.textContent = "Создать аккаунт";
}

function destination() {
  const next = parameters.get("next");
  return next && next.startsWith("/") && !next.startsWith("//") ? next : "/";
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  submit.disabled = true;
  feedback.hidden = true;
  const payload = invite
    ? { token: invite, password: password.value }
    : { email: form.elements.namedItem("email").value, password: password.value };
  try {
    const response = await fetch(invite ? "/api/auth/register" : "/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(typeof body.detail === "string" ? body.detail : "Не удалось выполнить вход.");
    }
    location.assign(destination());
  } catch (error) {
    feedback.textContent = error.message;
    feedback.hidden = false;
    submit.disabled = false;
  }
});
