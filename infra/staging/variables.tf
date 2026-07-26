variable "cloudflare_account_id" {
  type = string
}

variable "cloudflare_zone_id" {
  type = string
}

variable "app_hostname" {
  type    = string
  default = "staging.ugc-orchestrator.example"
}

variable "workers_hostname" {
  type    = string
  default = "ugc-orchestrator-staging.workers.dev"
}

variable "access_email_domain" {
  type = string
}

variable "r2_bucket_name" {
  type    = string
  default = "ugc-orchestrator-media-staging"
}

variable "neon_org_id" {
  type     = string
  nullable = true
  default  = null
}

variable "runtime_role_password" {
  type      = string
  sensitive = true
}
