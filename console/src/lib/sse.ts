/**
 * Yields the `data` payload of every Server-Sent Event in a text stream (WHATWG HTML §9.2 framing: events end at a
 * blank line, multi-line data is joined with "\n", comments and other fields are ignored, an unterminated final event
 * is discarded). Accepts LF, CR and CRLF line endings, including a CRLF split across two chunks.
 */
export async function* sseData(chunks: AsyncIterable<string>): AsyncGenerator<string> {
  let line = "";
  let data: string[] = [];
  let skipLeadingLf = false;
  for await (const chunk of chunks) {
    for (const char of chunk) {
      if (skipLeadingLf && char === "\n") {
        skipLeadingLf = false;
        continue;
      }
      skipLeadingLf = char === "\r";
      if (char !== "\r" && char !== "\n") {
        line += char;
        continue;
      }
      if (line === "") {
        if (data.length > 0) {
          yield data.join("\n");
        }
        data = [];
      } else if (line === "data" || line.startsWith("data:")) {
        data.push(line.slice(5).replace(/^ /, ""));
      }
      line = "";
    }
  }
}

/** Decodes a fetch response body into text chunks for {@link sseData}. */
export async function* textChunks(body: ReadableStream<Uint8Array<ArrayBuffer>>): AsyncGenerator<string> {
  const reader = body.pipeThrough(new TextDecoderStream()).getReader();
  try {
    for (let next = await reader.read(); !next.done; next = await reader.read()) {
      yield next.value;
    }
  } finally {
    reader.releaseLock();
  }
}
