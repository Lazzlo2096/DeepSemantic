const spaceId = new URLSearchParams(location.search).get("id");
const backLink = document.querySelector("#space-back-link");

if (spaceId) {
  backLink.href = `/space.html?id=${encodeURIComponent(spaceId)}`;
}
