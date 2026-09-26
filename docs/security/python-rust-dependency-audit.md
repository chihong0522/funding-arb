# Python and Rust dependency audit

**Baseline:** `c85a48ee0de02b20b7dce4dd924a9364a4dad32d` (`2026-09-26`)
**Audit date:** `2026-09-26`
**Scope:** Python manifests/locks and the Rust manifest only. No frontend, trading, exchange, or credential changes were made.
**Status:** Python locks generated and verified in isolated containers. The paper-only lock is now the default startup/CI profile for the paper Binance/Bybit scope; it excludes optional DEX SDKs and uses cross-platform Uvicorn core dependencies. The full compatibility lock has confirmed advisory findings; the server lock remains a separate profile. Rust lock/advisory coverage remains untested.

This is a dependency checkpoint, not a security certification or a claim that the repository has no backdoors.

## Files added

- `requirements.lock` — hash-pinned resolution of the existing root `requirements.txt`, including all declared optional DEX SDKs so their declared install behavior is preserved.
- `requirements.paper.in` — explicit paper-only CEX/server/test profile. It keeps `requests`, `keyring`, `pytest`, FastAPI, Uvicorn core, and WebSockets, and intentionally excludes optional DEX SDKs. Uvicorn's optional native `standard` extras are not part of this cross-platform paper profile.
- `requirements.paper.lock` — hash-pinned paper-only profile used by `setup.sh`, `start.sh`, `start.ps1`, the Python CI job, and the default scanner workflow path.
- `server/requirements.lock` — hash-pinned resolution of the existing `server/requirements.txt`.

The existing `requirements.txt` and `server/requirements.txt` ranges were not silently rewritten. The full lock is the compatibility inventory; the paper lock is the safer install profile for the current paper-only fork.

## Isolation and evidence

Dependency acquisition and advisory queries ran separately from project execution. No host packages, repository code, credentials, SSH material, Docker socket, or secret-bearing directories were mounted.

- Container image: `python:3.12-slim`, digest `sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f`.
- Python: `3.12.14`.
- Container UID: `1001:1001` (non-root).
- Runtime controls: `--read-only`, `--cap-drop=ALL`, `--security-opt=no-new-privileges`, bounded CPU/memory/PIDs, executable temporary filesystem only for the disposable virtual environment.
- Registry/advisory egress: public PyPI and OSV only. Project execution was not performed in the network-enabled acquisition containers.
- Evidence directory: `/mnt/vol-1/hermes/cache/scratch/funding-arb-dependency-audit-20260926/`.

The scratch directory contains the exact resolver/audit logs, installed-package JSON, lock hashes, OSV response, PyPI metadata response, and status files. Important evidence files include:

- `requirements.public.lock`, `server.requirements.public.lock`, `requirements.paper.lock`
- `pip-audit.public.json`, `pip-audit.server.public.json`, `pip-audit.paper.public.json`
- `osv-query-results.json`, `pypi-metadata.json`
- `requirements.all.installed.json`, `server.requirements.installed.json`, `requirements.paper.installed.json`
- `public-audit-status.txt`, `root-audit-status.txt`, `server-audit-status.txt`, `paper-audit-status.txt`

## Direct and transitive inventory

The committed lock files are the complete exact-version/hash inventory. Resolver output was installed into fresh disposable virtual environments with `--require-hashes`; `pip check` passed for each profile. Package-name comparison uses normalized PEP 503 names.

### Root compatibility profile

Direct requirements from the existing root manifest and resolved versions:

| Direct requirement | Resolved |
| --- | ---: |
| `requests>=2.28` | `2.34.2` |
| `keyring>=23.0` | `25.7.0` |
| `lighter-sdk>=1.0` (optional DEX) | `1.1.4` |
| `edgex-python-sdk>=2.0.0` (optional DEX) | `2.0.1` |
| `dydx-v4-client>=1.1.5` (optional DEX) | `1.1.6` |
| `hyperliquid-python-sdk>=0.22.0` (optional DEX) | `0.24.0` |
| `pytest>=7.0` (test) | `9.1.1` |

