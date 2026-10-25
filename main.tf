locals {
  skills = merge(
    var.smart_home_skill_id == null ? {} : {
      smart_home = {
        skill_id  = var.smart_home_skill_id
        principal = "alexa-connectedhome.amazon.com"
      }
    },
    var.custom_skill_id == null ? {} : {
      custom = {
        skill_id  = var.custom_skill_id
        principal = "alexa-appkit.amazon.com"
      }
    }
  )
  account_linking_client_ids = {
    us-east-1 = "https://pitangui.amazon.com/"
    eu-west-1 = "https://layla.amazon.com/"
    us-west-2 = "https://alexa.amazon.co.jp/"
  }
}

data "archive_file" "bridge" {
  type        = "zip"
  output_path = "${path.module}/.build/bridge.zip"
  source {
    content  = file("${path.module}/lambda/bridge.py")
    filename = "bridge.py"
  }
  source {
    content  = file("${path.module}/lambda/relay.py")
    filename = "relay.py"
  }
}

resource "aws_cloudwatch_log_group" "bridge" {
  for_each = local.skills

  name              = "/aws/lambda/${var.name}-${each.key}"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "bridge" {
  for_each = local.skills

  name = "${var.name}-${each.key}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "logs" {
  for_each = local.skills

  name = "write-own-logs"
  role = aws_iam_role.bridge[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = "${aws_cloudwatch_log_group.bridge[each.key].arn}:*"
    }]
  })
}

resource "aws_lambda_function" "bridge" {
  for_each = local.skills

  function_name    = "${var.name}-${each.key}"
  description      = "Forward Alexa ${each.key} requests to Home Assistant"
  role             = aws_iam_role.bridge[each.key].arn
  filename         = data.archive_file.bridge.output_path
  source_code_hash = data.archive_file.bridge.output_base64sha256
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "bridge.${each.key}"
  timeout          = 8
  memory_size      = 512

  environment {
    variables = merge({
      BASE_URL = var.home_assistant_url == null ? "" : trimsuffix(var.home_assistant_url, "/")
      SKILL_ID = each.value.skill_id
      }, var.relay_enabled ? {
      RELAY_FUNCTION = aws_lambda_function.relay[0].function_name
    } : {})
  }

  lifecycle {
    precondition {
      condition     = var.relay_enabled || var.home_assistant_url != null
      error_message = "Provide home_assistant_url or enable the relay."
    }
  }

  depends_on = [aws_iam_role_policy.logs]
}

resource "aws_lambda_permission" "alexa" {
  for_each = local.skills

  statement_id       = "AllowAlexaSkill"
  action             = "lambda:InvokeFunction"
  function_name      = aws_lambda_function.bridge[each.key].function_name
  principal          = each.value.principal
  event_source_token = each.value.skill_id
}
