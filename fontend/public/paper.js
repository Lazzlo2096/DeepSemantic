import { arxivPath, arxivUrl } from "./arxiv.js";
import { authFetch, loadCurrentUser } from "./auth.js?v=3";
import { renderPaperIndexes, renderSummaryStatus } from "./paper-index.js?v=4";
import MarkdownIt from "./vendor/markdown-it.mjs";
import { setMarkdownContent } from "./math-render.js?v=1";

const markdown = new MarkdownIt({ html: false });
const mathOptions = {
  engine: window.katex,
  delimiters: "dollars",
  katexOptions: { trust: false, throwOnError: false, maxExpand: 500, maxSize: 25 },
};
const summaryMarkdown = new MarkdownIt({ html: false }).use(window.texmath, mathOptions);
const articleMarkdown = new MarkdownIt({ html: true }).use(window.texmath, mathOptions);
const tableMarkdown = new MarkdownIt("zero").use(window.texmath, mathOptions);
const tableTextTags = new Set(["caption", "th", "td"]);
const tableTags = new Set([...tableTextTags, "thead", "tbody", "tfoot", "tr"]);
// The arXiv export can include memoir layout commands before the article heading.
const layoutPreamble = /^\\isopage\s+(?:\[\d+\]\s+)?\\setlrmargins[^\n]*\n\\checkandfixthelayout\s+(?=# )/;
const query = new URLSearchParams(location.search);
const arxivId = query.get("id");
const fileId = query.get("file_id");
const spaceId = query.get("space_id");
const status = document.querySelector("#status");
const pageStatus = document.querySelector("#paper-load-status");
const list = document.querySelector("#paper-chunks");
const template = document.querySelector("#chunk-template");
const chunksPanel = document.querySelector("#paper-chunks-section");
const debugControl = document.querySelector("#debug-mode-control");
const debugMode = document.querySelector("#debug-mode");
const debugStorageKey = "deepsemantic_debug_mode";
let chunksUrl;
let chunksLoaded = false;
let chunksRequest;

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
    if (tableTextTags.has(child.localName)) {
      cell.innerHTML = tableMarkdown.renderInline(child.textContent);
    } else {
      copyTableChildren(child, cell);
    }
    target.append(cell);
  }
}

// Keep the table structure and render formulas in its text without copying the source HTML.
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
  const kind = chunk.source_kind === "summary" ? "саммари" : "статьи";
  card.querySelector(".chunk-heading").textContent = `Чанк ${kind} №${chunkNumber}`;
  setMarkdownContent(card.querySelector(".chunk-text"), chunk.text);
  return card;
}

function savedDebugMode() {
  try {
    return localStorage.getItem(debugStorageKey) === "true";
  } catch {
    return false;
  }
}

function saveDebugMode(enabled) {
  try {
    localStorage.setItem(debugStorageKey, String(enabled));
  } catch {
    // The switch still works for this page when persistent storage is unavailable.
  }
}

async function loadChunks() {
  if (!chunksUrl || chunksLoaded || chunksRequest) return chunksRequest;
  status.textContent = "Загружаем чанки…";
  list.setAttribute("aria-busy", "true");
  chunksRequest = authFetch(chunksUrl, { cache: "no-store" });
  try {
    const response = await chunksRequest;
    if (!response.ok) throw new Error(`Chunks request returned ${response.status}`);
    const chunks = await response.json();
    list.replaceChildren(...chunks.map(renderChunk));
    status.textContent = chunks.length ? `Всего чанков: ${chunks.length}` : "У этой статьи пока нет чанков.";
    chunksLoaded = true;
    if (location.hash.startsWith("#chunk-")) document.getElementById(location.hash.slice(1))?.scrollIntoView();
  } catch {
    status.textContent = "Не удалось загрузить чанки статьи.";
  } finally {
    chunksRequest = undefined;
    list.setAttribute("aria-busy", "false");
  }
}

function setDebugMode(enabled) {
  debugMode.checked = enabled;
  chunksPanel.hidden = !enabled;
  saveDebugMode(enabled);
  if (enabled) loadChunks();
}

async function initializeDebugMode() {
  try {
    const user = await loadCurrentUser();
    if (user?.role !== "admin") return;
    debugControl.hidden = false;
    setDebugMode(savedDebugMode());
  } catch {
    // Debug controls stay unavailable when the current role cannot be verified.
  }
}

