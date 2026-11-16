#!/usr/bin/env bash
# Deploy the committed backend (git HEAD) to the Oracle VM.
#   ./deploy/deploy.sh
# Override target with VM=user@host KEY=~/.ssh/key ./deploy/deploy.sh
set -euo pipefail

VM="${VM:-opc@129.80.138.126}"
KEY="${KEY:-$HOME/.ssh/ssh-key-2025-10-14.key}"
SSH=(ssh -i "$KEY" -o IdentitiesOnly=yes "$VM")

cd "$(dirname "$0")/.."

if [ -n "$(git status --porcelain)" ]; then
  echo "Note: uncommitted changes are NOT deployed (deploying $(git rev-parse --short HEAD))."
fi

echo "==> Uploading $(git rev-parse --short HEAD)"
git archive --format=tar HEAD | "${SSH[@]}" \
  'rm -rf ~/scanmyfood/src && mkdir -p ~/scanmyfood/src && tar -x -C ~/scanmyfood/src'

echo "==> Building image and restarting the API"
"${SSH[@]}" 'set -euo pipefail
cd ~/scanmyfood/src
test -f ~/scanmyfood/api.env || { echo "Missing ~/scanmyfood/api.env (see deploy/api.env.example)"; exit 1; }
nice -n 10 podman build -q -t localhost/scanmyfood-api:latest . >/dev/null
mkdir -p ~/.config/containers/systemd
cp deploy/quadlet/* ~/.config/containers/systemd/
systemctl --user daemon-reload
systemctl --user start scanmyfood-redis
systemctl --user restart scanmyfood-api
# Remove only our own superseded images
podman image prune -f --filter label=app=scanmyfood >/dev/null
for i in $(seq 1 20); do
  if curl -fsS -m 3 http://127.0.0.1:8001/health/ >/dev/null 2>&1; then
    echo "Healthy: $(curl -fsS -m 3 http://127.0.0.1:8001/health/)"; exit 0
  fi
  sleep 1
done
echo "API did not become healthy; recent logs:"; journalctl --user -u scanmyfood-api -n 30 --no-pager; exit 1
'
