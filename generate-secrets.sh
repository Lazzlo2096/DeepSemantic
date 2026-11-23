#!/usr/bin/env bash
# Generate secure random secrets for all default values in .env
# Called from bootstrap.sh after copying .env.example -> .env

set -euo pipefail

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

ENV_FILE="${1:-.env}"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "Error: $ENV_FILE not found" >&2
    exit 1
fi

# Generate a cryptographically secure random string
# Usage: gen_secret [length] [charset]
# Default: 32 chars, base64url-safe (no = + /)
gen_secret() {
    local length="${1:-32}"
    local charset="${2:-A-Za-z0-9_-}"
    openssl rand -base64 48 | tr -d '=+/' | cut -c1-"$length"
}

# Generate a hex string (for secrets that prefer hex)
gen_hex() {
    local bytes="${1:-32}"
    openssl rand -hex "$bytes"
}

# Update a key=value line in .env if it matches the default
# Usage: update_if_default "KEY" "old_default_value" "new_generated_value"
update_if_default() {
    local key="$1"
    local old_value="$2"
    local new_value="$3"

    if grep -q "^${key}=${old_value}$" "$ENV_FILE"; then
        sed -i "s|^${key}=${old_value}$|${key}=${new_value}|" "$ENV_FILE"
        printf "${GREEN}[secrets]${NC} Generated ${key}\n"
        return 0
    fi
    return 1
}

printf "${YELLOW}[secrets]${NC} Generating secure secrets for $ENV_FILE...\n"

updated=0

# ===== Authentication =====
# AUTH_PROXY_SECRET - 32 bytes hex (64 chars) for HMAC
if update_if_default "AUTH_PROXY_SECRET" "" "$(gen_hex 32)"; then
    ((updated++))
fi

# ===== PostgreSQL =====
# POSTGRES_PASSWORD - strong base64url
if update_if_default "POSTGRES_PASSWORD" "deepsemantic" "$(gen_secret 32)"; then
    ((updated++))
fi

# ===== RustFS (S3-compatible storage) =====
# RUSTFS_ACCESS_KEY - keep as identifier, but make unique
if update_if_default "RUSTFS_ACCESS_KEY" "deepsemantic" "deepsemantic-$(gen_secret 8)"; then
    ((updated++))
fi
# RUSTFS_SECRET_KEY - strong secret
if update_if_default "RUSTFS_SECRET_KEY" "deepsemantic-local" "$(gen_secret 32)"; then
    ((updated++))
fi

# ===== Grafana =====
# GRAFANA_ADMIN_PASSWORD
if update_if_default "GRAFANA_ADMIN_PASSWORD" "deepsemantic" "$(gen_secret 24)"; then
    ((updated++))
fi

# ===== Optional: pgAdmin (dev only, but secure anyway) =====
# PGADMIN_DEFAULT_PASSWORD - not in .env.example but could be added
# We don't set it here since pgAdmin runs in desktop mode without password

# ===== Summary =====
if (( updated > 0 )); then
    printf "${GREEN}[secrets]${NC} Updated $updated secret(s) in $ENV_FILE\n"
else
    printf "${YELLOW}[secrets]${NC} No default secrets found to update (already customized?)\n"
fi

# Verify critical secrets are set
printf "${YELLOW}[secrets]${NC} Verifying critical secrets...\n"
missing=0
for key in AUTH_PROXY_SECRET POSTGRES_PASSWORD RUSTFS_SECRET_KEY GRAFANA_ADMIN_PASSWORD; do
    value=$(grep "^${key}=" "$ENV_FILE" | cut -d= -f2-)
    if [[ -z "$value" ]]; then
        printf "${YELLOW}[secrets]${NC} WARNING: $key is empty!\n"
        ((missing++))
    fi
done

if (( missing > 0 )); then
    printf "${YELLOW}[secrets]${NC} $missing critical secret(s) missing - please review $ENV_FILE\n"
    exit 1
fi

printf "${GREEN}[secrets]${NC} All critical secrets are set.\n"