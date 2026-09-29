variable "region" {
  type    = string
  default = "us-east-1"
}

variable "instance_type" {
  description = "The full stack (4 ClickHouse servers, Kafka, Postgres, Grafana) needs ~10-12 GB"
  type        = string
  default     = "t4g.xlarge"
}

variable "git_repository" {
  type    = string
  default = "https://github.com/ifyjakande/bank-network-intelligence.git"
}

variable "git_ref" {
  description = "Commit or branch the instance deploys on first boot"
  type        = string
  default     = "main"
}

variable "cloudflare_account_id" {
  type = string
}

variable "cloudflare_zone" {
  description = "Domain on the Cloudflare account"
  type        = string
  default     = "ifeakande.com"
}

variable "grafana_subdomain" {
  type    = string
  default = "netdemo"
}

variable "budget_monthly_usd" {
  type    = string
  default = "30"
}

variable "budget_email" {
  description = "Who gets the budget alerts"
  type        = string
}