`requirements.lock` contains **83** exact package entries, all with hashes. The fresh install contained the same 83 normalized package/version pairs (plus the virtualenv bootstrap `pip` package). Selected transitive/security-relevant resolutions include `bip-utils==2.12.2`, `ecdsa==0.19.2`, `urllib3==2.0.7`, and `setuptools==84.0.0`.

Working-tree lock SHA-256: `b1e3f95aab0dd1ed56e76b675deaa31cf773d6074ec5bd8b607613822ac1c050`.

### Server profile

Direct requirements from the existing server manifest and resolved versions:

| Direct requirement | Resolved |
| --- | ---: |
| `fastapi>=0.110.0` | `0.141.1` |
| `uvicorn[standard]>=0.29.0` | `0.54.0` |
| `websockets>=12.0` | `17.1` |

`server/requirements.lock` contains **19** exact package entries, all with hashes. The fresh install contained the same 19 normalized package/version pairs (plus virtualenv bootstrap `pip`).

Working-tree lock SHA-256: `a5d6d0953d7fa4302804b411435a6c773473a22a20148c6de3739cd998117467`.

### Paper-only profile

`requirements.paper.in` resolves the server requirements plus `requests`, `keyring`, and `pytest`. It deliberately excludes `lighter-sdk`, `edgex-python-sdk`, `dydx-v4-client`, and `hyperliquid-python-sdk`; no Python source or existing manifest was changed to remove those optional capabilities. The paper profile uses `uvicorn` core rather than `uvicorn[standard]` so Windows CI/startup does not require the Linux-only `uvloop` wheel set.

`requirements.paper.lock` contains **33** exact package entries, all with hashes. The fresh install contained the same 33 normalized package/version pairs. Security-relevant resolutions include `requests==2.34.2`, `urllib3==2.8.0`, `fastapi==0.141.1`, `uvicorn==0.54.0`, and `websockets==17.1`.

Working-tree lock SHA-256: `9a3cc903dc74f4cb0ce71053275fec266a79aaff2956ff8d5e54d19e44be39e5`.

### Existing audit-test image inventory

For comparison, the supplied `funding-arb-audit-tests:local` image (`sha256:ad85677017171ef613e8810f0011bb729a0a569071789f44eecde6429ffbe015`) was queried read-only with `--network none`. It contains **27** installed packages, including `fastapi==0.141.1`, `httpx==0.28.1`, `pytest==9.1.1`, `requests==2.34.2`, `websockets==17.1`, `PyYAML==6.0.3`, and `uvicorn==0.54.0`; `keyring` is absent. The complete `pip list --format=json` is saved as `audit-tests-image.installed.json`. This image inventory is test-harness evidence, not a substitute for the working-tree hash locks.

## Audit commands and results

### Hash-lock generation

Run in the isolated `python:3.12-slim` container with public PyPI selected:

```text
python -m venv /tmp/venv
/tmp/venv/bin/pip install --no-cache-dir pip-tools==7.5.0
/tmp/venv/bin/pip-compile --generate-hashes --allow-unsafe --strip-extras \
  --emit-index-url --index-url=https://pypi.org/simple \
  --output-file=/audit/requirements.public.lock /inputs/requirements.txt
/tmp/venv/bin/pip-compile --generate-hashes --allow-unsafe --strip-extras \
  --emit-index-url --index-url=https://pypi.org/simple \
  --output-file=/audit/server.requirements.public.lock /inputs/server-requirements.txt
/tmp/venv/bin/pip-compile --generate-hashes --allow-unsafe --strip-extras \
  --emit-index-url --index-url=https://pypi.org/simple \
  --output-file=/audit/requirements.paper.lock /audit/requirements.paper.in
```

The committed files are byte-for-byte copies of the corresponding public-resolution outputs named above.

### Runtime and CI integration

The paper profile is the default for the owned startup and CI paths:

