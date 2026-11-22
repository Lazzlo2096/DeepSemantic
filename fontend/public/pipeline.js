import { authFetch } from "./auth.js?v=1";

const settingsEndpoint = "/api/pipeline/settings";
const reconciliationEndpoint = "/api/pipeline/summary-reconciliation";
const modeBadge = document.querySelector("#summary-mode");
const description = document.querySelector("#pipeline-description");
const feedback = document.querySelector("#pipeline-feedback");
const refreshButton = document.querySelector("#refresh-pipeline");
const modeButtons = [...document.querySelectorAll(".pipeline-mode-button")];
const reconciliationStatus = document.querySelector("#reconciliation-status");
const reconciliationError = document.querySelector("#reconciliation-error");
const counters = document.querySelector("#reconciliation-counters");
let mode = null;
let changing = false;
let loading;

function updateButtons() {
  for (const button of modeButtons) {
    const buttonMode = button.dataset.mode;
    button.disabled = changing || mode === null || mode === buttonMode;
    button.setAttribute("aria-pressed", String(mode === buttonMode));
  }
  refreshButton.disabled = changing;
}

function showSettings(settings) {
  if (!["enabled", "deferred"].includes(settings?.summary_mode)) {
    throw new Error("Сервер вернул неизвестный режим обработки.");
  }
  mode = settings.summary_mode;
  modeBadge.textContent = mode === "enabled" ? "Включено" : "Отложено";
  modeBadge.dataset.stage = mode === "enabled" ? "completed" : "deferred";
  description.textContent = mode === "enabled"
    ? "Работают обе ветки: индекс статьи и саммари с эмбеддингами. Фоновая сверка обрабатывает недостающие результаты."
    : "Работает только индексация статей. Создание саммари и их эмбеддингов отложено до включения этой ветки.";
  updateButtons();
}

function showReconciliation(data) {
  const runtime = data?.runtime;
  const progress = runtime?.progress;
  counters.hidden = !progress || !Object.hasOwn(progress, "scanned");
  for (const key of ["scanned", "submitted", "passes"]) {
    const value = progress?.[key];
    document.querySelector(`#reconciliation-${key}`).textContent = Number.isSafeInteger(value) && value >= 0
      ? value.toLocaleString("ru-RU") : "—";
  }
  if (!runtime) {
    reconciliationStatus.textContent = "Фоновая сверка ещё не запущена. Она запускается при переключении режима обработки.";
  } else if (runtime.status === "failed") {
    reconciliationStatus.textContent = "Фоновая сверка остановилась с ошибкой.";
  } else if (runtime.status === "queued") {
    reconciliationStatus.textContent = "Ожидаем запуска фоновой сверки.";
  } else if (runtime.status === "completed") {
    reconciliationStatus.textContent = "Фоновая сверка завершена.";
  } else if (mode === "deferred") {
    reconciliationStatus.textContent = "Обработка накопившихся саммари отложена. Она продолжится после включения ветки.";
  } else {
    reconciliationStatus.textContent = "Фоновая сверка работает. Каталог проверяется повторно каждые 60 секунд.";
  }
  reconciliationError.textContent = progress?.last_error || runtime?.last_error || "";
  reconciliationError.hidden = !reconciliationError.textContent;
}

async function requestJson(url, options = {}) {
  const response = await authFetch(url, { cache: "no-store", ...options });
  if (!response.ok) {
    const error = await response.json().catch(() => null);
    throw new Error(typeof error?.detail === "string" ? error.detail : "Сервис временно недоступен.");
  }
  return response.json();
}

async function loadPipeline() {
  if (loading || changing) return;
  const request = new AbortController();
  loading = request;
  const signal = AbortSignal.any([request.signal, AbortSignal.timeout(10000)]);
  try {
    const [settings, reconciliation] = await Promise.allSettled([
      requestJson(settingsEndpoint, { signal }),
      requestJson(reconciliationEndpoint, { signal }),
    ]);
    if (request.signal.aborted) return;
    try {
      if (settings.status === "rejected") throw settings.reason;
      showSettings(settings.value);
    } catch {
      mode = null;
      modeBadge.textContent = "Режим недоступен";
      modeBadge.dataset.stage = "failed";
      description.textContent = "Не удалось прочитать настройку. Нажми «Обновить режим», чтобы повторить попытку.";
    }
    if (reconciliation.status === "fulfilled") {
      showReconciliation(reconciliation.value);
    } else {
      counters.hidden = true;
      reconciliationError.hidden = true;
      reconciliationStatus.textContent = "Не удалось обновить состояние фоновой сверки.";
    }
  } finally {
    if (loading === request) {
      loading = undefined;
      updateButtons();
    }
  }
}

async function changeMode(nextMode) {
  if (changing || mode === null || mode === nextMode) return;
  changing = true;
  loading?.abort();
  loading = undefined;
  updateButtons();
  feedback.hidden = false;
  feedback.dataset.error = "false";
  feedback.textContent = "Сохраняем режим…";
  try {
    const settings = await requestJson(settingsEndpoint, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ summary_mode: nextMode }),
      signal: AbortSignal.timeout(15000),
    });
    showSettings(settings);
    feedback.textContent = mode === "enabled"
      ? "Саммари включены. Накопившиеся статьи будут обработаны автоматически."
      : "Саммари отложены. Индексация статей продолжает работать.";
  } catch (error) {
    feedback.dataset.error = "true";
    feedback.textContent = `Не удалось подтвердить переключение. ${error.message || "Обнови режим и повтори попытку."}`;
  } finally {
    changing = false;
    updateButtons();
    await loadPipeline();
  }
}

for (const button of modeButtons) {
  button.addEventListener("click", () => changeMode(button.dataset.mode));
}
refreshButton.addEventListener("click", loadPipeline);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) loadPipeline();
});
setInterval(() => {
  if (!document.hidden) loadPipeline();
}, 5000);
loadPipeline();
