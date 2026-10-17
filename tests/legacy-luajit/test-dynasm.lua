-- Run with the pinned reference luajit from the worktree root.
-- All generated files stay in the explicitly supplied task scratch directory.
local runner, scratch = ...
assert(runner and scratch, 'usage: luajit test-dynasm.lua MINILUA SCRATCH')
local dynasm = 'experiments/legacy-luajit/runtime/dynasm/dynasm.lua'
local function quote(s) return "'"..s:gsub("'", "'\\''").."'" end
local function read(path)
  local f = assert(io.open(path)); local s = f:read('*a'); f:close(); return s
end
local cases = {
  {'missing_state_scratch', 'mov nativeptr [eax], ecx', 'dedicated .arena_scratch'},
  {'scratch_qword_state', '.arena_scratch rax, 8', '32-bit state register'},
  {'scratch_sp_state', '.arena_scratch esp, 8', '32-bit state register'},
  {'scratch_reserved_state', '.arena_scratch r11d, 8', '32-bit state register'},
  {'mov64_load', 'mov64 rax, [12345]', 'managed MOV64 memory'},
  {'mov64_store', 'mov64 [12345], rax', 'managed MOV64 memory'},
  {'mov64_r10', 'mov64 r10, 1', 'reserved scratch'},
  {'mov64_r11', 'mov64 r11, 1', 'reserved scratch'},
  {'nativeptr_qword_source', 'mov nativeptr [eax], rcx', '32-bit source'},
  {'nativeptr_rsp_source', 'mov nativeptr [eax], rsp', '32-bit source'},
  {'nativeptr_esp_source', 'mov nativeptr [eax], esp', '32-bit source'},
  {'absolute', 'mov eax, dword [12345]', 'unclassified absolute'},
  {'multiple', 'mov dword [eax], dword [ecx]', 'multiple memory'},
  {'scratch_register', 'mov r10d, eax', 'reserved scratch'},
  {'scratch', 'mov eax, dword [r10]', 'reserved scratch'},
  {'highbyte', 'mov ah, byte [eax]', 'high-byte register'},
}
for _,c in ipairs(cases) do
  local input, output = scratch..'/'..c[1]..'.dasc', scratch..'/'..c[1]..'.log'
  local f = assert(io.open(input,'w'))
  f:write('|.arch x64\n|.section code\n|.arena managed\n| ',c[2],'\n'); f:close()
  local status = os.execute(quote(runner)..' '..quote(dynasm)..' '..quote(input)..' >'..quote(output)..' 2>&1')
  assert(status ~= 0, c[1]..': unsafe generation unexpectedly accepted')
  assert(read(output):find(c[3],1,true), c[1]..': unexpected failure: '..read(output))
  print('PASS generator rejects '..c[1])
end
