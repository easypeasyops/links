#!/usr/bin/env bash
# Restore drill for a CloudNativePG database (platform plan 4.3): prove the newest backup of
# a namespace's Cluster restores, base backup AND the WAL archived after it.
#
#   SOURCE=shortly-staging scripts/restore-drill-cnpg.sh        # into a scratch Cluster
#   KEEP=1 SOURCE=shortly-staging scripts/restore-drill-cnpg.sh # leave it to poke at
#   BACKUP=1 SOURCE=shortly-staging scripts/restore-drill-cnpg.sh # take a base backup first
#
# It never touches the source. It creates a throwaway namespace (restore-drill) with a
# Cluster bootstrapped by `recovery` from the source's ObjectStore (s3://cnpg-backups/
# <namespace>/shortly-db, the service chart's database.cluster): the operator fetches the
# newest base backup and replays every archived WAL segment after it, which is what a real
# restore does. Then it checks the result:
#   - the schema is at the same Alembic revision as the source
#   - EXPECT_LINKS, if given: exactly that many links came back
#   - EXPECT_CODE, if given: a link written after the base backup came back (from WAL)
# and prints how long the restore took: the RTO evidence (docs/DESIGN.md, Backups).
# Run by scripts/k8s-e2e.sh (step 11c) against staging. The pg_dump drill
# (scripts/restore-drill.sh) covers a namespace still on the old Postgres.
set -euo pipefail

# shellcheck source=scripts/lib/platform.sh
source "$(dirname "$0")/lib/platform.sh"
SOURCE="${SOURCE:-$STAGING_NS}" # a namespace
CLUSTER=shortly-db             # the source Cluster (the service chart: <release>-db)
NS=restore-drill
DRILL=drill
PLUGIN=barman-cloud.cloudnative-pg.io

