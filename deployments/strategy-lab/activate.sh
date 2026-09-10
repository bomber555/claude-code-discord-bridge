#!/usr/bin/env bash
set -euo pipefail
bundle_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec bash "$bundle_dir/install.sh" \
    --token-file /home/bomber/.ccdb-lab-token \
    --channel-id 1547533034583101511 --backend codex
