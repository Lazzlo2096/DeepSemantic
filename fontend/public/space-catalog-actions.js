import { authFetch, loadCurrentUser } from "./auth.js?v=3";

const spaceId = new URLSearchParams(location.search).get("id");
const title = document.querySelector("#space-title");
const visibility = document.querySelector("#space-visibility");
const description = document.querySelector("#space-description");
const overview = document.querySelector("#space-overview");
const editButton = document.querySelector("#space-edit-button");
const deleteButton = document.querySelector("#space-delete-button");
const editForm = document.querySelector("#space-edit-form");
const editName = document.querySelector("#space-edit-name");
const editDescription = document.querySelector("#space-edit-description");
const editVisibility = document.querySelector("#space-edit-visibility");
const editSubmit = document.querySelector("#space-edit-submit");
const editCancel = document.querySelector("#space-edit-cancel");
const editStatus = document.querySelector("#space-edit-status");
const arxivTargetButton = document.querySelector("#arxiv-target-button");
const arxivTargetStatus = document.querySelector("#arxiv-target-status");
const uploadForm = document.querySelector("#space-upload-form");
const fileInput = document.querySelector("#space-file-input");
const uploadSubmit = document.querySelector("#space-upload-submit");
const uploadStatus = document.querySelector("#space-upload-status");
let currentSpace;

const apiBase = () => `/api/spaces/${encodeURIComponent(spaceId)}`;

async function loadSpace() {
  if (!spaceId) throw new Error("В ссылке не указан Space.");
  let response = await authFetch(`/api/spaces/public/${encodeURIComponent(spaceId)}`, { cache: "no-store" });
  if (!response.ok) response = await authFetch(apiBase(), { cache: "no-store" });
  if (!response.ok) throw new Error("Space не найден или недоступен.");
  currentSpace = await response.json();
  document.title = `${currentSpace.name} · DeepSemantic`;
  title.textContent = currentSpace.name;
  description.textContent = currentSpace.description || "Описание отсутствует.";
  visibility.textContent = currentSpace.visibility === "public" ? "Public Space" : "Private Space";
  overview.hidden = false;
}

async function setupOwnerActions(user) {
  if (!user || user.id !== currentSpace.owner_id) return;
  editButton.hidden = false;
  deleteButton.hidden = false;
  editButton.onclick = () => {
    editName.value = currentSpace.name;
    editDescription.value = currentSpace.description || "";
    editVisibility.value = currentSpace.visibility;
    editStatus.textContent = "";
    editForm.hidden = false;
    editName.focus();
  };
  editCancel.onclick = () => { editForm.hidden = true; };
  editForm.onsubmit = async (event) => {
    event.preventDefault();
    editSubmit.disabled = true;
    editStatus.textContent = "Сохраняем…";
    try {
      const response = await authFetch(apiBase(), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: editName.value, description: editDescription.value || null, visibility: editVisibility.value }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || "Не удалось сохранить Space.");
      currentSpace = body;
      document.title = `${body.name} · DeepSemantic`;
      title.textContent = body.name;
      description.textContent = body.description || "Описание отсутствует.";
      visibility.textContent = body.visibility === "public" ? "Public Space" : "Private Space";
      editForm.hidden = true;
    } catch (error) {
      editStatus.textContent = error.message;
    } finally {
      editSubmit.disabled = false;
    }
  };
  deleteButton.onclick = async () => {
    if (!confirm(`Удалить Space «${currentSpace.name}»? Это действие нельзя отменить.`)) return;
    deleteButton.disabled = true;
    editButton.disabled = true;
    const response = await authFetch(apiBase(), { method: "DELETE" });
    if (response.ok) location.assign("/my-spaces.html");
    else {
      editStatus.textContent = "Не удалось удалить Space.";
      deleteButton.disabled = false;
      editButton.disabled = false;
    }
  };
}

async function setupAdminAction(user) {
  if (user?.role !== "admin") return;
  arxivTargetButton.hidden = false;
  const response = await authFetch(`${apiBase()}/arxiv-import-target`, { cache: "no-store" });
  if (response.ok && (await response.json()).enabled) {
    arxivTargetButton.disabled = true;
    arxivTargetButton.textContent = "это пространство уже приёмник импортов arXiv";
    return;
  }
  arxivTargetButton.onclick = async () => {
    arxivTargetButton.disabled = true;
    arxivTargetStatus.textContent = "Сохраняем…";
    const result = await authFetch(`${apiBase()}/arxiv-import-target`, { method: "POST" });
    if (result.ok) {
      arxivTargetStatus.textContent = "Space назначен приёмником импортов arXiv.";
      arxivTargetButton.textContent = "Приёмник импортов arXiv";
    } else {
      arxivTargetStatus.textContent = "Не удалось назначить приёмник.";
      arxivTargetButton.disabled = false;
    }
  };
}

async function setupUpload(user) {
  if (!user) return;
  uploadForm.hidden = false;
  uploadForm.onsubmit = async (event) => {
    event.preventDefault();
    const files = [...fileInput.files];
    if (!files.length) {
      uploadStatus.textContent = "Выберите хотя бы один файл.";
      return;
    }
    uploadSubmit.disabled = true;
    uploadStatus.textContent = `Загружаем 0 из ${files.length}…`;
    try {
      for (const [index, file] of files.entries()) {
        const payload = new FormData();
        payload.append("file", file);
        const response = await authFetch(`${apiBase()}/files`, { method: "POST", body: payload });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.detail || `Не удалось загрузить ${file.name}.`);
        uploadStatus.textContent = `Загружаем ${index + 1} из ${files.length}…`;
      }
      location.reload();
    } catch (error) {
      uploadStatus.textContent = error.message;
      uploadSubmit.disabled = false;
    }
  };
}

try {
  await loadSpace();
  const user = await loadCurrentUser();
  await Promise.all([setupOwnerActions(user), setupAdminAction(user), setupUpload(user)]);
} catch (error) {
  title.textContent = "Space недоступен";
  description.textContent = error.message;
  overview.hidden = false;
}
