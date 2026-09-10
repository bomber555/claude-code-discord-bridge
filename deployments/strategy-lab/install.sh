#!/usr/bin/env bash
set -euo pipefail
bundle_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd /home/bomber
if [[ -e /etc/systemd/system/ccdb-lab.service ]]; then
    echo 'ccdb-lab.service already exists; refusing to replace it.' >&2
    exit 1
fi
python3 "$bundle_dir/prepare.py" "$@" --check-only
/home/bomber/.local/bin/uvx --from git+https://github.com/bomber555/claude-code-discord-bridge.git@cf8ffa437ebc4c827f9c6c1297d071ffde8beb86 ccdb --help >/dev/null
sudo -v
python3 "$bundle_dir/prepare.py" "$@"
sudo install -m 644 "$bundle_dir/ccdb-lab.service" /etc/systemd/system/ccdb-lab.service
sudo systemctl daemon-reload
sudo systemctl enable --now ccdb-lab.service
for attempt in {1..20}; do
    if curl --fail --silent http://127.0.0.1:8088/api/health; then
        systemctl is-active ccdb-lab.service
        echo 'Check Discord login: journalctl -u ccdb-lab.service -n 40 --no-pager'
        exit 0
    fi
    sleep 2
done
echo 'Health check timed out. Inspect: journalctl -u ccdb-lab.service -n 60 --no-pager' >&2
exit 1
