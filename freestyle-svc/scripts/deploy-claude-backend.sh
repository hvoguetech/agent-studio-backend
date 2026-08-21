#!/usr/bin/env bash
#
# Deploy the ros-claude-backend image so a code change on `main` (e.g. the
# claude_code error-capture fix) actually runs on Freestyle VMs.
#
# The VM bakes the `ros` package from a FRESH git clone of ROS_INSTALL_REF at
# image-bake time — so a running VM keeps its OLD code until a new image is
# baked and the service is pointed at it. This script does, in order:
#   1. build the service (tsc) so dist/ is current,
#   2. bake a new snapshot from ROS_INSTALL_REF (default: main),
#   3. parse the printed ROS_SNAPSHOT_ID,
#   4. (optional) set that snapshot on the Railway service + redeploy,
#   5. (optional) DELETE the stale VM so the next /run boots from the new image.
#
# Steps 4-5 run only when their inputs are provided; otherwise the script prints
# exactly what to do by hand. Nothing here is destructive without OLD_VM_ID set.
#
# ---------------------------------------------------------------------------
# Usage:
#   cd agent-studio-backend/freestyle-svc
   FREESTYLE_API_KEY=VaHJZEpgFTXX1fbHwMvPVn-Btj2UhvyFd4FiUJWarfCYsdg729daE8zBq96AKFSmf4u
   ROS_INSTALL_REPO_URL="https://github.com/hvoguetech/agent-studio-backend.git" \
   ROS_INSTALL_TOKEN="$(gh auth token)" \
   ROS_INSTALL_REF=main \
