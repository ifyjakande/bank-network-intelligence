variable "region" {
  type    = string
  default = "us-east-1"
}

variable "tfc_organization" {
  type    = string
  default = "ifyjakande-sandbox"
}

variable "tfc_live_workspace" {
  type    = string
  default = "bni-demo"
}

variable "github_repository" {
  description = "owner@owner_id/repo@repo_id: GitHub's immutable OIDC subject, safe from renames"
  type        = string
  default     = "ifyjakande@53233563/bank-network-intelligence@1395810700"
}
