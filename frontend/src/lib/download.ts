/**
 * 触发浏览器下载：把文本内容作为 Blob 生成临时 <a download> 点击。
 * 仅在浏览器端调用（client component 内）。
 */
export function triggerDownload(
  filename: string,
  content: string,
  mime = "text/markdown",
): void {
  const blob = new Blob([content], { type: `${mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

/**
 * 去掉文件名非法字符（/ \ : * ? " < > |），空名回退为 "spec"。
 */
export function sanitizeFilename(name: string): string {
  return name.replace(/[/\\:*?"<>|]/g, "_").trim() || "spec";
}
