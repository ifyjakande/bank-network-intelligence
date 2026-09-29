# The tunnel token goes to SSM as a SecureString and is read by the instance at boot, never
# placed in user data (which anyone who can describe the instance can read).
module "tunnel_token" {
  source  = "terraform-aws-modules/ssm-parameter/aws"
  version = "2.1.2"

  name        = "/bni/demo/cloudflare-tunnel-token"
  description = "cloudflared token for the bni-demo tunnel"
  type        = "SecureString"
  secure_type = true
  value       = data.cloudflare_zero_trust_tunnel_cloudflared_token.grafana.token
}

# Read-only deploy key so the instance can clone the repo whether it is public or private.
# The private half lives in Terraform state (encrypted in TFC) and SSM, nowhere else, and
# the key is removed from the repo on destroy.
resource "tls_private_key" "deploy" {
  algorithm = "ED25519"
}

resource "github_repository_deploy_key" "instance" {
  repository = var.github_repository
  title      = "bni-demo instance (terraform)"
  key        = tls_private_key.deploy.public_key_openssh
  read_only  = true
}

module "deploy_key" {
  source  = "terraform-aws-modules/ssm-parameter/aws"
  version = "2.1.2"

  name        = "/bni/demo/github-deploy-key"
  description = "Read-only GitHub deploy key for the bni-demo instance"
  type        = "SecureString"
  secure_type = true
  value       = tls_private_key.deploy.private_key_openssh
}
