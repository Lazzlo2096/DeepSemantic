import { authFetch, loadCurrentUser } from "./auth.js?v=2";

const spaceId = new URLSearchParams(location.search).get("id");
const title = document.querySelector("#space-title");
const description = document.querySelector("#space-description");
const status = document.querySelector("#files-status");
const error = document.querySelector("#space-error");
const list = document.querySelector("#files-list");
const empty = document.querySelector("#files-empty");
const arxivTargetButton = document.querySelector("#arxiv-target-button");
const arxivTargetStatus = document.querySelector("#arxiv-target-status");
const spaceCatalogLink = document.querySelector("#space-catalog-link");
const uploadForm = document.querySelector("#space-upload-form");
const fileInput = document.querySelector("#space-file-input");
const uploadSubmit = document.querySelector("#space-upload-submit");
const uploadStatus = document.querySelector("#space-upload-status");
const editButton = document.querySelector("#space-edit-button");
const deleteButton = document.querySelector("#space-delete-button");
const editForm = document.querySelector("#space-edit-form");
const editName = document.querySelector("#space-edit-name");
const editDescription = document.querySelector("#space-edit-description");
const editVisibility = document.querySelector("#space-edit-visibility");
const editSubmit = document.querySelector("#space-edit-submit");
const editCancel = document.querySelector("#space-edit-cancel");
const editStatus = document.querySelector("#space-edit-status");
let currentSpace;

const dateFormat = new Intl.DateTimeFormat("ru-RU", {
  year: "numeric", month: "long", day: "numeric",
});

function formatSize(size) {
  if (size == null) return "Размер неизвестен";
  if (size < 1024) return `${size} Б`;
  if (size < 1024 ** 2) return `${Math.round(size / 1024)} КБ`;
  return `${(size / 1024 ** 2).toFixed(1)} МБ`;
}

function renderFile(file) {
  const card = document.createElement("article");
  card.className = "paper";
  const meta = document.createElement("div");
  meta.className = "paper-meta";
  const type = document.createElement("span");
  type.className = "paper-stage";
  type.textContent = file.content_type || "Файл";
  const date = document.createElement("time");
  date.dateTime = file.created_at;
  date.textContent = `${formatSize(file.size_bytes)} · ${dateFormat.format(new Date(file.created_at))}`;
  meta.append(type, date);
  const name = document.createElement("h3");
  name.textContent = file.filename;
  card.append(meta, name);
  return card;
}

async function loadSpace() {
  if (!spaceId) throw new Error("В ссылке не указан Space.");
  spaceCatalogLink.href = `/space-catalog.html?id=${encodeURIComponent(spaceId)}`;
  const publicBase = `/api/spaces/public/${encodeURIComponent(spaceId)}`;
  let base = publicBase;
  let spaceResponse = await authFetch(base, { cache: "no-store" });
  if (!spaceResponse.ok) {
    base = `/api/spaces/${encodeURIComponent(spaceId)}`;
    spaceResponse = await authFetch(base, { cache: "no-store" });
  }
  const filesResponse = await authFetch(`${base}/files`, { cache: "no-store" });
  if (!spaceResponse.ok || !filesResponse.ok) {
    throw new Error("Space не найден или больше не является публичным.");
  }
  const space = await spaceResponse.json();
  currentSpace = space;
  const files = await filesResponse.json();
  document.title = `${space.name} · DeepSemantic`;
  title.textContent = space.name;
  description.textContent = space.description || "Описание отсутствует.";
  list.replaceChildren(...files.map(renderFile));
  empty.hidden = files.length !== 0;
  status.textContent = `${files.length} ${files.length === 1 ? "файл" : "файлов"}`;
}

