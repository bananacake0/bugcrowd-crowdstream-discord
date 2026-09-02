#!/usr/bin/env bash

set -u
set -o pipefail

project_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$project_dir" || exit 1

uv_bin=${UV_BIN:-uv}
if ! command -v "$uv_bin" >/dev/null 2>&1; then
    echo "[!] uv was not found. Set UV_BIN to its absolute path."
    exit 1
fi

state_files=(processed_ids.json programs.json)
bot_status=0
"$uv_bin" run --locked main.py || bot_status=$?

if git diff --quiet HEAD -- "${state_files[@]}"; then
    echo "[*] Submission and program state are unchanged; no commit created."
else
    diff_status=$?
    if ((diff_status != 1)); then
        echo "[!] Could not compare submission and program state with HEAD."
        exit "$diff_status"
    fi

    if git commit --only -m "chore: update CrowdStream state" -- "${state_files[@]}"; then
        echo "[*] Committed updated submission or program state."
    else
        commit_status=$?
        echo "[!] Could not commit updated submission or program state."
        exit "$commit_status"
    fi
fi

exit "$bot_status"
