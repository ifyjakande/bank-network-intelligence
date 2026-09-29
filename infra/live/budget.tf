module "budget" {
  source  = "cloudposse/budgets/aws"
  version = "0.8.1"

  name                  = "bni-demo"
  notifications_enabled = false # email straight from AWS Budgets: no SNS topic or KMS key

  budgets = [
    {
      name         = "bni-demo-monthly"
      budget_type  = "COST"
      limit_amount = var.budget_monthly_usd
      limit_unit   = "USD"
      time_unit    = "MONTHLY"
      notification = [
        {
          comparison_operator        = "GREATER_THAN"
          threshold                  = "50"
          threshold_type             = "PERCENTAGE"
          notification_type          = "ACTUAL"
          subscriber_email_addresses = [var.budget_email]
        },
        {
          comparison_operator        = "GREATER_THAN"
          threshold                  = "100"
          threshold_type             = "PERCENTAGE"
          notification_type          = "FORECASTED"
          subscriber_email_addresses = [var.budget_email]
        },
      ]
    },
  ]
}
