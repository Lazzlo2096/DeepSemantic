#!/usr/bin/env bash
set -euo pipefail

if (( EUID != 0 )); then
    printf 'Run this script with sudo: sudo bash %s\n' "$0" >&2
    exit 1
fi

swap_file=/swapfile

exec 9>/run/lock/deepsemantic-swapfile.lock
flock 9

if [[ -L "$swap_file" || ( -e "$swap_file" && ! -f "$swap_file" ) ]]; then
    printf 'Expected a regular file at %s\n' "$swap_file" >&2
    exit 1
fi

if [[ ! -e "$swap_file" ]]; then
    printf '[swap] Writing 8 GiB to create %s.\n' "$swap_file"
    (
        temp_file=$(mktemp "${swap_file}.XXXXXX")
        trap 'rm -f -- "$temp_file"' EXIT
        dd if=/dev/zero of="$temp_file" bs=16M count=512 conv=fsync status=progress
        printf '[swap] Formatting the new swap file.\n'
        mkswap "$temp_file"
        ln -T -- "$temp_file" "$swap_file"
    )
else
    printf '[swap] Using the existing swap file at %s.\n' "$swap_file"
fi

if ! swapon --show=NAME --noheadings --raw | grep -Fx -- "$swap_file" >/dev/null; then
    printf '[swap] Activating %s.\n' "$swap_file"
    swapon "$swap_file"
else
    printf '[swap] %s is already active.\n' "$swap_file"
fi

if ! awk -v file="$swap_file" '$1 == file && $3 == "swap" { found = 1 } END { exit !found }' /etc/fstab; then
    printf '[swap] Adding %s to /etc/fstab for activation at boot.\n' "$swap_file"
    printf '\n%s none swap sw 0 0\n' "$swap_file" >> /etc/fstab
else
    printf '[swap] %s is already configured in /etc/fstab.\n' "$swap_file"
fi
