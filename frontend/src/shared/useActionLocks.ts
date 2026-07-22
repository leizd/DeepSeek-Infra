import { useCallback, useRef } from "react";

export function useActionLocks() {
  const locks = useRef(new Map<string, Promise<unknown>>());

  return useCallback(<T,>(key: string, action: () => Promise<T>): Promise<T> => {
    const existing = locks.current.get(key);
    if (existing) return existing as Promise<T>;

    let pending: Promise<T>;
    try {
      pending = action();
    } catch (reason) {
      return Promise.reject(reason);
    }

    locks.current.set(key, pending);
    const release = () => {
      if (locks.current.get(key) === pending) locks.current.delete(key);
    };
    void pending.then(release, release);
    return pending;
  }, []);
}