debugMode.addEventListener("change", () => setDebugMode(debugMode.checked));

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
      result.innerHTML = articleMarkdown.render(source.replace(layoutPreamble, ""));
    } else {
      setMarkdownContent(result, source);
    }
    result.hidden = false;
    feedback.textContent = "";
  } catch {
    feedback.textContent = "Не удалось загрузить полный текст.";
  }
}

function loadSummary(url, hasText, isHtml, summaryIndex) {
  const panel = document.querySelector("#paper-summary");
  const button = document.querySelector("#summarize");
  const feedback = document.querySelector("#summary-status");
  const result = document.querySelector("#summary-result");
  const summaryBadge = document.querySelector('[data-status-kind="summary"]');
  panel.hidden = false;
  let refreshTimer;

  function render(job) {
    const busy = job?.status === "queued" || job?.status === "running";
    const deferred = job?.status === "deferred";
    button.disabled = !(hasText || (job && isHtml)) || busy || deferred;
    document.querySelector("#summary-deferred-link").hidden = !deferred;
    button.textContent = job?.status === "failed" ? "Повторить" : "Суммаризировать";
    renderSummaryStatus(summaryBadge, summaryIndex, job?.status === "completed");
    if (job?.status === "completed") {
      result.innerHTML = summaryMarkdown.render(job.summary);
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
    } else if (deferred) {
      feedback.textContent = "Создание саммари отложено для всей системы. Оно начнётся после включения ветки на странице «Воркеры».";
    } else if (job?.status === "failed") {
      feedback.textContent = job.last_error || "Не удалось суммаризировать статью.";
    } else {
      feedback.textContent = hasText ? "Суммаризация ещё не создана." : "Полный текст статьи пока недоступен.";
    }
    return busy || deferred || (!job && isHtml);
  }

  async function refresh(method = "GET", polling = false) {
    try {
      const response = await authFetch(url, { method, cache: "no-store" });
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
  const url = `/api/papers/${arxivPath(id)}`;
  try {
    const paperResponse = await fetch(url);
    if (paperResponse.status === 404) {
      pageStatus.textContent = "Статья не найдена.";
      pageStatus.hidden = false;
      return;
    }
    if (!paperResponse.ok) throw new Error("Could not load the paper");

    const paper = await paperResponse.json();
    renderPaperIndexes(document, paper);
    const title = paper.title || paper.arxiv_id;
    document.title = `${title} · DeepSemantic`;
    setMarkdownContent(document.querySelector("#paper-title"), title, { inline: true });
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
    loadSummary(`${url}/summary`, paper.status === "texted" && textKey.endsWith(".md"), isHtml, paper.summary_index);
    const textLoading = loadText(url, textKey.endsWith(".md") || textKey.endsWith(".txt"));
    chunksUrl = `${url}/chunks`;
    if (!debugControl.hidden && debugMode.checked) loadChunks();
    await textLoading;
    if (["#paper-summary", "#paper-text"].includes(location.hash)) {
      document.getElementById(location.hash.slice(1))?.scrollIntoView();
    }
  } catch {
    pageStatus.textContent = "Не удалось загрузить статью. Попробуй обновить страницу.";
    pageStatus.hidden = false;
  }
}

async function loadUploadedFile(id, currentSpaceId) {
  if (!currentSpaceId) {
    pageStatus.textContent = "В ссылке отсутствует Space документа.";
    pageStatus.hidden = false;
    return;
  }
  const url = `/api/spaces/${encodeURIComponent(currentSpaceId)}/files/${encodeURIComponent(id)}`;
  try {
    const response = await authFetch(url, { cache: "no-store" });
    if (response.status === 404) {
      pageStatus.textContent = "Документ не найден.";
      pageStatus.hidden = false;
      return;
    }
    if (!response.ok) throw new Error(`Document request returned ${response.status}`);
    const disposition = response.headers.get("Content-Disposition") || "";
    const encodedName = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
    const filename = encodedName ? decodeURIComponent(encodedName) : "Документ";
    const contentType = response.headers.get("Content-Type") || "";
    const processingResponse = await authFetch(`${url}/processing`, { cache: "no-store" });
    const processing = processingResponse.ok ? await processingResponse.json() : null;
    const textPanel = document.querySelector("#paper-text");
    const textStatus = document.querySelector("#text-status");
    const result = document.querySelector("#text-result");
    document.title = `${filename} · DeepSemantic`;
    document.querySelector("#paper-id").textContent = "Документ Space";
    document.querySelector("#paper-title").textContent = filename;
    document.querySelector("#catalog-back").href = `/space.html?id=${encodeURIComponent(currentSpaceId)}`;
    document.querySelector("#catalog-back").textContent = "← Вернуться к Space";
    document.querySelector("#summarize").hidden = true;
    document.querySelector("#summary-deferred-link").hidden = true;
    chunksUrl = `${url}/chunks`;
    chunksPanel.hidden = debugControl.hidden || !debugMode.checked;
    if (!chunksPanel.hidden) loadChunks();
    if (processing) renderUploadedFileProcessing(processing);
    const download = document.querySelector("#paper-html");
    download.href = url;
    download.textContent = "Открыть исходный файл";
    download.target = "_blank";
    download.removeAttribute("download");
    download.hidden = false;
    textPanel.hidden = false;
    textStatus.textContent = "Загружаем документ…";
    if (contentType.startsWith("text/html")) {
      const frame = document.createElement("iframe");
      frame.src = url;
      frame.title = filename;
      frame.setAttribute("sandbox", "");
      frame.style.width = "100%";
      frame.style.minHeight = "70vh";
      frame.style.border = "0";
      result.replaceChildren(frame);
    } else if (contentType.startsWith("text/") || contentType.includes("json")) {
      setMarkdownContent(result, await response.text());
    } else {
      result.textContent = "Предпросмотр этого типа файла недоступен. Используйте ссылку «Открыть исходный файл».";
    }
    result.hidden = false;
    textStatus.textContent = "";
  } catch {
    pageStatus.textContent = "Не удалось загрузить документ. Попробуй обновить страницу.";
    pageStatus.hidden = false;
  }
}

function renderUploadedFileProcessing(processing) {
  const summaryPanel = document.querySelector("#paper-summary");
  const summaryStatusText = document.querySelector("#summary-status");
  const summaryResult = document.querySelector("#summary-result");
  const articleStatus = document.querySelector('[data-status-kind="article-index"]');
  const summaryStatus = document.querySelector('[data-status-kind="summary"]');
  const article = processing.article_index;
  const summary = processing.summary_index;
  const textStatus = document.querySelector('[data-status-kind="text"]');
  const statusState = (value) => value === "completed" ? "ready" : value === "failed" ? "error" : "running";
  const setProcessingStatus = (row, label, state, value, detail) => {
    row.dataset.state = state;
    row.querySelector(".paper-status-label").textContent = label;
    row.querySelector(".paper-status-value").textContent = value;
    row.querySelector(".paper-status-icon").textContent = state === "ready" ? "✓" : state === "error" ? "!" : "◌";
    row.title = detail;
    row.setAttribute("aria-label", `${label}: ${value}`);
  };
  setProcessingStatus(textStatus, "Текст статьи", statusState(processing.text), processing.text === "completed" ? "Готово" : "В работе", "Состояние извлечения текста документа.");
  setProcessingStatus(articleStatus, "Индекс статьи", statusState(article.status), `${article.chunks_indexed}/${article.chunks_total} чанков`, "Количество чанков статьи, найденных в Qdrant.");
  setProcessingStatus(summaryStatus, "Саммари", processing.summary_available ? statusState(summary.status) : "pending", processing.summary_available ? `${summary.chunks_indexed}/${summary.chunks_total} чанков` : "Ожидает", "Количество чанков саммари, найденных в Qdrant.");
  summaryPanel.hidden = false;
  if (processing.summary) {
    summaryStatusText.textContent = "Саммари готово.";
    summaryResult.innerHTML = summaryMarkdown.render(processing.summary);
    summaryResult.hidden = false;
  } else {
    summaryStatusText.textContent = "Саммари ещё не создано.";
    summaryResult.replaceChildren();
    summaryResult.hidden = true;
  }
  const runtime = processing.runtime;
  document.querySelector("#paper-load-status").textContent = runtime?.status === "failed"
    ? "Обработка завершилась с ошибкой."
    : runtime?.status === "completed" && article.status === "completed" && summary.status === "completed"
      ? "Обработка завершена."
      : "Обработка выполняется…";
  document.querySelector("#paper-load-status").hidden = false;
}

initializeDebugMode();

if (fileId) {
  loadUploadedFile(fileId, spaceId);
} else if (arxivId) {
  loadPaper(arxivId);
} else {
  pageStatus.textContent = "В ссылке отсутствует идентификатор статьи.";
  pageStatus.hidden = false;
}
