# Forensics / Stego Cheatsheet

## First moves (per artifact)
- Unknown file: `file X` → `binwalk X` → `strings -n 8 X | head -50` → `xxd X | head`.
- Archive: `7z l` (list, incl. comments + extra fields); passwords to try first:
  empty, "infected", "password", flag-format words, files inside the challenge dir.
- Image (PNG/JPG): `exiftool` → `zsteg png` (LSB all channels) → `binwalk -e` →
  `steghide extract -sf img` (empty/weak pass) → alpha channel / palette anomalies.
- PCAP: `tshark -r x.pcap --export-objects http,out/`; follow streams
  (`tshark -q -z conv,tcp`); look at DNS TXT queries, ICMP payloads, FTP creds.

## Memory dump
volatility3 order: `pslist` → `cmdline` → `filescan | grep -i flag` → `dumpfiles`.

## Disk / filesystem
- Deleted files: `fls -r disk.img` + `icat` (TSK) or `photorec`.
- Mount read-only first: `mount -o ro,loop disk.img /mnt`.

## Encoding chains
Recognize by alphabet: base64 (A-Za-z0-9+/=), base32 (A-Z2-7=), hex (0-9a-f only),
base85 (~ high-ascii). Decode iteratively — flag may be nested 2-3 layers deep;
`base64 -d` until stable, never eyeball long chains.
