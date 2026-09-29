# One public subnet and no NAT gateway: the instance reaches out (packages, images,
# Cloudflare, SSM) directly, and nothing reaches in.
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "6.7.3"

  name = "bni-demo"
  cidr = "10.60.0.0/16"
  azs  = ["${var.region}a"]

  public_subnets          = ["10.60.1.0/24"]
  map_public_ip_on_launch = false
  enable_nat_gateway      = false
  enable_dns_hostnames    = true

  # the default security group allows nothing
  manage_default_security_group  = true
  default_security_group_ingress = []
  default_security_group_egress  = []
}

# Outbound is limited to HTTPS (packages, images, git, SSM) and the tunnel's port 7844.
# The destinations are public CDNs with no fixed ranges, so the CIDR has to stay open;
# DNS, NTP and instance metadata go to link-local addresses that security groups don't filter.
#trivy:ignore:AWS-0104
module "instance_sg" {
  source  = "terraform-aws-modules/security-group/aws"
  version = "6.0.0"

  name            = "bni-demo-instance"
  description     = "Demo instance: no inbound at all; Grafana is published through a Cloudflare Tunnel"
  vpc_id          = module.vpc.vpc_id
  use_name_prefix = false

  ingress_rules = {}
  egress_rules = {
    https = {
      description = "Packages, container images, git, SSM"
      cidr_ipv4   = "0.0.0.0/0"
      ip_protocol = "tcp"
      from_port   = 443
      to_port     = 443
    }
    tunnel_quic = {
      description = "Cloudflare Tunnel (QUIC)"
      cidr_ipv4   = "0.0.0.0/0"
      ip_protocol = "udp"
      from_port   = 7844
      to_port     = 7844
    }
    tunnel_http2 = {
      description = "Cloudflare Tunnel (HTTP/2 fallback)"
      cidr_ipv4   = "0.0.0.0/0"
      ip_protocol = "tcp"
      from_port   = 7844
      to_port     = 7844
    }
  }
}
