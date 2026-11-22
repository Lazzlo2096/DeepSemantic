import { authFetch } from "./auth.js";

const status = document.querySelector("#spaces-status");
const error = document.querySelector("#spaces-error");
const list = document.querySelector("#spaces-list");
const empty = document.querySelector("#spaces-empty");

const dateFormat = new Intl.DateTimeFormat("ru-RU", {
  year: "numeric", month: "long", day: "numeric",
});

function renderSpace(space) {
  const card = document.createElement("article");
  card.className = "paper";

  const meta = document.createElement("div");
  meta.className = "paper-meta";
  const visibility = document.createElement("span");
  visibility.className = "paper-stage";
  visibility.textContent = space.visibility === "public" ? "Public" : "Private";
  const date = document.createElement("time");
  date.dateTime = space.created_at;
  date.textContent = `Создано ${dateFormat.format(new Date(space.created_at))}`;
  meta.append(visibility, date);

  const title = document.createElement("h3");
  title.textContent = space.name;
  const description = document.createElement("p");
  description.textContent = space.description || "Описание отсутствует.";
  description.className = "formatted-text";

  card.append(meta, title, description);
  return card;
}

async function loadSpaces() {
  try {
    const response = await authFetch("/api/spaces", { cache: "no-store" });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(typeof body.detail === "string" ? body.detail : "Не удалось загрузить Spaces.");
    }
    const spaces = await response.json();
    list.replaceChildren(...spaces.map(renderSpace));
    empty.hidden = spaces.length !== 0;
    status.textContent = `${spaces.length} ${spaces.length === 1 ? "пространство" : "пространств"}`;
  } catch (loadError) {
    status.textContent = "Ошибка загрузки";
    error.textContent = loadError.message;
    error.hidden = false;
  }
}

loadSpaces();
