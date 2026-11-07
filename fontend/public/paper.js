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
  card.querySelector(".chunk-heading").textContent = `Чанк №${chunk.chunk_index + 1}`;
  card.querySelector(".chunk-text").textContent = chunk.text;
  return card;
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

    list.replaceChildren(...chunks.map(renderChunk));
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
