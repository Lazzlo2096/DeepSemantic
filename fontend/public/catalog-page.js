const mode = document.body.dataset.catalogMode || "home";
const spaceId = new URLSearchParams(location.search).get("id");
window.catalogSpaceId = mode === "space" ? spaceId : null;
const response = await fetch("/catalog-components.html", { cache: "no-store" });
if (!response.ok) throw new Error("Не удалось загрузить компоненты каталога.");
document.body.replaceChildren();
document.body.insertAdjacentHTML("afterbegin", await response.text());

if (mode === "space") {
  document.querySelector("#space-promo").remove();
  document.querySelector("#catalog-eyebrow").remove();
  document.querySelector("#page-title").remove();
  const backLink = document.querySelector("#space-back-link");
  backLink.hidden = false;
  if (spaceId) backLink.href = `/space.html?id=${encodeURIComponent(spaceId)}`;
  document.querySelector("#results-title").textContent = "Файлы и обработка Space";
  document.querySelector("#catalog-filters").hidden = true;
  document.querySelector("#space-promo")?.remove();
  backLink.textContent = "← Мои Spaces";
  backLink.href = "/my-spaces.html";
  const spacePageLink = document.querySelector("#space-page-link");
  spacePageLink.hidden = false;
  if (spaceId) spacePageLink.href = `/space.html?id=${encodeURIComponent(spaceId)}`;
} else {
  document.querySelector("#catalog-intro").classList.add("catalog-home-intro");
  document.querySelector("#space-promo").hidden = false;
}

await import("./auth.js?v=2");
await import("./app.js?v=12");
if (mode === "space") await import("./space-catalog-actions.js?v=1");
