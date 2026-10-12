import { useCallback, useEffect, useRef, useState } from "react";

export interface Poll<T> {
  data: T | null;
  error: Error | null;
  /** True only on the first load. Refreshes must not blank the screen — a
   *  dashboard that flashes a spinner every second is unreadable. */
  loading: boolean;
  reload: () => void;
}

/**
 * Fetch on mount, then re-fetch on an interval that the caller can turn off.
 *
 * `intervalMs` of 0 means "read once". Callers pass 0 the moment a run reaches a
 * terminal state, so a page full of finished runs makes no requests at all.
 */
export function usePoll<T>(
  fetcher: () => Promise<T>,
  deps: unknown[],
  intervalMs = 0,
): Poll<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    let cancelled = false;
    const read = async () => {
      try {
        const next = await fetcherRef.current();
        if (!cancelled) {
          setData(next);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) setError(err as Error);
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    read();
    if (intervalMs > 0) {
      const timer = setInterval(read, intervalMs);
      return () => {
        cancelled = true;
        clearInterval(timer);
      };
    }
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, intervalMs, nonce]);

  return { data, error, loading, reload };
}
