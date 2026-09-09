# C/C++ runner service

Standalone internal ASGI service that compiles and runs C/C++ manifests. It is
deliberately separate from the main web process: the API accepts source content
and approved profile IDs, never shell commands, mount paths, environment values,
container images, archives, or compiler flags.

The selected executor deliberately mirrors the legacy application: it invokes
the compiler and resulting binary as ordinary subprocesses in a unique working
directory. In the shipped deployment those processes live inside the dedicated
`runner` Docker container, but there is no per-job mount/network namespace.
Responses therefore state `filesystem_isolated=false`, `network=host` and
`policy=UNRESTRICTED_CONTAINER` instead of claiming a sandbox that is not there.

## Important security boundary

`UNRESTRICTED_CONTAINER` is an explicitly unsafe interim policy. Submitted code
can read the runner image, use the runner container's network and write anywhere
that the container user/filesystem permits. Do not treat it as protection from
malicious code. Docker read-only rootfs, dropped capabilities, container CPU/RAM/
PID limits and private job tmpfs remain deployment boundaries, not a per-job
sandbox. Never mount application data, secrets or the Docker socket into runner.

## API

Endpoints:

- `GET /health/live` — process liveness, no authentication;
- `GET /health/ready` — executor/toolchain readiness, no authentication;
- `GET /v1/profiles` — approved profiles, signed authentication;
- `POST /v1/jobs` — synchronous compile or compile-and-run, signed authentication.

The initial profile IDs are:

- `cpp-gcc-c++20-single`, `cpp-gcc-c++20-multi`;
- `cpp-clang-c++20-single`, `cpp-clang-c++20-multi`;
- `c-gcc-c17-single`, `c-gcc-c17-multi`;
- `c-clang-c17-single`, `c-clang-c17-multi`.

These IDs select fixed flags and limits. Adding arbitrary flags to a request is
rejected as an extra JSON field.

The FastAPI baseline accepts at most 64 files and 512 KiB of aggregate UTF-8
text before dispatch. The end-to-end workspace accepts C/C++ translation units
(`.c/.cc/.cpp/.cxx`), headers (`.h/.hh/.hpp/.hxx`), `.inc` and `.txt`; runner
limits remain a second independent boundary. The compiler receives only the
translation units and headers/includes. Before execution, `.txt` files are
copied with their safe relative paths into the same private writable working
directory as the executable.

The program may read or overwrite those text files and may create additional
text or binary files in its working directory. All runtime mutations and
generated files are transient: they are not returned as workspace files or
persisted into edit history/Moodle, and the complete job directory is removed
when the one-shot or interactive run finishes.

### HMAC authentication v1

Generate at least 32 random bytes, for example `openssl rand -hex 32`, and give
the same secret to the backend and runner through their secret stores. Do not put
it in a URL, source code, student environment, or access log.

Every internal request to `/v1/*` supplies:

```text
X-Runner-Timestamp: <Unix seconds>
X-Runner-Nonce: <16..128 characters from A-Z a-z 0-9 . _ ->
X-Runner-Signature: v1=<lowercase hex HMAC-SHA256>
```

The canonical signature payload is exactly:

```text
ASCII(timestamp) + "\n" + ASCII(nonce) + "\n" +
ASCII_HEX(SHA256(exact raw HTTP request body))
```

The HMAC key is the raw shared-secret bytes. The default clock window is ±60
seconds and a nonce can be used only once for 300 seconds. Sign the serialized
bytes that are actually sent; re-serializing JSON after signing invalidates it.

Backend helper (the same implementation is exported as
`edu_runner.security.signed_headers`):

```python
import hashlib
import hmac
import secrets
import time


def runner_headers(secret: bytes, body: bytes) -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    digest = hashlib.sha256(body).hexdigest()
    canonical = f"{timestamp}\n{nonce}\n{digest}".encode("ascii")
    signature = hmac.new(secret, canonical, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Runner-Timestamp": timestamp,
        "X-Runner-Nonce": nonce,
        "X-Runner-Signature": f"v1={signature}",
    }
```

The replay cache is process-local, so the provided command intentionally runs a
single ASGI worker. Multiple replicas require a shared nonce store or independent
per-replica keys and deterministic routing before they are exposed to one
backend.

### Request contract

```json
{
  "schema_version": "1.0",
  "request_id": "backend-run-018f...",
  "profile_id": "cpp-gcc-c++20-multi",
  "action": "compile_and_run",
  "files": [
    {"path": "include/sum.hpp", "content": "#pragma once\nint sum(int, int);\n"},
    {"path": "src/sum.cpp", "content": "#include \"../include/sum.hpp\"\nint sum(int a,int b){return a+b;}\n"},
    {"path": "src/main.cpp", "content": "#include \"../include/sum.hpp\"\n#include <iostream>\nint main(){std::cout<<sum(2,3);}\n"}
  ],
  "stdin": ""
}
```

