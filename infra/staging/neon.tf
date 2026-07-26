resource "neon_project" "staging" {
  name              = "ugc-orchestrator-staging"
  org_id            = var.neon_org_id
  region_id         = "aws-sa-east-1"
  pg_version        = 16
  history_retention = 604800

  branch = {
    name      = "main"
    protected = true
    endpoint = {
      min_cu          = 0.25
      max_cu          = 1
      suspend_timeout = 300
    }
  }
}

resource "neon_role" "migration" {
  project_id = neon_project.staging.id
  branch_id  = neon_project.staging.branch.id
  name       = "orchestrator_migration"
}

resource "neon_database" "orchestrator" {
  project_id = neon_project.staging.id
  branch_id  = neon_project.staging.branch.id
  name       = "orchestrator"
  owner_name = neon_role.migration.name
}

locals {
  direct_host = neon_project.staging.branch.endpoint.host
}

output "migration_database_url" {
  description = "Conexão direta privilegiada; nunca use endpoint -pooler."
  value = format(
    "postgresql://%s:%s@%s/%s?sslmode=require",
    neon_role.migration.name,
    urlencode(neon_role.migration.password),
    local.direct_host,
    neon_database.orchestrator.name,
  )
  sensitive = true
}

output "runtime_database_url" {
  description = "Conexão direta do papel RLS; nunca use endpoint -pooler."
  value = format(
    "postgresql://orchestrator_runtime:%s@%s/%s?sslmode=require",
    urlencode(var.runtime_role_password),
    local.direct_host,
    neon_database.orchestrator.name,
  )
  sensitive = true
}
