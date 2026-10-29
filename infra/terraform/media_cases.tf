variable "enable_media_case_search" {
  description = "Expose self-owned trend-media case search using existing Postgres, jobs, Apify and Gemini."
  type        = bool
  default     = false
}

variable "media_case_gemini_monthly_usd" {
  description = "Monthly Gemini budget for the ten-account media case corpus (separate from Apify)."
  type        = string
  default     = "15"

  validation {
    condition     = can(tonumber(var.media_case_gemini_monthly_usd)) && try(tonumber(var.media_case_gemini_monthly_usd) > 0, false)
    error_message = "media_case_gemini_monthly_usd must be positive."
  }
}
