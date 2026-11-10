const endpoint = "/api/workers";
const workerNames = new Map([
  ["arxiv-grabber", "Скачивание arXiv"],
  ["arxiv-text-worker", "Извлечение текста arXiv"],
  ["embeding-worker", "Индексация статей"],
  ["summary-worker", "Суммаризация"],
  ["embeding-service", "Эмбеддинги BGE-M3"],
]);
const statusLabels = new Map([
  ["created", "Готов к запуску"],
  ["running", "Запущен"],
  ["exited", "Остановлен"],
  ["restarting", "Перезапускается"],
  ["paused", "На паузе"],
  ["removing", "Удаляется"],
  ["dead", "Ошибка контейнера"],
  ["missing", "Контейнер не создан"],
  ["mixed", "Разные состояния контейнеров"],
  ["external", "Запускается в Windows"],
]);

const template = document.querySelector("#worker-template");
const list = document.querySelector("#workers-list");
const listStatus = document.querySelector("#workers-status");
const refresh = document.querySelector("#refresh-workers");
const cards = new Map();
let refreshRequest;
let changing = false;

for (const [id, name] of workerNames) {
  const card = template.content.cloneNode(true);
  const title = card.querySelector(".worker-name");
  title.textContent = name;
  title.id = `title-${id}`;
  card.querySelector("article").setAttribute("aria-labelledby", title.id);
  card.querySelector(".worker-id").textContent = id;
  const status = card.querySelector(".paper-stage");
  const feedback = card.querySelector(".worker-feedback");
  const start = card.querySelector('[data-action="start"]');
  const stop = card.querySelector('[data-action="stop"]');
  start.setAttribute("aria-label", `Старт · ${name}`);
  stop.setAttribute("aria-label", `Стоп · ${name}`);
  start.addEventListener("click", () => changeWorker(id, "start"));
  stop.addEventListener("click", () => changeWorker(id, "stop"));
  cards.set(id, { status, feedback, start, stop, worker: null });
  list.append(card);
}

function updateButtons() {
  refresh.disabled = changing;
  for (const { start, stop, worker } of cards.values()) {
    start.disabled = changing || !worker?.can_start;
    stop.disabled = changing || !worker?.can_stop;
  }
}

function showWorker(id, worker) {
  const card = cards.get(id);
  if (!card) return;
  card.worker = worker;
  const label = statusLabels.get(worker?.state) ?? "Статус недоступен";
  if (card.status.textContent !== label) card.status.textContent = label;
  card.status.dataset.stage = worker?.state === "running" ? "completed" : worker?.state ?? "";
}

async function loadWorkers() {
  if (refreshRequest || changing) return;
  const request = new AbortController();
  refreshRequest = request;
  try {
    const response = await fetch(endpoint, { cache: "no-store", signal: request.signal });
    if (!response.ok) throw new Error();
    const workers = await response.json();
    if (request.signal.aborted) return;
    for (const worker of workers) showWorker(worker.id, worker);
    listStatus.hidden = true;
  } catch {
    if (request.signal.aborted) return;
    for (const id of cards.keys()) showWorker(id, null);
    listStatus.textContent = "Не удалось обновить статусы воркеров.";
    listStatus.hidden = false;
  } finally {
    if (refreshRequest === request) {
      refreshRequest = undefined;
      updateButtons();
    }
  }
}

async function changeWorker(id, action) {
  const card = cards.get(id);
  if (changing || (action === "start" ? card.start.disabled : card.stop.disabled)) return;
  changing = true;
  refreshRequest?.abort();
  refreshRequest = undefined;
  updateButtons();
  card.feedback.textContent = "";
  card.status.textContent = action === "start" ? "Запускаем…" : "Останавливаем…";
  card.status.dataset.stage = "running";
  try {
    const response = await fetch(`${endpoint}/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    });
    if (!response.ok) {
      const error = await response.json().catch(() => null);
      throw new Error(typeof error?.detail === "string" ? error.detail : "");
    }
    showWorker(id, await response.json());
  } catch (error) {
    showWorker(id, null);
    card.feedback.textContent = error.message || "Не удалось выполнить команду.";
  } finally {
    changing = false;
    updateButtons();
    loadWorkers();
  }
}

refresh.addEventListener("click", loadWorkers);
setInterval(() => {
  if (!document.hidden) loadWorkers();
}, 5000);
loadWorkers();
