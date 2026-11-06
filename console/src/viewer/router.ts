/** Hash routes of the viewer app: `#/personas` (who's watching), `#/` (rails) and `#/watch/<channel>`. */
import { useSyncExternalStore } from "react";

export type ViewerRoute =
  | { readonly view: "personas" }
  | { readonly view: "home" }
  | { readonly view: "watch"; readonly channel: string };

export function parseViewerRoute(hash: string): ViewerRoute {
  const [view = "", detail] = hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent);
  switch (view) {
    case "personas":
      return { view };
    case "watch":
      return detail ? { view, channel: detail } : { view: "home" };
    default:
      return { view: "home" };
  }
}

export function viewerHref(route: ViewerRoute): string {
  switch (route.view) {
    case "personas":
      return "#/personas";
    case "home":
      return "#/";
    case "watch":
      return `#/watch/${encodeURIComponent(route.channel)}`;
  }
}

const subscribe = (onChange: () => void) => {
  window.addEventListener("hashchange", onChange);
  return () => {
    window.removeEventListener("hashchange", onChange);
  };
};

export function useViewerRoute(): ViewerRoute {
  return parseViewerRoute(useSyncExternalStore(subscribe, () => window.location.hash));
}
