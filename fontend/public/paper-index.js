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
  const summaryReady = paper.summary_index?.status === "completed";
  summaryStatus.dataset.ready = String(summaryReady);
  summaryStatus.textContent = summaryReady ? "Суммаризация готова"
    : paper.summary_index?.status === "unavailable" ? "Индекс саммари: нет связи" : "Суммаризация не готова";
  summaryStatus.title = summaryReady
    ? `Саммари создано, все ${paper.summary_index.chunks_total} его чанков найдены в Qdrant.`
    : "Ветка саммари: текст → чанки → эмбеддинги. Готовность подтверждается наличием всех актуальных чанков в Qdrant.";
}
