# Legacy LuaJIT compatibility prototype implementation plan

> **For agentic workers:** Use subagent-driven-development for bounded nonconflicting runtime tasks; reconcile shared C/VM interfaces before running the runtime. Preserve the approved prototype scope and actual verification gates. No commits, publication, FEX edits or original game replacement.

**Goal:** Run a genuine LuaJIT2.0.4 bytecode-v1 interpreter with Lua-managed memory above4GiB, preserving its public Lua5.1 ABI.

**Architecture:** A single stable4GiB-aligned process arena with2GiB virtual capacity stores all managed memory. GCRef/MRef fields remain32-bit offsets and TValue/FR1 remain unchanged. The JIT-disabled x64 interpreter translates managed effective addresses through volatile r10/r11; native C stack, callback/code/lightuserdata pointers retain real native representation.

**Tech Stack:** Pinned LuaJIT2.0.4 C/DynASM, clang/LLVM, Windows x64 MinGW, POSIX/macOS x64 host probe, owned C/Lua fixtures and Python orchestration.

**Spec:** docs/superpowers/specs/2026-10-12-legacy-luajit-compat-design.md

## Global constraints

- User approved the experimental prototype; it does not declare Kingdom Rush fixed.
- Upstream pin is69e5342eb893815b18a1ec84ba74b0e0d1cc9beb, release2.0.4; archiveSHA5f791711adc30c2f773d2c796d489c9a76334c9c6f53833013810cd9ce98046f. Keep its197-file source snapshot immutable and retain MIT provenance.
- Compile LUAJIT_DISABLE_JIT: runtime jit.off alone is insufficient. Retain FFI and jit.status=false behavior; do not modify FEX.
- Keep GCRef/MRef4bytes, TValue8bytes, FR1 fields and bytecodev1; zero isNULL and the first64KiB arena prefix remains inaccessible/unallocated.
- Public allocator/API pointers remain real; outside-arena allocator results must be rejected safely at allocation boundaries.
- Use the native-managed isolated worktree /Users/hick/.devspace/worktrees/Madeira-0d3808df (workspace ws_d77995a483) for APFS-dependent cross-link/symlink steps. Store provenance/evidence/deliverables externally under app/Build/Products/IPAInspection/lua-compat-prototype; copy new source externally before cleaning completed task-owned APFS scratch. Keep in-progress source and existing evidence.
- Do not alter reviewed build16 App/runtime files or any original game DLL/archive. Real iPad target is Fang Dong M5; do not terminate an active session.
- Rosetta installation was explicitly approved; verify execution capability, not just installer output.

## Review focus

- Mixed offset/native register and field accesses, especially L.base/top, tmpbuf.buf, C frames, FFI stubs and0/1-or-pointer helper returns.
- Pointer identity/hash/lightuserdata and code-relative continuations must not be biased.
- Every managed VM memory operand needs address translation without clobbering flags, ABI arguments or scratch liveness.
- Multiple states, failure cleanup, concurrent allocation/free and arena ownership must not unmap another live state's memory.
- Successful construction or parsing does not prove calls/GC/coroutines/native callbacks or actual game execution.

## Shared interfaces and files

The prototype lives under experiments/legacy-luajit in the isolated worktree. upstream/ is immutable; runtime/ is its derived working copy.

- C worker owns runtime/src/lj_compat_arena.[ch], lj_obj.h, lj_alloc.c, lj_state.c, lj_gc.c and lj_ccallback.c. Expose `uintptr_t lj_compat_arena_base` (stable4GiB alignment), `uint32_t lj_compat_ref_encode(const void *)`, `void *lj_compat_ref_decode(uint32_t)`, `int lj_compat_contains(const void *, size_t)`, `void *lj_compat_arena_map(size_t)` and `int lj_compat_arena_unmap(void *, size_t)`. Raw copy/hash references stay raw offsets.
- VM worker owns runtime/src/vm_x86.dasc, runtime/dynasm/dasm_x86.lua and any explicit generation annotations; any buildvm Mach data relocation change is coordinated with Root. Managed-EA wrappers use the same lj_compat_arena_base and preserve flags with LEA.
- Root owns build.sh/provenance/plan coordination and tests/legacy-luajit/{probe.c,corpus.lua,run-probes.py}; these are isolated prototype harnesses, not application source.

## Tasks

### Task1: Exact source, reference build and failing high-address probe

- [ ] Archive the exact official commit, verify provenance and immutable source hash.
- [ ] Build an unmodified2.0.4 interpreter/library and generate owned bytecode-v1 semantic fixtures.
- [ ] Run an actual high-address allocator through lua_newstate: unmodified code must returnNULL rather than meeting the new high-pointer contract. Record compiler success and runtime rejection separately.
- [ ] Verify local x64 execution after Rosetta; cross-build reference Windows x64. Ordinary modern GC64 incompatibility remains a separate existing negative probe, not this implementation's test.

### Task2: Stable high arena and C representation

- [ ] Write actual native arena/reference tests forNULL, alignment, bounds, interior/one-past cursors, allocation reuse and independent states before implementation.
- [ ] Implement shared synchronized arena reserve/commit/decommit lifecycle. Reuse original dlmalloc through arena map/unmap, not a parallel fake Lua allocator.
- [ ] Change only pointer encode/decode and identified allocation-location checks; adapt actual FFI callback global-state immediate. Preserve native ABI and raw hashes/code pointers.
- [ ] Native tests and cross-build must pass; independent code review before full VM integration.

### Task3: Actual interpreter address and ABI translation

- [ ] Make the full runtime high-state/openlibs probe fail against C-only changes, establishing the missing VM translation.
- [ ] Implement explicit managed/native DynASM annotations and memory-operand lowering with r10/r11. Reject unclassified or unsafe operands; retain widths and live flags.
- [ ] Convert full native pointer fields and C arguments/returns, preservingNULL/boolean sentinels and code-relative continuation fields; adapt coroutine/error/FFI boundaries.
- [ ] Compile and execute the actual generated x64 VM, fixing concrete failing semantic cases without skipping them.

### Task4: Semantic, native-boundary and format gates

- [ ] Run owned BCv1 calls/returns/tailcalls/multret/varargs/metamethods/hooks, upvalues/stack growth, strings/tables/GC/finalizers, coroutines and multiple states.
- [ ] Verify native callbacks receive high state/string/userdata pointers, use actual memcpy/memmove, reenter Lua and unwind errors correctly. Retain FFI behavior through native call/callback tests.
- [ ] Generate ABI register/flag regression cases and realistic compiled runtime mutations, with exact task-owned process/fixture cleanup.
- [ ] Load all575 actual game chunks without execution for format coverage. Semantic fixtures, not parsing alone, establish equivalence.

### Task5: Review and Windows/iPad prototype delivery

- [ ] Independently review C/assembly/build/harness/security and fix verified issues; compile Windows x64 probe/DLL and verify original public import/export requirements.
- [ ] Copy only owned prototype source, binary and evidence externally; preserve original files and finally-clean completed own APFS scratch.
- [ ] Test the same standalone probe on Madeira on the current iPad, using a safe idle session. No claim of physical success until actual results exist.
- [ ] Only after the complete gate, prepare opt-in private-runtime integration and actual Kingdom Rush startup/graphics/control/save/exit test. If any prototype gate fails, retain the failure and report its exact scope.
