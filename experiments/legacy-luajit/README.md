# Legacy LuaJIT high-address prototype

This isolated experiment implements LuaJIT2.0.4 bytecode-v1 interpretation with managed memory above4GiB. The source pin is `69e5342eb893815b18a1ec84ba74b0e0d1cc9beb` (official2.0.4); `upstream/` is immutable, and `runtime/` contains only the14 derived files copied over that snapshot during a fresh build. The upstream MIT notice is in [COPYRIGHT](upstream/COPYRIGHT).

All public exports match the original game DLL (135 names); Kingdom Rush Frontiers imports16 of them by name. GCRef/MRef/TValue remain4/4/8 bytes. Managed references use offsets into one bounded2GiB process arena aligned to4GiB. A per-global-state scratch preserves native registers and flags without modifying the native stack or unwind frame. Lua tracing is compiled out; `jit.status()` is false. Madeira's FEX translation JIT remains a separate mechanism.

Current validation: actual macOS x64 execution under Rosetta passes61 owned source/BCv1 assertions,48 concurrent state lifecycles,96 allocation budgets, protected OOM/retry, callbacks surviving80 other state closes and200 exhausted-state refusals/full capacity reuse. Independent code, VM, Python and memory-safety reviews passed. All575 game scripts parse in load-only mode; they are not executed. Windows DLL/probe cross-builds pass. Windows/FEX/iPad and actual game behavior are still pending; the App17 USB package does not include this runtime.

Build in an internal APFS worktree. Export artifacts/evidence to the designated external inspection directory, then clean only completed task-owned build scratch. No original game file is replaced by these scripts.

```sh
bash experiments/legacy-luajit/reference-build.sh macos
bash experiments/legacy-luajit/build.sh macos
bash experiments/legacy-luajit/build.sh windows-dynamic
```

`build.sh` prints a fresh `PROTOTYPE_BUILD_DIR`. Its macOS probe accepts `--expect-high --corpus FILE`, `--stress`, or `--load-manifest FILE`. Reference builds preserve upstream source bytes and explicitly clean generated objects with the matching architecture. Both use `-fno-strict-float-cast-overflow` to preserve the old integer-narrowing probe under modern Clang. Mach links use `-no_deduplicate` because VM dispatch stores computed code offsets.

The optional `tools/stage-kingdom-luajit.py` tool reads an existing reviewed build from `--build-dir`, then `MADEIRA_LUAJIT_BUILD_DIR`, then `.prototype-build/windows-dynamic` under the repository. Set the environment variable to the reviewed `PROTOTYPE_BUILD_DIR` to use the same build in `tests/host/check-kingdom-luajit-stage.py`. Original game files and prior review evidence under `app/Build/Products/` are private inputs and are not included here. The two tests that need them explicitly skip when their inputs are absent; independent destination-refusal tests still run. Staging retains all pinned hashes and source/export checks. PE inspection uses system temporary storage; keep `TMPDIR` on internal APFS if overriding it on macOS.

The original x64 public `lua_newstate` custom-allocator restriction is preserved. The reference `--high-core` fixture uses the internal constructor to expose its actual high-address rejection; it is distinct from the public-API restriction. Host POSIX concurrent exception throws retain the old static unwind-object limitation; sequential failure recovery is verified. This is not Windows runtime evidence.

Design and implementation plan: [design](../../docs/superpowers/specs/2026-10-12-legacy-luajit-compat-design.md), [plan](../../docs/superpowers/plans/2026-10-12-legacy-luajit-compat.md).