- `setup.sh`, `start.sh`, and `start.ps1` fail closed if `requirements.paper.lock` is missing, then run `python -m pip install --require-hashes -r requirements.paper.lock` before starting the API or desktop/browser runtime.
- `.github/workflows/ci.yml` installs the same lock on both Ubuntu and Windows and runs the complete `scripts/tests/` suite; it does not exclude venue tests because optional DEX SDKs are absent.
- The default path in `.github/workflows/telegram-push.yml` installs the paper lock and passes `--venues binance,bybit`. The typed `include_dex` input defaults to `false`; an explicit `true` fails before checkout or dependency installation with an unsupported-profile message. The full-lock findings remain documented and are not treated as cleared by this default-path fix.
- The existing server-owned Binance/Bybit execution allowlist and process-level live kill switch are unchanged. Dependency/startup wiring cannot enable live writes.
- The no-network audit-test image had no `lighter`, `edgex_sdk`, `dydx_v4_client`, or `hyperliquid` importable SDKs; the complete existing suite still passed (`534 passed`), so no tests were excluded for the paper profile.

### Fresh hash-verified installs

Each profile was installed into a new disposable Python 3.12 virtual environment:

```text
python -m venv /tmp/app
/tmp/app/bin/pip install --no-cache-dir --require-hashes -r /audit/<lockfile>
/tmp/app/bin/pip list --format=json
/tmp/app/bin/pip check
```

Results:

| Profile | Install | `pip check` |
| --- | ---: | ---: |
| `requirements.lock` | `0` | `0` |
| `server/requirements.lock` | `0` | `0` |
| `requirements.paper.lock` (33-package cross-platform profile) | `0` | `0` |

For the current paper lock, a fresh non-root `python:3.12-slim` acquisition container installed all **33/33** lock entries with `--require-hashes`; `pip check` reported no broken requirements. A separate `--platform win_amd64 --python-version 3.12 --only-binary=:all:` dry-run also completed successfully. The earlier lock's unconditional `uvloop` requirement failed that Windows compatibility check; removing Uvicorn's optional `standard` extras is the bounded fix.

### `pip-audit`

`pip-audit==2.9.0` audited the exact lock requirements through the OSV-backed advisory service. The queries returned JSON and completed; the zero-finding results below are not failed-query results.

```text
/tmp/tools/bin/pip-audit -r /audit/requirements.public.lock \
  --format=json --output=/audit/pip-audit.public.json --desc --progress-spinner off
/tmp/tools/bin/pip-audit -r /audit/server.requirements.public.lock \
  --format=json --output=/audit/pip-audit.server.public.json --desc --progress-spinner off
/tmp/tools/bin/pip-audit -r /audit/requirements.paper.lock \
  --format=json --output=/audit/pip-audit.paper.public.json --desc --progress-spinner off
```

Results:

- **Full root compatibility lock:** exit `1`; `Found 8 known vulnerabilities in 3 packages`.
- **Server lock:** exit `0`; `No known vulnerabilities found`.
- **Current 33-package paper-only lock:** exit `0`; `No known vulnerabilities found` (fresh re-run after removing the Uvicorn standard extras).

The full-lock findings are:

1. `ecdsa==0.19.2`, transitive through `bip-utils -> dydx-v4-client`: `GHSA-wj6h-64fc-37mp` / `CVE-2024-23342` / `PYSEC-2026-1325` (Minerva timing attack on P-256). The advisory has no upstream fix version in the audit result.
2. `dydx-v4-client==1.1.6`: `GHSA-4f84-67cv-qrv3` (a malicious `1.1.5.post1` release contained an obfuscated loader). OSV matches the package/version query, while the linked PyPA advisory evidence identifies the affected artifact as `1.1.5.post1`; this is recorded as a supply-chain review blocker, not proof that `1.1.6` contains that loader.
3. `urllib3==2.0.7`, pulled by both `lighter-sdk` and `requests`: six audit findings, including `CVE-2024-37891`, `CVE-2025-50181`, `CVE-2025-66418`, `CVE-2025-66471`, `CVE-2026-21441`, and `CVE-2026-44431` (the corresponding GHSA/PYSEC identifiers are preserved in `pip-audit.public.json`).

