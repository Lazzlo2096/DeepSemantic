import { arxivUrl } from "./arxiv.js";

const PAGE_SIZE = 12;
const stageLabels = new Map([
  ["downloaded", "Исходник скачан"],
  ["texting", "Извлекается текст"],
  ["texted", "Текст извлечён"],
]);
const dateFormat = new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeZone: "UTC" });
const list = document.querySelector("#papers");
const status = document.querySelector("#status");
const retry = document.querySelector("#retry");
const pagination = document.querySelector("#pagination");
const pageNumber = document.querySelector("#page-number");
const previous = document.querySelector("#previous");
const next = document.querySelector("#next");
const template = document.querySelector("#paper-template");
const resultsTitle = document.querySelector("#results-title");
const catalog = document.querySelector("#catalog");
const catalogFilters = document.querySelector("#catalog-filters");
const dateFrom = catalogFilters.elements.namedItem("date_from");
const dateTo = catalogFilters.elements.namedItem("date_to");
const statusFilter = catalogFilters.elements.namedItem("status");
const searchForm = document.querySelector("#search-form");
const searchMode = searchForm.elements.namedItem("mode");
const searchQuery = document.querySelector("#search-query");
const searchSubmit = document.querySelector("#search-submit");
const searchResults = document.querySelector("#search-results");
const searchTitle = document.querySelector("#search-title");
const searchStatus = document.querySelector("#search-status");
const searchChunks = document.querySelector("#search-chunks");
const searchTemplate = document.querySelector("#search-result-template");
const backToCatalog = document.querySelector("#back-to-catalog");
const ragResult = document.querySelector("#rag-result");
const ragPoints = document.querySelector("#rag-points");
const ragSources = document.querySelector("#rag-sources");
const ragExplanation = document.querySelector("#rag-explanation");
const ragExplanationText = document.querySelector("#rag-explanation-text");
const ragTrace = document.querySelector("#rag-trace");
const ragTraceStatus = document.querySelector("#rag-trace-status");
const ragModelHistory = document.querySelector("#rag-model-history");
const ragModelTurns = document.querySelector("#rag-model-turns");
const ragTraceEmpty = document.querySelector("#rag-trace-empty");
const ragUnassignedStep = document.querySelector("#rag-unassigned-step");
const ragUnassignedCalls = document.querySelector("#rag-unassigned-calls");
const ragPermalink = document.querySelector("#rag-permalink");
const scoreFormat = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 3 });
const searchModes = {
  rag: {
    endpoint: "/api/rag/answer",
    title: "Поиск по RAG",
    pending: "Ищем источники и готовим ответ…",
    placeholder: "Например, что такое фильтрация в этой статье?",
  },
  vector: {
    endpoint: "/api/chunks/search",
    title: "Векторный поиск по эмбеддингам",
    pending: "Ищем похожие фрагменты…",
    placeholder: "Например, как модели запоминают прошлые разговоры",
    empty: "Похожие фрагменты не найдены. Поиск работает по индексированным чанкам.",
  },
  text: {
    endpoint: "/api/chunks/search/text",
    title: "Текстовый поиск",
    pending: "Ищем строку в тексте чанков…",
    placeholder: "Введите слово, фразу или формулу",
    empty: "В сохранённых чанках нет такой строки.",
  },
};

let currentPage = 0;
let requestedPage = 0;
let pageCount = 0;
let appliedFilters = new URLSearchParams();
let catalogRequest;
let searchRequest;

for (const [value, label] of stageLabels) {
  statusFilter.add(new Option(label, value));
}

function paperUrl(arxivId) {
  return `/paper.html?${new URLSearchParams({ id: arxivId })}`;
}

function linkPaper(card, arxivId) {
  card.querySelector(".paper-title").href = paperUrl(arxivId);
  const original = card.querySelector(".paper-arxiv");
  original.href = arxivUrl(arxivId);
  original.hidden = false;
}

