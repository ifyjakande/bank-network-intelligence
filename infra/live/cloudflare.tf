# The one exception to "modules only": no trustworthy registry module exists for Cloudflare
# Tunnel, so these are the official cloudflare provider's resources, as in Cloudflare's own
# Terraform guide. They are destroyed with everything else.

data "cloudflare_zone" "this" {
  filter = {
    name = var.cloudflare_zone
  }
}

locals {
  grafana_hostname = "${var.grafana_subdomain}.${var.cloudflare_zone}"
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "grafana" {
  account_id = var.cloudflare_account_id
  name       = "bni-demo"
  config_src = "cloudflare"
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "grafana" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.grafana.id
  config = {
    ingress = [
      {
        hostname = local.grafana_hostname
        service  = "http://localhost:3000"
      },
      {
        service = "http_status:404"
      },
    ]
  }
}

resource "cloudflare_dns_record" "grafana" {
  zone_id = data.cloudflare_zone.this.zone_id
  name    = local.grafana_hostname
  content = "${cloudflare_zero_trust_tunnel_cloudflared.grafana.id}.cfargotunnel.com"
  type    = "CNAME"
  ttl     = 1
  proxied = true
  comment = "bni demo grafana (terraform)"
}

data "cloudflare_zero_trust_tunnel_cloudflared_token" "grafana" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.grafana.id
}
