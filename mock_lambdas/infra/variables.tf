variable "aws_profile" {
  type        = string
  description = "Profile AWS local (~/.aws/credentials) usado pelo provider."
  default     = "trustedlake"
}

variable "images_bucket" {
  type        = string
  description = "Bucket (existente) onde o app sobe as fotos de ECG."
  default     = "ecg-lambdas-images-785236377332-sa-east-1"
}

variable "results_bucket" {
  type        = string
  description = "Bucket (existente) onde a leitura grava overlay e sinal."
  default     = "ecg-lambdas-results-785236377332-sa-east-1"
}

variable "code_version" {
  type        = string
  description = "Versão exposta em meta.engine_version (ex.: git sha)."
  default     = "dev"
}

variable "mock_scenario" {
  type        = string
  description = "Cenário padrão quando o payload não traz mock.scenario. Vazio = success."
  default     = ""

  validation {
    condition     = contains(["", "success", "success_with_warnings", "failure", "refused"], var.mock_scenario)
    error_message = "mock_scenario deve ser vazio, success, success_with_warnings, failure ou refused."
  }
}
