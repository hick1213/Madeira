#!/usr/bin/env python3
"""Run the production Mach CAS branch against pool and registered code aliases."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def extract(source: str, start: int) -> str:
    brace = source.index('{', start)
    depth, end = 1, brace + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


HARNESS = r'''
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
static struct {
 uintptr_t user_va, user_va_end, jit_rx_alias, jit_rw_alias;
} ios_jit_anon_aliases[2];
static int ios_jit_anon_alias_count;
/* COVER */
/* CAS */
static int handle(uint32_t insn, uintptr_t fault_addr, uintptr_t rw_addr,
                  uintptr_t rx, size_t sz, int already, uint64_t gpr[29])
{
 struct { uint64_t __x[29]; } state;
 memcpy(state.__x, gpr, sizeof(state.__x));
 uintptr_t rw = rw_addr;
 uint64_t fault_pc = 0x16b32cd30;
 int in_jit = rx && rw && sz && fault_addr >= rx && fault_addr - rx < sz;
 int emulated = already;
 /* BRANCH */
 memcpy(gpr, state.__x, sizeof(state.__x));
 return emulated;
}
static unsigned checks, failures;
static void check(int value, const char *name) {
 checks++; if (!value) { failures++; fprintf(stderr, "FAIL: %s\n", name); }
}
static const uintptr_t ANON = UINT64_C(0x71933f7480), POOL = UINT64_C(0x148000000);
static uint64_t words[8], regs[29];
static void reset(void) {
 memset(words, 0, sizeof(words)); memset(regs, 0, sizeof(regs));
 memset(ios_jit_anon_aliases, 0, sizeof(ios_jit_anon_aliases));
 ios_jit_anon_alias_count = 1;
 ios_jit_anon_aliases[0].user_va = ANON;
 ios_jit_anon_aliases[0].user_va_end = ANON + 24;
 ios_jit_anon_aliases[0].jit_rw_alias = (uintptr_t)words;
 ios_jit_anon_aliases[0].jit_rx_alias = POOL;
 words[2] = UINT64_C(0x1122334455667788);
 regs[11] = words[2]; regs[0] = UINT64_C(0x8877665544332211); regs[28] = 0xabcdef;
}
static void reject(uint32_t insn, uintptr_t address, uintptr_t alias, uintptr_t pool, size_t size, const char *name) {
 uint64_t before[8], saved[29];
 memcpy(before, words, sizeof(words)); memcpy(saved, regs, sizeof(regs));
 check(handle(insn, address, alias, pool, size, 0, regs) == 0, name);
 check(!memcmp(before, words, sizeof(words)) && !memcmp(saved, regs, sizeof(regs)), "rejected CAS leaves memory and registers unchanged");
}
int main(void) {
 const uint32_t actual = 0xc8ebff20u; /* CASAL X11, X0, [X25] from device */
 reset();
 check(handle(actual, POOL + 16, (uintptr_t)&words[2], POOL, sizeof(words), 0, regs), "existing direct-pool CAS succeeds");
 check(words[2] == UINT64_C(0x8877665544332211), "direct pool stores desired value");
 reset();
 check(handle(actual, ANON + 16, (uintptr_t)&words[2], POOL, sizeof(words), 0, regs), "exact device CAS succeeds through registered anonymous alias");
 check(words[2] == UINT64_C(0x8877665544332211) && regs[11] == UINT64_C(0x1122334455667788), "anonymous success stores desired and reports old value");
 check(regs[28] == 0xabcdef && regs[0] == UINT64_C(0x8877665544332211), "unrelated registers and desired operand retained");
 reset(); regs[11] = 0x1234;
 check(handle(actual, ANON + 16, (uintptr_t)&words[2], POOL, sizeof(words), 0, regs), "comparison mismatch is still handled");
 check(words[2] == UINT64_C(0x1122334455667788) && regs[11] == words[2], "mismatch leaves memory and reports observed value");
 for (unsigned order = 0; order < 4; order++) for (unsigned wide = 0; wide < 2; wide++) {
  reset();
  uint32_t insn = (actual & ~(0x40000000u | 0x00400000u | 0x8000u)) |
      (wide ? 0x40000000u : 0) | ((order & 1) ? 0x00400000u : 0) | ((order & 2) ? 0x8000u : 0);
  check(handle(insn, ANON + 16, (uintptr_t)&words[2], POOL, sizeof(words), 0, regs), "all 32/64-bit CAS acquire/release variants handled");
  check(words[2] == (wide ? UINT64_C(0x8877665544332211) : UINT64_C(0x1122334444332211)), "CAS writes exactly its operand width");
  check(regs[11] == (wide ? UINT64_C(0x1122334455667788) : UINT64_C(0x55667788)), "32-bit observed value is zero-extended");
 }
 reset(); ios_jit_anon_aliases[0].user_va_end = ANON + 23;
 reject(actual, ANON + 16, (uintptr_t)&words[2], POOL, sizeof(words), "cross-end anonymous CAS rejected");
 reset();
 reject(actual, POOL + 16, (uintptr_t)&words[2], POOL, 23, "cross-end pool CAS rejected");
 reset(); ios_jit_anon_alias_count = 0;
 reject(actual, ANON + 16, (uintptr_t)&words[2], POOL, sizeof(words), "unregistered alias rejected");
 reset(); ios_jit_anon_aliases[0].jit_rx_alias = 0;
 reject(actual, ANON + 16, (uintptr_t)&words[2], POOL, sizeof(words), "incomplete alias rejected");
 reset();
 reject(actual, ANON + 16, (uintptr_t)&words[3], POOL, sizeof(words), "mismatched writable alias rejected");
 reset();
 reject(actual, ANON + 17, (uintptr_t)&words[2] + 1, POOL, sizeof(words), "misaligned CAS rejected");
 reset(); ios_jit_anon_aliases[0].user_va = UINTPTR_MAX - 15;
 ios_jit_anon_aliases[0].user_va_end = UINTPTR_MAX;
 ios_jit_anon_aliases[0].jit_rw_alias = (uintptr_t)&words[1];
 reject(actual, UINTPTR_MAX - 7, (uintptr_t)&words[2], POOL, sizeof(words), "overflowing anonymous coverage rejected");
 reset();
 reject((actual & ~(31u << 16)) | (29u << 16), ANON + 16, (uintptr_t)&words[2], POOL, sizeof(words), "unsupported FP operand rejected");
 reset(); uint64_t before = words[2];
 check(handle(actual, ANON + 16, (uintptr_t)&words[2], POOL, sizeof(words), 1, regs) == 1 && words[2] == before, "already handled instruction is not emulated twice");
 printf("%u checks, %u failures\n", checks, failures);
 return failures ? 1 : 0;
}
'''


def main() -> int:
    signal = (ROOT / 'build/ntdll-unix/signal_arm64_ios.c').read_text()
    virtual = (ROOT / 'build/ntdll-unix/virtual_ios.c').read_text()
    call = signal.index('ios_mach_emulate_cas(insn, rw_addr, state.__x)')
    branch = extract(signal, signal.rindex('if (!emulated', 0, call))
    code = HARNESS.replace('/* COVER */', extract(virtual, virtual.index('int ios_jit_anon_alias_find_cover(')))
    code = code.replace('/* CAS */', extract(signal, signal.index('static int ios_mach_emulate_cas(')))
    code = code.replace('/* BRANCH */', branch)
    failed = False
    with tempfile.TemporaryDirectory(prefix='madeira-mach-cas-') as directory:
        source, binary = Path(directory) / 'cas.c', Path(directory) / 'cas'
        source.write_text(code)
        for name, flags in [('sanitizers', ['-fsanitize=address,undefined', '-fno-sanitize-recover=all']), ('optimized', ['-O2'])]:
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', *flags, str(source), '-o', str(binary)], check=True, timeout=60)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            print(f'{name}: exit {result.returncode}\n{result.stdout}{result.stderr}', end='')
            failed |= result.returncode != 0
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
