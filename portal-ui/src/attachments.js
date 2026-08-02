import pdfWorkerUrl from "pdfjs-dist/legacy/build/pdf.worker.min.mjs?url";

export const MAX_ATTACHMENT_BYTES = 12 * 1024 * 1024;
export const MAX_TOTAL_ATTACHMENT_BYTES = 24 * 1024 * 1024;
export const MAX_VISUAL_INPUTS = 8;
export const ACCEPTED_ATTACHMENTS =
  "image/png,image/jpeg,image/webp,image/gif,application/pdf,text/plain,text/markdown,text/csv,application/json,.md,.log";

const TEXT_TYPES = new Set([
  "text/plain",
  "text/markdown",
  "text/csv",
  "application/json",
]);
const TEXT_EXTENSIONS = /\.(?:txt|md|markdown|csv|json|log)$/i;

function attachmentId() {
  return globalThis.crypto?.randomUUID?.() ||
    `attachment-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function dataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error(`无法读取文件：${file.name}`));
    reader.readAsDataURL(file);
  });
}

async function pageDataUrl(page) {
  const baseViewport = page.getViewport({ scale: 1 });
  const scale = Math.min(
    2,
    1600 / Math.max(baseViewport.width, baseViewport.height),
  );
  const viewport = page.getViewport({ scale: Math.max(1, scale) });
  const canvas = document.createElement("canvas");
  const context = canvas.getContext("2d", { alpha: false });
  if (!context) throw new Error("浏览器无法创建 PDF 渲染画布");
  canvas.width = Math.ceil(viewport.width);
  canvas.height = Math.ceil(viewport.height);
  await page.render({ canvasContext: context, viewport, canvas }).promise;
  return canvas.toDataURL("image/jpeg", 0.88);
}

async function preparePdf(file, remainingVisuals) {
  if (remainingVisuals < 1) throw new Error("图片与 PDF 页面合计最多 8 个");
  const pdfjs = await import("pdfjs-dist/legacy/build/pdf.mjs");
  pdfjs.GlobalWorkerOptions.workerSrc = pdfWorkerUrl;
  const task = pdfjs.getDocument({ data: new Uint8Array(await file.arrayBuffer()) });
  let document;
  try {
    document = await task.promise;
    const rendered = Math.min(document.numPages, remainingVisuals);
    const pages = [];
    for (let number = 1; number <= rendered; number += 1)
      pages.push(await pageDataUrl(await document.getPage(number)));
    return {
      id: attachmentId(),
      name: file.name,
      size: file.size,
      kind: "pdf",
      pages,
      pageCount: document.numPages,
      truncated: document.numPages > rendered,
    };
  } catch (error) {
    throw new Error(`PDF 解析失败（${file.name}）：${error.message}`);
  } finally {
    if (document) await document.destroy();
    else await task.destroy?.();
  }
}

export function attachmentKind(file) {
  if (String(file.type || "").startsWith("image/")) return "image";
  if (file.type === "application/pdf" || /\.pdf$/i.test(file.name)) return "pdf";
  if (TEXT_TYPES.has(file.type) || TEXT_EXTENSIONS.test(file.name)) return "text";
  return "unsupported";
}

export function visualInputCount(attachments) {
  return (attachments || []).reduce(
    (total, item) =>
      total +
      (item.kind === "image" ? 1 : item.kind === "pdf" ? item.pages.length : 0),
    0,
  );
}

export async function prepareAttachment(file, remainingVisuals = MAX_VISUAL_INPUTS) {
  if (!file?.name) throw new Error("没有可读取的文件");
  if (file.size > MAX_ATTACHMENT_BYTES)
    throw new Error(`单个文件不能超过 12 MiB：${file.name}`);
  const kind = attachmentKind(file);
  if (kind === "unsupported")
    throw new Error(`不支持该文件格式：${file.name}`);
  if (kind === "pdf") return preparePdf(file, remainingVisuals);
  if (kind === "image") {
    if (remainingVisuals < 1) throw new Error("图片与 PDF 页面合计最多 8 个");
    return {
      id: attachmentId(),
      name: file.name,
      size: file.size,
      kind,
      dataUrl: await dataUrl(file),
    };
  }
  const text = await file.text();
  return {
    id: attachmentId(),
    name: file.name,
    size: file.size,
    kind,
    text: text.slice(0, 1_000_000),
    truncated: text.length > 1_000_000,
  };
}

export function buildUserContent(prompt, attachments) {
  const items = [{ type: "text", text: prompt.trim() || "请分析附件内容。" }];
  for (const attachment of attachments || []) {
    if (attachment.kind === "image") {
      items.push({
        type: "image_url",
        image_url: { url: attachment.dataUrl },
      });
    } else if (attachment.kind === "pdf") {
      attachment.pages.forEach((url, index) => {
        items.push({
          type: "text",
          text: `附件 ${attachment.name} · 第 ${index + 1} 页`,
        });
        items.push({ type: "image_url", image_url: { url } });
      });
    } else if (attachment.kind === "text") {
      items.push({
        type: "text",
        text: `\n\n附件 ${attachment.name}：\n${attachment.text}`,
      });
    }
  }
  return items.length === 1 && !(attachments || []).length ? items[0].text : items;
}
