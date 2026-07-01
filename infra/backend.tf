# Uncomment AFTER credentials arrive and state bucket exists
#
# terraform {
#   backend "s3" {
#     bucket       = "whayne-terraform-state"
#     key          = "dev/terraform.tfstate"
#     region       = "ap-south-1"
#     use_lockfile = true
#   }
# }