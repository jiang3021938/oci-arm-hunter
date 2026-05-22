# OCI A1 capacity hunter

This repository runs a GitHub Actions job that keeps trying to create one OCI
Always Free Ampere A1 instance for OpenClaw.

Target instance:

- Shape: `VM.Standard.A1.Flex`
- OCPU: `4`
- Memory: `24 GB`
- Boot volume: `200 GB`
- Public IP: enabled
- Image: set through `OCI_IMAGE_OCID`, recommended for Ubuntu 22.04 Minimal aarch64
- Instance name: `openclaw-a1`

## Why this version

The `maoucodes/oci-free-arm-instance` repository currently has no workflow file
on the main branch, so it cannot be used as-is. The Python repository is closer
to what we need, but this project wraps the same OCI SDK approach in a GitHub
Actions workflow and fixes the settings for a single OpenClaw host.

## GitHub Secrets

Set these repository secrets before running the workflow:

- `OCI_TENANCY_OCID`
- `OCI_COMPARTMENT_OCID` - usually the same as tenancy OCID for root compartment
- `OCI_USER_OCID`
- `OCI_FINGERPRINT`
- `OCI_PRIVATE_KEY_B64` - base64 encoded OCI API private key
- `OCI_REGION` - for example `us-phoenix-1`
- `OCI_SUBNET_OCID` - public subnet OCID
- `OCI_IMAGE_OCID` - Ubuntu 22.04 Minimal aarch64 image OCID
- `OCI_AVAILABILITY_DOMAINS` - comma-separated full AD names, for example `hmwq:PHX-AD-1,hmwq:PHX-AD-2,hmwq:PHX-AD-3`
- `SSH_PUBLIC_KEY` - public SSH key used to log in to the instance
- `DISCORD_WEBHOOK_URL` - optional

## Running

The workflow runs manually with `workflow_dispatch` and automatically every six
hours. Each job retries every 45 seconds for about 5 hours and 50 minutes, then
lets the next scheduled run continue. It checks for an existing active instance
named `openclaw-a1` before each attempt, so it will not intentionally create a
second matching A1 instance after success.

After success, disable the workflow to keep the repository quiet.
