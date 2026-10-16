import type { FlashbarProps } from "@cloudscape-design/components/flashbar";
import { useCallback, useMemo, useState, type ReactNode } from "react";
import { NotificationsContext, type Notice } from "../lib/notifications";

/** Flashbar messages for the AppLayout `notifications` slot; each one is dismissible, newest first, at most five. */
export function NotificationsProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<FlashbarProps.MessageDefinition[]>([]);
  const notify = useCallback((notice: Notice) => {
    const id = crypto.randomUUID();
    const onDismiss = () => {
      setItems((current) => current.filter((item) => item.id !== id));
    };
    setItems((current) => [{ ...notice, id, dismissible: true, dismissLabel: "Dismiss", onDismiss }, ...current.slice(0, 4)]);
  }, []);
  const value = useMemo(() => ({ items, notify }), [items, notify]);
  return <NotificationsContext value={value}>{children}</NotificationsContext>;
}
