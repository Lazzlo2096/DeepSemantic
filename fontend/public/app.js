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
const searchForm = document.querySelector("#search-form");
const searchQuery = document.querySelector("#search-query");
const searchSubmit = document.querySelector("#search-submit");
const searchResults = document.querySelector("#search-results");
const searchStatus = document.querySelector("#search-status");
const searchChunks = document.querySelector("#search-chunks");
const searchTemplate = document.querySelector("#search-result-template");
const backToCatalog = document.querySelector("#back-to-catalog");
const scoreFormat = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 3 });

let currentPage = 0;
let requestedPage = 0;
let hasNext = false;

function arxivUrl(arxivId) {
  const path = arxivId.split("/").map(encodeURIComponent).join("/");
  return `https://arxiv.org/abs/${path}`;
}

function renderPaper(paper) {
  const card = template.content.cloneNode(true);
  const title = card.querySelector(".paper-title");
  const date = card.querySelector(".paper-date");
  const stage = card.querySelector(".paper-stage");

  title.textContent = paper.title || paper.arxiv_id;
  title.href = arxivUrl(paper.arxiv_id);
  card.querySelector(".paper-id").textContent = `arXiv:${paper.arxiv_id}`;
  date.dateTime = paper.submitted_at;
  date.textContent = dateFormat.format(new Date(paper.submitted_at));
  stage.dataset.stage = paper.status ?? "";
  stage.textContent = stageLabels.get(paper.status) ?? "Статус неизвестен";
  card.querySelector(".paper-authors").textContent = paper.authors.join(", ");
  card.querySelector(".paper-abstract").textContent = paper.abstract || "Аннотация отсутствует.";
  return card;
}

async function loadPage(page, moveFocus = false) {
  requestedPage = page;
  previous.disabled = true;
  next.disabled = true;
  retry.hidden = true;
  status.textContent = "Загружаем статьи…";
  list.setAttribute("aria-busy", "true");

  try {
    const offset = page * PAGE_SIZE;
    const response = await fetch(`/api/papers?limit=${PAGE_SIZE + 1}&offset=${offset}`);
    if (!response.ok) {
      throw new Error(`Catalog request returned ${response.status}`);
    }

    const papers = await response.json();
    const visiblePapers = papers.slice(0, PAGE_SIZE);
    list.replaceChildren(...visiblePapers.map(renderPaper));
    currentPage = page;
    hasNext = papers.length > PAGE_SIZE;
    pageNumber.textContent = `Страница ${page + 1}`;
    pagination.hidden = page === 0 && !hasNext;
    status.textContent = visiblePapers.length
      ? `Записи ${offset + 1}–${offset + visiblePapers.length}`
      : page === 0 ? "В каталоге пока нет статей." : "На этой странице нет статей.";

    if (moveFocus) {
      resultsTitle.focus();
    }
  } catch {
    status.textContent = "Не удалось загрузить статьи. Попробуй ещё раз.";
    retry.hidden = false;
  } finally {
    previous.disabled = currentPage === 0;
    next.disabled = !hasNext;
    list.setAttribute("aria-busy", "false");
  }
}

function renderSearchResult(chunk) {
  const card = searchTemplate.content.cloneNode(true);
  const title = card.querySelector(".search-paper");
  title.textContent = chunk.paper_title || chunk.arxiv_id || "Фрагмент без статьи";
  if (chunk.arxiv_id) {
    title.href = arxivUrl(chunk.arxiv_id);
  }
  card.querySelector(".search-fragment").textContent = `Чанк №${chunk.chunk_id}`;
  card.querySelector(".search-score").textContent = `Сходство ${scoreFormat.format(chunk.score)}`;
  card.querySelector(".search-excerpt").textContent = chunk.text;
  return card;
}

searchForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = searchQuery.value.trim();
  if (!query) {
    searchQuery.value = "";
    searchQuery.reportValidity();
    return;
  }

  catalog.hidden = true;
  searchResults.hidden = false;
  searchSubmit.disabled = true;
  backToCatalog.disabled = true;
  searchChunks.replaceChildren();
  searchChunks.setAttribute("aria-busy", "true");
  searchStatus.textContent = "Ищем похожие фрагменты…";

  try {
    const response = await fetch("/api/chunks/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    });
    if (response.status === 422) {
      searchStatus.textContent = "Запрос слишком длинный для поиска. Сократи его и попробуй снова.";
      return;
    }
    if (!response.ok) {
      throw new Error(`Search request returned ${response.status}`);
    }

    const matches = await response.json();
    searchChunks.replaceChildren(...matches.map(renderSearchResult));
    searchStatus.textContent = matches.length
      ? `Найдено фрагментов: ${matches.length}`
      : "Похожие фрагменты не найдены. Поиск работает по индексированным чанкам.";
    document.querySelector("#search-title").focus();
  } catch {
    searchStatus.textContent = "Не удалось выполнить поиск. Попробуй ещё раз.";
  } finally {
    searchSubmit.disabled = false;
    backToCatalog.disabled = false;
    searchChunks.setAttribute("aria-busy", "false");
  }
});

backToCatalog.addEventListener("click", () => {
  searchResults.hidden = true;
  catalog.hidden = false;
  resultsTitle.focus();
});

previous.addEventListener("click", () => loadPage(currentPage - 1, true));
next.addEventListener("click", () => loadPage(currentPage + 1, true));
retry.addEventListener("click", () => loadPage(requestedPage, true));
loadPage(0);
