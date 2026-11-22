import { authFetch } from "./auth.js?v=2";

const inviteForm = document.querySelector("#invite-form");
const inviteResult = document.querySelector("#invite-result");
const inviteUrl = document.querySelector("#invite-url");
const copyInvite = document.querySelector("#copy-invite");
const feedback = document.querySelector("#users-feedback");
const usersList = document.querySelector("#users-list");
const refreshUsers = document.querySelector("#refresh-users");

async function errorMessage(response) {
  const body = await response.json().catch(() => ({}));
  return typeof body.detail === "string" ? body.detail : `Ошибка HTTP ${response.status}`;
}

function showFeedback(message, error = true) {
  feedback.textContent = message;
  feedback.dataset.error = String(error);
  feedback.hidden = false;
}

async function updateUser(id, update) {
  const response = await authFetch(`/api/auth/users/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(update),
  });
  if (!response.ok) throw new Error(await errorMessage(response));
  await loadUsers();
}

function userRow(user) {
  const row = document.createElement("tr");
  const identity = document.createElement("td");
  identity.textContent = user.email;
  const roleCell = document.createElement("td");
  const role = document.createElement("select");
  role.append(new Option("Пользователь", "user"), new Option("Администратор", "admin"));
  role.value = user.role;
  role.addEventListener("change", async () => {
    role.disabled = true;
    try { await updateUser(user.id, { role: role.value }); }
    catch (error) { showFeedback(error.message); role.disabled = false; role.value = user.role; }
  });
  roleCell.append(role);
  const state = document.createElement("td");
  state.textContent = user.disabled_at ? "Отключён" : "Активен";
  const actionCell = document.createElement("td");
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.textContent = user.disabled_at ? "Включить" : "Отключить";
  toggle.addEventListener("click", async () => {
    toggle.disabled = true;
    try { await updateUser(user.id, { disabled: !user.disabled_at }); }
    catch (error) { showFeedback(error.message); toggle.disabled = false; }
  });
  actionCell.append(toggle);
  row.append(identity, roleCell, state, actionCell);
  return row;
}

async function loadUsers() {
  refreshUsers.disabled = true;
  try {
    const response = await authFetch("/api/auth/users", { cache: "no-store" });
    if (!response.ok) throw new Error(await errorMessage(response));
    const users = await response.json();
    usersList.replaceChildren(...users.map(userRow));
  } catch (error) {
    showFeedback(error.message);
  } finally {
    refreshUsers.disabled = false;
  }
}

inviteForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = inviteForm.querySelector("button[type=submit]");
  submit.disabled = true;
  feedback.hidden = true;
  const data = new FormData(inviteForm);
  try {
    const response = await authFetch("/api/auth/invites", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: data.get("email"), role: data.get("role") }),
    });
    if (!response.ok) throw new Error(await errorMessage(response));
    const invite = await response.json();
    inviteUrl.value = invite.invite_url;
    inviteResult.hidden = false;
    showFeedback("Приглашение создано.", false);
  } catch (error) {
    showFeedback(error.message);
  } finally {
    submit.disabled = false;
  }
});

copyInvite.addEventListener("click", async () => {
  await navigator.clipboard.writeText(inviteUrl.value);
  copyInvite.textContent = "Скопировано";
});
refreshUsers.addEventListener("click", loadUsers);
loadUsers();
