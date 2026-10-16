import { useCallback, useEffect, useState } from "react";

export interface AsyncState<T> {
  /** Latest result; kept while a reload is in flight so tables do not flash empty. */
  readonly data: T | undefined;
  readonly error: unknown;
  readonly loading: boolean;
  /** Stable across renders. */
  readonly reload: () => void;
}

/**
 * Runs `load` whenever it changes (memoise it with `useCallback`) or `reload()` is called. A superseded run is
 * aborted and its result dropped.
 */
export function useAsync<T>(load: (signal: AbortSignal) => Promise<T>): AsyncState<T> {
  const [generation, setGeneration] = useState(0);
  const [result, setResult] = useState<{ load: typeof load; generation: number; data?: T; error?: unknown }>();

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal).then(
      (data) => {
        if (!controller.signal.aborted) {
          setResult({ load, generation, data });
        }
      },
      (error: unknown) => {
        if (!controller.signal.aborted) {
          setResult((previous) => ({ load, generation, data: previous?.data, error }));
        }
      },
    );
    return () => {
      controller.abort();
    };
  }, [load, generation]);

  const reload = useCallback(() => {
    setGeneration((n) => n + 1);
  }, []);
  const settled = result?.load === load && result.generation === generation;
  return { data: result?.data, error: settled ? result.error : undefined, loading: !settled, reload };
}
