import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router";
import { PersistQueryClientProvider } from "@tanstack/react-query-persist-client";
import { AppRoutes } from "./router";
import {
  QUERY_CACHE_BUSTER,
  QUERY_CACHE_MAX_AGE_MS,
  queryClient,
  queryPersister,
  shouldPersistQuery,
} from "./api/queryClient";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <PersistQueryClientProvider
      client={queryClient}
      persistOptions={{
        persister: queryPersister,
        maxAge: QUERY_CACHE_MAX_AGE_MS,
        buster: QUERY_CACHE_BUSTER,
        dehydrateOptions: { shouldDehydrateQuery: shouldPersistQuery },
      }}
    >
      <BrowserRouter>
        <AppRoutes />
      </BrowserRouter>
    </PersistQueryClientProvider>
  </React.StrictMode>
);
