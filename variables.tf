variable "name" {
  description = "Resource name prefix; use a different name for each deployment."
  type        = string
  default     = "ha-alexa"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,39}$", var.name))
    error_message = "Use 1-40 lowercase letters, digits, or hyphens, starting with a letter."
  }
}

variable "aws_region" {
  description = "Alexa endpoint region for the skill locale, not your physical location."
  type        = string
  default     = "us-east-1"

  validation {
    condition     = contains(["us-east-1", "eu-west-1", "us-west-2"], var.aws_region)
    error_message = "Alexa requires us-east-1, eu-west-1, or us-west-2."
  }
}

variable "home_assistant_url" {
  description = "Existing public HTTPS Home Assistant origin on port 443, without a path."
  type        = string
  default     = null

  validation {
    condition     = var.home_assistant_url == null ? true : can(regex("^https://[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:443)?/?$", var.home_assistant_url))
    error_message = "Provide an HTTPS hostname on port 443, with no credentials, path, query, or fragment."
  }

}

variable "relay_enabled" {
  description = "Use the outbound connector instead of a public HA URL."
  type        = bool
  default     = false
}

variable "alexa_vendor_id" {
  description = "Amazon vendor ID used to restrict account-linking redirect URLs in relay mode."
  type        = string
  default     = ""
  validation {
    condition     = var.alexa_vendor_id == "" || can(regex("^[A-Za-z0-9]+$", var.alexa_vendor_id))
    error_message = "Vendor ID must contain only letters and digits."
  }
}

variable "smart_home_skill_id" {
  description = "ID of the Smart Home skill created in the Alexa Developer Console."
  type        = string
  default     = null

  validation {
    condition     = var.smart_home_skill_id == null ? true : can(regex("^amzn1\\.ask\\.skill\\.[a-fA-F0-9-]+$", var.smart_home_skill_id))
    error_message = "Provide an Alexa skill ID beginning with amzn1.ask.skill."
  }
}

variable "custom_skill_id" {
  description = "Optional separate Custom skill ID; null disables the custom-intent Lambda."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.custom_skill_id == null ? true : can(regex("^amzn1\\.ask\\.skill\\.[a-fA-F0-9-]+$", var.custom_skill_id))
    error_message = "Provide an Alexa skill ID beginning with amzn1.ask.skill, or null."
  }
}

variable "log_retention_days" {
  description = "CloudWatch log retention."
  type        = number
  default     = 14

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365], var.log_retention_days)
    error_message = "Choose a supported CloudWatch retention period (1-365 days)."
  }
}
