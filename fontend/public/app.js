import { arxivUrl } from "./arxiv.js";

const PAGE_SIZE = 12;
const stageLabels = new Map([
  ["downloaded", "PDF скачан"],
  ["texted", "Текст извлечён"],
  ["chunked", "Чанки готовы"],
  ["embeddinged", "Эмбеддинги готовы"],
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
const scoreFormat = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 3 });

let currentPage = 0;
let requestedPage = 0;
let pageCount = 0;
let appliedFilters = new URLSearchParams();
let catalogRequest;
let searchRequest;

for (const [value, label] of stageLabels) {
  statusFilter.add(new Option(label, value));
}

function linkPaper(card, arxivId) {
  card.querySelector(".paper-title").href = `/paper.html?${new URLSearchParams({ id: arxivId })}`;
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
      : params.has("status") || params.has("date_from") || params.has("date_to")
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
