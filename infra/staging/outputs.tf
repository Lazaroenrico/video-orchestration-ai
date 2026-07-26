output "staging_origin" {
  value = "https://${var.app_hostname}"
}

output "neon_project_id" {
  value = neon_project.staging.id
}