function renderPaper(paper) {
  const card = template.content.cloneNode(true);
  const title = card.querySelector(".paper-title");
  const date = card.querySelector(".paper-date");
  const stage = card.querySelector(".paper-stage");

  title.textContent = paper.title || paper.arxiv_id;
  linkPaper(card, paper.arxiv_id);
  card.querySelector(".paper-id").textContent = `arXiv:${paper.arxiv_id}`;
  date.dateTime = paper.submitted_at;
  date.textContent = dateFormat.format(new Date(paper.submitted_at));
  stage.dataset.stage = paper.status ?? "";
  stage.textContent = stageLabels.get(paper.status) ?? "Статус неизвестен";
  const summaryStatus = card.querySelector(".paper-summary-status");
  const summaryReady = paper.has_summary === true;
  summaryStatus.dataset.ready = String(summaryReady);
  summaryStatus.textContent = summaryReady ? "Суммаризация готова" : "Суммаризация не готова";
  card.querySelector(".paper-authors").textContent = paper.authors.join(", ");
  card.querySelector(".paper-abstract").textContent = paper.abstract || "Аннотация отсутствует.";
  return card;
}

async function loadPage(page, moveFocus = false) {
  catalogRequest?.abort();
  const request = new AbortController();
  catalogRequest = request;
  requestedPage = page;
  previous.disabled = true;
  next.disabled = true;
  retry.hidden = true;
  status.textContent = "Загружаем статьи…";
  list.setAttribute("aria-busy", "true");

  try {
    const offset = page * PAGE_SIZE;
    const params = new URLSearchParams(appliedFilters);
    params.set("limit", PAGE_SIZE);
    params.set("offset", offset);
    const response = await fetch(`/api/papers?${params}`, { signal: request.signal });
    if (request.signal.aborted) return;
    if (response.status === 422) {
      status.textContent = "Проверь диапазон дат и примени фильтры снова.";
      return;
    }
    if (!response.ok) {
      throw new Error(`Catalog request returned ${response.status}`);
    }

    const totalHeader = response.headers.get("X-Total-Count");
    const total = Number(totalHeader);
    if (totalHeader === null || !Number.isSafeInteger(total) || total < 0) {
      throw new Error("Invalid catalog count");
    }

    const totalPages = Math.ceil(total / PAGE_SIZE);
    if (page > 0 && page >= totalPages) {
      const lastPage = Math.max(0, totalPages - 1);
      setPageInUrl(lastPage, true);
      await loadPage(lastPage, moveFocus);
      return;
    }

    const papers = await response.json();
    if (request.signal.aborted) return;
    list.replaceChildren(...papers.map(renderPaper));
    currentPage = page;
    pageCount = totalPages;
    pageNumber.textContent = `Страница ${page + 1} из ${pageCount}`;
    pagination.hidden = pageCount === 0;
    status.textContent = papers.length
      ? `Записи ${offset + 1}–${offset + papers.length}`
      : params.has("status") || params.has("date_from") || params.has("date_to") || params.has("has_summary")
        ? "По этим фильтрам статьи не найдены."
        : "В каталоге пока нет статей.";

    if (moveFocus) {
      resultsTitle.focus();
    }
  } catch {
    if (!request.signal.aborted) {
      status.textContent = "Не удалось загрузить статьи. Попробуй ещё раз.";
      retry.hidden = false;
    }
  } finally {
    if (!request.signal.aborted) {
      catalogRequest = undefined;
      previous.disabled = currentPage === 0;
      next.disabled = currentPage + 1 >= pageCount;
      list.setAttribute("aria-busy", "false");
    }
  }
}

function updateUrl(url, replace = false) {
  if (url.href === location.href) return;
  if (replace) {
    history.replaceState(null, "", url);
  } else {
    history.pushState(null, "", url);
  }
}

function setPageInUrl(page, replace = false) {
  const url = new URL(location.href);
  if (page > 0) {
    url.searchParams.set("page", page + 1);
  } else {
    url.searchParams.delete("page");
  }
  updateUrl(url, replace);
}

