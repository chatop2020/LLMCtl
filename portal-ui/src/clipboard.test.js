import { describe, expect, it, vi } from "vitest";
import { writeClipboardText } from "./clipboard.js";

function fallbackDocument(copyResult = true) {
  const field = {
    style: {},
    setAttribute: vi.fn(),
    focus: vi.fn(),
    select: vi.fn(),
    setSelectionRange: vi.fn(),
    remove: vi.fn(),
  };
  return {
    field,
    body: { appendChild: vi.fn() },
    createElement: vi.fn(() => field),
    execCommand: vi.fn(() => copyResult),
  };
}

describe("writeClipboardText", () => {
  it("uses the modern clipboard API when the browser permits it", async () => {
    const writeText = vi.fn(async () => {});
    const result = await writeClipboardText("sk-test", {
      clipboard: { writeText },
    });
    expect(result).toEqual({ copied: true, method: "clipboard" });
    expect(writeText).toHaveBeenCalledWith("sk-test");
  });

  it("falls back to a temporary selection on an HTTP or restricted origin", async () => {
    const documentObject = fallbackDocument();
    const result = await writeClipboardText(
      "sk-http-test",
      { clipboard: { writeText: vi.fn(async () => Promise.reject()) } },
      documentObject,
    );
    expect(result).toEqual({ copied: true, method: "selection" });
    expect(documentObject.execCommand).toHaveBeenCalledWith("copy");
    expect(documentObject.field.value).toBe("sk-http-test");
    expect(documentObject.field.remove).toHaveBeenCalled();
  });

  it("reports a blocked copy instead of claiming success", async () => {
    const documentObject = fallbackDocument(false);
    const result = await writeClipboardText("blocked", {}, documentObject);
    expect(result).toEqual({ copied: false, method: "selection" });
  });
});
