interface ContainerSecrets {
  DATABASE_URL: string;
  R2_ENDPOINT_URL: string;
  R2_ACCESS_KEY_ID: string;
  R2_SECRET_ACCESS_KEY: string;
  R2_BUCKET: string;
  CF_ACCESS_TEAM_DOMAIN: string;
  CF_ACCESS_AUDIENCE: string;
  ORCH_INTERNAL_TOKEN: string;
}

interface Env extends ContainerSecrets {}

declare namespace Cloudflare {
  interface Env extends ContainerSecrets {}
}