function applyFilters() {
  const url = new URL(location.href);
  for (const [name, value] of new FormData(catalogFilters)) {
    if (value && !(name === "sort" && value === "downloaded_desc")) {
      url.searchParams.set(name, value);
    } else {
      url.searchParams.delete(name);
    }
  }
  url.searchParams.delete("page");
  updateUrl(url);
  showLocation();
}

function updateSearchInput(mode) {
  searchQuery.placeholder = searchModes[mode].placeholder;
  if (mode === "rag") searchQuery.removeAttribute("maxlength");
  else searchQuery.maxLength = 1000;
}

function showLocation(moveFocus = false) {
  const params = new URLSearchParams(location.search);
  const selectedMode = params.get("mode");
  const mode = Object.hasOwn(searchModes, selectedMode) ? selectedMode : "rag";
  const runId = mode === "rag" ? params.get("run") : null;
  searchMode.value = mode;
  updateSearchInput(mode);
  searchQuery.value = params.get("query") ?? "";
  const query = searchQuery.value.trim();
  catalog.hidden = Boolean(query || runId);
  searchResults.hidden = !catalog.hidden;
  ragResult.hidden = true;
  ragTrace.hidden = true;
  ragTrace.open = false;
  ragUnassignedStep.hidden = true;
  ragUnassignedStep.open = false;
  ragUnassignedCalls.replaceChildren();
  ragModelTurns.replaceChildren();
  ragModelHistory.hidden = true;

  if (query || runId) {
    catalogRequest?.abort();
    catalogRequest = undefined;
    loadSearch(query, mode, moveFocus, runId);
    return;
  }

  searchRequest?.abort();
  searchRequest = undefined;
  searchSubmit.disabled = false;
  searchResults.setAttribute("aria-busy", "false");
  catalogFilters.reset();
  dateTo.setCustomValidity("");
  appliedFilters = new URLSearchParams();
  for (const [name, defaultValue] of new FormData(catalogFilters)) {
    const field = catalogFilters.elements.namedItem(name);
    field.value = params.get(name) ?? defaultValue;
    if (!field.validity.valid || !field.value) field.value = defaultValue;
    if (field.value) appliedFilters.set(name, field.value);
  }

  const requestedIndex = Number(params.get("page")) - 1;
  const validPage = requestedIndex >= 0 && Number.isInteger(requestedIndex)
    && Number.isSafeInteger(requestedIndex * PAGE_SIZE);
  currentPage = 0;
  pageCount = 0;
  list.replaceChildren();
  pagination.hidden = true;
  loadPage(validPage ? requestedIndex : 0, moveFocus);
}

function renderSearchResult(chunk) {
  const card = searchTemplate.content.cloneNode(true);
  const title = card.querySelector(".search-paper");
  title.textContent = chunk.paper_title || chunk.arxiv_id || "Фрагмент без статьи";
  if (chunk.arxiv_id) {
    linkPaper(card, chunk.arxiv_id);
  }
  const label = chunk.source_kind === "summary" ? "Чанк суммаризации" : "Чанк";
  card.querySelector(".search-fragment").textContent = `${label} №${chunk.chunk_id}`;
  const score = card.querySelector(".search-score");
  score.hidden = typeof chunk.score !== "number";
  if (!score.hidden) score.textContent = `Сходство ${scoreFormat.format(chunk.score)}`;
  card.querySelector(".search-excerpt").textContent = chunk.text;
  return card;
}

