output "smart_home_endpoint_arn" {
  description = "Smart Home skill > Smart Home service endpoint."
  value       = try(aws_lambda_function.bridge["smart_home"].arn, null)
}

output "custom_endpoint_arn" {
  description = "Custom skill > Endpoint > AWS Lambda ARN (null when disabled)."
  value       = try(aws_lambda_function.bridge["custom"].arn, null)
}

output "account_linking" {
  description = "Account-linking endpoints. Relay secrets are provisioned by the wizard, not Terraform."
  value = {
    authorization_uri     = var.relay_enabled ? "${aws_apigatewayv2_api.http[0].api_endpoint}/authorize" : "${trimsuffix(coalesce(var.home_assistant_url, "https://invalid.local"), "/")}/auth/authorize"
    access_token_uri      = var.relay_enabled ? "${aws_apigatewayv2_api.http[0].api_endpoint}/token" : "${trimsuffix(coalesce(var.home_assistant_url, "https://invalid.local"), "/")}/auth/token"
    client_id             = var.relay_enabled ? var.smart_home_skill_id : local.account_linking_client_ids[var.aws_region]
    custom_client_id      = var.relay_enabled ? var.custom_skill_id : local.account_linking_client_ids[var.aws_region]
    authentication_scheme = "Credentials in request body"
    grant_type            = "Authorization Code Grant"
    smart_home_scope      = "smart_home"
    custom_scope          = "intent"
  }
}

output "relay" {
  description = "Connector bootstrap metadata; no secret values."
  value = var.relay_enabled ? {
    websocket_url       = "${aws_apigatewayv2_api.socket[0].api_endpoint}/connector"
    connector_parameter = "/${var.name}/connector-key"
    oauth_parameter     = "/${var.name}/oauth-secret"
    http_url            = aws_apigatewayv2_api.http[0].api_endpoint
    function_arn        = aws_lambda_function.relay[0].arn
  } : null
}