Direct public registry metadata confirmed that the latest available `lighter-sdk` is `1.1.4` and it requires `urllib3<2.1.0`; the current full resolution therefore cannot be safely repaired by forcing `urllib3==2.8.0` without violating the SDK's declared constraint. `urllib3==2.8.0` is the resolved version in the paper-only profile and returned no OSV findings in the direct query.

## Direct OSV and registry queries

The isolated OSV query script sent successful PyPI package/version queries for:

```text
ecdsa==0.19.2
dydx-v4-client==1.1.6
dydx-v4-client==1.1.5.post1
urllib3==2.0.7
urllib3==2.6.3
urllib3==2.8.0
```

Observed results were recorded in `osv-query-results.json`: `ecdsa==0.19.2` matched two duplicate records for the same CVE, `dydx-v4-client==1.1.6` matched the GHSA supply-chain record, `urllib3==2.0.7` matched 12 OSV records/aliases, and `urllib3==2.8.0` matched none. The query script exited `0`; a nonzero HTTP/query failure would not have been reported as zero findings.

Authoritative references used for interpretation:

- [python-ecdsa GHSA-wj6h-64fc-37mp](https://github.com/tlsfuzzer/python-ecdsa/security/advisories/GHSA-wj6h-64fc-37mp)
- [PyPA advisory evidence for `dydx-v4-client`](https://github.com/pypa/advisory-database/tree/main/vulns/dydx-v4-client/PYSEC-2026-1.yaml)
- [PyPI inspector evidence for the malicious `1.1.5.post1` artifact](https://inspector.pypi.io/project/dydx-v4-client/1.1.5.post1/)
- [urllib3 security advisories](https://github.com/urllib3/urllib3/security/advisories)

## Optional DEX decision

The full `requirements.lock` intentionally preserves all existing optional DEX declarations so the repository's declared capabilities are not silently removed. It is **not installed by the default setup/startup/CI paths**, and the Telegram workflow rejects its former opt-in path while the findings above remain unresolved.

The minimal, functionality-preserving remediation added here is a separate `requirements.paper.lock` profile that excludes optional DEX SDKs from the current paper-only CEX/server/test environment. This avoids the `lighter-sdk` `urllib3<2.1.0` constraint and the dYdX/`ecdsa` dependency path without changing source behavior or deleting DEX support from the repository.

Parent acceptance is still required before:

- installing the full compatibility lock in a runtime that does not need the optional DEX paths;
- accepting `lighter-sdk`'s unresolved vulnerable transitive constraint;
- accepting the dYdX advisory match or selecting a reviewed replacement/fork;
- claiming the optional DEX paths are safe for live use.

No major SDK upgrade, forced constraint override, or version was invented in this worker's scope.

## Rust status

`web/src-tauri/Cargo.toml` declares `tauri`, `serde`, `serde_json`, and `tokio`, but the baseline has no `web/src-tauri/Cargo.lock`.

Rust was not audited: `rustc` and `cargo` were unavailable on the host, and the available local Docker images did not include a Rust toolchain. No Rust package was installed on the host, no Cargo lock was generated, and no `cargo audit`/RustSec query was run. Rust dependency versions, advisories, and build reproducibility remain **untested** and require a separately approved Rust toolchain container.

## Remaining scope

- Parent Astra must accept the optional-DEX profile boundary and the full-lock findings before using `requirements.lock` for any broader runtime.
- A reviewed remediation is still needed for `lighter-sdk`'s constrained `urllib3` path; do not force-install an incompatible `urllib3` version.
- The dYdX package requires a supply-chain decision despite the installed `1.1.6` being newer than the specifically documented malicious `1.1.5.post1` artifact.
- Rust `Cargo.lock` generation and RustSec audit are outstanding.
- This worker did not run exchange calls, public-market paper validation, the project server, or live/paper trading code. Those remain separate parent acceptance gates.
- No commit or push was performed.
