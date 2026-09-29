output "grafana_url" {
  value = "https://${local.grafana_hostname}/"
}

output "instance_id" {
  value = module.instance.id
}

output "shell" {
  description = "Shell on the instance, no SSH"
  value       = "aws ssm start-session --region ${var.region} --target ${module.instance.id}"
}

output "grafana_admin_password" {
  description = "Generated on the instance; read it through SSM"
  value       = "aws ssm start-session --region ${var.region} --target ${module.instance.id} --document-name AWS-StartInteractiveCommand --parameters command='sudo grep GRAFANA_ADMIN_PASSWORD /opt/bni/.env.stack'"
}
