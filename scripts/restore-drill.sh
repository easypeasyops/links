#!/usr/bin/env bash
# Restore drill: prove the newest backup of a namespace's database actually restores.
# A backup nobody has restored is a hope, not a backup.
#
#   SOURCE=prod scripts/restore-drill.sh                 # newest prod dump, into a scratch DB
#   SOURCE=prod DUMP=shortly-20261022T214100Z.dump scripts/restore-drill.sh
#   KEEP=1 SOURCE=prod scripts/restore-drill.sh          # leave the scratch DB to poke at
#   LIST=1 SOURCE=prod scripts/restore-drill.sh          # only list the backups (size, age)
#
# It never touches the source database. It creates a throwaway namespace (restore-drill)
# with an empty Postgres, downloads the dump from the bucket (deploy/kind/backups.yaml),
# runs pg_restore, then checks the result:
#   - the schema is at the same Alembic revision as the source
#   - EXPECT_LINKS, if given: exactly that many links came back (the source's count when
#     the dump was taken; the e2e records it)
#   - EXPECT_CODE, if given: a canary link written just before the dump came back
# and prints how long the restore took: the RTO evidence (docs/DESIGN.md, Backups).
# Run by scripts/k8s-e2e.sh (step 11), so CI restores a fresh backup on every PR.
set -euo pipefail

SOURCE="${SOURCE:-prod}"
NS=restore-drill
POD=restore
RCLONE_IMAGE=rclone/rclone@sha256:45401ad7410db1d67ffdb58e19059ad20b0d8e0285a60e38bbec55cc1019c7a5

