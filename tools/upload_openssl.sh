#!/bin/sh
# 通过 openssl s_client 上传 Release 资产（绕开 schannel 证书链问题）
# 用法: sh tools/upload_openssl.sh <file> <asset-name>
set -u
cd "$(dirname "$0")/.." || exit 1

FILE="${1:-dist/GameBox-Portable-v1.0.exe}"
NAME="${2:-GameBox-Portable-v1.0.exe}"
IP="${3:-20.205.243.161}"

TOK=$(sed -n 's|https://[^:]*:\([^@]*\)@github.com.*|\1|p' "$HOME/.git-credentials" | head -1)
if [ -z "$TOK" ]; then echo "no token"; exit 1; fi

SZ=$(stat -c %s "$FILE")
HDR=$(mktemp)
trap 'rm -f "$HDR"' EXIT

printf 'POST /repos/zxc441521/GameBox/releases/438185255/assets?name=%s HTTP/1.1\r\nHost: uploads.github.com\r\nAuthorization: Bearer %s\r\nAccept: application/vnd.github+json\r\nContent-Type: application/octet-stream\r\nContent-Length: %s\r\nUser-Agent: GameBox-Uploader\r\nConnection: close\r\n\r\n' \
  "$NAME" "$TOK" "$SZ" > "$HDR"

echo "uploading $FILE ($SZ bytes) as $NAME via $IP"
( cat "$HDR"; cat "$FILE" ) | timeout 1200 openssl s_client -quiet \
  -connect "$IP:443" -servername uploads.github.com -tls1_2 -ign_eof > /dev/null 2>&1
echo "openssl exit=$?"