function renderRagAnswer(answer) {
  const links = new Map();
  ragSources.replaceChildren(...answer.sources.map((source) => {
    const details = document.createElement("details");
    const heading = document.createElement("summary");
    const link = document.createElement("a");
    const excerpt = document.createElement("p");
    const isSummary = source.source_kind === "summary";
    const anchor = isSummary ? "paper-summary"
      : source.chunk_index === null ? "paper-text" : `chunk-${source.chunk_index + 1}`;
    link.href = `${paperUrl(source.arxiv_id)}#${anchor}`;
    link.textContent = isSummary ? "Открыть суммаризацию" : "Открыть фрагмент статьи";
    heading.textContent = `[${source.id}] ${source.paper_title || source.arxiv_id} · [${source.source_start}, ${source.source_end})`;
    excerpt.className = "chunk-text";
    excerpt.textContent = source.text;
    details.append(heading, excerpt, link);
    links.set(source.id, link.href);
    return details;
  }));
  ragPoints.replaceChildren(...answer.points.map((point) => {
    const item = document.createElement("li");
    item.textContent = point.text;
    for (const id of new Set(point.source_ids)) {
      const link = document.createElement("a");
      link.href = links.get(id);
      link.textContent = `[${id}]`;
      link.setAttribute("aria-label", `Открыть источник ${id}`);
      item.append(" ", link);
    }
    return item;
  }));
  ragExplanationText.textContent = answer.source_explanation;
  ragExplanation.hidden = !answer.source_explanation;
  ragResult.hidden = false;
}

const toolLabels = new Map([
  ["search_vectors", "Векторный поиск"],
  ["search_text", "Текстовый поиск"],
  ["read_document", "Чтение документа"],
]);
const argumentLabels = new Map([
  ["query", "Запрос"], ["limit", "Лимит"], ["document_id", "Документ"],
  ["source_kind", "Источник"], ["source_start", "Начало"], ["source_end", "Конец"],
]);

function toolArguments(args) {
  if (typeof args === "string") {
    try {
      return JSON.parse(args);
    } catch {
      return args;
    }
  }
  return args;
}

function renderToolArguments(args) {
  if (!args || typeof args !== "object" || Array.isArray(args)) {
    const raw = document.createElement("pre");
    raw.textContent = typeof args === "string" ? args : JSON.stringify(args);
    return raw;
  }
  const list = document.createElement("dl");
  list.className = "rag-tool-arguments";
  for (const [name, value] of Object.entries(args)) {
    const label = document.createElement("dt");
    const field = document.createElement("dd");
    label.textContent = argumentLabels.get(name) ?? name;
    field.textContent = typeof value === "string" ? value : JSON.stringify(value);
    list.append(label, field);
  }
  return list;
}

function renderToolExcerpt(item) {
  const card = document.createElement("article");
  const title = document.createElement("a");
  const meta = document.createElement("p");
  const excerpt = document.createElement("p");
  const isSummary = item.source_kind === "summary";
  const anchor = isSummary ? "paper-summary" : item.chunk_index == null ? "paper-text" : `chunk-${item.chunk_index + 1}`;
  const score = typeof item.score === "number" ? ` · Сходство ${scoreFormat.format(item.score)}` : "";
  card.className = "rag-tool-excerpt";
  title.href = `${paperUrl(item.document_id)}#${anchor}`;
  title.textContent = `[${item.source_id}] ${item.paper_title || item.document_id}`;
  meta.className = "rag-tool-meta";
  meta.textContent = `${isSummary ? "Суммаризация" : "Статья"} · arXiv:${item.document_id} · [${item.source_start}, ${item.source_end})${score}`;
  excerpt.className = "chunk-text";
  excerpt.textContent = item.text;
  card.append(title, meta, excerpt);
  return card;
}

