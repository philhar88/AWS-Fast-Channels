import type { FlashbarProps } from "@cloudscape-design/components/flashbar";
import { createContext, use, type ReactNode } from "react";
import { errorMessage } from "./errors";
import { ToolCallError } from "./gateway";

export interface Notice {
  readonly type: "success" | "error" | "info" | "warning";
  readonly header: string;
  readonly content?: ReactNode;
}

export interface Notifications {
  readonly items: FlashbarProps.MessageDefinition[];
  readonly notify: (notice: Notice) => void;
}

export const NotificationsContext = createContext<Notifications | null>(null);

export function useNotifications(): Notifications {
  const value = use(NotificationsContext);
  if (value === null) {
    throw new Error("useNotifications() outside NotificationsProvider");
  }
  return value;
}

/** Flash content for a failed call: the tool registry's message, its hint and code. */
export function errorNotice(header: string, error: unknown): Notice {
  if (error instanceof ToolCallError) {
    return { type: "error", header, content: `${error.message}${error.hint ? ` (${error.hint})` : ""} [${error.code}]` };
  }
  return { type: "error", header, content: errorMessage(error) };
}
