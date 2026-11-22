import { authFetch } from "./auth.js?v=3";

const list = document.querySelector("#targets-list");
const status = document.querySelector("#targets-status");
const error = document.querySelector("#targets-error");
const empty = document.querySelector("#targets-empty");

function renderTarget(space) {
  const card = document.createElement("article");
  card.className = "paper";
  const title = document.createElement("h3");
  const link = document.createElement("a");
  link.className = "paper-title";
  link.href = `/space.html?id=${encodeURIComponent(space.id)}`;
  link.textContent = space.name;
  title.append(link);
  const description = document.createElement("p");
  description.textContent = space.description || "Описание отсутствует.";
  card.append(title, description);
  return card;
}

async function loadTargets() {
  try {
    const response = await authFetch("/api/spaces/arxiv-import-targets", { cache: "no-store" });
    if (!response.ok) throw new Error("Не удалось загрузить приёмники arXiv.");
    const targets = await response.json();
    list.replaceChildren(...targets.map(renderTarget));
    empty.hidden = targets.length !== 0;
    status.textContent = `${targets.length} ${targets.length === 1 ? "Space" : "Spaces"}`;
  } catch (loadError) {
    status.textContent = "Ошибка загрузки";
    error.textContent = loadError.message;
    error.hidden = false;
  }
}

loadTargets();
