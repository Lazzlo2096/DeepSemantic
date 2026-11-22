import { authFetch } from "./auth.js";

const form = document.querySelector("#space-create-form");
const submit = document.querySelector("#space-create-submit");
const feedback = document.querySelector("#space-create-feedback");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  submit.disabled = true;
  feedback.hidden = true;
  const data = new FormData(form);
  try {
    const response = await authFetch("/api/spaces", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: data.get("name"),
        description: data.get("description") || null,
        visibility: data.get("visibility"),
      }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(typeof body.detail === "string" ? body.detail : "Не удалось создать Space.");
    }
    const space = await response.json();
    location.assign(`/space.html?id=${encodeURIComponent(space.id)}`);
  } catch (error) {
    feedback.textContent = error.message;
    feedback.hidden = false;
    submit.disabled = false;
  }
});
