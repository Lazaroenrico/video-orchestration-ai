terraform {
  required_version = ">= 1.8.0"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.22"
    }
    neon = {
      source  = "terraform-community-providers/neon"
      version = "~> 0.1.15"
    }
  }
}

provider "cloudflare" {}
provider "neon" {}
