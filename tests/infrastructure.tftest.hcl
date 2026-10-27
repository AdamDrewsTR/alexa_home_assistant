mock_provider "aws" {}

variables {
  home_assistant_url  = "https://ha.example.com/"
  smart_home_skill_id = "amzn1.ask.skill.00000000-0000-0000-0000-000000000001"
}

run "smart_home_only" {
  command = plan

  assert {
    condition     = length(aws_lambda_function.bridge) == 1 && output.custom_endpoint_arn == null
    error_message = "The custom skill must be opt-in."
  }
  assert {
    condition     = aws_lambda_permission.alexa["smart_home"].principal == "alexa-connectedhome.amazon.com" && aws_lambda_permission.alexa["smart_home"].event_source_token == var.smart_home_skill_id
    error_message = "Smart Home invocation must be restricted to its Alexa skill."
  }
  assert {
    condition     = output.account_linking.authorization_uri == "https://ha.example.com/auth/authorize"
    error_message = "Account linking must use native HA OAuth without duplicate slashes."
  }
}

run "both_skills" {
  command = plan
  variables {
    custom_skill_id = "amzn1.ask.skill.00000000-0000-0000-0000-000000000002"
    aws_region      = "eu-west-1"
  }
  assert {
    condition     = length(aws_lambda_function.bridge) == 2 && aws_lambda_function.bridge["custom"].handler == "bridge.custom"
    error_message = "Custom intents need a separate handler."
  }
  assert {
    condition     = aws_lambda_permission.alexa["custom"].principal == "alexa-appkit.amazon.com" && aws_lambda_permission.alexa["custom"].event_source_token == var.custom_skill_id
    error_message = "Custom invocation must be restricted to its Alexa skill."
  }
  assert {
    condition     = output.account_linking.client_id == "https://layla.amazon.com/"
    error_message = "Account linking must select the regional Alexa client ID."
  }
}

run "reject_insecure_url" {
  command = plan
  variables {
    home_assistant_url = "http://ha.example.com"
  }
  expect_failures = [var.home_assistant_url]
}

run "reject_unsupported_region" {
  command = plan
  variables {
    aws_region = "us-east-2"
  }
  expect_failures = [var.aws_region]
}

run "relay_without_public_ha" {
  command = plan
  variables {
    relay_enabled      = true
    alexa_vendor_id    = "TESTVENDOR"
    home_assistant_url = null
  }
  assert {
    condition     = length(aws_lambda_function.relay) == 1 && aws_lambda_function.bridge["smart_home"].environment[0].variables.BASE_URL == ""
    error_message = "Relay mode must not require public HA access."
  }
  assert {
    condition     = aws_dynamodb_table.relay[0].billing_mode == "PAY_PER_REQUEST" && aws_dynamodb_table.relay[0].ttl[0].enabled
    error_message = "Transient relay data must expire, using on-demand billing."
  }
  assert {
    condition     = length(aws_apigatewayv2_route.http) == 4 && length(aws_apigatewayv2_route.socket) == 3 && contains(keys(aws_apigatewayv2_route.http), "POST /api/webhook/{webhook_id}")
    error_message = "Publish only OAuth, enrolled phone webhooks, and the authenticated connector socket."
  }
}

run "relay_bootstrap_without_skills" {
  command = plan
  variables {
    relay_enabled       = true
    alexa_vendor_id     = "TESTVENDOR"
    smart_home_skill_id = null
    home_assistant_url  = null
  }
  assert {
    condition     = length(aws_lambda_function.bridge) == 0 && output.smart_home_endpoint_arn == null
    error_message = "The wizard must be able to deploy the relay before creating Alexa skills."
  }
}
