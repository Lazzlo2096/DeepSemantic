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
const searchQuery = document.querySelector("#search-query");
const searchSubmit = document.querySelector("#search-submit");
const searchResults = document.querySelector("#search-results");
const searchTitle = document.querySelector("#search-title");
const searchStatus = document.querySelector("#search-status");
const searchChunks = document.querySelector("#search-chunks");
const searchTemplate = document.querySelector("#search-result-template");
const backToCatalog = document.querySelector("#back-to-catalog");
const ragSubmit = document.querySelector("#rag-submit");
const ragStatus = document.querySelector("#rag-status");
const ragResult = document.querySelector("#rag-result");
const ragPoints = document.querySelector("#rag-points");
const ragSources = document.querySelector("#rag-sources");
const scoreFormat = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 3 });

let currentPage = 0;
let requestedPage = 0;
let pageCount = 0;
let appliedFilters = new URLSearchParams();
let catalogRequest;
let searchRequest;
let ragRequest;

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

function showLocation(moveFocus = false) {
  const params = new URLSearchParams(location.search);
  searchQuery.value = params.get("query") ?? "";
  const query = searchQuery.value.trim();
  catalog.hidden = Boolean(query);
  searchResults.hidden = !query;
  ragRequest?.abort();
  ragRequest = undefined;
  ragSubmit.disabled = true;
  ragStatus.textContent = "";
  ragResult.hidden = true;

  if (query) {
    catalogRequest?.abort();
    catalogRequest = undefined;
    loadSearch(query, moveFocus);
    return;
  }

  searchRequest?.abort();
  searchRequest = undefined;
  searchSubmit.disabled = false;
  searchChunks.setAttribute("aria-busy", "false");
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
  card.querySelector(".search-fragment").textContent = `Чанк №${chunk.chunk_id}`;
  card.querySelector(".search-score").textContent = `Сходство ${scoreFormat.format(chunk.score)}`;
  card.querySelector(".search-excerpt").textContent = chunk.text;
  return card;
}

async function loadSearch(query, moveFocus) {
  searchRequest?.abort();
  const request = new AbortController();
  searchRequest = request;
  searchSubmit.disabled = true;
  searchChunks.replaceChildren();
  searchChunks.setAttribute("aria-busy", "true");
  searchStatus.textContent = "Ищем похожие фрагменты…";

  try {
    const response = await fetch("/api/chunks/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
      signal: request.signal,
    });
    if (request.signal.aborted) return;
    if (response.status === 422) {
      searchStatus.textContent = "Запрос слишком длинный для поиска. Сократи его и попробуй снова.";
      return;
    }
    if (!response.ok) {
      throw new Error(`Search request returned ${response.status}`);
    }

    const matches = await response.json();
    if (request.signal.aborted) return;
    searchChunks.replaceChildren(...matches.map(renderSearchResult));
    ragSubmit.disabled = !matches.some((chunk) => chunk.arxiv_id);
    searchStatus.textContent = matches.length
      ? `Найдено фрагментов: ${matches.length}`
      : "Похожие фрагменты не найдены. Поиск работает по индексированным чанкам.";
    if (moveFocus) searchTitle.focus();
  } catch {
    if (!request.signal.aborted) {
      searchStatus.textContent = "Не удалось выполнить поиск. Попробуй ещё раз.";
    }
  } finally {
    if (!request.signal.aborted) {
      searchRequest = undefined;
      searchSubmit.disabled = false;
      searchChunks.setAttribute("aria-busy", "false");
    }
  }
}

function renderRagAnswer(answer) {
  const links = new Map();
  ragSources.replaceChildren(...answer.sources.map((source) => {
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    const article = document.createElement("a");
    const excerpt = document.createElement("p");
    const chunkNumber = source.chunk_index + 1;
    article.href = `${paperUrl(source.arxiv_id)}#chunk-${chunkNumber}`;
    article.textContent = `Открыть чанк №${chunkNumber} в статье`;
    article.className = "paper-title";
    summary.textContent = `[${source.id}] ${source.paper_title || source.arxiv_id}`;
    excerpt.className = "chunk-text";
    excerpt.textContent = source.text;
    details.append(summary, excerpt, article);
    links.set(source.id, article.href);
    return details;
  }));
  ragPoints.replaceChildren(...answer.points.map((point) => {
    const item = document.createElement("li");
    item.textContent = point.text;
    for (const id of new Set(point.source_ids)) {
      const reference = document.createElement("a");
      reference.href = links.get(id);
      reference.textContent = `[${id}]`;
      reference.setAttribute("aria-label", `Открыть источник ${id}`);
      item.append(" ", reference);
    }
    return item;
  }));
  ragResult.hidden = false;
}

ragSubmit.addEventListener("click", async () => {
  const query = new URLSearchParams(location.search).get("query");
  const request = new AbortController();
  ragRequest = request;
  ragSubmit.disabled = true;
  ragResult.hidden = true;
  ragStatus.textContent = "Готовим ответ по статьям…";

  try {
    const response = await fetch("/api/rag/answer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
      signal: request.signal,
    });
    if (request.signal.aborted) return;
    if (response.status === 503) {
      ragStatus.textContent = "Поиск или Ollama пока недоступны. Попробуй ещё раз.";
      return;
    }
    if (response.status === 504) {
      ragStatus.textContent = "Сервис не успел ответить. Попробуй ещё раз.";
      return;
    }
    if (!response.ok) throw new Error(`RAG request returned ${response.status}`);

    const answer = await response.json();
    if (request.signal.aborted) return;
    if (answer.status === "insufficient_evidence") {
      ragStatus.textContent = "В найденных фрагментах недостаточно данных для ответа.";
      return;
    }
    renderRagAnswer(answer);
    ragStatus.textContent = "Ответ готов.";
  } catch {
    if (!request.signal.aborted) ragStatus.textContent = "Не удалось получить ответ по статьям.";
  } finally {
    if (!request.signal.aborted) {
      ragRequest = undefined;
      ragSubmit.disabled = false;
    }
  }
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
  updateUrl(url);
  showLocation(true);
});

backToCatalog.addEventListener("click", () => {
  const url = new URL(location.href);
  url.searchParams.delete("query");
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
