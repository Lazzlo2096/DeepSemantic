export function renderSummaryStatus(summaryStatus, summaryIndex, hasSummary) {
  const summaryIndexed = summaryIndex?.status === "completed";
  const summaryReady = hasSummary || summaryIndexed;
  summaryStatus.dataset.ready = String(summaryReady);
  if (summaryIndexed) {
    summaryStatus.textContent = "Саммари готово · в Qdrant";
    summaryStatus.title = `Саммари создано, все ${summaryIndex.chunks_total} его чанков найдены в Qdrant.`;
  } else if (hasSummary) {
    const unavailable = summaryIndex?.status === "unavailable";
    summaryStatus.textContent = unavailable ? "Саммари готово · индекс недоступен" : "Саммари готово · индексируется";
    summaryStatus.title = unavailable
      ? "Саммари создано, но сейчас не удалось проверить его индекс в Qdrant."
      : `Саммари создано. В Qdrant загружено ${summaryIndex?.chunks_in_qdrant ?? 0} из ${summaryIndex?.chunks_total ?? 0} чанков.`;
  } else {
    const unavailable = summaryIndex?.status === "unavailable";
    summaryStatus.textContent = unavailable ? "Индекс саммари: нет связи" : "Саммари не готово";
    summaryStatus.title = "Саммари ещё не создано. После создания его чанки будут добавлены в Qdrant.";
  }
}

export function renderPaperIndexes(card, paper) {
  const articleIndex = card.querySelector(".paper-index-status");
  const articleReady = paper.article_index?.status === "completed";
  articleIndex.dataset.ready = String(articleReady);
  articleIndex.textContent = articleReady ? "Статья в Qdrant"
    : paper.article_index?.status === "unavailable" ? "Индекс статьи: нет связи" : "Индекс статьи не готов";
  articleIndex.title = articleReady
    ? `Все ${paper.article_index.chunks_total} чанков полного текста найдены в Qdrant. Ветка саммари проверяется отдельно.`
    : "Индекс полного текста: готовность подтверждается наличием всех актуальных чанков в Qdrant.";
  const summaryStatus = card.querySelector(".paper-summary-status");
  renderSummaryStatus(summaryStatus, paper.summary_index, paper.has_summary === true);
}