pass() { echo "ok   $*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }
cleanup() {
  if [[ "${KEEP:-0}" == 1 ]]; then
    echo "     kept: kubectl -n $NS exec -it $POD -c postgres -- psql -U shortly shortly"
  else
    kubectl delete namespace "$NS" --wait=false >/dev/null 2>&1 || true
  fi
}

src_sql() { kubectl -n "$SOURCE" exec postgres-0 -- psql -U shortly -d shortly -tAc "$1"; }
dst_sql() { kubectl -n "$NS" exec "$POD" -c postgres -- psql -h 127.0.0.1 -U shortly -d shortly -tAc "$1"; }

kubectl -n "$SOURCE" get cronjob postgres-backup >/dev/null 2>&1 \
  || fail "no postgres-backup CronJob in $SOURCE (scripts/cluster.sh up)"
if [[ "${LIST:-0}" == 1 ]]; then
  # A one-off pod in the source namespace, labelled as a backup pod so the bucket admits it.
  kubectl -n "$SOURCE" run backup-list --image="$RCLONE_IMAGE" --restart=Never --rm -i --quiet \
    --labels=app.kubernetes.io/component=backup --overrides='{"spec":{"automountServiceAccountToken":false,
      "securityContext":{"runAsNonRoot":true,"runAsUser":70,"seccompProfile":{"type":"RuntimeDefault"}},
      "containers":[{"name":"list","image":"'"$RCLONE_IMAGE"'","args":["lsl","s3:shortly-backups/'"$SOURCE"'"],
        "env":[{"name":"HOME","value":"/tmp"},{"name":"RCLONE_CONFIG_S3_TYPE","value":"s3"},
          {"name":"RCLONE_CONFIG_S3_PROVIDER","value":"SeaweedFS"},
          {"name":"RCLONE_CONFIG_S3_ENDPOINT","value":"http://seaweedfs.backups.svc:8333"},
          {"name":"RCLONE_CONFIG_S3_ACCESS_KEY_ID","valueFrom":{"secretKeyRef":{"name":"backup-s3","key":"access-key"}}},
          {"name":"RCLONE_CONFIG_S3_SECRET_ACCESS_KEY","valueFrom":{"secretKeyRef":{"name":"backup-s3","key":"secret-key"}}}],
        "securityContext":{"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]}}}]}}'
  exit 0
fi

s3_key=$(kubectl -n "$SOURCE" get secret backup-s3 -o jsonpath='{.data.access-key}' | base64 -d)
s3_secret=$(kubectl -n "$SOURCE" get secret backup-s3 -o jsonpath='{.data.secret-key}' | base64 -d)

# A clean slate every time: the drill proves the backup needs nothing but itself.
kubectl delete namespace "$NS" --ignore-not-found --wait=true --timeout=120s >/dev/null
kubectl create namespace "$NS" >/dev/null
kubectl label namespace "$NS" pod-security.kubernetes.io/enforce=restricted >/dev/null
trap cleanup EXIT
kubectl -n "$NS" create secret generic backup-s3 \
  --from-literal=access-key="$s3_key" --from-literal=secret-key="$s3_secret" >/dev/null

start=$SECONDS
# One pod: an init container fetches the dump (named, or the newest), then an empty
# Postgres starts next to it. pg_restore runs over the local socket, so the drill needs no
# Service and no network rule beyond reaching the bucket (component=backup).
kubectl -n "$NS" apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: $POD
  labels:
    app.kubernetes.io/name: postgres-restore
    app.kubernetes.io/component: backup
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  securityContext:
    runAsNonRoot: true
    runAsUser: 70
    runAsGroup: 70
    fsGroup: 70
    seccompProfile: { type: RuntimeDefault }
  initContainers:
    - name: fetch
      image: $RCLONE_IMAGE
      command: [sh, -ec]
      args:
        - |
          dir="s3:shortly-backups/$SOURCE"
          name="${DUMP:-}"
          [ -n "\$name" ] || name=\$(rclone lsf --files-only "\$dir" | sort | tail -n 1)
          [ -n "\$name" ] || { echo "no backups in \$dir"; exit 1; }
          rclone copyto "\$dir/\$name" /work/shortly.dump
          echo "\$name" > /work/NAME
      env:
        - { name: HOME, value: /tmp }
        - { name: RCLONE_CONFIG_S3_TYPE, value: s3 }
        - { name: RCLONE_CONFIG_S3_PROVIDER, value: SeaweedFS }
        - { name: RCLONE_CONFIG_S3_ENDPOINT, value: "http://seaweedfs.backups.svc:8333" }
        - name: RCLONE_CONFIG_S3_ACCESS_KEY_ID
          valueFrom: { secretKeyRef: { name: backup-s3, key: access-key } }
        - name: RCLONE_CONFIG_S3_SECRET_ACCESS_KEY
          valueFrom: { secretKeyRef: { name: backup-s3, key: secret-key } }
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities: { drop: [ALL] }
      volumeMounts:
        - { name: work, mountPath: /work }
        - { name: tmp, mountPath: /tmp }
  containers:
    - name: postgres
      image: postgres:17-alpine
      env:
        - { name: POSTGRES_USER, value: shortly }
        - { name: POSTGRES_DB, value: shortly }
        - { name: POSTGRES_PASSWORD, value: restore-drill-only }
        - { name: PGDATA, value: /var/lib/postgresql/data/pgdata }
      # Over TCP, not the socket: on first start the image initialises the database with a
      # temporary server that listens on the socket only, then restarts. A socket check
      # passes during that window, and a restore started then fails mid-restart.
      readinessProbe:
        exec: { command: [pg_isready, -h, 127.0.0.1, -U, shortly, -d, shortly] }
        periodSeconds: 2
      securityContext:
        allowPrivilegeEscalation: false
        capabilities: { drop: [ALL] }
      volumeMounts:
        - { name: work, mountPath: /work, readOnly: true }
        - { name: data, mountPath: /var/lib/postgresql/data }
  volumes:
    - { name: work, emptyDir: { sizeLimit: 512Mi } }
    - { name: tmp, emptyDir: { sizeLimit: 64Mi } }
    - { name: data, emptyDir: { sizeLimit: 1Gi } }
YAML
if ! kubectl -n "$NS" wait pod/"$POD" --for=condition=Ready --timeout=180s >/dev/null; then
  kubectl -n "$NS" logs "$POD" -c fetch --tail=20 2>/dev/null || true
  kubectl -n "$NS" describe pod "$POD" | tail -15 || true
  fail "the scratch database never became ready (did the download fail? logs above)"
fi
dump=$(kubectl -n "$NS" exec "$POD" -c postgres -- cat /work/NAME)

# --exit-on-error: a partial restore must fail the drill, not pass with missing tables.
kubectl -n "$NS" exec "$POD" -c postgres -- \
  pg_restore --host 127.0.0.1 --username shortly --dbname shortly --no-owner --no-privileges --exit-on-error \
  /work/shortly.dump
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
  [[ -n "$target" ]] || fail "canary link $EXPECT_CODE is not in the restored database"
fi
pass "restore drill: $SOURCE/$dump restored into a fresh Postgres in ${took}s; revision $got_rev, $links links${EXPECT_CODE:+, canary $EXPECT_CODE present}"
