# shellcheck shell=bash
# The environments (staging, prod) and the namespace each one runs in. Sourced by the scripts
# so the mapping lives in one place (platform plan, Phase 2: a namespace per app and
# environment). Today an environment's namespace is its name; with NAMESPACE_PREFIX set
# ("shortly-") it becomes <app>-<env>, so several apps can share a cluster.
NAMESPACE_PREFIX="${NAMESPACE_PREFIX-}"
ENVS=(staging prod)
ns_of() { echo "${NAMESPACE_PREFIX}$1"; }
STAGING_NS=$(ns_of staging)
PROD_NS=$(ns_of prod)
