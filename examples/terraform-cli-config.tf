# Terraform CLI configuration for tf-registry-proxy
#
# Put this in ~/.terraformrc (or point TF_CLI_CONFIG_FILE at it), then use module
# sources exactly as before — `terraform init` now goes through the proxy.

# ---------------------------------------------------------------------------
# Option A (recommended): keep using registry.terraform.io in your module
# sources and send the module API to the proxy.
#
#   source = "registry.terraform.io/hashicorp/consul/aws"   # unchanged
#   source = "hashicorp/consul/aws"                        # also unchanged
# ---------------------------------------------------------------------------
host "registry.terraform.io" {
  services = {
    modules.v1 = "http://127.0.0.1:8080/v1/modules/"
  }
}

# ---------------------------------------------------------------------------
# Option B: a private registry hostname used explicitly in module sources.
# The proxy serves /.well-known/terraform.json, so no `services` block is needed.
#
#   source = "tf.example.com/hashicorp/consul/aws"
#
# host "tf.example.com" {
#   host = "tf.example.com"
#   # plain HTTP only for local experiments; use TLS in production
# }
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Option C: pin the proxy to a specific TLS certificate (self-signed CA example)
#
# host "tf.example.com" {
#   host = "tf.example.com"
#   services = {
#     modules.v1 = "https://tf.example.com/v1/modules/"
#   }
#   cert = "/etc/ssl/private/tf-proxy-ca.pem"
# }
# ---------------------------------------------------------------------------
