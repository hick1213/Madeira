#!/usr/bin/env python3
"""Compile the production x64 image classifier against bounded mock image views."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'build/ntdll-unix/virtual_ios.c'


def function(source: str, name: str, return_type: str = 'int') -> str:
    start = source.index('static ' + return_type + ' ' + name + '(')
    while source.index(';', start) < source.index('{', start):
        start = source.index('static ' + return_type + ' ' + name + '(', start + 1)
    brace = source.index('{', start)
    depth, end = 1, brace + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


source = SOURCE.read_text()
# Integration ordering: the local host EXEC downgrade leaves Wine's VPROT_EXEC
# bookkeeping and notification paths untouched, and view lookup holds the lock.
protect = source[source.index('NTSTATUS WINAPI NtProtectVirtualMemory('):]
assert protect.index('server_enter_uninterrupted_section( &virtual_mutex, &sigset );') < protect.index('ios_x64_iat_bind_live_data( base, size );')
assert 'unix_prot &= ~PROT_EXEC;' in source
mapping = source[source.index('static NTSTATUS map_image_into_view('):]
assert mapping.index('update_arm64ec_ranges( view, nt, dir, &image_info->entry_point );') < mapping.index('/* set the image protections */', mapping.index('update_arm64ec_ranges( view, nt, dir, &image_info->entry_point );'))
helper = function(source, 'ios_unpooled_x64_image')
preamble = r'''
#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <stdio.h>
#include <windef.h>
#include <winnt.h>
#define VPROT_ARM64EC 0x100
#define VPROT_SYSTEM 0x200
struct file_view { void *base; size_t size; unsigned int protect; };
static _Alignas(16) unsigned char bytes[16384];
static struct file_view view;
static const uintptr_t host_page_mask=0x3fff;
static int enabled=1, fail_read;
static int ios_x64_image_host_data_enabled(void) { return enabled; }
static struct file_view *find_view(const void *p,size_t n) {
 uintptr_t off=(uintptr_t)p-(uintptr_t)view.base;
 return off<=view.size && n<=view.size-off ? &view:NULL;
}
static int ios_safe_read(uintptr_t p,void *out,size_t n) {
 if(fail_read || p<(uintptr_t)bytes || p-(uintptr_t)bytes>sizeof(bytes) || n>sizeof(bytes)-(p-(uintptr_t)bytes)) return 0;
 memcpy(out,(void*)p,n); return 1;
}
static IMAGE_NT_HEADERS64 *setup(void) {
 memset(bytes,0,sizeof(bytes)); view=(struct file_view){bytes,sizeof(bytes),SEC_IMAGE}; enabled=1; fail_read=0;
 *(unsigned short*)bytes=IMAGE_DOS_SIGNATURE; *(unsigned int*)(bytes+0x3c)=0x80;
 IMAGE_NT_HEADERS64 *nt=(void*)(bytes+0x80); nt->Signature=IMAGE_NT_SIGNATURE;
 nt->FileHeader.Machine=IMAGE_FILE_MACHINE_AMD64;
 nt->FileHeader.SizeOfOptionalHeader=sizeof(nt->OptionalHeader);
 nt->OptionalHeader.Magic=IMAGE_NT_OPTIONAL_HDR64_MAGIC; nt->OptionalHeader.SizeOfImage=sizeof(bytes); nt->OptionalHeader.NumberOfRvaAndSizes=16;
 return nt;
}
'''
iat_mocks = r'''
#define MACH_PORT_NULL 0
#define KERN_SUCCESS 0
#define VM_REGION_BASIC_INFO_COUNT_64 1
#define VM_REGION_BASIC_INFO_64 1
#define VM_PROT_READ 1
#define VM_PROT_WRITE 2
typedef uintptr_t mach_vm_address_t;
typedef size_t mach_vm_size_t;
typedef unsigned int mach_msg_type_number_t;
typedef int mach_port_t;
typedef void *vm_region_info_t;
typedef struct { int protection; } vm_region_basic_info_data_64_t;
struct { void *pe_base; size_t size; } ios_jit_mappings[3];
static int ios_jit_mapping_count, read_only, region_failed, target_plain;
static unsigned char target[512], target_pool[512];
static int mach_task_self(void) { return 1; }
static int mach_vm_region(int task,mach_vm_address_t *addr,mach_vm_size_t *size,int flavor,vm_region_info_t out,mach_msg_type_number_t *count,mach_port_t *obj) {
 (void)task;(void)addr;(void)size;(void)flavor;(void)count;(void)obj;
 if(region_failed) return 1;
 ((vm_region_basic_info_data_64_t*)out)->protection=VM_PROT_READ|(read_only?0:VM_PROT_WRITE);return 0;
}
static int ios_jit_mapping_machine(int i) { return i==0?0xaa64:0x8664; }
static int ios_jit_mapping_is_hybrid(int i) { return i==0 && !target_plain; }
static void *ios_jit_current_peb(void) { return NULL; }
static int ios_img_off_is_exec(const void *img,size_t size,size_t off) { (void)img;(void)size;return off<128; }
static void *ios_jit_translate_addr_for_owner(void *p,void *owner) {
 (void)owner;uintptr_t off=(uintptr_t)p-(uintptr_t)target;
 return off<sizeof(target)?target_pool+off:p;
}
'''
iat = function(source, 'ios_x64_iat_bind_live_data', 'void')
cases = r'''
int main(void) {
 void *img=NULL; size_t span=0; IMAGE_NT_HEADERS64 *nt=setup();
 assert(ios_unpooled_x64_image(bytes+0x200,0x100,&img,&span)); assert(img==bytes && span==sizeof(bytes));
 assert(ios_unpooled_x64_image(bytes,sizeof(bytes),NULL,NULL));
 assert(!ios_unpooled_x64_image(bytes+sizeof(bytes)-16,0x20,NULL,NULL));
 enabled=0; assert(!ios_unpooled_x64_image(bytes,1,NULL,NULL)); enabled=1;
 view.protect=0; assert(!ios_unpooled_x64_image(bytes,1,NULL,NULL));
 view.protect=SEC_IMAGE|VPROT_ARM64EC; assert(!ios_unpooled_x64_image(bytes,1,NULL,NULL));
 view.protect=SEC_IMAGE|VPROT_SYSTEM; assert(!ios_unpooled_x64_image(bytes,1,NULL,NULL));
 nt=setup(); nt->FileHeader.Machine=0xaa64; assert(!ios_unpooled_x64_image(bytes,1,NULL,NULL));
 nt=setup(); fail_read=1; assert(!ios_unpooled_x64_image(bytes,1,NULL,NULL));
 nt=setup(); *(unsigned int*)(bytes+0x3c)=0xfffffff0; assert(!ios_unpooled_x64_image(bytes,1,NULL,NULL));
 nt=setup(); *(unsigned short*)bytes=0; assert(!ios_unpooled_x64_image(bytes,1,NULL,NULL));
 nt=setup(); nt->OptionalHeader.Magic=0x10b; assert(!ios_unpooled_x64_image(bytes,1,NULL,NULL));
 nt=setup(); nt->OptionalHeader.NumberOfRvaAndSizes=17; assert(!ios_unpooled_x64_image(bytes,1,NULL,NULL));
 nt=setup(); view.size=63; assert(!ios_unpooled_x64_image(bytes,1,NULL,NULL));
 nt=setup(); nt->Signature=0; assert(!ios_unpooled_x64_image(bytes,1,NULL,NULL));
 nt=setup(); nt->FileHeader.SizeOfOptionalHeader=1; assert(!ios_unpooled_x64_image(bytes,1,NULL,NULL));
 nt=setup(); nt->OptionalHeader.SizeOfImage=32768; assert(!ios_unpooled_x64_image(bytes,1,NULL,NULL));
 nt=setup(); nt->OptionalHeader.SizeOfImage=0x300; assert(!ios_unpooled_x64_image(bytes+0x300,0x100,NULL,NULL));
 nt=setup(); nt->OptionalHeader.SizeOfImage=0x3000; view.size=0x4000;
 assert(ios_unpooled_x64_image(bytes+0x2000,0x2000,NULL,NULL));
 view.size=0x3000; assert(ios_unpooled_x64_image(bytes+0x2000,0x2000,NULL,NULL));
 assert(!ios_unpooled_x64_image(bytes+0x2000,0x2001,NULL,NULL));
 nt=setup(); nt->OptionalHeader.DataDirectory[10]=(IMAGE_DATA_DIRECTORY){0,16}; assert(!ios_unpooled_x64_image(bytes,1,NULL,NULL));
 nt=setup(); nt->OptionalHeader.DataDirectory[10]=(IMAGE_DATA_DIRECTORY){0x400,1}; assert(!ios_unpooled_x64_image(bytes,1,NULL,NULL));
 nt=setup(); nt->OptionalHeader.DataDirectory[10]=(IMAGE_DATA_DIRECTORY){sizeof(bytes)-16,0x100}; assert(!ios_unpooled_x64_image(bytes,1,NULL,NULL));
 nt=setup(); nt->OptionalHeader.DataDirectory[10]=(IMAGE_DATA_DIRECTORY){0x400,sizeof(IMAGE_LOAD_CONFIG_DIRECTORY64)};
 IMAGE_LOAD_CONFIG_DIRECTORY64 *cfg=(void*)(bytes+0x400); cfg->Size=sizeof(*cfg); cfg->CHPEMetadataPointer=0x140001000;
 assert(!ios_unpooled_x64_image(bytes,1,NULL,NULL));
 cfg->CHPEMetadataPointer=0; assert(ios_unpooled_x64_image(bytes,1,NULL,NULL));
 cfg->Size=0; assert(!ios_unpooled_x64_image(bytes,1,NULL,NULL));
 cfg->Size=offsetof(IMAGE_LOAD_CONFIG_DIRECTORY64,CHPEMetadataPointer)+1; assert(!ios_unpooled_x64_image(bytes,1,NULL,NULL));
 puts("PASS: production classifier native/hybrid/nonimage/bounds/read failures/full span");
 nt=setup(); nt->OptionalHeader.DataDirectory[12]=(IMAGE_DATA_DIRECTORY){0x800,24};
 ios_jit_mappings[0].pe_base=target; ios_jit_mappings[0].size=sizeof(target); ios_jit_mapping_count=1;
 uint64_t *slots=(void*)(bytes+0x800); slots[0]=(uintptr_t)(target+200); slots[1]=(uintptr_t)(target+32); slots[2]=123;
 ios_x64_iat_bind_live_data((char*)bytes+0x800,24);
 assert(slots[0]==(uintptr_t)(target_pool+200)); assert(slots[1]==(uintptr_t)(target+32)); assert(slots[2]==123);
 slots[0]=(uintptr_t)(target+200); read_only=1;
 ios_x64_iat_bind_live_data((char*)bytes+0x800,24); assert(slots[0]==(uintptr_t)(target+200)); read_only=0;
 target_plain=1; ios_x64_iat_bind_live_data((char*)bytes+0x800,24); assert(slots[0]==(uintptr_t)(target+200)); target_plain=0;
 region_failed=1; ios_x64_iat_bind_live_data((char*)bytes+0x800,24); assert(slots[0]==(uintptr_t)(target+200)); region_failed=0;
 fail_read=1; ios_x64_iat_bind_live_data((char*)bytes+0x800,24); assert(slots[0]==(uintptr_t)(target+200)); fail_read=0;
 view.protect|=VPROT_ARM64EC; ios_x64_iat_bind_live_data((char*)bytes+0x800,24); assert(slots[0]==(uintptr_t)(target+200)); view.protect=SEC_IMAGE;
 nt->OptionalHeader.DataDirectory[12].Size=0xffff; ios_x64_iat_bind_live_data((char*)bytes+0x800,24); assert(slots[0]==(uintptr_t)(target+200));
 nt->OptionalHeader.DataDirectory[12].Size=24; enabled=0; ios_x64_iat_bind_live_data((char*)bytes+0x800,24); assert(slots[0]==(uintptr_t)(target+200)); enabled=1;
 ios_jit_mappings[1].pe_base=bytes; ios_jit_mappings[1].size=sizeof(bytes); ios_jit_mapping_count=2;
 ios_x64_iat_bind_live_data((char*)bytes+0x800,24); assert(slots[0]==(uintptr_t)(target_pool+200));
 puts("PASS: production IAT binding unpooled/pooled importer, EC data/code, malformed bounds, readonly, rollback");
}
'''
with tempfile.TemporaryDirectory(prefix='madeira-x64-host-data-') as td:
    c = Path(td) / 'check.c'
    binary = Path(td) / 'check'
    c.write_text(preamble + '\n' + helper + '\n' + iat_mocks + '\n' + iat + '\n' + cases)
    subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-Wno-pragma-pack', '-fsanitize=address,undefined', '-fno-sanitize-recover=all', '-I', str(ROOT / 'wine/include'), str(c), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