The optional request field
`"limits": {"cpu_seconds": 2, "memory_mb": 256}` applies to the runtime
phase only. It can only lower the selected profile's CPU/address-space limits;
larger requested values are clamped to the immutable profile and compile limits
never change through this field. The effective value participates in the
manifest hash.

`action` is `compile` or `compile_and_run`. Paths are normalized relative POSIX
paths. Absolute paths, backslashes, empty/dot/parent components, duplicate paths,
symlinks and unsupported extensions are rejected. Files are staged using
exclusive regular-file creation; the request cannot name a host path.

A single profile requires exactly one `.c` or C++ translation unit and may also
contain headers. A multi profile compiles every language-appropriate translation
unit as a separate source in one link command. Source/header content is UTF-8
JSON text; archives and binary files are not accepted.

### Response contract

Compile failures and student runtime failures are normal HTTP `200` results:

```json
{
  "schema_version": "1.0",
  "request_id": "backend-run-018f...",
  "job_id": "d8de56e275214d77ad46f0011f3f3cd8",
  "status": "SUCCESS",
  "manifest_sha256": "...",
  "executable_sha256": "...",
  "profile": {
    "id": "cpp-gcc-c++20-multi",
    "language": "cpp",
    "compiler_family": "gcc",
    "compiler_version": "g++ (...) ...",
    "standard": "c++20",
    "mode": "multi"
  },
  "isolation": {
    "policy": "UNRESTRICTED_CONTAINER",
    "policy_version": "unrestricted-container-v1",
    "executor": "local",
    "filesystem_isolated": false,
    "network": "host",
    "warning": "UNRESTRICTED EXECUTION: ..."
  },
  "compilation": {
    "status": "SUCCEEDED",
    "exit_code": 0,
    "duration_ms": 412,
    "stdout": {"text": "", "bytes_captured": 0, "truncated": false},
    "stderr": {"text": "", "bytes_captured": 0, "truncated": false}
  },
  "execution": {
    "status": "SUCCEEDED",
    "exit_code": 0,
    "duration_ms": 14,
    "stdout": {"text": "5", "bytes_captured": 1, "truncated": false},
    "stderr": {"text": "", "bytes_captured": 0, "truncated": false}
  },
  "diagnostics": []
}
```

Top-level statuses are `COMPILED`, `SUCCESS`, `COMPILE_ERROR`, `RUNTIME_ERROR`,
`TIME_LIMIT`, `MEMORY_LIMIT`, `OUTPUT_LIMIT`, `WORKSPACE_LIMIT`,
`FILESYSTEM_DENIED`, and `INFRA_ERROR`. `FILESYSTEM_DENIED` is reserved for an
executor that can attribute a policy denial reliably; ordinary program `EACCES`
currently remains `RUNTIME_ERROR`.

Diagnostics contain `producer`, `producer_version`, `severity`, stable compiler
`code` where available, `message`, normalized source `file`, one-based
`start/end line/column`, related locations and compiler-provided fix-its. GCC JSON
and Clang SARIF are parsed first, with a version-tolerant text fallback. Host job
paths and terminal control sequences are removed from returned logs.

HTTP errors:

- `401` missing/stale/bad HMAC;
- `409` replayed nonce;
- `413` declared or parsed body too large;
- `422` invalid schema, path, manifest or unapproved profile;
- `429` all local execution slots are busy;
- `503` selected compiler or executor is unavailable.

## Resource limits

Profiles impose CPU and wall time, address-space memory (Linux), process count
(Linux), open-file count, per-file size, sampled total workspace size and a
strict combined stdout/stderr byte budget. The watchdog kills the process group
on wall/output/workspace limits and also kills children left after their parent
exits. Core dumps are disabled. Workspace directories are private, uniquely
named and removed in `finally` after every result.

Default values mirror `docs/06-runner-and-decision-support.md`: single builds get
10 CPU/20 wall seconds and 1 GiB; multi builds get 20/40 seconds and 2 GiB; a run
gets 2 CPU/3 wall seconds, 256 MiB and 256 KiB combined output.

The shipped executor reports the actual `network=host` state. The backend stores
this observation for teacher diagnostics and does not reject the run solely
because isolation is absent.

Limit attribution has OS-level caveats: `RLIMIT_AS` is an address-space limit,
not exact resident memory; `RLIMIT_NPROC` is UID-scoped; workspace usage is
sampled; an allocator may catch its own failure and exit normally. Production
capacity control should add cgroup v2 limits around each job when the deployment
launcher supports them. The API already has stable limit statuses for that
upgrade.

