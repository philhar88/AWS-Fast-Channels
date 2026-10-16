import { describe, expect, it } from "vitest";
import { href, parseRoute, type Route } from "./router";

describe("router", () => {
  it.each<Route>([{ view: "assistant" }, { view: "channels" }, { view: "channels", channel: "fast demo" }, { view: "library", tab: "ladders" }])(
    "round-trips %o",
    (route) => {
      expect(parseRoute(href(route))).toEqual(route);
    },
  );

  it("falls back to the assistant", () => {
    expect(parseRoute("")).toEqual({ view: "assistant" });
    expect(parseRoute("#/nope")).toEqual({ view: "assistant" });
  });
});
