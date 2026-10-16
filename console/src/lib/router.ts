/**
 * Hash routes (`#/assistant`, `#/channels/<name>`, `#/library/ladders`): no CloudFront SPA fallback needed, and the
 * OIDC redirect (`/?code=…`) never collides with a route.
 */
import { useSyncExternalStore } from "react";

export type Route =
  | { readonly view: "assistant" }
  | { readonly view: "channels"; readonly channel?: string }
  | { readonly view: "library"; readonly tab: "assets" | "ladders" };

export function parseRoute(hash: string): Route {
  const [view = "", detail] = hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent);
  switch (view) {
    case "channels":
      return detail ? { view, channel: detail } : { view };
    case "library":
      return { view, tab: detail === "ladders" ? "ladders" : "assets" };
    default:
      return { view: "assistant" };
  }
}

export function href(route: Route): string {
  switch (route.view) {
    case "assistant":
      return "#/assistant";
    case "channels":
      return route.channel ? `#/channels/${encodeURIComponent(route.channel)}` : "#/channels";
    case "library":
      return `#/library/${route.tab}`;
  }
}

const subscribe = (onChange: () => void) => {
  window.addEventListener("hashchange", onChange);
  return () => {
    window.removeEventListener("hashchange", onChange);
  };
};

export function useRoute(): Route {
  return parseRoute(useSyncExternalStore(subscribe, () => window.location.hash));
}
