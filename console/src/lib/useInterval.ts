import { useEffect, useEffectEvent } from "react";

/** Calls the latest `callback` every `ms` milliseconds while the component is mounted. */
export function useInterval(callback: () => void, ms: number): void {
  const tick = useEffectEvent(callback);
  useEffect(() => {
    const timer = window.setInterval(() => {
      tick();
    }, ms);
    return () => {
      window.clearInterval(timer);
    };
  }, [ms]);
}
