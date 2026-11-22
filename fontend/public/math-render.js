import MarkdownIt from "./vendor/markdown-it.mjs";

const mathOptions = {
  engine: window.katex,
  delimiters: "dollars",
  katexOptions: { trust: false, throwOnError: false, maxExpand: 500, maxSize: 25 },
};

const markdown = new MarkdownIt({ html: false, breaks: true }).use(window.texmath, mathOptions);

export function renderMarkdown(source) {
  return markdown.render(String(source ?? ""));
}

export function renderInlineMarkdown(source) {
  return markdown.renderInline(String(source ?? ""));
}

export function setMarkdownContent(element, source, { inline = false } = {}) {
  element.innerHTML = inline ? renderInlineMarkdown(source) : renderMarkdown(source);
}
