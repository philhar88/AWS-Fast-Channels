import { describe, expect, it } from "vitest";
import { parseViewerRoute, viewerHref, type ViewerRoute } from "./router";

describe("viewer routes", () => {
  it.each<[string, ViewerRoute]>([
    ["", { view: "home" }],
    ["#/", { view: "home" }],
    ["#/personas", { view: "personas" }],
    ["#/watch/fastm-demo", { view: "watch", channel: "fastm-demo" }],
    ["#/watch", { view: "home" }],
    ["#/unknown", { view: "home" }],
  ])("%s", (hash, route) => {
    expect(parseViewerRoute(hash)).toEqual(route);
  });

  it("round-trips", () => {
    for (const route of [{ view: "personas" }, { view: "home" }, { view: "watch", channel: "a b" }] satisfies ViewerRoute[]) {
      expect(parseViewerRoute(viewerHref(route))).toEqual(route);
    }
  });
});
