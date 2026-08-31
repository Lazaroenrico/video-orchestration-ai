import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, it, expect, vi } from "vitest";
import { SessionBoundary } from "./SessionBoundary";
import { api, HttpError } from "../api/client";
import type { UserSession } from "../api/contracts";

function createQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });
}

describe("SessionBoundary", () => {
  it("renders loading state and not children while fetching session", () => {
    vi.spyOn(api, "getMe").mockImplementation(
      () => new Promise(() => {}), // never resolves
    );
    const client = createQueryClient();

    render(
      <QueryClientProvider client={client}>
        <SessionBoundary>
          <div data-testid="app-content">App Content</div>
        </SessionBoundary>
      </QueryClientProvider>,
    );

    expect(screen.getByTestId("session-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("app-content")).not.toBeInTheDocument();
  });

  it("renders 401 error message when authentication fails", async () => {
    vi.spyOn(api, "getMe").mockRejectedValue(new HttpError(401, "Unauthorized"));
    const client = createQueryClient();

    render(
      <QueryClientProvider client={client}>
        <SessionBoundary>
          <div data-testid="app-content">App Content</div>
        </SessionBoundary>
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("session-error-401")).toBeInTheDocument();
    });
    expect(screen.queryByTestId("app-content")).not.toBeInTheDocument();
  });

  it("renders 403 error message when membership is pending or denied", async () => {
    vi.spyOn(api, "getMe").mockRejectedValue(new HttpError(403, "Forbidden"));
    const client = createQueryClient();

    render(
      <QueryClientProvider client={client}>
        <SessionBoundary>
          <div data-testid="app-content">App Content</div>
        </SessionBoundary>
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("session-error-403")).toBeInTheDocument();
    });
    expect(screen.queryByTestId("app-content")).not.toBeInTheDocument();
  });

  it("renders generic error message on 503 or network failure", async () => {
    vi.spyOn(api, "getMe").mockRejectedValue(new HttpError(503, "Service Unavailable"));
    const client = createQueryClient();

    render(
      <QueryClientProvider client={client}>
        <SessionBoundary>
          <div data-testid="app-content">App Content</div>
        </SessionBoundary>
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("session-error-generic")).toBeInTheDocument();
    });
    expect(screen.queryByTestId("app-content")).not.toBeInTheDocument();
  });

  it("renders children when session resolves successfully", async () => {
    const mockSession: UserSession = {
      id: "u-1",
      subject: "access|alice",
      email: "alice@acme.com",
      display_name: "Alice",
      organization: { id: "o-1", slug: "acme", name: "Acme Inc." },
      role: "member",
      permissions: ["runs:read", "runs:create"],
      auth_mode: "cloudflare_access",
    };
    vi.spyOn(api, "getMe").mockResolvedValue(mockSession);
    const client = createQueryClient();

    render(
      <QueryClientProvider client={client}>
        <SessionBoundary>
          <div data-testid="app-content">App Content</div>
        </SessionBoundary>
      </QueryClientProvider>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("app-content")).toBeInTheDocument();
    });
    expect(screen.queryByTestId("session-loading")).not.toBeInTheDocument();
  });
});
