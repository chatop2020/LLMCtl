export async function writeClipboardText(
  value,
  navigatorObject = globalThis.navigator,
  documentObject = globalThis.document,
) {
  const text = String(value ?? "");
  if (!text) return { copied: false, method: "empty" };

  if (navigatorObject?.clipboard?.writeText) {
    try {
      await navigatorObject.clipboard.writeText(text);
      return { copied: true, method: "clipboard" };
    } catch {
      // HTTP origins and restrictive browser policies commonly reject this API.
      // Continue with the selection-based fallback below.
    }
  }

  if (!documentObject?.body || typeof documentObject.execCommand !== "function")
    return { copied: false, method: "unavailable" };

  const field = documentObject.createElement("textarea");
  field.value = text;
  field.setAttribute("readonly", "");
  field.setAttribute("aria-hidden", "true");
  Object.assign(field.style, {
    position: "fixed",
    inset: "0 auto auto -9999px",
    opacity: "0",
    pointerEvents: "none",
  });
  documentObject.body.appendChild(field);
  try {
    field.focus();
    field.select();
    field.setSelectionRange?.(0, text.length);
    return {
      copied: Boolean(documentObject.execCommand("copy")),
      method: "selection",
    };
  } catch {
    return { copied: false, method: "blocked" };
  } finally {
    field.remove();
  }
}
