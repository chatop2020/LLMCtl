function textFromBlocks(value, reasoning = false) {
  if (typeof value === "string") return value;
  if (!Array.isArray(value)) return "";
  return value
    .map((block) => {
      if (typeof block === "string") return reasoning ? "" : block;
      if (!block || typeof block !== "object") return "";
      const type = String(block.type || "").toLowerCase();
      const isReasoning = type.includes("reason") || type.includes("think");
      if (isReasoning !== reasoning) return "";
      return typeof block.text === "string"
        ? block.text
        : typeof block.content === "string"
          ? block.content
          : "";
    })
    .join("");
}

export function chunkParts(chunk) {
  const choice = Array.isArray(chunk?.choices) ? chunk.choices[0] : null;
  const message = choice?.delta || choice?.message || {};
  const reasoning =
    [message.reasoning_content, message.reasoning, message.thinking]
      .map(
        (value) =>
          textFromBlocks(value, true) ||
          (typeof value === "string" ? value : ""),
      )
      .find(Boolean) || textFromBlocks(message.content, true);
  return {
    content: textFromBlocks(message.content, false),
    reasoning,
    finishReason: choice?.finish_reason || null,
    usage: chunk?.usage || null,
    id: chunk?.id || "",
    model: chunk?.model || "",
  };
}

export function splitThinkingMarkup(content) {
  if (typeof content !== "string" || !/<think>/i.test(content))
    return { content: content || "", reasoning: "" };
  const reasoning = [];
  const answer = content.replace(
    /<think>([\s\S]*?)<\/think>/gi,
    (_, thought) => {
      reasoning.push(thought.trim());
      return "";
    },
  );
  return { content: answer.trimStart(), reasoning: reasoning.join("\n\n") };
}

export function parseSseBlock(block) {
  const data = [];
  const metadata = {};
  for (const rawLine of block.split("\n")) {
    const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
    if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    if (line.startsWith(":")) {
      const match = /^:\s*([^=]+)=(.*)$/.exec(line);
      if (match) metadata[match[1].trim().toLowerCase()] = match[2].trim();
    }
  }
  return { data: data.join("\n").trim(), metadata };
}

export async function consumeChatResponse(
  response,
  onChunk,
  onMetadata = () => {},
) {
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    onChunk(await response.json());
    return;
  }
  if (!response.body) throw new Error("浏览器未收到可读取的流式响应");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let done = false;

  const dispatch = (block) => {
    if (!block.trim()) return;
    const event = parseSseBlock(block);
    if (Object.keys(event.metadata).length) onMetadata(event.metadata);
    if (!event.data) return;
    if (event.data === "[DONE]") {
      done = true;
      return;
    }
    try {
      onChunk(JSON.parse(event.data));
    } catch {
      throw new Error(`流式响应包含无效 JSON：${event.data.slice(0, 160)}`);
    }
  };

  while (!done) {
    const part = await reader.read();
    buffer += decoder.decode(part.value || new Uint8Array(), {
      stream: !part.done,
    });
    buffer = buffer.replace(/\r\n/g, "\n");
    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      dispatch(block);
      if (done) break;
    }
    if (part.done) break;
  }
  if (!done && buffer.trim()) dispatch(buffer);
}
