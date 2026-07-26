variable "aws_region" {
  description = "Região do exercício; mantém compute perto do banco atual."
  type        = string
  default     = "sa-east-1"
}

variable "environment" {
  type    = string
  default = "staging"
}

variable "image_tag" {
  description = "SHA imutável já publicado no ECR; nunca use latest."
  type        = string

  validation {
    condition     = length(trimspace(var.image_tag)) >= 7 && var.image_tag != "latest"
    error_message = "image_tag deve ser um SHA imutável e não pode ser latest."
  }
}

variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  type = list(string)

  validation {
    condition     = length(var.private_subnet_ids) >= 2
    error_message = "Use ao menos duas subnets privadas em AZs distintas."
  }
}

variable "public_subnet_ids" {
  type = list(string)

  validation {
    condition     = length(var.public_subnet_ids) >= 2
    error_message = "Use ao menos duas subnets públicas em AZs distintas para o ALB."
  }
}

variable "certificate_arn" {
  type = string
}

variable "media_bucket_name" {
  type = string
}

variable "database_secret_arn" {
  description = "Secret contendo somente a DATABASE_URL runtime direta."
  type        = string
}

variable "r2_access_key_secret_arn" {
  type = string
}

variable "r2_secret_key_secret_arn" {
  type = string
}

variable "r2_account_id" {
  type = string
}

variable "r2_bucket" {
  type = string
}

variable "cloudflare_access_team_domain" {
  type = string
}

variable "cloudflare_access_audience" {
  type = string
}

variable "organization_slug" {
  type    = string
  default = "staging"
}

variable "organization_name" {
  type    = string
  default = "UGC Orchestrator Staging"
}

variable "runner_subject" {
  type    = string
  default = "service|aws-ecs-runner"
}

variable "app_origin" {
  type = string
}

variable "api_desired_count" {
  description = "Zero no exercício: task definition pronta, sem tráfego."
  type        = number
  default     = 0
}

variable "runner_desired_count" {
  description = "Zero no exercício: não consome SQS antes do Go."
  type        = number
  default     = 0
}

variable "alarm_topic_arns" {
  type    = list(string)
  default = []
}
