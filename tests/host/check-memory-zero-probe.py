#!/usr/bin/env python3
"""Exercise the actual VM zero-read probe and remap instrumentation on the host.

The C fixture extracts production code unchanged. Mach reads and fixed remaps
are the OS boundary doubles; counters observe actual reads of the fixture, not
log frequency. Compilation stays on internal temporary storage on macOS.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import uuid


ROOT = Path(__file__).resolve().parents[2]


def positive_timeout(value: str) -> float:
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise argparse.ArgumentTypeError("timeout must be finite and greater than zero")
    return timeout


def invoke(command: list[str], phase: str, timeout: float, records: list[dict],
           environment: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Record successful, failed, timed-out and unlaunchable real subprocesses."""
    record = dict(phase=phase, command=command, timeout_seconds=timeout, status="running")
    records.append(record)
    try:
        result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        record.update(status="timeout", stdout=(error.stdout or b"").decode(errors="replace")
                      if isinstance(error.stdout, bytes) else error.stdout or "",
                      stderr=(error.stderr or b"").decode(errors="replace")
                      if isinstance(error.stderr, bytes) else error.stderr or "")
        raise AssertionError(f"{phase} timed out after {timeout}s") from error
    except OSError as error:
        record.update(status="launch-error", error=str(error))
        raise AssertionError(f"{phase} could not launch: {error}") from error
    record.update(status="pass" if result.returncode == 0 else "fail", returncode=result.returncode,
                  stdout=result.stdout, stderr=result.stderr)
    assert result.returncode == 0, f"{phase} exited {result.returncode}\n{result.stdout}{result.stderr}"
    return result


