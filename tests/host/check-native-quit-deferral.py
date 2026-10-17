#!/usr/bin/env python3
"""Run the real ARM64 dispatchers and SIGQUIT handler on an isolated host stack.

The held-mutex case detects asynchronous native teardown. The epilogue case
inserts a lock-free rendezvous after the actual pending check so SIGQUIT lands
in the check-to-stack-switch interval. Each case also runs with a zero or foreign
saved guest X18 while the native pthread TEB remains valid. No application
libraries are rebuilt.
"""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def function(source, signature):
    start = source.index(signature)
    return source[start:source.index('\n}', start) + 2] + '\n'


def assembly(source, name):
    start = source.index('__ASM_GLOBAL_FUNC( ' + name + ',')
    end = source.index('\n\n', start)
    # Blank lines inside the syscall dispatcher separate local assembly labels.
    while not source[start:end].rstrip().endswith(')'):
        end = source.index('\n\n', end + 2)
    return source[start:end] + '\n'


def main():
    source = (ROOT / 'build/ntdll-unix/signal_arm64_ios.c').read_text()
    pieces = []
    state_start = source.find('struct ios_quit_thread_data\n')
    if state_start >= 0:
        state_end = source.index('\n#endif', state_start)
        pieces.append(source[state_start:state_end])
    pieces += [function(source, 'static void quit_handler('),
               function(source, 'static void usr2_handler('),
               function(source, 'void syscall_dispatcher_return_slowpath('),
               function(source, 'NTSTATUS signal_alloc_thread(')]
    for name in ['__wine_syscall_dispatcher', '__wine_syscall_dispatcher_return',
                 '__wine_unix_call_dispatcher']:
        asm = assembly(source, name)
        # This rendezvous executes only caller-saved integer instructions. It
        # pauses after the pending read without calling C or changing the SP.
        for label, kind in [('ios_syscall_quit_checked', 1), ('ios_unixcall_quit_checked', 2)]:
            marker = '__ASM_LOCAL_LABEL("' + label + '") ":\\n\\t"'
            if marker in asm:
                hook = ('\n                   "adrp x10, _epilogue_kind@PAGE\\n\\t"'
                        '\n                   "ldr w11, [x10, _epilogue_kind@PAGEOFF]\\n\\t"'
                        f'\n                   "cmp w11, #{kind}\\n\\t"'
                        '\n                   "b.ne 98f\\n\\t"'
                        '\n                   "adrp x10, _epilogue_waiting@PAGE\\n\\t"'
                        '\n                   "mov w11, #1\\n\\t"'
                        '\n                   "str w11, [x10, _epilogue_waiting@PAGEOFF]\\n"'
                        '\n                   "97: b 97b\\n"'
                        '\n                   "98:\\n\\t"')
                asm = asm.replace(marker, marker + hook)
        pieces.append(asm)
    with tempfile.TemporaryDirectory(prefix='madeira-quit-deferral-') as temp:
        folder = Path(temp)
        (folder / 'native-quit-source.inc').write_text('\n'.join(pieces))
        binary = folder / 'quit-test'
        for flags in [['-O0', '-g'], ['-O2']]:
            subprocess.run(['clang', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                            '-Wno-unused-parameter', '-Wno-unused-function',
                            '-I', str(folder), *flags,
                            str(ROOT / 'tests/host/native-quit-deferral.c'),
                            '-o', str(binary)], check=True, timeout=60)
            for saved_x18_mode in range(3):
                subprocess.run([str(binary), str(saved_x18_mode)], check=True, timeout=25)


if __name__ == '__main__':
    main()
