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

module "instance_sg" {
  source  = "terraform-aws-modules/security-group/aws"
  version = "6.0.0"

  name            = "bni-demo-instance"
  description     = "Demo instance: no inbound at all; Grafana is published through a Cloudflare Tunnel"
  vpc_id          = module.vpc.vpc_id
  use_name_prefix = false

  ingress_rules = {}
  egress_rules = {
    all_ipv4 = {
      description = "Outbound: packages, images, Cloudflare Tunnel, SSM"
      cidr_ipv4   = "0.0.0.0/0"
      ip_protocol = "-1"
    }
  }
}
