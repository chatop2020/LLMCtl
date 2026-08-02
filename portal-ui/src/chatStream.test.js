import { describe, expect, it } from "vitest";
import { chunkParts, splitThinkingMarkup } from "./chatStream.js";

describe("streaming chat parsing", () => {
  it("separates reasoning and final content from native delta fields", () => {
    const parsed = chunkParts({
      id: "req-1",
      model: "gdn-inside",
      choices: [
        { delta: { reasoning_content: "先分析", content: "最终回答" } },
      ],
      usage: { prompt_tokens: 3, completion_tokens: 4 },
    });
    expect(parsed.reasoning).toBe("先分析");
    expect(parsed.content).toBe("最终回答");
    expect(parsed.usage.total_tokens).toBeUndefined();
  });

  it("extracts think markup without leaking it into the final answer", () => {
    const parsed = splitThinkingMarkup("<think>逐步分析</think>这里是答案");
    expect(parsed.reasoning).toBe("逐步分析");
    expect(parsed.content).toBe("这里是答案");
  });
});
