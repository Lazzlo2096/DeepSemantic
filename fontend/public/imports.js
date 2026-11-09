const endpoint = "/api/arxiv/imports";
const pageSize = 20;
const refreshInterval = 5000;
const statusLabels = new Map([
  ["queued", "В очереди"],
  ["running", "В работе"],
  ["pausing", "Приостанавливается"],
  ["paused", "На паузе"],
  ["completed", "Завершено"],
  ["failed", "Ошибка"],
]);
const dateTimeFormat = new Intl.DateTimeFormat("ru-RU", { dateStyle: "short", timeStyle: "short" });
const form = document.querySelector("#import-form");
const dateTo = form.elements.namedItem("date_to");
const submit = document.querySelector("#import-submit");
const feedback = document.querySelector("#import-feedback");
const listStatus = document.querySelector("#imports-status");
const controlFeedback = document.querySelector("#import-control-feedback");
const table = document.querySelector("#imports-table-wrap");
const list = document.querySelector("#imports-list");
const template = document.querySelector("#import-row-template");
const pagination = document.querySelector("#imports-pagination");
const previous = document.querySelector("#imports-previous");
const next = document.querySelector("#imports-next");
const pageNumber = document.querySelector("#imports-page");

let currentPage = 0;
let listRequest;
const pendingActions = new Set();

function formatTime(timestamp) {
  return dateTimeFormat.format(new Date(timestamp));
}

function renderJob(job) {
  const row = template.content.cloneNode(true);
  const created = row.querySelector(".import-created");
  created.dateTime = job.created_at;
  created.textContent = formatTime(job.created_at);
  row.querySelector(".import-job-id").textContent = job.id;
  row.querySelector(".import-dates").textContent = `${job.date_from} — ${job.date_to}`;
  row.querySelector(".import-bucket").textContent = job.bucket;
  row.querySelector(".import-query").textContent = job.query || "Все категории";

  const stage = row.querySelector(".paper-stage");
  stage.dataset.stage = job.status;
  stage.textContent = statusLabels.get(job.status);
  const button = row.querySelector(".import-action");
  if (["queued", "running", "paused"].includes(job.status)) {
    const action = job.status === "paused" ? "resume" : "pause";
    button.hidden = false;
    button.disabled = pendingActions.has(job.id);
    button.textContent = action === "pause" ? "Приостановить" : "Продолжить";
    button.addEventListener("click", () => changeImport(job.id, action, button));
  }
  row.querySelector(".import-attempts").textContent = `Попыток: ${job.attempts}`;
  const timing = row.querySelector(".import-timing");
  if (job.finished_at) {
    timing.textContent = `Завершение ${formatTime(job.finished_at)}`;
  } else if (job.status === "queued" && job.attempts > 0 && job.processed_articles == null) {
    timing.textContent = `Повтор с ${formatTime(job.available_at)}`;
  } else if (job.started_at) {
    timing.textContent = `Старт ${formatTime(job.started_at)}`;
  }

  const result = job.result;
  const resultText = row.querySelector(".import-result");
  if (result) {
    const summary = `Исходников доступно: ${result.pdf_available}. Исходников недоступно: ${result.pdf_failures}.`;
    resultText.textContent = result.articles_found == null
      ? summary
      : `Найдено: ${result.articles_found} · Уже было в бакете: ${result.pdf_reused}\n${summary}`;
    if (job.processed_articles != null && result.articles_found != null) {
      resultText.textContent += `\nОбработано: ${job.processed_articles} из ${result.articles_found}`;
    }
  } else {
    resultText.textContent = job.status === "running" ? "Ожидаем результаты поиска…" : "—";
  }
  return row;
}

async function changeImport(id, action, button) {
  if (pendingActions.has(id)) return;
  pendingActions.add(id);
  button.disabled = true;
  controlFeedback.hidden = true;
  try {
    const response = await fetch(`${endpoint}/${id}/${action}`, { method: "POST" });
    if (!response.ok) throw new Error();
  } catch {
    controlFeedback.textContent = "Не удалось выполнить команду. Проверь текущий статус задания.";
    controlFeedback.hidden = false;
  } finally {
    pendingActions.delete(id);
    button.disabled = false;
    await loadImports();
  }
}

async function loadImports(page = currentPage) {
  listRequest?.abort();
  const request = new AbortController();
  listRequest = request;
  const params = new URLSearchParams({ limit: pageSize + 1, offset: page * pageSize });
  try {
    const response = await fetch(`${endpoint}?${params}`, { signal: request.signal, cache: "no-store" });
    if (!response.ok) throw new Error(`Import list returned ${response.status}`);
    const jobs = await response.json();
    if (request.signal.aborted) return;

    list.replaceChildren(...jobs.slice(0, pageSize).map(renderJob));
    currentPage = page;
    table.hidden = jobs.length === 0;
    listStatus.hidden = jobs.length > 0;
    if (!jobs.length) listStatus.textContent = "Заданий пока нет.";
    previous.disabled = page === 0;
    next.disabled = jobs.length <= pageSize;
    pagination.hidden = previous.disabled && next.disabled;
    pageNumber.textContent = `Страница ${page + 1}`;
  } catch {
    if (!request.signal.aborted) {
      listStatus.hidden = false;
      listStatus.textContent = "Не удалось обновить список заданий.";
    }
  } finally {
    if (!request.signal.aborted) listRequest = undefined;
  }
}

form.addEventListener("input", () => dateTo.setCustomValidity(""));
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (submit.disabled) return;
  const payload = Object.fromEntries(new FormData(form));
  if (payload.date_from > payload.date_to) {
    dateTo.setCustomValidity("Дата окончания должна быть не раньше даты начала.");
    dateTo.reportValidity();
    return;
  }
  payload.query = payload.query.trim();

  submit.disabled = true;
  feedback.textContent = "Создаём задание…";
  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (response.status === 422) {
      feedback.textContent = "Проверь диапазон дат, фильтр и имя бакета.";
      return;
    }
    if (!response.ok) throw new Error(`Import request returned ${response.status}`);
    const job = await response.json();
    feedback.textContent = `Задание ${job.id} создано.`;
    loadImports(0);
  } catch {
    feedback.textContent = "Не удалось получить подтверждение. Проверь список заданий перед повтором.";
  } finally {
    submit.disabled = false;
  }
});

document.querySelector("#refresh-imports").addEventListener("click", () => loadImports());
previous.addEventListener("click", () => loadImports(currentPage - 1));
next.addEventListener("click", () => loadImports(currentPage + 1));
setInterval(() => {
  if (!document.hidden && !listRequest) loadImports();
}, refreshInterval);
loadImports();
