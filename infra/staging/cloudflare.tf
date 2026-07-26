resource "cloudflare_r2_bucket" "media" {
  account_id    = var.cloudflare_account_id
  name          = var.r2_bucket_name
  location      = "enam"
  storage_class = "Standard"
}

resource "cloudflare_r2_bucket_cors" "media" {
  account_id  = var.cloudflare_account_id
  bucket_name = cloudflare_r2_bucket.media.name
  rules = [{
    id = "staging-browser-read"
    allowed = {
      methods = ["GET", "HEAD"]
      origins = ["https://${var.app_hostname}"]
      headers = ["Range"]
    }
    expose_headers  = ["Content-Length", "Content-Range", "ETag"]
    max_age_seconds = 3600
  }]
}

resource "cloudflare_queue" "wake_dlq" {
  account_id = var.cloudflare_account_id
  queue_name = "ugc-wake-dlq-staging"
  settings = {
    message_retention_period = 1209600
  }
}

resource "cloudflare_queue" "wake" {
  account_id = var.cloudflare_account_id
  queue_name = "ugc-wake-staging"
  settings = {
    delivery_delay           = 0
    message_retention_period = 345600
  }
}

resource "cloudflare_zero_trust_access_application" "staging" {
  account_id       = var.cloudflare_account_id
  name             = "UGC Orchestrator Staging"
  domain           = var.app_hostname
  type             = "self_hosted"
  session_duration = "8h"
  policies = [{
    name       = "Allow staging organization"
    decision   = "allow"
    precedence = 1
    include = [{
      email_domain = {
        domain = var.access_email_domain
      }
    }]
    require = [{
      auth_method = {
        auth_method = "mfa"
      }
    }]
  }]
}

resource "cloudflare_ruleset" "api_rate_limit" {
  zone_id = var.cloudflare_zone_id
  name    = "UGC staging API rate limiting"
  kind    = "zone"
  phase   = "http_ratelimit"
  rules = [{
    ref         = "ugc_staging_api_per_ip"
    description = "Limit API requests per client IP"
    expression  = "(http.host eq \"${var.app_hostname}\" and http.request.uri.path matches \"^/api/\")"
    action      = "block"
    ratelimit = {
      characteristics     = ["cf.colo.id", "ip.src"]
      period              = 60
      requests_per_period = 120
      mitigation_timeout  = 60
    }
  }]
}

resource "cloudflare_dns_record" "staging" {
  zone_id = var.cloudflare_zone_id
  name    = var.app_hostname
  type    = "CNAME"
  content = var.workers_hostname
  proxied = true
  ttl     = 1
  comment = "Managed by OpenTofu; Worker Static Assets + Containers."
}

output "wake_queue_id" {
  value = cloudflare_queue.wake.queue_id
}

output "r2_bucket" {
  value = cloudflare_r2_bucket.media.name
}

output "access_audience" {
  value = cloudflare_zero_trust_access_application.staging.aud
}
