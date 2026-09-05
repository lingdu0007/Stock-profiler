#!/bin/sh
set -eu

source_certificate=/run/secrets/gateway_tls_certificate
source_private_key=/run/secrets/gateway_tls_private_key
runtime_secret_directory=/run/stock-profiler
certificate=$runtime_secret_directory/gateway_tls_certificate
private_key=$runtime_secret_directory/gateway_tls_private_key
hostname=${STOCK_PROFILER_GATEWAY_HOSTNAME:?set a stable private HTTPS hostname}
bind_address=${STOCK_PROFILER_GATEWAY_BIND_ADDRESS:?set a permitted private IPv4 bind address}

case "$hostname" in
  "" | .* | *.) echo "gateway hostname is invalid" >&2; exit 1 ;;
  *[!A-Za-z0-9.-]*) echo "gateway hostname is invalid" >&2; exit 1 ;;
esac

case "$bind_address" in
  127.*.*.* | 10.*.*.* | 192.168.*.* | 172.1[6-9].*.* | 172.2[0-9].*.* | 172.3[0-1].*.*) ;;
  100.6[4-9].*.* | 100.[7-9][0-9].*.* | 100.1[0-1][0-9].*.* | 100.12[0-7].*.*) ;;
  *)
    echo "gateway bind address must be loopback, RFC1918, or CGNAT IPv4" >&2
    exit 1
    ;;
esac

openssl x509 -in "$source_certificate" -noout -checkend 0
openssl pkey -in "$source_private_key" -noout

certificate_not_before=$(
  openssl x509 -in "$source_certificate" -noout -dateopt iso_8601 -startdate | cut -d= -f2
)
if ! certificate_not_before_epoch=$(date -u -d "${certificate_not_before%Z}" +%s); then
  echo "gateway certificate validity start time is invalid" >&2
  exit 1
fi
if [ "$certificate_not_before_epoch" -gt "$(date -u +%s)" ]; then
  echo "gateway certificate is not yet valid" >&2
  exit 1
fi

certificate_public_key=$(
  openssl x509 -in "$source_certificate" -pubkey -noout |
    openssl pkey -pubin -pubout -outform DER |
    sha256sum
)
private_key_public_key=$(
  openssl pkey -in "$source_private_key" -pubout -outform DER |
    sha256sum
)
if [ "$certificate_public_key" != "$private_key_public_key" ]; then
  echo "gateway certificate does not match its private key" >&2
  exit 1
fi

if ! openssl x509 -in "$source_certificate" -noout -checkhost "$hostname" 2>&1 |
  grep -Fq " does match certificate"; then
  echo "gateway certificate does not match its configured hostname" >&2
  exit 1
fi

install -d -m 700 -o 10001 -g 10001 "$runtime_secret_directory"
install -m 600 -o 10001 -g 10001 "$source_certificate" "$certificate"
install -m 600 -o 10001 -g 10001 "$source_private_key" "$private_key"

exec su-exec 10001:10001 caddy run --environ --config /etc/caddy/Caddyfile --adapter caddyfile