## Native Linux deployment

Prerequisites: Python 3.12+, GCC/G++ and Clang. On Debian/Ubuntu:

```bash
sudo apt-get install python3-venv gcc g++ clang
python3 -m venv .venv
.venv/bin/pip install .
```

Run under a dedicated unprivileged OS account with no home/application access:

```bash
export RUNNER_SHARED_SECRET_FILE=/run/secrets/edu-runner-hmac
export RUNNER_EXECUTOR=local
export RUNNER_WORK_ROOT=/var/lib/edu-runner/jobs
.venv/bin/edu-runner
```

Verify before connecting the backend:

```bash
curl --fail http://127.0.0.1:8081/health/live
curl --fail http://127.0.0.1:8081/health/ready
```

`ready` must report `executor: local`, `policy: UNRESTRICTED_CONTAINER` and
`ready: true`. Allow only the backend
to reach port 8081. Keep NTP active because requests are time-bound. Use one
Uvicorn worker; horizontal replicas need shared replay protection as described
above.

Преподавательская песочница использует краткоживущие интерактивные сеансы:
скомпилированный процесс может ждать построчный stdin, а преподаватель может
остановить его вручную. `RUNNER_INTERACTIVE_WALL_SECONDS` (по умолчанию 60)
ограничивает полное время жизни процесса, включая ожидание ввода. Завершённый
вывод остаётся доступен для опроса ещё
`RUNNER_INTERACTIVE_TERMINAL_TTL_SECONDS` (по умолчанию 300), после чего сеанс
удаляется из памяти. Запуск, ввод, чтение состояния и остановка подписываются
тем же HMAC-контрактом; обычный синхронный `/v1/jobs` сохранён без изменений.

## Docker

Build from this directory:

```bash
docker build -t edu-programming-runner:0.1.0 .
```

Example development/staging start on a Linux host:

```bash
export EDU_RUNNER_DEMO_SECRET="$(openssl rand -hex 32)"
docker run --rm --name edu-runner \
  --read-only \
  --pids-limit 256 \
  --memory 3g \
  --cpus 2 \
  --security-opt no-new-privileges \
  --tmpfs /tmp:rw,nosuid,nodev,size=64m,mode=1777 \
  --tmpfs /var/lib/edu-runner/jobs:rw,nosuid,nodev,exec,size=1g,uid=65532,gid=65532,mode=0700 \
  -e RUNNER_SHARED_SECRET="$EDU_RUNNER_DEMO_SECRET" \
  -p 127.0.0.1:8081:8081 \
  edu-programming-runner:0.1.0
```

The ordinary subprocess does not need broad seccomp/AppArmor exceptions. Do not
mount host source, homes, credentials or `/var/run/docker.sock`. In real
deployment inject a stable Docker/Kubernetes secret rather than generating one
in the start command.

## Developer mode and tests

The same local executor is used on developer machines and in the container:

```bash
export RUNNER_SHARED_SECRET=development-secret-at-least-32-bytes-long
export RUNNER_EXECUTOR=local
python -m uvicorn edu_runner.app:create_app --factory --port 8081
```

Every response includes an explicit unrestricted-execution warning. The backend
stores the warning metadata but, by current product decision, accepts the run.

Run the suite:

```bash
python -m pip install -e '.[test]'
pytest
```

Tests cover HMAC/replay behavior, forbidden paths and extra command fields,
single/multi-file builds, line/column diagnostics, output bounds, process-tree
termination and wall limits. Container smoke tests additionally verify that the
runner image starts with the local executor and reports its actual policy.

## Current intentional limitations

- The endpoint is synchronous and intentionally does not know whether `stdin`
  is hidden. Core v1 now invokes one fresh signed `TEST` job per teacher-authored
  hidden case and persists the report; the runner itself has no test-suite,
  grading or hidden-file storage API. Queueing, cancellation, streaming terminal
  I/O, public/visible suite orchestration and harness/data mounts remain future
  gateway/core work.
- Runtime input supports bounded `stdin` plus workspace `.txt` files in the
  executable's cwd. Arbitrary external task-data mounts and persistence of
  runtime-generated files are intentionally absent from the public contract.
- The service returns hashes and evidence, not executable artifacts.
- Ordinary subprocess execution is not a hostile-code sandbox. Reintroducing a
  `HARDENED` gVisor/microVM/namespace policy is future work.
- Compiler/runtime images should be pinned by digest and receive SBOM/signature in
  the deployment pipeline; the repository Dockerfile is a buildable baseline.
