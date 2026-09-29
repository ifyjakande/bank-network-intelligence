output "tfc_live_role_arn" {
  description = "Set as TFC_AWS_RUN_ROLE_ARN on the bni-demo workspace"
  value       = module.tfc_live_role.arn
}

output "github_deploy_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE_ARN repository variable"
  value       = module.github_deploy_role.arn
}
