const mode = document.body.dataset.catalogMode || "home";
const spaceId = new URLSearchParams(location.search).get("id");
const response = await fetch("/catalog-components.html", { cache: "no-store" });
if (!response.ok) throw new Error("Не удалось загрузить компоненты каталога.");
document.body.replaceChildren();
document.body.insertAdjacentHTML("afterbegin", await response.text());

if (mode === "space") {
  document.querySelector("#catalog-eyebrow").remove();
  document.querySelector("#page-title").remove();
  const backLink = document.querySelector("#space-back-link");
  backLink.hidden = false;
  if (spaceId) backLink.href = `/space.html?id=${encodeURIComponent(spaceId)}`;
}

await import("./auth.js?v=1");
await import("./app.js?v=10");
