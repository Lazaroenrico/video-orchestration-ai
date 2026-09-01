import { QueryClient, type Query } from "@tanstack/react-query";
import { createAsyncStoragePersister } from "@tanstack/query-async-storage-persister";

export const QUERY_CACHE_BUSTER = "orchestrator-front-cache-v2";
export const QUERY_CACHE_MAX_AGE_MS = 1000 * 60 * 60 * 12;
export const QUERY_CACHE_GC_MS = 1000 * 60 * 60 * 24;
const QUERY_CACHE_MAX_PAYLOAD_BYTES = 500_000;

export const NON_PERSISTED_QUERY_ROOTS = new Set(["session", "members", "invitations"]);

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      gcTime: QUERY_CACHE_GC_MS,
      staleTime: 5_000,
      retry: 1,
      refetchOnWindowFocus: true,
    },
  },
});

export const queryPersister = createAsyncStoragePersister({
  key: "orchestrator-query-cache",
  storage: typeof window === "undefined" ? undefined : window.localStorage,
  throttleTime: 1_000,
});

export function shouldPersistQuery(query: Query): boolean {
  if (query.state.status !== "success") return false;
  const rootKey = query.queryKey[0];
  if (typeof rootKey === "string" && NON_PERSISTED_QUERY_ROOTS.has(rootKey)) {
    return false;
  }
  try {
    return JSON.stringify(query.state.data).length <= QUERY_CACHE_MAX_PAYLOAD_BYTES;
  } catch {
    return false;
  }
}
