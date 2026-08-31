import { env } from "cloudflare:workers";
import { Container, getContainer, getRandom } from "@cloudflare/containers";

const sharedEnv = () => ({
  DATABASE_URL: env.DATABASE_URL,
  ORCH_ENV: "staging",
  ORCH_CONFIG_DIR: "/app/config-staging",
  ORCH_AUTH_MODE: "cloudflare_access",
  ORCH_QUEUE_BACKEND: "database",
  ORCHESTRATOR_LOG_FORMAT: "json",
  ORCH_SERVE_LOCAL_MEDIA: "0",
  ORCH_CORS_ORIGINS: env.ORCH_APP_ORIGIN,
  ORCH_ENABLE_PAID_ADAPTERS: "false",
  ORCH_PUBLIC_API_BASE_URL: env.ORCH_PUBLIC_API_BASE_URL,
  REPLICATE_WEBHOOK_SIGNING_SECRET: env.REPLICATE_WEBHOOK_SIGNING_SECRET,
  ORCH_WEBHOOK_CORRELATION_SECRET: env.ORCH_WEBHOOK_CORRELATION_SECRET,
  R2_ENDPOINT_URL: env.R2_ENDPOINT_URL,
  R2_ACCESS_KEY_ID: env.R2_ACCESS_KEY_ID,
  R2_SECRET_ACCESS_KEY: env.R2_SECRET_ACCESS_KEY,
  R2_BUCKET: env.R2_BUCKET,
  CF_ACCESS_TEAM_DOMAIN: env.CF_ACCESS_TEAM_DOMAIN,
  CF_ACCESS_AUDIENCE: env.CF_ACCESS_AUDIENCE,
  ORCH_ORGANIZATION_SLUG: env.ORCH_ORGANIZATION_SLUG,
  ORCH_ORGANIZATION_NAME: env.ORCH_ORGANIZATION_NAME,
});

export class ApiContainer extends Container {
  defaultPort = 8000;
  sleepAfter = "10m";
  entrypoint = ["orchestrator", "api", "--host", "0.0.0.0", "--port", "8000"];
  envVars = sharedEnv();
  pingEndpoint = "localhost/readyz";
}

export class RunnerContainer extends Container {
  defaultPort = 8000;
  sleepAfter = "15m";
  entrypoint = [
    "orchestrator",
    "runner-service",
    "--host",
    "0.0.0.0",
    "--port",
    "8000",
  ];
  envVars = {
    ...sharedEnv(),
    ORCH_INTERNAL_TOKEN: env.ORCH_INTERNAL_TOKEN,
    ORCH_USER_SUBJECT: env.ORCH_RUNNER_SUBJECT,
  };
  pingEndpoint = "localhost/healthz";
}

async function wakeRunner(bindings: Env): Promise<Response> {
  const runner = getContainer(bindings.RUNNER_CONTAINER, "staging-runner");
  return runner.fetch(
    new Request("http://runner/internal/runner/once", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${bindings.ORCH_INTERNAL_TOKEN}`,
      },
    }),
  );
}

async function forwardApi(request: Request, bindings: Env): Promise<Response> {
  const url = new URL(request.url);
  const headers = new Headers(request.headers);
  const accessJwt = request.headers.get("Cf-Access-Jwt-Assertion");
  if (accessJwt) {
    headers.set("Cf-Access-Jwt-Assertion", accessJwt);
  }
  const api = await getRandom(bindings.API_CONTAINER, 2);
  const response = await api.fetch(new Request(request, { headers }));

  if (url.pathname === "/api/run" && request.method === "POST" && response.ok) {
    try {
      const body = (await response.clone().json()) as { run_id?: string };
      await bindings.WAKE_QUEUE.send({
        topic: "run.ready",
        message_key: body.run_id ?? crypto.randomUUID(),
      });
    } catch (error) {
      console.error("wake queue publish failed; cron/database sweep will recover", error);
    }
  }
  return response;
}

async function forwardReplicateWebhook(request: Request, bindings: Env): Promise<Response> {
  // This public callback bypasses Cloudflare Access headers. Authentication is the
  // raw-body Replicate HMAC plus the tenant-bound correlation token in the path.
  const api = await getRandom(bindings.API_CONTAINER, 2);
  return api.fetch(request);
}

export default {
  async fetch(request: Request, bindings: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname.startsWith("/webhooks/replicate/")) {
      return forwardReplicateWebhook(request, bindings);
    }
    if (url.pathname.startsWith("/api/")) {
      return forwardApi(request, bindings);
    }
    return bindings.ASSETS.fetch(request);
  },

  async queue(batch: MessageBatch, bindings: Env): Promise<void> {
    for (const message of batch.messages) {
      try {
        const response = await wakeRunner(bindings);
        if (!response.ok) {
          throw new Error(`Runner wake failed with HTTP ${response.status}`);
        }
        message.ack();
      } catch (error) {
        console.error("Runner wake failed", error);
        message.retry();
      }
    }
  },

  async scheduled(
    _controller: ScheduledController,
    bindings: Env,
    _context: ExecutionContext,
  ): Promise<void> {
    const response = await wakeRunner(bindings);
    if (!response.ok) {
      throw new Error(`Runner sweep failed with HTTP ${response.status}`);
    }
  },
} satisfies ExportedHandler<Env>;