function renderToolCall(call) {
  const details = document.createElement("details");
  const heading = document.createElement("summary");
  const name = document.createElement("span");
  const code = document.createElement("code");
  const badge = document.createElement("span");
  const duration = document.createElement("span");
  const body = document.createElement("div");
  const inputLabel = document.createElement("h4");
  const outputLabel = document.createElement("h4");
  const output = document.createElement("div");
  const rawDetails = document.createElement("details");
  const rawLabel = document.createElement("summary");
  const raw = document.createElement("pre");
  const error = Object.hasOwn(call.result, "error");
  const matches = Array.isArray(call.result.matches) ? call.result.matches : null;
  const excerpts = matches ?? (typeof call.result.text === "string" ? [call.result] : []);
  details.className = "rag-tool";
  name.className = "rag-tool-name";
  name.textContent = `${call.position}. ${toolLabels.get(call.tool_name) ?? call.tool_name}`;
  code.textContent = call.tool_name;
  badge.className = "rag-tool-badge";
  badge.dataset.error = String(error);
  badge.textContent = error ? "Ошибка" : matches ? `Совпадений: ${matches.length}` : excerpts.length ? "Фрагмент" : "Готово";
  duration.className = "rag-tool-meta";
  duration.textContent = call.duration_ms >= 1000
    ? `${scoreFormat.format(call.duration_ms / 1000)} с` : `${call.duration_ms} мс`;
  heading.append(name, code, badge, duration);
  body.className = "rag-tool-body";
  inputLabel.textContent = "Аргументы";
  outputLabel.textContent = "Результат";
  output.className = error ? "rag-tool-error" : "paper-list";
  if (error) output.textContent = typeof call.result.error === "string" ? call.result.error : JSON.stringify(call.result.error);
  else if (excerpts.length) output.append(...excerpts.map(renderToolExcerpt));
  else output.textContent = "Совпадений нет.";
  rawLabel.textContent = "Исходные данные (JSON)";
  raw.textContent = JSON.stringify({ arguments: call.arguments, result: call.result }, null, 2);
  rawDetails.append(rawLabel, raw);
  body.append(inputLabel, renderToolArguments(toolArguments(call.arguments)), outputLabel, output, rawDetails);
  details.append(heading, body);
  return details;
}

function renderModelTurn(turn) {
  const details = document.createElement("details");
  const heading = document.createElement("summary");
  const name = document.createElement("span");
  const status = document.createElement("span");
  const body = document.createElement("div");
  details.className = "rag-tool";
  body.className = "rag-tool-body";
  name.className = "rag-tool-name";
  name.textContent = `Шаг ${turn.position} · ${turn.phase === "answer" ? "Формирование ответа" : "Поиск источников"}`;
  status.className = "rag-tool-badge";
  status.textContent = turn.reasoning.length ? "Есть рассуждение" : turn.reasoning_available
    ? "Текст рассуждения недоступен" : "Рассуждение не передано";
  heading.append(name, status);
  for (const part of turn.reasoning) {
    const label = document.createElement("h4");
    const text = document.createElement("pre");
    label.textContent = part.kind === "reasoning.summary" ? "Сводка рассуждения от провайдера" : "Рассуждение от провайдера";
    text.textContent = part.text;
    body.append(label, text);
  }
  if (turn.content) {
    const label = document.createElement("h4");
    const text = document.createElement("pre");
    label.textContent = "Сообщение модели";
    text.textContent = turn.content;
    body.append(label, text);
  }
  if (turn.requested_tools.length) {
    const label = document.createElement("h4");
    const calls = document.createElement("div");
    const requested = document.createElement("details");
    const requestedLabel = document.createElement("summary");
    const text = document.createElement("pre");
    label.textContent = "Вызовы инструментов";
    calls.className = "paper-list rag-step-calls";
    requestedLabel.textContent = "Запросы модели (JSON)";
    text.textContent = JSON.stringify(turn.requested_tools, null, 2);
    requested.append(requestedLabel, text);
    body.append(label, calls, requested);
  }
  details.append(heading, body);
  return details;
}

function rememberRagRun(runId) {
  const url = new URL(location.href);
  if (runId) url.searchParams.set("run", runId);
  else url.searchParams.delete("run");
  updateUrl(url, true);
}

