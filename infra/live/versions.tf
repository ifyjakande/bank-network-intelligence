terraform {
  required_version = ">= 1.16"

  cloud {
    organization = "ifyjakande-sandbox"
    workspaces {
      name = "bni-demo"
    }
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.66"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.26"
    }
    github = {
      source  = "integrations/github"
      version = "~> 6.13"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.4"
    }
  }
}

# credentials: TFC dynamic credentials (TFC_AWS_PROVIDER_AUTH + TFC_AWS_RUN_ROLE_ARN on the
# workspace), CLOUDFLARE_API_TOKEN and GITHUB_TOKEN as sensitive workspace variables.
# Nothing in code.
provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project   = "bni"
      ManagedBy = "terraform"
      Stack     = "live"
    }
  }
}

provider "cloudflare" {}

provider "github" {
  owner = var.github_owner
}
