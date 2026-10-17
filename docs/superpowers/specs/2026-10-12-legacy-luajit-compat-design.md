# Legacy LuaJIT compatibility prototype

Status: user approved the bounded compatibility prototype on2026-10-12 with “确认”. Implement its gates autonomously. Product integration follows only after those gates pass; replacing original game files or publication is not approved. The Game Mode/iCloud save build16 is independent and does not contain this fix.

## Confirmed problem

The user's actual Kingdom Rush Frontiers (Steam458710) uses Windows x64 LuaJIT2.0.4. Its luaL_newstate requests128KiB below2GiB and returns NULL after iOS rejects the address range. The game does not test the result before luaL_openlibs/lua_pushcclosure reads NULL+8. PE disassembly and return addresses match the actual build15 log exactly. Madeira remains running; measured physical memory peaks at1724MB and Steam downloads were already paused.

All575 script files in the actual fused game archive are LuaJIT bytecode v1. A local load-only test with ordinary LuaJIT2.1 GC64 rejects actual main.lua as incompatible; no game bytecode was executed by that test. XNU's ARM64 loader requires4GiB PAGEZERO. Dropping ZeroBits, reducing the linker guard, raising physical RAM or enabling the old lowalloc experiment does not establish a valid repair.

Evidence is preserved under app/Build/Products/CrashInvestigations/2026-10-12-kingdom-recurrence/analyzer and analyzer-feasibility. The copied actual DLL/EXE remain inspection inputs; no original game or runtime file on the device has been replaced.

## Proposed bounded first stage

Build a separate LuaJIT2.0 compatibility prototype preserving bytecode v1, Lua5.1 C API and original public version marker. Allocate managed objects in a bounded contiguous arena above4GiB and encode internal32-bit references as validated relative offsets. Keep NULL encoded as0 and return real high pointers at every public/native boundary. Audit both C reference macros and the shared x86 VM assembly; merely changing allocation cannot work. Initially disable Lua's tracing optimizer; Madeira/FEX translation JIT remains necessary and unchanged.

A shared process arena with explicit allocation ownership and synchronized multi-state lifetime is the initial design choice. The effective Lua-managed heap still has the old approximately2GiB bound. Do not claim that this provides arbitrary large game memory.

The prototype is isolated from Steam, game installations and the deployed application. No FEX source modifications, game archive rewrites, disabled memory-safety checks, fake successful allocations or hash-specific title exceptions are permitted. Preserve upstream licenses and source provenance.

## Acceptance gates before integration

1. Force the real arena above4GiB and verify create/openlibs/close and repeated allocation/free with bounds and NULL checks.
2. Run owned bytecode-v1 semantic fixtures: calls, tail calls, varargs/multret, closures/upvalues, recursive functions, iterators, tables/strings, coroutines and explicit GC. Compare behavior with an unmodified reference where available.
3. Exercise multiple states and threads, native C callbacks, real string/userdata pointers and memcpy/memmove. There must be no invalid low pointer escape or dependence on serviced Mach faults.
4. Compile a Windows x64 probe and compatible export surface. Load every actual script without executing it to establish format coverage; loading alone is not execution equivalence.
5. Only after the above gates, propose an opt-in application integration using a private runtime and preserved original files, then test actual startup, graphics, controls, saves and exit on the current iPad.

If any gate fails, report the exact failure and stop that design's integration. Modern GC64 plus a full v1-to-FR2 semantic translator is a separate alternative, not a header tweak. A matching complete32-bit game/runtime build is another conditional option; the current x64 executable cannot load a lone PE32 replacement DLL.

## Product decision requiring user input

Proceed with this separate, experimental runtime prototype, or defer the new runtime and use a genuinely compatible game build. This changes the runtime architecture and initially trades Lua tracing optimization for compatibility; it is a material decision under the user's AGENTS.md instructions. Approval of a prototype does not authorize replacing original game files or publishing a runtime before its acceptance gates pass.