#   bash scripts/deploy-claude-backend.sh
#
# Optional (enables the wire-up + teardown steps):
#   RAILWAY_SERVICE="ros-freestyle-svc"   # railway service to set ROS_SNAPSHOT_ID on + redeploy
#   OLD_VM_ID="uvktbuqcencvuz648wfj"       # stale VM to tear down after the new snapshot is live
#   ROS_FREESTYLE_SERVICE_URL="https://..." FREESTYLE_SERVICE_SECRET=...  # for the DELETE /vm call
#
# Reuse an already-baked image (skip the ~20 min bake) — just wire it up + retire the old VM:
#   SKIP_BAKE=1 ROS_SNAPSHOT_ID=sh-... \
#   RAILWAY_SERVICE="ros-freestyle-svc" \
#   OLD_VM_ID="uvktbuqcencvuz648wfj" \
#   ROS_FREESTYLE_SERVICE_URL="https://..." FREESTYLE_SERVICE_SECRET=... \
#   bash scripts/deploy-claude-backend.sh
# ---------------------------------------------------------------------------
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$here"

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mx %s\033[0m\n' "$*" >&2; exit 1; }

# --- 0. prerequisites -------------------------------------------------------
# SKIP_BAKE=1 reuses an already-baked image: supply the snapshot in ROS_SNAPSHOT_ID
# and this jumps straight to wiring it up + tearing down the stale VM (no re-bake).
SKIP_BAKE="${SKIP_BAKE:-0}"

if [ "$SKIP_BAKE" = "1" ]; then
  : "${ROS_SNAPSHOT_ID:?SKIP_BAKE=1 requires ROS_SNAPSHOT_ID=<existing snapshot id>}"
  snapshot_id="$ROS_SNAPSHOT_ID"
  say "Skipping bake — reusing snapshot ROS_SNAPSHOT_ID=$snapshot_id"
else
  : "${FREESTYLE_API_KEY:?set FREESTYLE_API_KEY (from dash.freestyle.sh)}"
  : "${ROS_INSTALL_REPO_URL:?set ROS_INSTALL_REPO_URL (plain https clone url, no token)}"
  ROS_INSTALL_REF="${ROS_INSTALL_REF:-main}"
  # Bake requires the secret var to exist even though it's not used during a bake.
  export FREESTYLE_SERVICE_SECRET="${FREESTYLE_SERVICE_SECRET:-placeholder-for-bake}"
  export FREESTYLE_API_KEY ROS_INSTALL_REPO_URL ROS_INSTALL_REF
  [ -n "${ROS_INSTALL_TOKEN:-}" ] && export ROS_INSTALL_TOKEN

  command -v node >/dev/null || die "node not found on PATH"
  command -v npm  >/dev/null || die "npm not found on PATH"

  # --- 1. build the service (dist/) ----------------------------------------
  say "Building freestyle-svc (tsc)"
  npm run build

  # --- 2. bake the snapshot ------------------------------------------------
  say "Baking ros-claude-backend from ref '$ROS_INSTALL_REF' (~20 min)"
  bake_log="$(mktemp -t ros-bake.XXXXXX.log)"
  # Tee so you watch it live AND we can parse the id afterwards.
  set +e
  npm run build:image 2>&1 | tee "$bake_log"
  bake_rc="${PIPESTATUS[0]}"
  set -e
  [ "$bake_rc" -eq 0 ] || die "bake failed (rc=$bake_rc) — see $bake_log"

  # --- 3. parse the new snapshot id ----------------------------------------
  # build-image.ts prints:  ROS_SNAPSHOT_ID=<id>
  snapshot_id="$(grep -oE 'ROS_SNAPSHOT_ID=[A-Za-z0-9_-]+' "$bake_log" | tail -n1 | cut -d= -f2 || true)"
  [ -n "$snapshot_id" ] || die "could not parse ROS_SNAPSHOT_ID from bake output ($bake_log)"
  say "New snapshot: ROS_SNAPSHOT_ID=$snapshot_id"
fi

# --- 4. wire the snapshot onto the Railway service --------------------------
if [ -n "${RAILWAY_SERVICE:-}" ] && command -v railway >/dev/null; then
  say "Setting ROS_SNAPSHOT_ID on Railway service '$RAILWAY_SERVICE' + redeploying"
  railway variables --service "$RAILWAY_SERVICE" --set "ROS_SNAPSHOT_ID=$snapshot_id"
  railway redeploy --service "$RAILWAY_SERVICE" --yes
else
  warn "Skipping Railway wire-up (set RAILWAY_SERVICE and install the railway CLI to automate)."
  echo "  Manually set on the freestyle-svc service, then redeploy:"
  echo "    ROS_SNAPSHOT_ID=$snapshot_id"
fi

# --- 5. tear down the stale VM so the next run boots from the new image -----
if [ -n "${OLD_VM_ID:-}" ]; then
  if [ -n "${ROS_FREESTYLE_SERVICE_URL:-}" ] && [ -n "${FREESTYLE_SERVICE_SECRET:-}" ] \
     && [ "$FREESTYLE_SERVICE_SECRET" != "placeholder-for-bake" ]; then
    say "Tearing down stale VM $OLD_VM_ID (next /run creates a fresh one from $snapshot_id)"
    curl -fsS -X DELETE "${ROS_FREESTYLE_SERVICE_URL%/}/vm/$OLD_VM_ID" \
      -H "Authorization: Bearer $FREESTYLE_SERVICE_SECRET" && echo "  deleted."
  else
    warn "OLD_VM_ID set but ROS_FREESTYLE_SERVICE_URL / FREESTYLE_SERVICE_SECRET missing — skipping teardown."
    echo "  Tear it down manually once the new snapshot is live:"
    echo "    curl -X DELETE \"\$ROS_FREESTYLE_SERVICE_URL/vm/$OLD_VM_ID\" -H \"Authorization: Bearer \$FREESTYLE_SERVICE_SECRET\""
  fi
else
  warn "No OLD_VM_ID given — existing persistent VMs keep the OLD baked code until torn down."
fi

say "Done"
echo "Snapshot in use: $snapshot_id"
echo "Also redeploy the ROS api + worker services from '$ROS_INSTALL_REF' so the fix lands there too."
echo "Then watch a run:  VM_ID=<new vmId> CLAUDE_ONLY=1 node scripts/vm-follow.mjs"