@contextmanager
def run_evidence(parent: Path | None):
    """Each invocation owns new evidence; finally records even setup/compile failures."""
    started = datetime.now(timezone.utc)
    run_id = started.strftime("%Y%m%dT%H%M%S.%fZ-") + uuid.uuid4().hex[:8]
    directory = parent / f"run-{run_id}" if parent else None
    if directory:
        directory.mkdir(parents=True, exist_ok=False)
    records: list[dict] = []
    summary = dict(run_id=run_id, started_utc=started.isoformat(), status="fail", phases=records)
    try:
        runtime = ROOT / "build/ntdll-unix/virtual_ios.c"
        summary["runtime_sha256"] = hashlib.sha256(runtime.read_bytes()).hexdigest()
        summary["test_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        yield records, directory
    except BaseException as error:
        summary.update(error_type=type(error).__name__, error=str(error))
        raise
    else:
        summary["status"] = "pass"
    finally:
        summary["finished_utc"] = datetime.now(timezone.utc).isoformat()
        summary["last_phase"] = records[-1]["phase"] if records else "setup"
        if directory:
            (directory / "result.json").write_text(json.dumps(summary, indent=2) + "\n")
            transcript = []
            for record in records:
                transcript.append(json.dumps({key: value for key, value in record.items()
                                               if key not in ("stdout", "stderr")})
                                  + "\n" + record.get("stdout", "") + record.get("stderr", ""))
            transcript.append(f"FINAL status={summary['status']} last_phase={summary['last_phase']}\n"
                              + summary.get("error", ""))
            (directory / "host-test-transcript.txt").write_text("\n".join(transcript))
            print(f"ARTIFACTS: {directory}", flush=True)
        print(f"RUN status={summary['status']} last_phase={summary['last_phase']}", flush=True)


def braced(source: str, start: int) -> str:
    """Return one C block, ignoring braces inside comments and strings."""
    token = re.compile(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[{}]', re.S)
    depth = 0
    for match in token.finditer(source, start):
        value = match.group()
        if value == "{":
            depth += 1
        elif value == "}":
            depth -= 1
            if depth == 0:
                return source[start:match.end()]
    raise AssertionError("unterminated production C block")


def production_code() -> str:
    source = (ROOT / "build/ntdll-unix/virtual_ios.c").read_text()
    signature = "static void ios_verify_commit_zero("
    start = source.index(signature)
    commit = source[start:source.index("\n}\n", start) + 3]
    probe_start = source.index("\n        {", source.index("/* ml547: same census")) + 9
    decommit = braced(source, probe_start)
    remap_start = source.index("\n            {", source.index("if (ios_swap_punch_resv( host_start")) + 13
    remap = braced(source, remap_start)
    core_start = source.find("/* zero-read probe core begin */")
    core = "" if core_start < 0 else source[core_start:source.index("/* zero-read probe core end */", core_start)]
    return "\n".join([
        core,
        commit,
        "static void fixture_decommit(void *base, size_t size, const char *dc_branch, const void *dc_verify, size_t dc_vsize)\n" + decommit,
        "static void fixture_remap(void *base, size_t size)\n{\n"
        "char *host_start = base, *host_end = host_start + size;\n"
        "const char *dc_branch = NULL; const void *dc_verify = NULL; size_t dc_vsize = 0;\n"
        + remap + "\n"
        "require(dc_verify == base && dc_vsize == size && !strcmp(dc_branch, \"mmap-over\"), \"remap diagnostics preserve verify branch\");\n}\n",
    ])


PRELUDE = r"""
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
typedef size_t SIZE_T;
typedef unsigned int ULONG;
typedef unsigned char BYTE;
typedef uint64_t mach_vm_size_t;
typedef uint64_t mach_vm_address_t;
typedef unsigned int mach_msg_type_number_t;
typedef unsigned int mach_port_t;
typedef int *vm_region_info_t;
typedef struct { int protection, max_protection; } vm_region_basic_info_data_64_t;
#define KERN_SUCCESS 0
#define MACH_PORT_NULL 0
#define VM_REGION_BASIC_INFO_COUNT_64 2
#define VM_REGION_BASIC_INFO_64 1
#define PAGE_NOACCESS 1
#define PAGE_READONLY 2
#define PAGE_READWRITE 4
#define PAGE_WRITECOPY 8
#define PAGE_EXECUTE_READ 32
#define PAGE_EXECUTE_READWRITE 64
#define PAGE_EXECUTE_WRITECOPY 128
#define PAGE_GUARD 256
static unsigned char fixture[80];
static int arena, swap_overlap, read_failure, remap_failure;
static unsigned long mach_reads, direct_reads, snapshot_checks, remap_calls, release_calls;
static void require(int ok, const char *message)
{
    if (!ok) { fprintf(stderr, "FAIL: %s\n", message); exit(1); }
}
static int ios_is_arena_addr(const void *base) { (void)base; return arena; }
int ios_swap_overlaps_probe(const void *base, size_t size) { (void)base; (void)size; return swap_overlap; }
static int mach_task_self(void) { return 1; }
static int mach_vm_read_overwrite(int task, mach_vm_address_t address, mach_vm_size_t size,
                                  mach_vm_address_t destination, mach_vm_size_t *got)
{
    require(task == 1 && address == (uintptr_t)fixture && size <= 64, "bounded Mach fixture read");
    mach_reads++;
    if (read_failure == 1) { *got = 0; return 1; }
    memcpy((void *)(uintptr_t)destination, fixture, size);
    *got = read_failure == 2 ? size - 1 : size;
    return KERN_SUCCESS;
}
static int mach_vm_region(int task, mach_vm_address_t *address, mach_vm_size_t *size,
                          int flavor, vm_region_info_t info, mach_msg_type_number_t *count,
                          mach_port_t *port)
{
    (void)task; (void)address; (void)size; (void)flavor; (void)info; (void)count; (void)port;
    return 1;
}
static BYTE get_host_page_vprot(const void *base) { (void)base; return 0; }
static int get_unix_prot(BYTE protection) { (void)protection; return 0; }
static long ios_first_nonzero(const void *base, size_t size)
{
    const unsigned char *bytes = base;
    if (base == fixture) direct_reads++; else snapshot_checks++;
    for (size_t i = 0; i < size; i++) if (bytes[i]) return (long)i;
    return -1;
}
static void ios_swap_release_range(void *base, size_t size, int copy_back)
{
    require(base == fixture && size == 64 && !copy_back, "release arguments preserved");
    release_calls++;
}
static void *anon_mmap_fixed(void *base, size_t size, int protection, int flags)
{
    require(base == fixture && size == 64 && protection == (PROT_READ | PROT_WRITE) && !flags,
            "fixed remap arguments preserved");
    remap_calls++;
    errno = remap_failure ? ENOMEM : EBUSY;
    return remap_failure ? MAP_FAILED : base;
}
"""


DRIVER = r"""
int main(int argc, char **argv)
{
    require(argc == 2, "one fixture scenario");
    const char *scenario = argv[1];
    if (!strcmp(scenario, "eligible"))
    {
        for (arena = 0; arena < 2; arena++)
        {
            for (int i = 0; i < 10000; i++) ios_verify_commit_zero(fixture, 64, PAGE_READWRITE, 0);
            for (int i = 0; i < 10000; i++) fixture_decommit(fixture, 64, "mmap-over", fixture, 64);
        }
    }
    else if (!strcmp(scenario, "quiet-buckets"))
    {
        for (int i = 0; i < 10000; i++) ios_verify_commit_zero(fixture, 64, PAGE_READWRITE, 0);
        arena = 1;
        ios_verify_commit_zero(fixture, 64, PAGE_READWRITE, 0);
        fixture_decommit(fixture, 64, "mmap-over", fixture, 64);
        arena = 0;
        fixture_decommit(fixture, 64, "mmap-over", fixture, 64);
    }
    else if (!strcmp(scenario, "ineligible"))
    {
        ios_verify_commit_zero(NULL, 64, PAGE_READWRITE, 0);
        ios_verify_commit_zero(fixture, 0, PAGE_READWRITE, 0);
        ios_verify_commit_zero(fixture, 64, PAGE_NOACCESS, 0);
        ios_verify_commit_zero(fixture, 64, PAGE_READWRITE | PAGE_GUARD, 0);
        ios_verify_commit_zero(fixture, 64, 16, 0);
        ios_verify_commit_zero(fixture, 64, PAGE_READWRITE, 1);
        swap_overlap = 1;
        ios_verify_commit_zero(fixture, 64, PAGE_READWRITE, 0);
        fixture_decommit(fixture, 64, "swap-hole", NULL, 0);
    }
    else if (!strcmp(scenario, "readfail") || !strcmp(scenario, "partial-read"))
    {
        read_failure = !strcmp(scenario, "readfail") ? 1 : 2;
        for (int i = 0; i < 4096; i++) ios_verify_commit_zero(fixture, 64, PAGE_READWRITE, 0);
    }
    else if (!strcmp(scenario, "stale"))
    {
        fixture[8] = 0xAA;
        for (int i = 0; i < 4096; i++) ios_verify_commit_zero(fixture, 64, PAGE_READWRITE, 0);
        for (int i = 0; i < 4096; i++) fixture_decommit(fixture, 64, "mmap-over", fixture, 64);
    }
    else if (!strcmp(scenario, "remap"))
    {
        for (int i = 0; i < 4096; i++)
        {
            remap_failure = i == 8;
            fixture_remap(fixture, 64);
            require(errno == (remap_failure ? ENOMEM : EBUSY), "diagnostics preserve remap errno");
        }
    }
    else if (!strcmp(scenario, "parse-once"))
    {
        ios_verify_commit_zero(fixture, 64, PAGE_READWRITE, 0);
        setenv("MADEIRA_VM_ZERO_SAMPLE", "0", 1);
        for (int i = 1; i < 10000; i++) ios_verify_commit_zero(fixture, 64, PAGE_READWRITE, 0);
    }
    else if (!strncmp(scenario, "commit-tail-", 12) || !strncmp(scenario, "decommit-tail-", 14))
    {
        size_t checked, offset;
        int commit = !strncmp(scenario, "commit-tail-", 12);
        require(sscanf(scenario + (commit ? 12 : 14), "%zu-%zu", &checked, &offset) == 2 &&
                checked && checked <= 64 && offset < checked, "valid tail fixture");
        memset(fixture, 0xEE, sizeof(fixture));
        memset(fixture, 0, checked);
        fixture[offset] = 0xAA;
        for (size_t i = offset + 1; i < checked; i++) fixture[i] = (unsigned char)(0xB0 + i - offset);
        if (commit) ios_verify_commit_zero(fixture, checked, PAGE_READWRITE, 0);
        else fixture_decommit(fixture, checked, "mmap-over", fixture, checked);
    }
    else require(0, "known fixture scenario");
    printf("RESULT mach=%lu direct=%lu snapshots=%lu remaps=%lu releases=%lu\n",
           mach_reads, direct_reads, snapshot_checks, remap_calls, release_calls);
    return 0;
}
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, help="parent for a new run-ID evidence directory")
    parser.add_argument("--compile-timeout", type=positive_timeout, default=60.0)
    parser.add_argument("--run-timeout", type=positive_timeout, default=15.0)
    args = parser.parse_args()
    directory = "/private/tmp" if Path("/private/tmp").is_dir() else None
    with run_evidence(args.artifact_dir) as (records, artifact_dir), \
            tempfile.TemporaryDirectory(prefix="madeira-zero-probe-", dir=directory) as temporary:
        if not __debug__:
            raise RuntimeError("Python assertions are disabled; run this test without -O")
        folder = Path(temporary)
        source_path, executable = folder / "zero-probe.c", folder / "zero-probe"
        source_path.write_text(PRELUDE + production_code() + DRIVER)
        if artifact_dir:
            shutil.copy2(source_path, artifact_dir / source_path.name)
        command = [os.environ.get("CC", "cc"), "-std=gnu11", "-Wall", "-Wextra", "-Werror",
                   "-Wno-unused-function", "-Wno-unused-parameter", "-g",
                   "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                   str(source_path), "-o", str(executable)]
        invoke(command, "compile", args.compile_timeout, records)

        def run(scenario: str, value: str | None) -> tuple[dict[str, int], str]:
            environment = dict(os.environ)
            environment.pop("MADEIRA_VM_ZERO_SAMPLE", None)
            if value is not None:
                environment["MADEIRA_VM_ZERO_SAMPLE"] = value
            result = invoke([str(executable), scenario], f"run:{scenario}:sample={value!r}",
                            args.run_timeout, records, environment)
            counters = dict((key, int(number)) for key, number in re.findall(r"(\w+)=(\d+)", result.stdout))
            return counters, result.stderr

        # This catches a sampling flag that merely limits logs while reading every range.
        counters, log = run("eligible", "1")
        assert counters == dict(mach=20, direct=20, snapshots=20, remaps=0, releases=0), counters
        summaries = [line for line in log.splitlines() if line.startswith("[vm-zero-probe]")]
        assert len(summaries) <= 48, "sampling summaries are bounded"
        for kind in ("commit", "decommit"):
            for band in ("GUEST", "arena/pool"):
                final = next(line for line in reversed(summaries) if f"kind={kind} band={band} " in line)
                assert "eligible=8192 reads=10 skipped=8182 readfail=0 stale=0" in final, final
        skipped = [line for line in summaries if "state=skipped-unverified" in line]
        assert len(skipped) == 4 and all(" OK " not in line and "state=zero" not in line for line in skipped), skipped
        print("PASS: sampling gates actual commit/decommit reads independently in both bands")

        # Absence, explicit zero, and every malformed value retain all-read behavior.
        for value in (None, "0", "", "yes", "2", "-1", "1junk", "01", " 1"):
            counters, log = run("eligible", value)
            assert counters == dict(mach=20000, direct=20000, snapshots=20000, remaps=0, releases=0), (value, counters)
            assert "mode=full" in log and "skipped-unverified" not in log, (value, log)
        print("PASS: default and invalid settings preserve full-read behavior")

        counters, log = run("quiet-buckets", "1")
        assert counters == dict(mach=11, direct=2, snapshots=11, remaps=0, releases=0), counters
        counters, log = run("ineligible", "1")
        assert not any(counters.values()) and "[vm-zero-probe]" not in log, (counters, log)
        print("PASS: busy buckets cannot starve quiet bands or the other operation; exclusions stay unread")

        for scenario in ("readfail", "partial-read"):
            counters, log = run(scenario, "1")
            assert counters["mach"] == 9 and counters["snapshots"] == 0, counters
            assert "eligible=4096 reads=9 skipped=4087 readfail=9 stale=0" in log, log
            assert "[commit-noaccess]" in log and "STALE ON COMMIT" not in log, log
            assert len(log.splitlines()) <= 32, "read failures are bounded"
            counters, log = run(scenario, None)
            assert counters["mach"] == 4096 and counters["snapshots"] == 0, counters
            assert "eligible=4096 reads=4096 skipped=0 readfail=4096 stale=0" in log, log
            assert len(log.splitlines()) <= 32, "full-read failure logs are bounded"
        counters, log = run("stale", "1")
        assert counters["mach"] == 9 and counters["direct"] == 9 and counters["snapshots"] == 9, counters
        assert log.count("eligible=4096 reads=9 skipped=4087 readfail=0 stale=9") == 2, log
        assert "STALE ON COMMIT" in log and "NOT ZEROED" in log, log
        assert len(log.splitlines()) <= 64, "nonzero anomaly output is bounded"
        counters, log = run("stale", None)
        assert counters["mach"] == 4096 and counters["direct"] == 4096 and counters["snapshots"] == 4096, counters
        assert log.count("eligible=4096 reads=4096 skipped=0 readfail=0 stale=4096") == 2, log
        assert len(log.splitlines()) <= 64, "full-read nonzero anomaly output is bounded"
        print("PASS: failed/short reads are distinct from confirmed stale data; anomaly markers survive")

        # These literals catch stack tail overreads and reporting bytes outside
        # a short checked range as observed data (the surrounding fixture is EE).
        tails = [
            (64, 63, "aa"), (64, 62, "aa b1"), (64, 61, "aa b1 b2"),
            (64, 60, "aa b1 b2 b3"), (64, 59, "aa b1 b2 b3 b4"),
            (64, 58, "aa b1 b2 b3 b4 b5"), (64, 57, "aa b1 b2 b3 b4 b5 b6"),
            (64, 56, "aa b1 b2 b3 b4 b5 b6 b7"),
            (1, 0, "aa"), (2, 0, "aa b1"), (2, 1, "aa"),
            (7, 0, "aa b1 b2 b3 b4 b5 b6"), (7, 6, "aa"),
            (8, 7, "aa"), (16, 15, "aa"), (63, 62, "aa"),
        ]
        for kind, marker in (("commit", "STALE ON COMMIT"), ("decommit", "NOT ZEROED")):
            for checked, offset, expected in tails:
                counters, log = run(f"{kind}-tail-{checked}-{offset}", "1")
                anomaly = next(line for line in log.splitlines() if marker in line)
                observed = re.search(r"bytes@nz=([0-9a-f ]+) bytes_count=(\d+)", anomaly)
                assert observed and observed[1] == expected and int(observed[2]) == len(expected.split()), anomaly
                assert f"first_nonzero=+0x{offset:x}" in anomaly, anomaly
                assert counters["mach"] == (1 if kind == "commit" else 0), counters
                assert counters["direct"] == (1 if kind == "decommit" else 0), counters
        print("PASS: stale tail/short diagnostics print only actual checked bytes, preserving anomaly markers")

        counters, log = run("remap", "1")
        assert counters == dict(mach=0, direct=0, snapshots=0, remaps=4096, releases=4096), counters
        assert "calls=4096 ok=4095 fail=1" in log and f"last_fail_errno={errno.ENOMEM}" in log, log
        assert len(log.splitlines()) <= 12, "no-touch remap output is bounded"
        counters, log = run("parse-once", "1")
        assert counters["mach"] == 10 and log.count("[vm-zero-probe-config]") == 1, (counters, log)
        print("PASS: remap return/errno counters do not read memory or alter branch/errno; config parses once")
    print("PASS: actual production zero-read probes, compiled with AddressSanitizer/UBSan")


if __name__ == "__main__":
    main()
