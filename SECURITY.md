# Security Policy

Aerodrome is a self-hosted ADS-B tracker maintained as a one-person hobby project. Security issues are taken seriously, but please calibrate expectations to a best-effort, no-SLA project.

## Supported Versions

Only the **latest released version** receives security patches. If you find an issue, please test against the latest release before reporting; if the issue persists there, the report will be acted on.

## Reporting a Vulnerability

**Please do not file a public GitHub issue for security vulnerabilities.** Use GitHub's private vulnerability reporting:

1. Go to the **Security** tab of the [Aerodrome repository](https://github.com/preston-peterson/aerodrome).
2. Click **Report a vulnerability**.
3. Submit the form.

A useful report includes:

- The Aerodrome version (`/api/version` endpoint, or the `VERSION` file in the install directory).
- A clear description of the issue.
- Steps to reproduce, if possible.
- The potential impact (data exposure, command execution, denial of service, etc.).
- A proof-of-concept if you're comfortable sharing one.

### What to expect

- **Acknowledgment:** best effort within a week. There is no SLA.
- **Triage:** the report will be assessed for severity, reproducibility, and scope.
- **Fix:** if the issue is confirmed and in scope, a patch lands in the next release. Critical issues may get an out-of-band patch release.
- **Disclosure:** coordinated disclosure is preferred. The fix will appear in the changelog without exploit details until users have had a reasonable window to update.

## Threat Model

Aerodrome assumes the deployment posture of a **trusted local network**. The web UI has no authentication by default because LAN-only deployments don't typically need it, and requiring authentication for the LAN case would add friction without meaningful protection. This is a deliberate design choice, not an oversight.

If you expose Aerodrome to the public internet or an untrusted network, **that's a configuration choice you make as the operator**. In that case, you're responsible for providing authentication and access control upstream of Aerodrome — for example, a reverse proxy with HTTP basic auth, a VPN, or Tailscale. Issues that exist only because Aerodrome is exposed to the internet are out of scope for this project.

## Scope

**In scope** (please report):

- Code-execution, command-injection, or arbitrary-file-write vulnerabilities in the Aerodrome codebase.
- SQL injection or unsafe query construction.
- Path traversal in endpoints that handle filenames or paths.
- Information disclosure beyond what the application is documented to expose.
- Logic flaws that allow unintended configuration changes or data manipulation, even on a LAN-only deployment.
- Vulnerabilities in the in-app update flow (zip handling, path traversal during apply, sudoers/systemd integration).

**Out of scope** (please report upstream or to the appropriate party):

- Issues in third-party dependencies (FastAPI, uvicorn, requests, ruamel.yaml, etc.) — please report to the upstream project.
- Issues in dump1090, readsb, tar1090, or other ADS-B receiver software — those are separate projects.
- "The web UI has no authentication" — by design, see the threat model above.
- Issues only exploitable when Aerodrome is intentionally exposed to an untrusted network.
- Denial-of-service via traffic flooding — Aerodrome has no rate limiting, and LAN deployment is the assumed posture.
- Theoretical issues without a clear exploitation path.
- Best-practice deviations that don't represent an exploitable vulnerability.

## Hardening Suggestions

A few practices worth following on any Aerodrome deployment:

- Run Aerodrome on a LAN-only or VPN-only network. Don't expose port 8080 to the public internet.
- Keep the host system patched. Debian, Ubuntu, and Raspberry Pi OS all receive regular security updates.
- Use the in-app update mechanism (gear menu → Check for Updates) rather than manual file edits or `git pull`, so the install layout, sudoers integration, and systemd unit stay consistent.
- Review the live `config.yaml` after updates. The config-merge-on-startup logic preserves your values across upgrades, but newly-added keys land with sensible defaults that you may want to review.

Thanks for helping keep Aerodrome secure.
