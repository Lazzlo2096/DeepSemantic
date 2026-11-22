import { authFetch, loadCurrentUser } from "./auth.js";

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
  const files = await filesResponse.json();
  document.title = `${space.name} · DeepSemantic`;
  title.textContent = space.name;
  description.textContent = space.description || "Описание отсутствует.";
  list.replaceChildren(...files.map(renderFile));
  empty.hidden = files.length !== 0;
  status.textContent = `${files.length} ${files.length === 1 ? "файл" : "файлов"}`;
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

loadSpace().catch((loadError) => {
  title.textContent = "Space недоступен";
  status.textContent = "Ошибка загрузки";
  error.textContent = loadError.message;
  error.hidden = false;
});
setupArxivTargetButton();
