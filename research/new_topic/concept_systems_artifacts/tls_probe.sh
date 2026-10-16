#!/bin/sh
# Замер байтов TLS-рукопожатия и сжатия ответа RouterAI (без ключа). Запуск: sh tls_probe.sh
H=routerai.ru
date -u
openssl version
echo "## полное рукопожатие, ClientHello по умолчанию (X25519MLKEM768+X25519)"
(sleep 2; echo) | openssl s_client -connect $H:443 -servername $H -alpn h2 -sess_out /tmp/s_pq.pem 2>&1 | grep -E 'handshake has read|^New|Peer Temp Key|ALPN'
echo "## полное рукопожатие, только X25519"
(sleep 2; echo) | openssl s_client -connect $H:443 -servername $H -alpn h2 -groups X25519 -sess_out /tmp/s_x.pem 2>&1 | grep -E 'handshake has read|^New'
openssl sess_id -in /tmp/s_x.pem -text -noout | grep -i 'lifetime'
echo "## возобновление по билету, только X25519"
(sleep 1; echo) | openssl s_client -connect $H:443 -servername $H -alpn h2 -groups X25519 -sess_in /tmp/s_x.pem 2>&1 | grep -E 'handshake has read|Reused'
echo "## сжатие ответа /api/v1/models"
for enc in identity gzip br zstd; do
  curl -s -o /dev/null -D /tmp/h_$enc -H "Accept-Encoding: $enc" -w "$enc http=%{http_version} size_download=%{size_download}\n" https://$H/api/v1/models
  grep -i 'content-encoding' /tmp/h_$enc || echo "  (нет content-encoding)"
done
