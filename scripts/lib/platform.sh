# shellcheck shell=bash
# The platform this repo runs on (platform plan 3.4): a checkout of easypeasyops/platform.
# CI checks it out at platform.lock's commit and sets PLATFORM_DIR; on a laptop it is the
# clone beside this one (../platform). Sourcing this sets PLATFORM_DIR and loads the
# platform's environment-to-namespace map (ENVS, ns_of, STAGING_NS, PROD_NS).
_links_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PLATFORM_DIR="${PLATFORM_DIR:-$_links_root/../platform}"
if [[ ! -f "$PLATFORM_DIR/scripts/lib/namespaces.sh" ]]; then
  echo "No platform checkout at $PLATFORM_DIR: clone easypeasyops/platform beside this repo," >&2
  echo "at the commit platform.lock names, or set PLATFORM_DIR." >&2
  exit 1
fi
PLATFORM_DIR="$(cd "$PLATFORM_DIR" && pwd)"
export PLATFORM_DIR
# Another commit is allowed (trying a platform change before bumping the lock), but said.
PLATFORM_LOCK=$(sed -nE 's/^easypeasyops\/platform@([0-9a-f]{40})$/\1/p' "$_links_root/platform.lock")
if _have=$(git -C "$PLATFORM_DIR" rev-parse HEAD 2>/dev/null) && [[ "$_have" != "$PLATFORM_LOCK" ]]; then
  echo "note: the platform at $PLATFORM_DIR is at ${_have:0:7}; platform.lock says ${PLATFORM_LOCK:0:7}" >&2
fi
# shellcheck source=/dev/null
source "$PLATFORM_DIR/scripts/lib/namespaces.sh"
