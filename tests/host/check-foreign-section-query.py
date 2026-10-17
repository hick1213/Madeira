#!/usr/bin/env python3
"""Compile production section query helpers with an explicit server namespace mock."""
from pathlib import Path
import subprocess
import tempfile

if not __debug__:
    raise RuntimeError('Run this regression check without Python optimization; integration assertions are required.')

ROOT = Path(__file__).resolve().parents[2]
source = (ROOT / 'build/ntdll-unix/virtual_ios.c').read_text()


def function(signature: str) -> str:
    start = source.index(signature)
    brace = source.index('{', start)
    depth, end = 1, brace + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


query = function('static SIZE_T get_committed_size_status(')
result = function('static unsigned int ios_basic_section_query_result(')
legacy = function('static SIZE_T get_committed_size( struct file_view')
fill = function('static unsigned int fill_basic_memory_info(')
basic = function('static unsigned int get_basic_memory_info(')
assert 'get_committed_size_status( view, base' in fill
assert 'status = ios_basic_section_query_result( view, base, status, info );' in fill
assert 'return status;' in fill
remote = basic[basic.index('if (process != NtCurrentProcess())'):basic.index('status = fill_basic_memory_info') ]
assert 'server_queue_process_apc' in remote and 'ios_basic_section_query_result' not in remote
code = r'''
#include <stdint.h>
#include <stddef.h>
#include <assert.h>
#include <stdio.h>
#include <windef.h>
#include <winnt.h>
typedef LONG NTSTATUS;
#include <ntstatus.h>
#define WINE_IOS 1
#define VPROT_COMMITTED 0x20
#define page_mask ((uintptr_t)0xfff)
#define ROUND_ADDR(p,mask) ((void*)((uintptr_t)(p)&~(mask)))
struct file_view {void *base;size_t size;unsigned int protect;};
struct request {uintptr_t base;size_t offset;} request;
struct reply {size_t size;int committed;} reply_mock;
#define SERVER_START_REQ(name) { struct request *req=&request;struct reply *reply=&reply_mock;
#define SERVER_END_REQ }
static unsigned int server_status;
static unsigned int server_calls;
static BYTE page_protect;
static size_t uniform_size, committed_mark_size;
static int mark_calls;
#define wine_server_client_ptr(p) ((uintptr_t)(p))
static unsigned int wine_server_call(struct request *req){(void)req;server_calls++;return server_status;}
#define NtCurrentProcess() ((HANDLE)(intptr_t)-1)
static BYTE get_page_vprot(void *p){(void)p;return page_protect;}
static void set_page_vprot_bits(void *p,size_t size,BYTE set,BYTE clear){(void)p;assert(set==VPROT_COMMITTED&&!clear);mark_calls++;committed_mark_size=size;}
static size_t get_vprot_range_size(void *p,size_t size,BYTE mask,BYTE *vp){(void)p;(void)mask;(void)vp;return uniform_size&&uniform_size<size?uniform_size:size;}
static void ios_vm_query_trace(unsigned stage,HANDLE process,const void *addr,const struct file_view *view,unsigned status,SIZE_T size,int committed,const MEMORY_BASIC_INFORMATION *info){(void)stage;(void)process;(void)addr;(void)view;(void)status;(void)size;(void)committed;(void)info;}
''' + query + chr(10) + legacy + chr(10) + result + r'''
static struct file_view view;
static MEMORY_BASIC_INFORMATION info;
static unsigned int probe(uintptr_t address){
 BYTE vp=0;unsigned status;
 info=(MEMORY_BASIC_INFORMATION){0}; info.BaseAddress=(void*)address;
 info.RegionSize=get_committed_size_status(&view,(void*)address,~(size_t)0,&vp,0xff,&status);
 info.State=(vp&VPROT_COMMITTED)?MEM_COMMIT:MEM_RESERVE;
 return ios_basic_section_query_result(&view,(void*)address,status,&info);
}
static void reset(void){
 view=(struct file_view){(void*)0x100000,0x20000,SEC_RESERVE};
 reply_mock=(struct reply){0x3000,1};server_status=STATUS_SUCCESS;server_calls=0;
 page_protect=0;uniform_size=0;mark_calls=0;committed_mark_size=0;
}
int main(void){
 reset();assert(probe(0x100000)==(unsigned int)STATUS_SUCCESS&&info.RegionSize==0x3000&&info.State==MEM_COMMIT&&mark_calls==1);
 reset();reply_mock.committed=0;assert(probe(0x100000)==(unsigned int)STATUS_SUCCESS&&info.RegionSize==0x3000&&info.State==MEM_RESERVE&&!mark_calls);
 reset();uniform_size=0x1000;assert(probe(0x100000)==(unsigned int)STATUS_SUCCESS&&info.RegionSize==0x1000);
 reset();server_status=STATUS_NOT_MAPPED_VIEW;page_protect=VPROT_COMMITTED;
 assert(probe(0x100000)==(unsigned int)STATUS_SUCCESS&&info.RegionSize==0x20000&&info.State==MEM_RESERVE&&info.Protect==PAGE_NOACCESS&&info.AllocationProtect==PAGE_NOACCESS&&info.Type==MEM_MAPPED&&!mark_calls);
 assert(info.AllocationBase==view.base);
 assert(probe(0x107000)==(unsigned int)STATUS_SUCCESS&&info.RegionSize==0x19000&&info.State==MEM_RESERVE);
 assert(request.base==(uintptr_t)view.base&&request.offset==0x7000);
 uintptr_t next=0x107000+info.RegionSize;assert(next==0x120000);
 unsigned before=server_calls;
 assert(probe(next)==(unsigned int)STATUS_INVALID_PARAMETER);assert(probe(0xff000)==(unsigned int)STATUS_INVALID_PARAMETER);
 assert(server_calls==before);
 reset();server_status=STATUS_ACCESS_DENIED;assert(probe(0x100000)==(unsigned int)STATUS_ACCESS_DENIED&&!info.RegionSize&&!mark_calls);
 reset();reply_mock.size=0;assert(probe(0x100000)==(unsigned int)STATUS_INVALID_PARAMETER&&!mark_calls);
 reset();reply_mock.size=0;BYTE legacy_vp=0;
 assert(!get_committed_size(&view,view.base,~(size_t)0,&legacy_vp,0xff));
 assert((legacy_vp&VPROT_COMMITTED)&&mark_calls==1&&!committed_mark_size);
 reset();reply_mock.size=0x40000;assert(probe(0x100000)==(unsigned int)STATUS_INVALID_PARAMETER&&!mark_calls);
 reset();server_status=STATUS_NOT_MAPPED_VIEW;view.size=0;assert(probe(0x100000)==(unsigned int)STATUS_INVALID_PARAMETER);
 reset();server_status=STATUS_NOT_MAPPED_VIEW;view.base=(void*)(UINTPTR_MAX-0xfff);view.size=0x2000;assert(probe((uintptr_t)view.base)==(unsigned int)STATUS_INVALID_PARAMETER);
 reset();server_status=STATUS_NOT_MAPPED_VIEW;view.protect=SEC_COMMIT;assert(ios_basic_section_query_result(&view,view.base,server_status,&info)==(unsigned int)STATUS_NOT_MAPPED_VIEW);
 reset();server_status=STATUS_NOT_MAPPED_VIEW;BYTE vp=0;assert(!get_committed_size(&view,view.base,~(size_t)0,&vp,0xff));
 puts("PASS: own committed/uncommitted boundaries, foreign reserved span/progression, other errors, zero/overflow/bounds");
}
'''
with tempfile.TemporaryDirectory(prefix='madeira-foreign-query-') as td:
    c = Path(td) / 'check.c'
    binary = Path(td) / 'check'
    c.write_text(code)
    subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-Wno-pragma-pack', '-fsanitize=address,undefined', '-fno-sanitize-recover=all', '-I', str(ROOT / 'wine/include'), str(c), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
