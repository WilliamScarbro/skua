#!/bin/bash
# SPDX-License-Identifier: BUSL-1.1
set -u

tcpdump_path="$(command -v tcpdump 2>/dev/null || true)"
rc=0
CAP_NET_ADMIN=12
CAP_NET_RAW=13

pass() {
    printf '[OK] %s\n' "$1"
}

warn() {
    printf '[!!] %s\n' "$1"
    rc=1
}

info() {
    printf '[--] %s\n' "$1"
}

print_caps() {
    local label="$1"
    local raw="$2"
    if command -v capsh >/dev/null 2>&1; then
        info "$label: $(capsh --decode="$raw" 2>/dev/null || printf '%s' "$raw")"
    else
        info "$label: $raw"
    fi
}

has_cap_bit() {
    local raw="$1"
    local bit="$2"
    [ -n "$raw" ] || return 1
    local value=$((16#$raw))
    (( (value & (1 << bit)) != 0 ))
}

report_required_caps() {
    local label="$1"
    local raw="$2"
    local missing=0
    local present=()
    local absent=()

    if has_cap_bit "$raw" "$CAP_NET_ADMIN"; then
        present+=("CAP_NET_ADMIN")
    else
        absent+=("CAP_NET_ADMIN")
        missing=1
    fi
    if has_cap_bit "$raw" "$CAP_NET_RAW"; then
        present+=("CAP_NET_RAW")
    else
        absent+=("CAP_NET_RAW")
        missing=1
    fi

    if [ "${#present[@]}" -gt 0 ]; then
        info "$label includes: ${present[*]}"
    fi
    if [ "$missing" -eq 0 ]; then
        pass "$label includes required tcpdump capabilities"
    else
        warn "$label is missing: ${absent[*]}"
    fi
}

echo "Monitoring check"
echo "  User: $(id -un) ($(id -u):$(id -g))"

if [ -z "$tcpdump_path" ]; then
    warn "tcpdump is not installed"
    exit "$rc"
fi

pass "tcpdump found at $tcpdump_path"

if command -v getcap >/dev/null 2>&1; then
    file_caps="$(getcap "$tcpdump_path" 2>/dev/null || true)"
    if printf '%s' "$file_caps" | grep -q 'cap_net_admin' && printf '%s' "$file_caps" | grep -q 'cap_net_raw'; then
        pass "tcpdump file capabilities are set"
    else
        warn "tcpdump file capabilities missing expected cap_net_admin/cap_net_raw"
    fi
    if [ -n "$file_caps" ]; then
        info "file caps: $file_caps"
    fi
else
    warn "getcap is not available; cannot inspect tcpdump file capabilities"
fi

cap_eff="$(awk '/^CapEff:/ {print $2}' /proc/self/status 2>/dev/null)"
cap_bnd="$(awk '/^CapBnd:/ {print $2}' /proc/self/status 2>/dev/null)"

if [ -n "$cap_eff" ]; then
    print_caps "effective caps" "$cap_eff"
fi
if [ -n "$cap_bnd" ]; then
    print_caps "bounding caps" "$cap_bnd"
    report_required_caps "bounding set" "$cap_bnd"
fi

probe_output="$(timeout 3 tcpdump -i any -L 2>&1)"
probe_status=$?
if [ "$probe_status" -eq 0 ]; then
    pass "tcpdump can open capture interfaces"
elif printf '%s' "$probe_output" | grep -qi 'permission denied\|operation not permitted'; then
    warn "tcpdump probe failed due to missing runtime capabilities"
    printf '[--] probe: %s\n' "$(printf '%s' "$probe_output" | tail -1)"
else
    warn "tcpdump probe failed"
    printf '[--] probe: %s\n' "$(printf '%s' "$probe_output" | tail -1)"
fi

exit "$rc"
