import { arxivPath, arxivUrl } from "./arxiv.js";

const arxivId = new URLSearchParams(location.search).get("id");
const status = document.querySelector("#status");
const list = document.querySelector("#paper-chunks");
const template = document.querySelector("#chunk-template");

if (document.referrer) {
  const referrer = new URL(document.referrer);
  if (referrer.origin === location.origin && referrer.pathname === "/") {
    document.querySelector("#catalog-back").href = referrer.href;
  }
}

function renderChunk(chunk) {
  const card = template.content.cloneNode(true);
  const chunkNumber = chunk.chunk_index + 1;
  card.querySelector("article").id = `chunk-${chunkNumber}`;
  card.querySelector(".chunk-heading").textContent = `Чанк №${chunkNumber}`;
  card.querySelector(".chunk-text").textContent = chunk.text;
  return card;
}

function loadSummary(url, hasText) {
  const panel = document.querySelector("#paper-summary");
  const button = document.querySelector("#summarize");
  const feedback = document.querySelector("#summary-status");
  const result = document.querySelector("#summary-result");
  panel.hidden = false;

  function render(job) {
    const busy = job?.status === "queued" || job?.status === "running";
    button.disabled = !hasText || busy;
    button.textContent = job?.status === "failed" ? "Повторить" : "Суммаризировать";
    if (job?.status === "completed") {
      for (const field of result.querySelectorAll("[data-summary]")) {
        const value = job.summary[field.dataset.summary];
        if (Array.isArray(value)) {
          field.replaceChildren(...(value.length ? value : ["В тексте не указано."]).map((entry) => {
            const item = document.createElement("li");
            item.textContent = entry;
            return item;
          }));
        } else {
          field.textContent = value;
        }
      }
      feedback.textContent = "Готово.";
      button.hidden = true;
      result.hidden = false;
    } else if (job?.status === "queued") {
      feedback.textContent = "Задание в очереди.";
    } else if (job?.status === "running") {
      if (!job.parts_total) {
        feedback.textContent = "Читаем текст статьи…";
      } else if (job.parts_completed === job.parts_total) {
        feedback.textContent = "Готовим итоговую суммаризацию…";
      } else {
        feedback.textContent = `Обработано частей: ${job.parts_completed} из ${job.parts_total}.`;
      }
    } else if (job?.status === "failed") {
      feedback.textContent = job.last_error || "Не удалось суммаризировать статью.";
    } else {
      feedback.textContent = hasText ? "Суммаризация ещё не создана." : "Полный текст статьи пока недоступен.";
    }
    return busy;
  }

  async function refresh(method = "GET", polling = false) {
    try {
      const response = await fetch(url, { method, cache: "no-store" });
      if (!response.ok) throw new Error("Could not load the summary");
      if (render(await response.json())) {
        setTimeout(() => refresh("GET", true), 3000);
      }
    } catch {
      feedback.textContent = "Не удалось обновить статус суммаризации.";
      if (polling) {
        setTimeout(() => refresh("GET", true), 5000);
      } else {
        button.disabled = !hasText;
      }
    }
  }

  button.addEventListener("click", () => {
    button.disabled = true;
    feedback.textContent = "Отправляем задание…";
    refresh("POST");
  });
  refresh();
}

async function loadPaper(id) {
  list.setAttribute("aria-busy", "true");
  const url = `/api/papers/${arxivPath(id)}`;
  try {
    const [paperResponse, chunksResponse] = await Promise.all([fetch(url), fetch(`${url}/chunks`)]);
    if (paperResponse.status === 404) {
      status.textContent = "Статья не найдена.";
      return;
    }
    if (!paperResponse.ok || !chunksResponse.ok) {
      throw new Error("Could not load the paper and its chunks");
    }

    const [paper, chunks] = await Promise.all([paperResponse.json(), chunksResponse.json()]);
    const title = paper.title || paper.arxiv_id;
    document.title = `${title} · DeepSemantic`;
    document.querySelector("#paper-title").textContent = title;
    document.querySelector("#paper-id").textContent = `arXiv:${paper.arxiv_id}`;
    const authors = document.querySelector("#paper-authors");
    authors.textContent = paper.authors.join(", ");
    authors.hidden = !authors.textContent;
    const original = document.querySelector("#paper-arxiv");
    original.href = arxivUrl(paper.arxiv_id);
    original.hidden = false;

    loadSummary(`${url}/summary`, Boolean(paper.text_object_key));
    list.replaceChildren(...chunks.map(renderChunk));
    if (location.hash.startsWith("#chunk-")) {
      document.getElementById(location.hash.slice(1))?.scrollIntoView();
    }
    status.textContent = chunks.length ? `Всего чанков: ${chunks.length}` : "У этой статьи пока нет чанков.";
  } catch {
    status.textContent = "Не удалось загрузить статью. Попробуй обновить страницу.";
  } finally {
    list.setAttribute("aria-busy", "false");
  }
}

if (arxivId) {
  loadPaper(arxivId);
} else {
  status.textContent = "В ссылке отсутствует идентификатор статьи.";
}
