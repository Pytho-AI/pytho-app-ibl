#!/usr/bin/env bash
# Regenerate the self-signed TLS keypair used by the nginx image's :8443
# internal listener. Writes BOTH cert.pem and key.pem as a matched pair
# (the cert is signed by the freshly-generated private key — they have
# to be regenerated together).
#
# Tracking: cert.pem is committed (public half, safe to share). key.pem
# is gitignored (private half; each dev / deploy gets their own). On a
# fresh checkout, rebuild.sh detects the missing key and runs this
# script automatically — no manual bootstrap required as long as openssl
# is on PATH (every dev box and the spark have it).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

# Subject Alternative Names — Firefox rejects the cert if the hostname
# the browser hit isn't in this list, EVEN AFTER the analyst trusts the
# CA. 10.0.1.30 is THE authoritative airgapped spark address that every
# analyst, every cert, every config targets; it is listed first because
# it is the load-bearing entry. The rest are supporting:
#
#   IP:10.0.1.30       — spark on the airgapped LAN — every analyst hits this
#   DNS:spark-dev      — internal hostname (used by ssh + docker DNS)
#   DNS:localhost      — Mac/Linux dev hitting their own machine
#   IP:127.0.0.1       — loopback (Mac dev + dev curl)
#   IP:0.0.0.0         — handler test fixture default
#
# Adding a new deploy target? Append it here AND re-run this script,
# THEN re-import the cert in Firefox (the fingerprint changes when the
# SAN list changes, so an already-trusted older cert becomes invalid).
SAN="IP:10.0.1.30,DNS:spark-dev,DNS:localhost,IP:127.0.0.1,IP:0.0.0.0"

# Allow extra SANs via env var without editing the script (e.g.
# PYTHO_SSL_EXTRA_SAN="IP:10.0.1.31,DNS:other-spark" ./regenerate.sh).
if [ -n "${PYTHO_SSL_EXTRA_SAN:-}" ]; then
    SAN="${SAN},${PYTHO_SSL_EXTRA_SAN}"
fi

openssl req -x509 -nodes -newkey rsa:2048 \
    -keyout "$HERE/key.pem" \
    -out    "$HERE/cert.pem" \
    -days   3650 \
    -subj   "/CN=spark-dev" \
    -addext "subjectAltName=${SAN}"
chmod 600 "$HERE/key.pem"
echo "regenerated $HERE/cert.pem and $HERE/key.pem"
echo "SAN: ${SAN}"