pass() { echo "ok   $*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }
cleanup() {
  if [[ "${KEEP:-0}" == 1 ]]; then
    echo "     kept: kubectl -n $NS exec -it $DRILL-1 -c postgres -- psql -d shortly"
  else
    kubectl delete namespace "$NS" --wait=false >/dev/null 2>&1 || true
  fi
}
primary() { kubectl -n "$1" get cluster "$2" -o jsonpath='{.status.currentPrimary}'; }
src_sql() { kubectl -n "$SOURCE" exec "$(primary "$SOURCE" "$CLUSTER")" -c postgres -- psql -d shortly -tAc "$1"; }
dst_sql() { kubectl -n "$NS" exec "$(primary "$NS" "$DRILL")" -c postgres -- psql -d shortly -tAc "$1"; }

kubectl -n "$SOURCE" get cluster "$CLUSTER" >/dev/null 2>&1 \
  || fail "no CloudNativePG Cluster $CLUSTER in $SOURCE (for the old Postgres: scripts/restore-drill.sh)"
kubectl -n "$SOURCE" get objectstore "$CLUSTER" >/dev/null 2>&1 \
  || fail "no ObjectStore $CLUSTER in $SOURCE: its backups are off (database.cluster.backup.enabled)"

if [[ "${BACKUP:-0}" == 1 ]]; then
  # A base backup now, through the plugin, as the ScheduledBackup takes them.
  name="drill-$(date -u +%Y%m%d%H%M%S)"
  kubectl -n "$SOURCE" apply -f - >/dev/null <<YAML
apiVersion: postgresql.cnpg.io/v1
kind: Backup
metadata: { name: $name }
spec:
  cluster: { name: $CLUSTER }
  method: plugin
  pluginConfiguration: { name: $PLUGIN }
YAML
  for _ in $(seq 1 90); do
    phase=$(kubectl -n "$SOURCE" get backup "$name" -o jsonpath='{.status.phase}')
    [[ "$phase" == completed ]] && break
    [[ "$phase" == failed ]] && fail "base backup $name failed: $(kubectl -n "$SOURCE" get backup "$name" -o jsonpath='{.status.error}')"
    sleep 2
  done
  [[ "$phase" == completed ]] || fail "base backup $name not completed after 180 s (phase '$phase')"
  pass "base backup: $SOURCE/$name completed"
fi

# The bucket's keys: the source namespace's copy (the chart's ExternalSecret). The drill's
# namespace isn't one the store hands them to, so they're copied, as a plain Secret.
key=$(kubectl -n "$SOURCE" get secret "$CLUSTER-backup" -o jsonpath='{.data.ACCESS_KEY_ID}' | base64 -d)
secret=$(kubectl -n "$SOURCE" get secret "$CLUSTER-backup" -o jsonpath='{.data.ACCESS_SECRET_KEY}' | base64 -d)
store=$(kubectl -n "$SOURCE" get objectstore "$CLUSTER" -o jsonpath='{.spec.configuration}')
image=$(kubectl -n "$SOURCE" get cluster "$CLUSTER" -o jsonpath='{.spec.imageName}')

# A clean slate every time: the drill proves the backup needs nothing but itself.
kubectl delete namespace "$NS" --ignore-not-found --wait=true --timeout=120s >/dev/null
kubectl create namespace "$NS" >/dev/null
kubectl label namespace "$NS" pod-security.kubernetes.io/enforce=restricted >/dev/null
trap cleanup EXIT
kubectl -n "$NS" create secret generic "$CLUSTER-backup" \
  --from-literal=ACCESS_KEY_ID="$key" --from-literal=ACCESS_SECRET_KEY="$secret" >/dev/null

start=$SECONDS
# The source's ObjectStore, as it is (same bucket, folder and keys' names), read-only from
# here: the drill Cluster names it as its recovery source and archives nowhere.
kubectl -n "$NS" apply -f - >/dev/null <<YAML
apiVersion: barmancloud.cnpg.io/v1
kind: ObjectStore
metadata: { name: source }
spec:
  configuration: $store
---
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata: { name: $DRILL }
spec:
  instances: 1
  imageName: $image
  storage: { size: 1Gi }
  resources:
    requests: { cpu: 10m, memory: 128Mi }
    limits: { memory: 512Mi }
  enablePDB: false
  bootstrap:
    recovery:
      source: origin
      database: shortly
      owner: shortly
  externalClusters:
    - name: origin
      plugin:
        name: $PLUGIN
        parameters:
          barmanObjectName: source
          serverName: $CLUSTER
YAML
for _ in $(seq 1 150); do
  phase=$(kubectl -n "$NS" get cluster "$DRILL" -o jsonpath='{.status.phase}' 2>/dev/null || true)
  [[ "$phase" == "Cluster in healthy state" ]] && break
  sleep 2
done
if [[ "$phase" != "Cluster in healthy state" ]]; then
  kubectl -n "$NS" get pods -o wide || true
  kubectl -n "$NS" logs -l cnpg.io/cluster="$DRILL" --all-containers --tail=30 2>/dev/null || true
  fail "the drill Cluster never became healthy (phase '$phase'; logs above)"
fi
took=$((SECONDS - start))

want_rev=$(src_sql "SELECT version_num FROM alembic_version")
got_rev=$(dst_sql "SELECT version_num FROM alembic_version")
[[ "$got_rev" == "$want_rev" ]] || fail "restored schema at revision '$got_rev', the source is at '$want_rev'"

links=$(dst_sql "SELECT count(*) FROM links")
if [[ -n "${EXPECT_LINKS:-}" ]]; then
  [[ "$links" == "$EXPECT_LINKS" ]] || fail "restored $links links, expected $EXPECT_LINKS"
fi
if [[ -n "${EXPECT_CODE:-}" ]]; then
  target=$(dst_sql "SELECT target FROM links WHERE code = '$EXPECT_CODE'")
  [[ -n "$target" ]] || fail "link $EXPECT_CODE (written after the base backup) is not in the restored database"
fi
pass "restore drill (CloudNativePG): $SOURCE/$CLUSTER's base backup and WAL recovered into a fresh Cluster in ${took}s; revision $got_rev, $links links${EXPECT_CODE:+, $EXPECT_CODE present (replayed from WAL)}"
