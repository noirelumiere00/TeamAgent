# ============================================================
# Administrative bypass — accepted risk, not a Terraform boundary
# ============================================================
#
# The account root, administrators, long-lived IAM users, and their access keys
# intentionally retain their current permissions. This state must not attach a
# deny policy, permissions boundary, or other policy that removes or narrows
# RegisterTaskDefinition, RunTask, PassRole, or any existing administrative
# capability.
#
# terraform_runtime_guard.sh is a reviewed workflow control for cooperating
# operators. It is not an authorization or security boundary against an
# administrator: an administrator can invoke Terraform or AWS APIs directly,
# modify this repository, change state/backend data, or bypass receipts.
#
# Write-capable preflight/apply automation should assume the exact dedicated
# trusted role documented in README.md and should validate exact family, image,
# command, and network inputs. That narrows the automation path without
# changing the existing administrator/user permissions. Organization SCPs,
# root credential controls, access-key rotation, and administrator permission
# changes are explicitly outside this Terraform state and require a separate
# user-approved change.
