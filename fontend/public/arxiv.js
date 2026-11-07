export function arxivPath(arxivId) {
  return arxivId.split("/").map(encodeURIComponent).join("/");
}

export function arxivUrl(arxivId) {
  return `https://arxiv.org/abs/${arxivPath(arxivId)}`;
}
