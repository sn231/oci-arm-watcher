# oci-arm-watcher

Poll OCI host capacity for Always Free compute in Singapore and launch the first
available target. Runs entirely on GitHub Actions (every 5 minutes), so it keeps
working while the local machine is off.

Targets, in priority order:

1. `VM.Standard.A1.Flex` 2 OCPU / 12 GB
2. `VM.Standard.A1.Flex` 1 OCPU / 6 GB
3. `VM.Standard.A1.Flex` 1 OCPU / 1 GB
4. `VM.Standard.E2.1.Micro` (1 OCPU / 1 GB, AMD)

The script refuses to launch beyond the Always Free entitlement (2 OCPU / 12 GB
A1 in total, 2 AMD micro instances) and skips everything once an instance named
`weport-a1-sg` or `weport-amd-sg` already exists.

## Repository secrets

| Name | Value |
|---|---|
| `OCI_USER` | user OCID |
| `OCI_TENANCY` | tenancy OCID |
| `OCI_FINGERPRINT` | API key fingerprint |
| `OCI_REGION` | e.g. `ap-singapore-1` |
| `OCI_KEY` | PEM contents of the API private key |

## Repository variables

| Name | Value |
|---|---|
| `OCI_AD` | availability domain, e.g. `cmbf:AP-SINGAPORE-1-AD-1` |
| `OCI_SUBNET_ID` | public subnet OCID |
| `OCI_IMAGE_A1` | aarch64 image OCID |
| `OCI_IMAGE_AMD` | x86_64 image OCID |
| `OCI_SSH_PUBLIC_KEY` | public key for `ubuntu` |

## Notes

- Scheduled workflows in public repositories get free runner minutes. Secrets are
  encrypted and are not exposed to forks or logs.
- The public IP is never written to workflow logs or the job summary; read it from
  the OCI console after an instance is created.
- Disable the workflow or delete the repository to stop it.
