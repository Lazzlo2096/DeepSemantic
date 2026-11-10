import { arxivPath, arxivUrl } from "./arxiv.js";
import MarkdownIt from "./vendor/markdown-it.mjs";

const markdown = new MarkdownIt({ html: false });
const articleMarkdown = new MarkdownIt({ html: true });
const tableTags = new Set(["caption", "thead", "tbody", "tfoot", "tr", "th", "td"]);
const arxivId = new URLSearchParams(location.search).get("id");
const status = document.querySelector("#status");
const list = document.querySelector("#paper-chunks");
const template = document.querySelector("#chunk-template");

function copyTableChildren(source, target) {
  for (const child of source.childNodes) {
    if (!tableTags.has(child.localName)) {
      if (child.nodeType !== Node.COMMENT_NODE) target.append(child.textContent);
      continue;
    }
    const cell = document.createElement(child.localName);
    for (const attribute of ["rowspan", "colspan"]) {
      const value = child.getAttribute(attribute);
      if (/^[0-9]{1,4}$/.test(value)) cell.setAttribute(attribute, value);
    }
    copyTableChildren(child, cell);
    target.append(cell);
  }
}

// The stored Markdown uses HTML for merged table cells. Copy only table structure and text.
articleMarkdown.renderer.rules.html_block = (tokens, index) => {
  const raw = tokens[index].content;
  if (!/^\s*<table(?:\s|>)[\s\S]*<\/table>\s*$/i.test(raw)) return markdown.utils.escapeHtml(raw);
  const parsed = document.createElement("template");
  parsed.innerHTML = raw;
  const source = parsed.content.querySelector("table");
  if (!source) return markdown.utils.escapeHtml(raw);
  const table = document.createElement("table");
  copyTableChildren(source, table);
  return table.outerHTML;
};
articleMarkdown.renderer.rules.html_inline = (tokens, index) => markdown.utils.escapeHtml(tokens[index].content);

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

async function loadText(url, available) {
  const panel = document.querySelector("#paper-text");
  const feedback = document.querySelector("#text-status");
  const result = document.querySelector("#text-result");
  panel.hidden = false;
  if (!available) {
    feedback.textContent = "Полный текст пока недоступен.";
    return;
  }

  feedback.textContent = "Загружаем полный текст…";
  try {
    const response = await fetch(`${url}/text`);
    if (response.status === 404) {
      feedback.textContent = "Полный текст пока недоступен.";
      return;
    }
    if (!response.ok) throw new Error(`Text request returned ${response.status}`);
    const source = await response.text();
    if (response.headers.get("Content-Type")?.startsWith("text/markdown")) {
      result.innerHTML = articleMarkdown.render(source);
    } else {
      const plain = document.createElement("pre");
      plain.className = "paper-plain-text";
      plain.textContent = source;
      result.replaceChildren(plain);
    }
    result.hidden = false;
    feedback.textContent = "";
  } catch {
    feedback.textContent = "Не удалось загрузить полный текст.";
  }
}

function loadSummary(url, hasText, isHtml) {
  const panel = document.querySelector("#paper-summary");
  const button = document.querySelector("#summarize");
  const feedback = document.querySelector("#summary-status");
  const result = document.querySelector("#summary-result");
  panel.hidden = false;
  let refreshTimer;

  function render(job) {
    const busy = job?.status === "queued" || job?.status === "running";
    button.disabled = !(hasText || (job && isHtml)) || busy;
    button.textContent = job?.status === "failed" ? "Повторить" : "Суммаризировать";
    if (job?.status === "completed") {
      result.innerHTML = markdown.render(job.summary);
      feedback.textContent = "Готово.";
      button.hidden = true;
      result.hidden = false;
    } else if (job?.status === "queued") {
      feedback.textContent = "Задание в очереди.";
    } else if (job?.status === "running") {
      if (!job.parts_total) {
        feedback.textContent = "Читаем текст статьи…";
      } else if (job.parts_total === 1) {
        feedback.textContent = "Суммаризируем полный текст статьи…";
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
    return busy || (!job && isHtml);
  }

  async function refresh(method = "GET", polling = false) {
    try {
      const response = await fetch(url, { method, cache: "no-store" });
      if (method === "POST" && response.status === 409) {
        const { detail } = await response.json();
        feedback.textContent = detail;
        button.disabled = !hasText;
        return;
      }
      if (!response.ok) throw new Error("Could not load the summary");
      if (render(await response.json())) {
        clearTimeout(refreshTimer);
        refreshTimer = setTimeout(() => refresh("GET", true), 3000);
      }
    } catch {
      feedback.textContent = "Не удалось обновить статус суммаризации.";
      if (polling) {
        clearTimeout(refreshTimer);
        refreshTimer = setTimeout(() => refresh("GET", true), 5000);
      } else {
        button.disabled = !hasText;
      }
    }
  }

  button.addEventListener("click", () => {
    clearTimeout(refreshTimer);
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

    const textKey = paper.text_object_key?.toLowerCase() ?? "";
    const isHtml = paper.object_key.toLowerCase().endsWith(".html");
    if (isHtml) {
      const download = document.querySelector("#paper-html");
      download.href = `${url}/html`;
      download.hidden = false;
    }
    loadSummary(`${url}/summary`, paper.status === "texted" && textKey.endsWith(".md"), isHtml);
    const textLoading = loadText(url, textKey.endsWith(".md") || textKey.endsWith(".txt"));
    list.replaceChildren(...chunks.map(renderChunk));
    status.textContent = chunks.length ? `Всего чанков: ${chunks.length}` : "У этой статьи пока нет чанков.";
    list.setAttribute("aria-busy", "false");
    await textLoading;
    if (["#paper-summary", "#paper-text"].includes(location.hash) || location.hash.startsWith("#chunk-")) {
      document.getElementById(location.hash.slice(1))?.scrollIntoView();
    }
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
