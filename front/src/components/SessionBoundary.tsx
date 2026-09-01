import type React from "react";
import { HttpError } from "../api/client";
import { useSessionQuery } from "../api/queries";

export interface SessionBoundaryProps {
  children?: React.ReactNode;
}

export function SessionBoundary({ children }: SessionBoundaryProps) {
  const session = useSessionQuery();

  if (session.isLoading || session.isPending) {
    return (
      <div
        data-testid="session-loading"
        className="flex min-h-screen items-center justify-center bg-slate-900 text-slate-200"
      >
        <div className="flex flex-col items-center gap-4">
          <div className="h-8 w-8 animate-spin rounded-full border-4 border-indigo-500 border-t-transparent" />
          <p className="text-sm font-medium text-slate-400">Carregando sessão...</p>
        </div>
      </div>
    );
  }

  if (session.isError) {
    const error = session.error;
    const status = error instanceof HttpError ? error.status : (error as { status?: number })?.status;

    if (status === 401) {
      return (
        <div
          data-testid="session-error-401"
          className="flex min-h-screen items-center justify-center bg-slate-900 px-4 text-slate-200"
        >
          <div className="max-w-md rounded-xl border border-slate-800 bg-slate-950 p-6 text-center shadow-xl">
            <h2 className="text-lg font-semibold text-slate-100">Autenticação necessária</h2>
            <p className="mt-2 text-sm text-slate-400">
              Sua sessão expirou ou o token de autenticação é inválido. Por favor, autentique-se novamente via Cloudflare Access.
            </p>
            <div className="mt-6 flex justify-center gap-3">
              <button
                type="button"
                onClick={() => {
                  window.location.href = "/cdn-cgi/access/logout";
                }}
                className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500"
              >
                Entrar com Cloudflare
              </button>
            </div>
          </div>
        </div>
      );
    }

    if (status === 403) {
      return (
        <div
          data-testid="session-error-403"
          className="flex min-h-screen items-center justify-center bg-slate-900 px-4 text-slate-200"
        >
          <div className="max-w-md rounded-xl border border-slate-800 bg-slate-950 p-6 text-center shadow-xl">
            <h2 className="text-lg font-semibold text-amber-400">Acesso não autorizado</h2>
            <p className="mt-2 text-sm text-slate-400">
              Você não possui membership ativa nesta organização. Verifique se o convite foi emitido para o mesmo e-mail verificado no Cloudflare Access.
            </p>
            <div className="mt-6 flex justify-center gap-3">
              <button
                type="button"
                onClick={() => {
                  window.location.href = "/cdn-cgi/access/logout";
                }}
                className="rounded-lg bg-slate-800 px-4 py-2 text-sm font-medium text-slate-300 hover:bg-slate-700"
              >
                Trocar conta / Sair
              </button>
            </div>
          </div>
        </div>
      );
    }

    return (
      <div
        data-testid="session-error-generic"
        className="flex min-h-screen items-center justify-center bg-slate-900 px-4 text-slate-200"
      >
        <div className="max-w-md rounded-xl border border-slate-800 bg-slate-950 p-6 text-center shadow-xl">
          <h2 className="text-lg font-semibold text-rose-400">Serviço indisponível</h2>
          <p className="mt-2 text-sm text-slate-400">
            Não foi possível verificar a sua sessão. Verifique sua conexão ou tente novamente.
          </p>
          <div className="mt-6 flex justify-center gap-3">
            <button
              type="button"
              onClick={() => session.refetch()}
              className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500"
            >
              Tentar novamente
            </button>
          </div>
        </div>
      </div>
    );
  }

  return <>{children}</>;
}
