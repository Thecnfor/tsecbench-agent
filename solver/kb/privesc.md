# Privesc / Post-Exploit Cheatsheet

## Linux (order by hit rate in CTFs)
1. `sudo -l` → GTFOBins for every permitted binary (env_keep, LD_PRELOAD, wildcards).
2. SUID: `find / -perm -4000 -type f 2>/dev/null` → GTFOBins; unusual SUID → read it.
3. Cron: `cat /etc/crontab`, `/etc/cron.*`; writable cron scripts → append payload.
4. Capabilities: `getcap -r / 2>/dev/null` (cap_setuid python = one-liner shell).
5. Writable service units / PATH hijack of relative-bin services:
   `systemctl list-timers`, `cat /etc/systemd/system/*.service`.
6. Kernel: `uname -r` → dirty pipe / pwnkit era checks (see cve_triage.md).
7. Secrets in files: `grep -rEi 'pass(word)?|api[_-]?key|flag' / --include='*.{conf,env,txt,sh}' -l`.

## Windows (if applicable)
1. `whoami /priv` → SeImpersonate (potato family), SeBackup/SeRestore.
2. AlwaysInstallElevated registry keys; unquoted service paths;
   `winPEAS` if available, else manual service query.

## Containers
- `/proc/1/cgroup` shows docker → check for mounted docker.sock (`-v /var/run/docker.sock`).
  Docker.sock escape: install docker client or use curl --unix-socket against the API
  to spawn a privileged container mounting the host root.
- Capabilities: cap_sys_admin → cgroup release_agent escape; check `capsh --print`.
- Flag often mounted read-only at /flag, /flag.txt, /root/flag — enumerate before escaping.
