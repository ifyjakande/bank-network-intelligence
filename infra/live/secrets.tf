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
