#!/usr/bin/env python3
"""Production native pending control and Wine ownership checks, without UIKit."""
from pathlib import Path
import subprocess
import tempfile

if not __debug__:
    raise RuntimeError('Run without optimization.')
ROOT = Path(__file__).resolve().parents[2]
native = (ROOT / 'app/Madeira/Winios/Winios.m').read_text()
driver = (ROOT / 'build/win32u-unix/driver_ios.c').read_text()
control = native.split('/* DESKTOP_CONTROL_BEGIN */')[1].split('/* DESKTOP_CONTROL_END */')[0]
checks = driver.split('/* DESKTOP_OWNER_BEGIN */')[1].split('/* DESKTOP_OWNER_END */')[0]
code = r'''
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <pthread.h>
#include <stdatomic.h>
#include <assert.h>
#include <string.h>
typedef void *HWND;
typedef unsigned int DWORD;
#define WM_CLOSE 0x10
static HWND desktop=(HWND)0x10;
static DWORD owner_pid=4,owner_tid=8,current_pid=4,current_tid=8;
static int posts,post_allowed=1;
static HWND get_desktop_window(void){return desktop;}
static DWORD get_window_thread(HWND h,DWORD *pid){if(h!=desktop||!h)return 0;if(pid)*pid=owner_pid;return owner_tid;}
#define GetCurrentProcessId() (current_pid)
#define GetCurrentThreadId() (current_tid)
static int NtUserPostMessage(HWND h,unsigned message,uintptr_t wp,intptr_t lp){assert(h==desktop&&message==WM_CLOSE&&!wp&&!lp);if(!post_allowed)return 0;posts++;return 1;}
uint64_t winios_control_session_token(void);
#define dprintf(...) ((void)0)
''' + checks + control + r'''
int main(void){
 setenv("MADEIRA_DESKTOP","1",1);
 assert(!winios_request_desktop_close(0));
 winios_control_reset();uint64_t one=winios_control_session_token();assert(one);
 assert(winios_request_desktop_close(one));
 current_pid=12;assert(!winios_control_process_events()&&!posts);current_pid=4;
 assert(winios_drv_desktop_identity(one,NULL,NULL)==0);
 uintptr_t hwnd=0;unsigned int pid=0;assert(winios_drv_desktop_identity(one,&hwnd,&pid)&&hwnd==(uintptr_t)desktop&&pid==4);
 assert(winios_drv_desktop_needs_poll());
 post_allowed=0;assert(!winios_control_process_events()&&!posts);
 post_allowed=1;assert(winios_control_process_events()&&posts==1);
 assert(winios_request_desktop_close(one));assert(!winios_control_process_events()&&posts==1);
 winios_control_reset();uint64_t two=winios_control_session_token();assert(two>one);
 assert(!winios_request_desktop_close(one));assert(!winios_drv_close_owned_desktop(one,hwnd,pid));
 assert(winios_request_desktop_close(two));
 current_tid=9;assert(!winios_control_process_events()&&posts==1);current_tid=8;
 desktop=(HWND)0x20;assert(winios_control_process_events()&&posts==2);
 assert(!winios_drv_close_owned_desktop(two,0x10,4));
 winios_control_reset();uint64_t three=winios_control_session_token();
 assert(winios_request_desktop_close(three));winios_control_reset();assert(!winios_control_process_events()&&posts==2);
 current_pid=12;assert(!winios_drv_desktop_needs_poll());current_pid=4;
 setenv("MADEIRA_DESKTOP","0",1);assert(!winios_drv_desktop_identity(winios_control_session_token(),&hwnd,&pid));
 puts("PASS: pending control is not dropped, close once, generation reset, exact owned HWND/PID/thread and idle polling eligibility");
}
'''
with tempfile.TemporaryDirectory(prefix='madeira-desktop-control-') as td:
    c = Path(td) / 'check.c'
    binary = Path(td) / 'check'
    c.write_text(code)
    subprocess.run(['cc', '-std=c11', '-D_DARWIN_C_SOURCE', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined', '-fno-sanitize-recover=all', str(c), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
