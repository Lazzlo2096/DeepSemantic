const PAGE_SIZE = 12;
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

let currentPage = 0;
let requestedPage = 0;
let hasNext = false;

function renderPaper(paper) {
  const card = template.content.cloneNode(true);
  const title = card.querySelector(".paper-title");
  const date = card.querySelector(".paper-date");
  const idPath = paper.arxiv_id.split("/").map(encodeURIComponent).join("/");

  title.textContent = paper.title || paper.arxiv_id;
  title.href = `https://arxiv.org/abs/${idPath}`;
  card.querySelector(".paper-id").textContent = `arXiv:${paper.arxiv_id}`;
  date.dateTime = paper.submitted_at;
  date.textContent = dateFormat.format(new Date(paper.submitted_at));
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

previous.addEventListener("click", () => loadPage(currentPage - 1, true));
next.addEventListener("click", () => loadPage(currentPage + 1, true));
retry.addEventListener("click", () => loadPage(requestedPage, true));
loadPage(0);
