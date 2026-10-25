locals {
  relay_resources = var.relay_enabled ? toset(["relay"]) : toset([])
}

resource "aws_dynamodb_table" "relay" {
  count        = var.relay_enabled ? 1 : 0
  name         = "${var.name}-relay"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "kind"
    type = "S"
  }
  attribute {
    name = "expires"
    type = "N"
  }
  global_secondary_index {
    name            = "pending"
    hash_key        = "kind"
    range_key       = "expires"
    projection_type = "ALL"
  }
  ttl {
    attribute_name = "expires"
    enabled        = true
  }
  server_side_encryption {
    enabled = true
  }
}

resource "aws_cloudwatch_log_group" "relay" {
  count             = var.relay_enabled ? 1 : 0
  name              = "/aws/lambda/${var.name}-relay"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "relay" {
  count = var.relay_enabled ? 1 : 0
  name  = "${var.name}-relay"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRole",
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

data "aws_caller_identity" "current" {}

resource "aws_iam_role_policy" "relay" {
  count = var.relay_enabled ? 1 : 0
  role  = aws_iam_role.relay[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Query"]
        Resource = [aws_dynamodb_table.relay[0].arn, "${aws_dynamodb_table.relay[0].arn}/index/pending"]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.relay[0].arn}:*"
      },
      {
        Effect = "Allow"
        Action = ["ssm:GetParameter"]
        Resource = [
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.name}/connector-key",
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.name}/oauth-secret"
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["execute-api:ManageConnections"]
        Resource = "${aws_apigatewayv2_api.socket[0].execution_arn}/connector/POST/@connections/*"
      }
    ]
  })
}

resource "aws_iam_role_policy" "invoke_relay" {
  for_each = var.relay_enabled ? local.skills : {}
  role     = aws_iam_role.bridge[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow", Action = "lambda:InvokeFunction",
      Resource = aws_lambda_function.relay[0].arn
    }]
  })
}

resource "aws_lambda_function" "relay" {
  count            = var.relay_enabled ? 1 : 0
  function_name    = "${var.name}-relay"
  role             = aws_iam_role.relay[0].arn
  filename         = data.archive_file.bridge.output_path
  source_code_hash = data.archive_file.bridge.output_base64sha256
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "relay.handler"
  timeout          = 7
  memory_size      = 512
  environment {
    variables = {
      TABLE               = aws_dynamodb_table.relay[0].name
      SOCKET_ENDPOINT     = "https://${aws_apigatewayv2_api.socket[0].id}.execute-api.${var.aws_region}.amazonaws.com/connector"
      CONNECTOR_PARAMETER = "/${var.name}/connector-key"
      OAUTH_PARAMETER     = "/${var.name}/oauth-secret"
      CLIENT_IDS          = jsonencode([for skill in values(local.skills) : skill.skill_id])
      VENDOR_ID           = var.alexa_vendor_id
    }
  }
  lifecycle {
    precondition {
      condition     = var.alexa_vendor_id != ""
      error_message = "Relay account linking requires alexa_vendor_id."
    }
  }
  depends_on = [aws_iam_role_policy.relay]
}

resource "aws_apigatewayv2_api" "socket" {
  count                      = var.relay_enabled ? 1 : 0
  name                       = "${var.name}-connector"
  protocol_type              = "WEBSOCKET"
  route_selection_expression = "$request.body.action"
}

resource "aws_apigatewayv2_integration" "socket" {
  count              = var.relay_enabled ? 1 : 0
  api_id             = aws_apigatewayv2_api.socket[0].id
  integration_type   = "AWS_PROXY"
  integration_uri    = aws_lambda_function.relay[0].invoke_arn
  integration_method = "POST"
}

resource "aws_apigatewayv2_route" "socket" {
  for_each  = var.relay_enabled ? toset(["$connect", "$disconnect", "$default"]) : toset([])
  api_id    = aws_apigatewayv2_api.socket[0].id
  route_key = each.key
  target    = "integrations/${aws_apigatewayv2_integration.socket[0].id}"
}

resource "aws_apigatewayv2_stage" "socket" {
  count       = var.relay_enabled ? 1 : 0
  api_id      = aws_apigatewayv2_api.socket[0].id
  name        = "connector"
  auto_deploy = true
  default_route_settings {
    throttling_burst_limit = 10
    throttling_rate_limit  = 5
  }
}

resource "aws_apigatewayv2_api" "http" {
  count         = var.relay_enabled ? 1 : 0
  name          = "${var.name}-linking"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "http" {
  count                  = var.relay_enabled ? 1 : 0
  api_id                 = aws_apigatewayv2_api.http[0].id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.relay[0].invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "http" {
  for_each  = var.relay_enabled ? toset(["GET /authorize", "GET /status", "POST /token"]) : toset([])
  api_id    = aws_apigatewayv2_api.http[0].id
  route_key = each.key
  target    = "integrations/${aws_apigatewayv2_integration.http[0].id}"
}

resource "aws_apigatewayv2_stage" "http" {
  count       = var.relay_enabled ? 1 : 0
  api_id      = aws_apigatewayv2_api.http[0].id
  name        = "$default"
  auto_deploy = true
  default_route_settings {
    throttling_burst_limit = 10
    throttling_rate_limit  = 2
  }
}

resource "aws_lambda_permission" "socket" {
  count         = var.relay_enabled ? 1 : 0
  statement_id  = "ConnectorGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.relay[0].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.socket[0].execution_arn}/*"
}

resource "aws_lambda_permission" "http" {
  count         = var.relay_enabled ? 1 : 0
  statement_id  = "LinkingGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.relay[0].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http[0].execution_arn}/*"
}
