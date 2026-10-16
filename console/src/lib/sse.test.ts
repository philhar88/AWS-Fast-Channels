import { describe, expect, it } from "vitest";
import { sseData } from "./sse";

async function collect(...chunks: string[]): Promise<string[]> {
  async function* source() {
    await Promise.resolve();
    yield* chunks;
  }
  const out: string[] = [];
  for await (const data of sseData(source())) {
    out.push(data);
  }
  return out;
}

describe("sseData", () => {
  it("yields one payload per event across arbitrary chunk boundaries", async () => {
    expect(await collect('data: {"a"', ":1}\n\nda", 'ta: {"b":2}\n', "\n")).toEqual(['{"a":1}', '{"b":2}']);
  });

  it("handles CRLF, including a CRLF split between chunks", async () => {
    expect(await collect("data: one\r", "\n\r\ndata: two\r\n\r\n")).toEqual(["one", "two"]);
  });

  it("joins multi-line data and ignores comments and other fields", async () => {
    expect(await collect(": keep-alive\n\nevent: message\nid: 7\ndata: a\ndata:b\n\n")).toEqual(["a\nb"]);
  });

  it("discards a final event that is not terminated by a blank line", async () => {
    expect(await collect("data: done\n\ndata: cut")).toEqual(["done"]);
  });
});
