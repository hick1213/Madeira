#!/usr/bin/env python3
"""Compile actual Wine process-name/argv helpers against Darwin's borrowed-name API."""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def function(source: str, signature: str) -> str:
    start = source.index(signature)
    brace = source.index("{", start)
    depth, end = 1, brace + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--mutation", choices=["remove-ios-gate", "skip-all-hosts"])
    args = parser.parse_args()
    if platform.system() != "Darwin":
        raise RuntimeError("This contract requires actual Darwin getprogname/setprogname")
    source = (ROOT / "build/ntdll-unix/env_ios.c").read_text()
    helpers = function(source, "static void set_process_name(") + "\n" + function(
        source, "static void rebuild_argv(")
    gate = "#if defined(HAVE_SETPROGNAME) && !defined(WINE_IOS)"
    if args.mutation:
        if helpers.count(gate) != 1:
            raise RuntimeError("Mutation requires the exact production iOS gate")
        helpers = helpers.replace(
            gate, "#ifdef HAVE_SETPROGNAME" if args.mutation == "remove-ios-gate"
            else "#if 0", 1
        )
    fixture = Path(__file__).with_name("wine-progname-contract.c").read_text()
    code = fixture.replace("/* PRODUCTION_FUNCTIONS */", helpers)
    report = {"source_sha256": hashlib.sha256(source.encode()).hexdigest(),
              "mutation": args.mutation, "runs": {}}
    if args.evidence:
        args.evidence.mkdir(parents=True, exist_ok=True)
        (args.evidence / "actual-helpers.c").write_text(helpers)
        (args.evidence / "contract.c").write_text(code)
    failed = False
    with tempfile.TemporaryDirectory(prefix="madeira-progname-") as td:
        c = Path(td) / "contract.c"
        binary = Path(td) / "contract"
        c.write_text(code)
        for build, options in [("sanitizers", ["-fsanitize=address,undefined",
                                              "-fno-sanitize-recover=all"]),
                               ("optimized", ["-O2"])]:
            for ios in [True, False]:
                name = build + ("-ios" if ios else "-non-ios")
                command = ["cc", "-std=c11", "-D_DARWIN_C_SOURCE", "-Wall", "-Wextra",
                           "-Werror", "-DHAVE_SETPROGNAME=1", f"-DHOST_EXPECT_IOS={int(ios)}",
                           *(["-DWINE_IOS=1"] if ios else []), *options,
                           str(c), "-pthread", "-o", str(binary)]
                compile_run = subprocess.run(command, capture_output=True, text=True, timeout=60)
                result = {"compile_command": command, "compile_exit": compile_run.returncode,
                          "compile_stdout": compile_run.stdout, "compile_stderr": compile_run.stderr}
                if compile_run.returncode == 0:
                    run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
                    result.update(exit_code=run.returncode, stdout=run.stdout, stderr=run.stderr)
                    failed |= run.returncode != 0
                    print(f"{name}: exit {run.returncode}\n{run.stdout}{run.stderr}", end="")
                else:
                    failed = True
                    print(f"{name}: compilation failed\n{compile_run.stderr}", end="")
                report["runs"][name] = result
    if args.evidence:
        (args.evidence / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
