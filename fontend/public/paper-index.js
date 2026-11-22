const stateIcons = { ready: "✓", running: "◌", pending: "—", unavailable: "!", error: "!" };

function updateStatus(row, label, state, value, detail) {
  if (!row) return;
  row.dataset.state = state;
  row.querySelector(".paper-status-label").textContent = label;
  row.querySelector(".paper-status-value").textContent = value;
  row.querySelector(".paper-status-icon").textContent = stateIcons[state] ?? "—";
  row.title = detail;
  row.setAttribute("aria-label", `${label}: ${value}`);
}

function countText(index) {
  if (!index || index.chunks_total == null) return "";
  const indexed = index.chunks_in_qdrant ?? index.chunks_indexed;
  return indexed == null ? `${index.chunks_total} чанков` : `${indexed}/${index.chunks_total} чанков`;
}

export function renderSummaryStatus(summaryStatus, summaryIndex, hasSummary) {
  const status = summaryIndex?.status;
  const counts = countText(summaryIndex);
  if (status === "completed") {
    updateStatus(summaryStatus, "Саммари", "ready", `Готово · ${counts || "индексировано"}`, "Саммари создано, все актуальные чанки найдены в Qdrant.");
  } else if (status === "unavailable") {
    updateStatus(summaryStatus, "Саммари", "unavailable", hasSummary ? "Индекс недоступен" : "Недоступно", "Не удалось проверить индекс саммари в Qdrant.");
  } else if (hasSummary) {
    updateStatus(summaryStatus, "Саммари", "running", `В работе${counts ? ` · ${counts}` : ""}`, "Саммари создано, его чанки ещё индексируются в Qdrant.");
  } else {
    updateStatus(summaryStatus, "Саммари", "pending", "Ожидает", "Саммари ещё не создано.");
  }
}

export function renderPaperIndexes(card, paper) {
  const textStatus = card.querySelector('[data-status-kind="text"]');
  const articleIndex = card.querySelector('[data-status-kind="article-index"]');
  const summaryStatus = card.querySelector('[data-status-kind="summary"]');
  const textReady = paper.status === "texted";
  updateStatus(textStatus, "Текст статьи", textReady ? "ready" : paper.status === "texting" ? "running" : "pending",
    textReady ? "Готово" : paper.status === "texting" ? "В работе" : "Ожидает",
    textReady ? "Markdown статьи сохранён." : "Текст статьи ещё не извлечён.");

  const index = paper.article_index;
  const counts = countText(index);
  if (index?.status === "completed") {
    updateStatus(articleIndex, "Индекс статьи", "ready", `Готово · ${counts || "индексировано"}`, "Все актуальные чанки статьи найдены в Qdrant.");
  } else if (index?.status === "unavailable") {
    updateStatus(articleIndex, "Индекс статьи", "unavailable", "Недоступно", "Не удалось проверить индекс статьи в Qdrant.");
  } else if (counts && !counts.startsWith("0/")) {
    updateStatus(articleIndex, "Индекс статьи", "running", `В работе · ${counts}`, "Индекс статьи создаётся в Qdrant.");
  } else {
    updateStatus(articleIndex, "Индекс статьи", "pending", "Ожидает", "Индекс статьи ещё не готов.");
  }
  renderSummaryStatus(summaryStatus, paper.summary_index, paper.has_summary === true);
}
