import type { QueryClient } from "@tanstack/react-query";

export function removeFailedMutations(
  queryClient: QueryClient,
  keys: readonly (readonly unknown[])[],
): void {
  const cache = queryClient.getMutationCache();
  for (const key of keys) {
    for (const mutation of cache.findAll({ mutationKey: key, exact: true })) {
      if (mutation.state.status === "error") cache.remove(mutation);
    }
  }
}
