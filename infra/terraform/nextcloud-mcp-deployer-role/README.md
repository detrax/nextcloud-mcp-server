# nextcloud-mcp-deployer-role

IAM role and least-privilege policy scoped to deploy the
[`nextcloud-mcp-server`](../nextcloud-mcp-server) Terraform module.

## Use cases

- **Client account**: a client creates this role in their AWS account with
  `trusted_principal_arns = ["arn:aws:iam::<your-account-id>:root"]` so you
  can assume it cross-account and deploy/maintain the MCP server on their
  behalf.
- **Your own testing**: instantiated in your account with your IAM user /
  admin role as the trusted principal, lets you run `terraform apply` for
  the nextcloud-mcp-server module under the same permission boundary the
  client will use — so any "works for me, breaks for them" gap surfaces in
  testing rather than at the client.

## Modes

The role's permissions are mode-aware via inputs:

| Mode | Inputs | What gets granted |
|---|---|---|
| **CloudFront default cert** (recommended) | (defaults) | ECS, ALB, EFS, CloudFront, scoped IAM/logs/secrets, EC2 SG + describe |
| **Custom domain** | `route53_zone_ids = [...]` | + Route53 (scoped to listed zones) and ACM |
| **Secret managed in same TF** | `allow_secret_create = true` | + Secrets Manager create/update/delete (scoped to `secret_name_prefix`) |

## Cross-account assume from your account

Once the client has applied this module in their account and given you the
output `role_arn`, configure the AWS provider in your client-deployment TF
project:

```hcl
provider "aws" {
  assume_role {
    role_arn = "arn:aws:iam::<client-account-id>:role/clients/nextcloud-mcp-deployer"
    # external_id = "..." # optional, recommended for cross-account
  }
}
```

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
| ---- | ------- |
| <a name="requirement_aws"></a> [aws](#requirement\_aws) | ~> 6.0 |

## Providers

| Name | Version |
| ---- | ------- |
| <a name="provider_aws"></a> [aws](#provider\_aws) | 6.43.0 |

## Modules

No modules.

## Resources

| Name | Type |
| ---- | ---- |
| [aws_iam_policy.deployer](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_policy) | resource |
| [aws_iam_role.this](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role) | resource |
| [aws_iam_role_policy_attachment.deployer](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy_attachment) | resource |
| [aws_caller_identity.current](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/caller_identity) | data source |
| [aws_iam_policy_document.deployer](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_iam_policy_document.trust](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_partition.current](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/partition) | data source |

## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| <a name="input_allow_secret_create"></a> [allow\_secret\_create](#input\_allow\_secret\_create) | When true, the deployer can create/update/delete Secrets Manager<br/>secrets matching `secret_name_prefix`. Set true if the secret is<br/>managed alongside the module in the same Terraform run; leave false if<br/>the secret is provisioned out of band (console / separate root TF) and<br/>only the ARN is passed in. | `bool` | `false` | no |
| <a name="input_module_name_prefix"></a> [module\_name\_prefix](#input\_module\_name\_prefix) | The `var.name` value passed to the nextcloud-mcp-server module. Used to<br/>scope IAM/logs/secrets ARNs. Defaults match the module default; change<br/>only if the module is instantiated with a non-default name. | `string` | `"nextcloud-mcp-server"` | no |
| <a name="input_role_name"></a> [role\_name](#input\_role\_name) | Name of the deployer IAM role. | `string` | `"nextcloud-mcp-deployer"` | no |
| <a name="input_role_path"></a> [role\_path](#input\_role\_path) | IAM path for the deployer role and its policy. | `string` | `"/clients/"` | no |
| <a name="input_route53_zone_ids"></a> [route53\_zone\_ids](#input\_route53\_zone\_ids) | Route53 hosted zone IDs the deployer is allowed to mutate. Only needed<br/>in the module's custom-domain mode. Leave empty (the default) for the<br/>CloudFront-default-cert path, which requires no DNS or ACM permissions. | `list(string)` | `[]` | no |
| <a name="input_secret_name_prefix"></a> [secret\_name\_prefix](#input\_secret\_name\_prefix) | Secrets Manager name prefix the deployer can read (and optionally<br/>create, see `allow_secret_create`). The module accepts a secret ARN as<br/>input; this prefix scopes the deployer's access to secrets matching<br/>that name pattern. | `string` | `"nextcloud-mcp"` | no |
| <a name="input_trusted_principal_arns"></a> [trusted\_principal\_arns](#input\_trusted\_principal\_arns) | Principal ARNs allowed to assume this role. For testing in your own<br/>account: the user/role you want to assume from. For client deployments:<br/>typically a single root-account ARN of the deploying party (e.g.<br/>"arn:aws:iam::<your-account-id>:root"), with MFA or external-id<br/>conditions added at the trust-policy level if required. | `list(string)` | n/a | yes |

## Outputs

| Name | Description |
| ---- | ----------- |
| <a name="output_policy_arn"></a> [policy\_arn](#output\_policy\_arn) | ARN of the inline-style managed policy attached to the role. |
| <a name="output_role_arn"></a> [role\_arn](#output\_role\_arn) | ARN of the deployer role; pass to STS AssumeRole or use as the assume\_role target in a provider block. |
| <a name="output_role_name"></a> [role\_name](#output\_role\_name) | Name of the deployer role. |
<!-- END_TF_DOCS -->
