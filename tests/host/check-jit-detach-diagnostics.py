#!/usr/bin/env python3
"""Exercise the actual detach diagnostic gate, quota, Mach reply, and BRK stub.

The fixture supplies only diagnostic/state-write spies and read-failure injection.
Mach message delivery and the final SIGTRAP instruction return run in isolated
child processes. It does not rebuild an application library or touch a device.
"""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def function(source, signature):
    start = source.index(signature)
    return source[start:source.index('\n}', start) + 2] + '\n'


def slices(signal, allocator):
    reader = function(signal, 'static int ios_fault_read_insn(')
    classifier = ''
    if 'static int ios_is_native_detach_breakpoint(' in signal:
        classifier = function(signal, 'static int ios_is_native_detach_breakpoint(')

    # Retain the actual admission branch and its existing quota statements;
    # replace the large diagnostic body only with observable test counters.
    counter = signal.index('static volatile int unhandled_count = 0;')
    gate = signal.rindex('            if (handled)\n', 0, counter)
    counter_end = signal.index('\n', signal.index('int cnt = ', counter))
    admission = signal[gate:counter_end]
    dump = signal.index('static volatile int dumped = 0;', counter_end)
    dump_end = signal.index(';', signal.index('ios_dump_jit_pool( "mach UNHANDLED" )', dump)) + 1
    quota = signal[dump:dump_end]
    reply = signal.index('        ios_exc_reply_t reply;', dump_end)
    reply_end = signal.index(';', signal.index('reply.ret_code = ', reply)) + 1
    dispatch = (admission + '\n                diagnostic_calls++; last_count = cnt;\n' +
                quota + '\n            }\n' + signal[reply:reply_end] + '\nreturn reply;\n')

    structs_start = signal.index('#pragma pack(4)\ntypedef struct {')
    structs_end = signal.index('#pragma pack()', structs_start) + len('#pragma pack()')
    detach = function(allocator, 'void jit26_detach(void)')
    fallback = function(allocator, 'static void sigtrap_handler(')
    trap = function(signal, 'static void trap_handler(')
    wine_entry = trap[trap.index('    /* StikDebug protocol BRK'):trap.index('    /* ml481')]
    return {
        'detach-types.inc': signal[structs_start:structs_end],
        'detach-read.inc': reader + classifier,
        'detach-dispatch.inc': dispatch,
        'detach-stub.inc': '__attribute__((noinline, optnone))\n' + detach + fallback,
        'detach-wine-trap.inc': wine_entry,
    }


def main():
    signal = (ROOT / 'build/ntdll-unix/signal_arm64_ios.c').read_text()
    allocator = (ROOT / 'app/Madeira/JITAllocator.c').read_text()
    with tempfile.TemporaryDirectory(prefix='madeira-detach-diagnostics-') as temp:
        folder = Path(temp)
        for name, content in slices(signal, allocator).items():
            (folder / name).write_text(content)
        binary = folder / 'detach-test'
        for flags in [['-O0', '-g'], ['-O2']]:
            subprocess.run(['clang', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                            '-Wno-unused-parameter', '-I', str(folder), *flags,
                            str(ROOT / 'tests/host/jit-detach-diagnostics.c'),
                            '-o', str(binary)], check=True, timeout=60)
            subprocess.run([str(binary)], check=True, timeout=40)


if __name__ == '__main__':
    main()
