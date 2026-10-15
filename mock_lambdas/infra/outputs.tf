output "function_names" {
  value = { for k, fn in aws_lambda_function.this : k => fn.function_name }
}

output "function_arns" {
  description = "Conceda lambda:InvokeFunction nesses ARNs ao principal IAM do backend."
  value       = { for k, fn in aws_lambda_function.this : k => fn.arn }
}
