terraform {
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.56"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Application = "ugc-orchestrator"
      Environment = var.environment
      ManagedBy   = "opentofu"
    }
  }
}
