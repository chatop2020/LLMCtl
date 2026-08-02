import { describe, expect, it } from "vitest";
import {
  attachmentKind,
  buildUserContent,
  visualInputCount,
} from "./attachments.js";

describe("playground attachments", () => {
  it("recognizes image, PDF and safe text formats", () => {
    expect(attachmentKind({ name: "scan.png", type: "image/png" })).toBe("image");
    expect(attachmentKind({ name: "report.pdf", type: "" })).toBe("pdf");
    expect(attachmentKind({ name: "notes.md", type: "" })).toBe("text");
    expect(attachmentKind({ name: "archive.zip", type: "application/zip" })).toBe(
      "unsupported",
    );
  });

  it("converts visual and text attachments into an OpenAI-compatible message", () => {
    const content = buildUserContent("分析这些附件", [
      { kind: "image", name: "a.png", dataUrl: "data:image/png;base64,AA==" },
      {
        kind: "pdf",
        name: "b.pdf",
        pages: ["data:image/jpeg;base64,BB==", "data:image/jpeg;base64,CC=="],
      },
      { kind: "text", name: "c.txt", text: "hello" },
    ]);
    expect(content[0]).toEqual({ type: "text", text: "分析这些附件" });
    expect(content.filter((item) => item.type === "image_url")).toHaveLength(3);
    expect(content.at(-1).text).toContain("附件 c.txt");
    expect(visualInputCount([
      { kind: "image" },
      { kind: "pdf", pages: ["a", "b"] },
      { kind: "text" },
    ])).toBe(3);
  });
});
