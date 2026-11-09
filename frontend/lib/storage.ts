import { useEffect, useState } from "react";

/**
 * A boolean kept in localStorage. SSR safe: the first render always uses `initial`,
 * the stored value is read once in an effect. Every storage access is guarded, so
 * private mode (where localStorage throws) just behaves like a plain useState.
 */
export function usePersistentFlag(key: string, initial: boolean): [boolean, (v: boolean) => void] {
  const [value, setValue] = useState(initial);

  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(key);
      if (stored === "1") setValue(true);
      else if (stored === "0") setValue(false);
    } catch {
      /* storage unavailable: keep the initial value */
    }
    // read once on mount
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const set = (v: boolean) => {
    setValue(v);
    try {
      window.localStorage.setItem(key, v ? "1" : "0");
    } catch {
      /* storage unavailable: the state still updates */
    }
  };
  return [value, set];
}