async function setupSpaceActions() {
  try {
    const user = await loadCurrentUser();
    if (!user || !currentSpace || user.id !== currentSpace.owner_id) return;
    editButton.hidden = false;
    deleteButton.hidden = false;
    editButton.addEventListener("click", () => {
      editName.value = currentSpace.name;
      editDescription.value = currentSpace.description || "";
      editVisibility.value = currentSpace.visibility;
      editStatus.textContent = "";
      editForm.hidden = false;
      editName.focus();
    });
    editCancel.addEventListener("click", () => { editForm.hidden = true; });
    editForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      editSubmit.disabled = true;
      editStatus.textContent = "Сохраняем…";
      try {
        const response = await authFetch(`/api/spaces/${encodeURIComponent(spaceId)}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: editName.value,
            description: editDescription.value || null,
            visibility: editVisibility.value,
          }),
        });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Не удалось сохранить Space.");
        currentSpace = body;
        document.title = `${body.name} · DeepSemantic`;
        title.textContent = body.name;
        description.textContent = body.description || "Описание отсутствует.";
        editForm.hidden = true;
      } catch (error) {
        editStatus.textContent = error.message;
      } finally {
        editSubmit.disabled = false;
      }
    });
    deleteButton.addEventListener("click", async () => {
      if (!confirm(`Удалить Space «${currentSpace.name}»? Это действие нельзя отменить.`)) return;
      deleteButton.disabled = true;
      editButton.disabled = true;
      try {
        const response = await authFetch(`/api/spaces/${encodeURIComponent(spaceId)}`, { method: "DELETE" });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Не удалось удалить Space.");
        location.assign("/my-spaces.html");
      } catch (error) {
        editStatus.textContent = error.message;
        deleteButton.disabled = false;
        editButton.disabled = false;
      }
    });
  } catch {
    // Anonymous visitors do not need owner controls.
  }
}

async function setupArxivTargetButton() {
  try {
    const user = await loadCurrentUser();
    if (user?.role !== "admin" || !spaceId) return;
    arxivTargetButton.hidden = false;
    const targetResponse = await authFetch(
      `/api/spaces/${encodeURIComponent(spaceId)}/arxiv-import-target`, { cache: "no-store" },
    );
    if (targetResponse.ok && (await targetResponse.json()).enabled) {
      arxivTargetButton.disabled = true;
      arxivTargetButton.textContent = "это пространство уже приёмник импортов архива";
      return;
    }
    arxivTargetButton.addEventListener("click", async () => {
      arxivTargetButton.disabled = true;
      arxivTargetStatus.textContent = "Сохраняем…";
      const response = await authFetch(`/api/spaces/${encodeURIComponent(spaceId)}/arxiv-import-target`, {
        method: "POST",
      });
      if (!response.ok) {
        arxivTargetStatus.textContent = "Не удалось назначить приёмник.";
        arxivTargetButton.disabled = false;
        return;
      }
      arxivTargetStatus.textContent = "Space назначен приёмником импортов arXiv.";
      arxivTargetButton.textContent = "Приёмник импортов arXiv";
    });
  } catch {
    // Public visitors do not need to see an admin-only control.
  }
}

async function setupUploadForm() {
  try {
    const user = await loadCurrentUser();
    if (!user || !spaceId) return;
    uploadForm.hidden = false;
  } catch {
    return;
  }
  uploadForm.addEventListener("submit", async (event) => {
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
        const response = await authFetch(`/api/spaces/${encodeURIComponent(spaceId)}/files`, {
          method: "POST",
          body: payload,
        });
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          throw new Error(typeof body.detail === "string" ? body.detail : `Не удалось загрузить ${file.name}.`);
        }
        uploadStatus.textContent = `Загружаем ${index + 1} из ${files.length}…`;
      }
      uploadStatus.textContent = `Загружено файлов: ${files.length}.`;
      fileInput.value = "";
      await loadSpace();
    } catch (error) {
      uploadStatus.textContent = error.message;
    } finally {
      uploadSubmit.disabled = false;
    }
  });
}

loadSpace().then(setupSpaceActions).catch((loadError) => {
  title.textContent = "Space недоступен";
  status.textContent = "Ошибка загрузки";
  error.textContent = loadError.message;
  error.hidden = false;
});
setupArxivTargetButton();
setupUploadForm();