function updateRagTrace(runId, running, calls = [], turns = []) {
  if (ragTrace.hidden) ragTrace.open = running;
  ragPermalink.href = `/?${new URLSearchParams({ mode: "rag", run: runId })}`;
  ragModelTurns.append(...turns.slice(ragModelTurns.children.length).map(renderModelTurn));
  // The API returns calls in execution order. Each step executes its requests before the next step.
  let callOffset = 0;
  for (const [index, turn] of turns.entries()) {
    const requestedCount = turn.requested_tools.length;
    if (requestedCount === 0) continue;
    const group = ragModelTurns.children[index].querySelector(".rag-step-calls");
    group.append(...calls.slice(callOffset + group.children.length, callOffset + requestedCount).map(renderToolCall));
    callOffset += requestedCount;
  }
  ragUnassignedCalls.append(...calls.slice(callOffset + ragUnassignedCalls.children.length).map(renderToolCall));
  ragUnassignedStep.hidden = ragUnassignedCalls.children.length === 0;
  ragModelHistory.hidden = turns.length === 0 && ragUnassignedStep.hidden;
  ragTraceEmpty.hidden = running || calls.length > 0;
  const status = running && !calls.length ? "Ожидаем первый результат инструмента."
    : `Вызовов инструментов: ${calls.length}${running ? " · поиск продолжается" : ""}`;
  if (ragTraceStatus.textContent !== status) ragTraceStatus.textContent = status;
  ragTrace.hidden = false;
}

async function showRagRun(runId, signal, allowPending = false) {
  const response = await fetch(`/api/rag/runs/${encodeURIComponent(runId)}`, { signal, cache: "no-store" });
  if (signal.aborted) return;
  if (response.status === 404 || response.status === 422) {
    if (!allowPending || response.status === 422) searchStatus.textContent = "Запуск RAG не найден.";
    return response.status === 404 ? "missing" : undefined;
  }
  if (!response.ok) throw new Error(`RAG history returned ${response.status}`);
  const run = await response.json();
  if (signal.aborted) return;

  if (ragTrace.hidden && !allowPending) searchQuery.value = run.query;
  updateRagTrace(run.run_id, run.status === "running", run.tool_calls, run.model_turns);
  if (run.status === "answered") {
    renderRagAnswer(run.answer);
    searchStatus.textContent = "Ответ готов.";
  } else if (run.status === "insufficient_evidence") {
    searchStatus.textContent = "В найденных фрагментах недостаточно данных для ответа.";
  } else if (run.status === "failed") {
    searchStatus.textContent = `Запуск завершился ошибкой. ${run.error ?? ""}`;
  } else {
    searchStatus.textContent = searchModes.rag.pending;
  }
  return run.status;
}

function waitForRagUpdate(signal) {
  return new Promise((resolve) => {
    const stop = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", stop);
      resolve();
    };
    const timer = setTimeout(stop, 1000);
    signal.addEventListener("abort", stop, { once: true });
    if (signal.aborted) stop();
  });
}

async function followRagRun(runId, signal, allowPending = false) {
  while (!signal.aborted) {
    const status = await showRagRun(runId, signal, allowPending);
    if (status !== "running" && !(allowPending && status === "missing")) return;
    await waitForRagUpdate(signal);
  }
}

async function postRagSearch(query, signal) {
  const runId = crypto.randomUUID();
  rememberRagRun(runId);
  updateRagTrace(runId, true);
  const polling = new AbortController();
  const stopPolling = () => polling.abort();
  signal.addEventListener("abort", stopPolling, { once: true });
  const pending = fetch(searchModes.rag.endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, run_id: runId }),
    signal,
  });
  const live = followRagRun(runId, polling.signal, true).catch(() => {
    if (!polling.signal.aborted) ragTraceStatus.textContent = "История обновится после завершения запроса.";
  });
  try {
    const response = await pending;
    if (!response.ok && !response.headers.has("X-RAG-Run-Id") && !signal.aborted) {
      rememberRagRun(null);
      ragTrace.hidden = true;
    }
    return response;
  } finally {
    stopPolling();
    signal.removeEventListener("abort", stopPolling);
    await live;
  }
}

