# shellcheck shell=bash
# The environments (staging, prod) and the namespace each one runs in. Sourced by the scripts
# so the mapping lives in one place (platform plan, Phase 2: a namespace per app and
# environment): <app>-<env>, so several apps can share a cluster. NAMESPACE_PREFIX= (empty)
# gives the environment-named namespaces of a cluster built before the move.
NAMESPACE_PREFIX="${NAMESPACE_PREFIX-shortly-}"
ENVS=(staging prod)
ns_of() { echo "${NAMESPACE_PREFIX}$1"; }
STAGING_NS=$(ns_of staging)
PROD_NS=$(ns_of prod)
