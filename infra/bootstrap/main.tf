# One-time trust between Terraform Cloud / GitHub Actions and AWS, so neither needs stored
# keys. Applied through the bni-bootstrap workspace with one-off AWS keys that are removed
# straight after (infra/tfc.py bootstrap-keys / drop-bootstrap-keys); state lives in TFC.
# Everything is built from terraform-aws-modules; destroying this removes the trust.

terraform {
  required_version = ">= 1.16"

  cloud {
    organization = "ifyjakande-sandbox"
    workspaces {
      name = "bni-bootstrap"
    }
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.66"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project   = "bni"
      ManagedBy = "terraform"
      Stack     = "bootstrap"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  tfc_host = "app.terraform.io"
}

# --- Terraform Cloud: dynamic credentials for the live workspace ------------------------

module "tfc_oidc_provider" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-oidc-provider"
  version = "6.8.2"

  url            = "https://${local.tfc_host}"
  client_id_list = ["aws.workload.identity"]
}

# what the live workspace may do: build the demo, and manage only its own IAM roles
module "tfc_live_iam_scope" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-policy"
  version = "6.8.2"

  name        = "bni-tfc-live-iam-scope"
  description = "IAM actions the bni-demo workspace needs, limited to bni-* roles and profiles"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "OwnRolesOnly"
        Effect = "Allow"
        Action = [
          "iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:UpdateRole",
          "iam:TagRole", "iam:UntagRole", "iam:ListRoleTags",
          "iam:AttachRolePolicy", "iam:DetachRolePolicy", "iam:ListAttachedRolePolicies",
          "iam:PutRolePolicy", "iam:GetRolePolicy", "iam:DeleteRolePolicy", "iam:ListRolePolicies",
          "iam:CreateInstanceProfile", "iam:DeleteInstanceProfile", "iam:GetInstanceProfile",
          "iam:AddRoleToInstanceProfile", "iam:RemoveRoleFromInstanceProfile",
          "iam:TagInstanceProfile", "iam:ListInstanceProfilesForRole", "iam:PassRole",
        ]
        Resource = [
          "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/bni-*",
          "arn:aws:iam::${data.aws_caller_identity.current.account_id}:instance-profile/bni-*",
        ]
      },
      {
        Sid      = "ReadPolicies"
        Effect   = "Allow"
        Action   = ["iam:GetPolicy", "iam:GetPolicyVersion", "iam:ListPolicyVersions"]
        Resource = "*"
      },
    ]
  })
}

module "tfc_live_role" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role"
  version = "6.8.2"

  name            = "bni-tfc-live"
  use_name_prefix = false
  description     = "Assumed by Terraform Cloud applies of the bni-demo workspace"

  # applies only: they need to build, and only the workspace owner can confirm one
  enable_oidc        = true
  oidc_provider_urls = [local.tfc_host]
  oidc_audiences     = ["aws.workload.identity"]
  oidc_wildcard_subjects = [
    "organization:${var.tfc_organization}:project:*:workspace:${var.tfc_live_workspace}:run_phase:apply",
  ]

  policies = {
    PowerUser = "arn:aws:iam::aws:policy/PowerUserAccess"
    IamScope  = module.tfc_live_iam_scope.arn
  }
}

# Plans (including speculative plans on pull requests) only read. A separate role keeps the
# apply role's power, which could create and escalate bni-* roles, away from any plan.
module "tfc_plan_role" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role"
  version = "6.8.2"

  name            = "bni-tfc-plan"
  use_name_prefix = false
  description     = "Assumed by Terraform Cloud plans of the bni-demo workspace; read only"

  enable_oidc        = true
  oidc_provider_urls = [local.tfc_host]
  oidc_audiences     = ["aws.workload.identity"]
  oidc_wildcard_subjects = [
    "organization:${var.tfc_organization}:project:*:workspace:${var.tfc_live_workspace}:run_phase:plan",
  ]

  policies = {
    ReadOnly = "arn:aws:iam::aws:policy/ReadOnlyAccess"
  }

  create_inline_policy = true
  inline_policy_permissions = {
    # refreshing the demo's SecureString parameters needs their values, and only theirs
    DecryptOwnParameters = {
      actions   = ["kms:Decrypt"]
      resources = ["*"]
      condition = [
        {
          test     = "StringEquals"
          variable = "kms:ViaService"
          values   = ["ssm.${var.region}.amazonaws.com"]
        },
        {
          test     = "StringLike"
          variable = "kms:EncryptionContext:PARAMETER_ARN"
          values   = ["arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/bni/*"]
        },
      ]
    }
    # ReadOnlyAccess would also read every object and secret in the account
    NoDataReads = {
      effect    = "Deny"
      actions   = ["s3:GetObject", "s3:GetObjectVersion", "secretsmanager:GetSecretValue"]
      resources = ["*"]
    }
  }
}

# --- GitHub Actions: redeploy the app over SSM on merge to main --------------------------

# The GitHub OIDC provider already exists in this account (one per URL) and other repos
# use it, so it is referenced by its standard ARN through enable_github_oidc, not managed here.
module "github_deploy_role" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role"
  version = "6.8.2"

  name            = "bni-github-deploy"
  use_name_prefix = false
  description     = "GitHub Actions on main: run the deploy command on the demo instance via SSM"

  enable_github_oidc = true
  oidc_subjects      = ["repo:${var.github_repository}:ref:refs/heads/main"]

  create_inline_policy = true
  inline_policy_permissions = {
    SendToDemoInstanceOnly = {
      actions   = ["ssm:SendCommand"]
      resources = ["arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:instance/*"]
      condition = [{
        test     = "StringEquals"
        variable = "ssm:resourceTag/Project"
        values   = ["bni"]
      }]
    }
    RunShellScriptDocument = {
      actions   = ["ssm:SendCommand"]
      resources = ["arn:aws:ssm:${var.region}::document/AWS-RunShellScript"]
    }
    ReadResults = {
      actions   = ["ssm:GetCommandInvocation", "ssm:ListCommandInvocations", "ec2:DescribeInstances"]
      resources = ["*"]
    }
  }
}