async function loadSearch(query, mode, moveFocus, runId) {
  const settings = searchModes[mode];
  searchRequest?.abort();
  const request = new AbortController();
  searchRequest = request;
  searchSubmit.disabled = true;
  searchTitle.textContent = settings.title;
  searchChunks.replaceChildren();
  searchResults.setAttribute("aria-busy", String(mode !== "rag"));
  searchStatus.textContent = runId ? "Загружаем историю запуска…" : settings.pending;

  try {
    if (runId) {
      await followRagRun(runId, request.signal);
      if (moveFocus && !request.signal.aborted) searchTitle.focus();
      return;
    }
    const response = mode === "rag" ? await postRagSearch(query, request.signal) : await fetch(settings.endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
      signal: request.signal,
    });
    if (request.signal.aborted) return;
    const failedRunId = mode === "rag" && response.headers.get("X-RAG-Run-Id");
    if (!response.ok && failedRunId) {
      rememberRagRun(failedRunId);
      await showRagRun(failedRunId, request.signal);
      if (moveFocus && !request.signal.aborted) searchTitle.focus();
      return;
    }
    if (response.status === 422) {
      searchStatus.textContent = mode === "rag" ? "Проверь запрос и попробуй ещё раз."
        : "Проверь запрос и попробуй ещё раз. Максимум 1000 символов.";
      return;
    }
    if (response.status === 503 && mode === "vector") {
      searchStatus.textContent = "Сервис эмбеддингов или векторный индекс сейчас недоступен.";
      return;
    }
    if (response.status === 503 && mode === "rag") {
      searchStatus.textContent = "Модель для RAG сейчас недоступна.";
      return;
    }
    if (response.status === 504 && mode === "rag") {
      searchStatus.textContent = "Модель не успела подготовить ответ.";
      return;
    }
    if (!response.ok) {
      throw new Error(`Search request returned ${response.status}`);
    }

    const result = await response.json();
    if (request.signal.aborted) return;
    if (mode === "rag") {
      rememberRagRun(result.run_id);
      await showRagRun(result.run_id, request.signal);
    } else {
      searchChunks.replaceChildren(...result.map(renderSearchResult));
      searchStatus.textContent = result.length ? `Найдено фрагментов: ${result.length}` : settings.empty;
    }
    if (moveFocus && !request.signal.aborted) searchTitle.focus();
  } catch {
    if (!request.signal.aborted) {
      searchStatus.textContent = "Не удалось выполнить поиск. Попробуй ещё раз.";
    }
  } finally {
    if (!request.signal.aborted) {
      searchRequest = undefined;
      searchSubmit.disabled = false;
      searchResults.setAttribute("aria-busy", "false");
    }
  }
}

searchForm.addEventListener("change", (event) => {
  if (event.target.name === "mode") updateSearchInput(searchMode.value);
});

searchForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const query = searchQuery.value.trim();
  if (!query) {
    searchQuery.value = "";
    searchQuery.reportValidity();
    return;
  }

  const url = new URL(location.href);
  url.searchParams.set("query", query);
  url.searchParams.set("mode", searchMode.value);
  url.searchParams.delete("run");
  updateUrl(url);
  showLocation(true);
});

backToCatalog.addEventListener("click", () => {
  const url = new URL(location.href);
  url.searchParams.delete("query");
  url.searchParams.delete("mode");
  url.searchParams.delete("run");
  updateUrl(url);
  showLocation(true);
});

function goToPage(page) {
  setPageInUrl(page);
  loadPage(page, true);
}

previous.addEventListener("click", () => goToPage(currentPage - 1));
next.addEventListener("click", () => goToPage(currentPage + 1));
retry.addEventListener("click", () => loadPage(requestedPage, true));
catalogFilters.addEventListener("input", () => dateTo.setCustomValidity(""));
catalogFilters.addEventListener("submit", (event) => {
  event.preventDefault();
  if (dateFrom.value && dateTo.value && dateFrom.value > dateTo.value) {
    dateTo.setCustomValidity("Дата окончания должна быть не раньше даты начала.");
    dateTo.reportValidity();
    return;
  }
  applyFilters();
});
document.querySelector("#clear-filters").addEventListener("click", () => {
  catalogFilters.reset();
  dateTo.setCustomValidity("");
  applyFilters();
});
window.addEventListener("popstate", () => showLocation());
window.addEventListener("pageshow", (event) => {
  if (event.persisted && !catalog.hidden) showLocation();
});
showLocation();
