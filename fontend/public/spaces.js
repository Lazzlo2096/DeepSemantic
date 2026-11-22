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
  const badge = document.createElement("span");
  badge.className = "paper-stage";
  badge.textContent = "Public";
  const date = document.createElement("time");
  date.dateTime = space.created_at;
  date.textContent = `Создано ${dateFormat.format(new Date(space.created_at))}`;
  meta.append(badge, date);

  const title = document.createElement("h3");
  const link = document.createElement("a");
  link.className = "paper-title";
  link.href = `/space.html?id=${encodeURIComponent(space.id)}`;
  link.textContent = space.name;
  title.append(link);
  const description = document.createElement("p");
  description.textContent = space.description || "Описание отсутствует.";
  description.className = "formatted-text";

  card.append(meta, title, description);
  return card;
}

async function loadSpaces() {
  try {
    const response = await fetch("/api/spaces/public", { cache: "no-store" });
    if (!response.ok) throw new Error("Не удалось загрузить публичные пространства.");
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
