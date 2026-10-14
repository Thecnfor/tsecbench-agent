#!/bin/bash
# 探测 VPN 网段内可达主机与端口，寻找模型网关真实入口
openvpn --config /config/vpn.ovpn --daemon --log /tmp/v.log --verb 3
sleep 10

echo "--- ping sweep 10.0.100.0/24:"
up=()
for i in $(seq 1 254); do
  ping -c1 -W1 10.0.100.$i >/dev/null 2>&1 && up+=("10.0.100.$i")
done
echo "up: ${up[*]}"

echo "--- port scan live hosts (80,443,8080,8000,3000,5000):"
for h in "${up[@]}"; do
  res=$(nmap -Pn -n -p 80,443,8080,8000,3000,5000,8888 --max-retries 1 -T5 "$h" 2>/dev/null | grep -E "^[0-9]+/tcp open")
  [ -n "$res" ] && echo "$h: $(echo "$res" | tr '\n' ' ')"
done

echo "--- done"
