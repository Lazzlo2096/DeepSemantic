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
const ragAnswer = document.querySelector("#rag-answer");
const ragQuery = document.querySelector("#rag-query");
const scoreFormat = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 3 });
const searchModes = {
  rag: {
    endpoint: "/api/rag/mock",
    title: "Поиск по RAG",
    pending: "Отправляем запрос в mock LLM…",
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

function showLocation(moveFocus = false) {
  const params = new URLSearchParams(location.search);
  const selectedMode = params.get("mode");
  const mode = Object.hasOwn(searchModes, selectedMode) ? selectedMode : "vector";
  searchMode.value = mode;
  searchQuery.placeholder = searchModes[mode].placeholder;
  searchQuery.value = params.get("query") ?? "";
  const query = searchQuery.value.trim();
  catalog.hidden = Boolean(query);
  searchResults.hidden = !query;
  ragResult.hidden = true;

  if (query) {
    catalogRequest?.abort();
    catalogRequest = undefined;
    loadSearch(query, mode, moveFocus);
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

async function loadSearch(query, mode, moveFocus) {
  const settings = searchModes[mode];
  searchRequest?.abort();
  const request = new AbortController();
  searchRequest = request;
  searchSubmit.disabled = true;
  searchTitle.textContent = settings.title;
  searchChunks.replaceChildren();
  searchResults.setAttribute("aria-busy", "true");
  searchStatus.textContent = settings.pending;

  try {
    const response = await fetch(settings.endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
      signal: request.signal,
    });
    if (request.signal.aborted) return;
    if (response.status === 422) {
      searchStatus.textContent = "Проверь запрос и попробуй ещё раз. Максимум 1000 символов.";
      return;
    }
    if (response.status === 503 && mode === "vector") {
      searchStatus.textContent = "Сервис эмбеддингов или векторный индекс сейчас недоступен.";
      return;
    }
    if (!response.ok) {
      throw new Error(`Search request returned ${response.status}`);
    }

    const result = await response.json();
    if (request.signal.aborted) return;
    if (mode === "rag") {
      ragAnswer.textContent = result.answer;
      ragQuery.textContent = result.query;
      ragResult.hidden = false;
      searchStatus.textContent = "Запрос обработан в деморежиме.";
    } else {
      searchChunks.replaceChildren(...result.map(renderSearchResult));
      searchStatus.textContent = result.length ? `Найдено фрагментов: ${result.length}` : settings.empty;
    }
    if (moveFocus) searchTitle.focus();
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
  if (event.target.name === "mode") searchQuery.placeholder = searchModes[searchMode.value].placeholder;
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
  updateUrl(url);
  showLocation(true);
});

backToCatalog.addEventListener("click", () => {
  const url = new URL(location.href);
  url.searchParams.delete("query");
  url.searchParams.delete("mode");
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
