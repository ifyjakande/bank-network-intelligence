data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

# SSM Session Manager instead of SSH: no key pair, no port 22, every session logged
module "instance_role" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role"
  version = "6.8.2"

  name                    = "bni-demo-instance"
  use_name_prefix         = false
  description             = "Demo instance: SSM access and its own tunnel token"
  create_instance_profile = true

  trust_policy_permissions = {
    Ec2Assume = {
      actions = ["sts:AssumeRole"]
      principals = [{
        type        = "Service"
        identifiers = ["ec2.amazonaws.com"]
      }]
    }
  }

  policies = {
    SSMCore = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
  }

  create_inline_policy = true
  inline_policy_permissions = {
    ReadTunnelToken = {
      actions   = ["ssm:GetParameter"]
      resources = [module.tunnel_token.ssm_parameter_arn]
    }
  }
}

module "instance" {
  source  = "terraform-aws-modules/ec2-instance/aws"
  version = "6.4.1"

  name          = "bni-demo"
  ami           = data.aws_ssm_parameter.al2023_arm64.value
  instance_type = var.instance_type
  subnet_id     = module.vpc.public_subnets[0]

  vpc_security_group_ids      = [module.instance_sg.id]
  create_security_group       = false
  associate_public_ip_address = true # outbound only: the security group admits nothing
  iam_instance_profile        = module.instance_role.instance_profile_name
  ignore_ami_changes          = true
  monitoring                  = false

  metadata_options = {
    http_endpoint               = "enabled"
    http_tokens                 = "required" # IMDSv2 only
    http_put_response_hop_limit = 1
  }

  root_block_device = {
    type                  = "gp3"
    size                  = 60
    encrypted             = true
    delete_on_termination = true
  }

  user_data_replace_on_change = true
  user_data = templatefile("${path.module}/bootstrap.sh.tftpl", {
    region              = var.region
    git_repository      = var.git_repository
    git_ref             = var.git_ref
    token_parameter     = module.tunnel_token.ssm_parameter_name
    grafana_root_url    = "https://${local.grafana_hostname}/"
    compose_version     = "5.5.1"
    buildx_version      = "0.37.1"
    cloudflared_version = "2026.9.3"
  })
}
