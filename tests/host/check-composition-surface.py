#!/usr/bin/env python3
"""Exercise production composition validation/binding against specific host boundaries."""
from pathlib import Path
import subprocess
import tempfile

if not __debug__:
    raise RuntimeError('Run without Python optimization; integration checks are required.')
ROOT = Path(__file__).resolve().parents[2]
source = (ROOT / 'madeira-d3d12/src/pe/madeira_d3d12.c').read_text()


def function(signature: str) -> str:
    start = source.index(signature)
    brace = source.index('{', start)
    end, depth = brace + 1, 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


validate = function('static HRESULT mad_composition_validate_desc(')
bind = function('static HRESULT mad_composition_bind_swapchain(')
claim = function('static BOOL mad_composition_claim_layer(')
unclaim = function('static void mad_composition_unclaim_layer(')
unbound = function('static HRESULT mad_composition_present_unbound(')
get_hwnd = function('static HRESULT STDMETHODCALLTYPE swap_GetHwnd(')
get_core = function('static HRESULT STDMETHODCALLTYPE swap_GetCoreWindow(')
set_fullscreen = function('static HRESULT STDMETHODCALLTYPE swap_SetFullscreenState(')
resize_target = function('static HRESULT STDMETHODCALLTYPE swap_ResizeTarget(')
get_output = function('static HRESULT STDMETHODCALLTYPE swap_GetContainingOutput(')
resize = function('static HRESULT STDMETHODCALLTYPE swap_ResizeBuffers(IDXGISwapChain4 *T')
start = source.index('struct mad_swapchain {')
struct = source[start:source.index('\n};', start) + 3]
code = r'''
#include <assert.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <stdio.h>
#define COBJMACROS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <dxgi1_5.h>
#include "madeira_composition.h"
#define MAD_SWAP_MAX_BUFFERS 8
typedef uint64_t obj_handle_t;
enum WMTPixelFormat { TEST_PIXEL_FORMAT };
/* Only the real object's common ABI/refcount prefix is used by resize.
 * No mock Metal/resource implementation is called by this test. */
struct mad_resource {void *vtbl;LONG refs;const IID *iid;const char *name;};
struct mad_device {obj_handle_t mtl_device;};
struct mad_queue {HANDLE sub_thread;struct mad_device *device;};
''' + struct + r'''
static int registry_lock;
static SRWLOCK g_composition_bind_lock;
static struct mad_swapchain *g_composition_bound;
#define AcquireSRWLockExclusive(p) do{(void)(p);assert(!registry_lock);registry_lock=1;}while(0)
#define ReleaseSRWLockExclusive(p) do{(void)(p);assert(registry_lock);registry_lock=0;}while(0)
#define MAD_SWAP_ROUTE_TRACE(...) ((void)0)
static int flushed;
static int locks, drained, release_count, apply_count, create_fail, alias_layer;
static DWORD test_current_pid=4, window_pid=4;
static HWND created_hwnd;
static obj_handle_t released_view;
#define EnterCriticalSection(p) do{(void)(p);assert(!locks);locks++;}while(0)
#define LeaveCriticalSection(p) do{(void)(p);assert(locks==1);locks--;}while(0)
#define IsWindow(h) ((h)==(HWND)0x11 || (h)==(HWND)0x22)
#define GetCurrentProcessId() (test_current_pid)
static DWORD test_window_pid(HWND h,DWORD *out){(void)h;*out=window_pid;return 8;}
#define GetWindowThreadProcessId test_window_pid
static void mad_queue_drain(struct mad_queue *q){(void)q;assert(locks==1);drained++;}
static obj_handle_t CreateMetalViewFromHWND(intptr_t h,obj_handle_t device,obj_handle_t *layer){
 assert(locks==1&&device==7);created_hwnd=(HWND)h;
 if(create_fail){*layer=0;return 0;}*layer=alias_layer?100:((obj_handle_t)h+100);return (obj_handle_t)h+200;
}
static void ReleaseMetalView(obj_handle_t view){assert(locks==1);release_count++;released_view=view;}
static const void *g_layer_cfg_owner;
static void mad_swap_apply_layer(struct mad_swapchain *s){assert(locks==1&&s->layer);apply_count++;}
static void mad_device_flush_all(struct mad_device *d){assert(locks==1&&d->mtl_device==7);flushed++;}
''' + validate + claim + unclaim + bind + unbound + get_hwnd + get_core + set_fullscreen + resize_target + get_output + r'''
static int resized;
static HRESULT swap_ResizeBuffers_inner(IDXGISwapChain4 *T,UINT count,UINT w,UINT h,DXGI_FORMAT fmt,UINT flags){
 struct mad_swapchain *s=(struct mad_swapchain*)T;assert(!s->composition||locks==1);(void)fmt;(void)flags;resized++;s->desc.Width=w;s->desc.Height=h;s->desc.BufferCount=count;return S_OK;
}
''' + resize + r'''
int main(void){
 DXGI_SWAP_CHAIN_DESC1 desc={0};desc.Width=640;desc.Height=480;desc.BufferCount=2;
 desc.Format=DXGI_FORMAT_B8G8R8A8_UNORM;desc.SampleDesc.Count=1;
 desc.SwapEffect=DXGI_SWAP_EFFECT_FLIP_SEQUENTIAL;desc.Scaling=DXGI_SCALING_STRETCH;desc.AlphaMode=DXGI_ALPHA_MODE_IGNORE;
 assert(mad_composition_validate_desc(&desc)==S_OK);
 desc.SwapEffect=DXGI_SWAP_EFFECT_FLIP_DISCARD;assert(mad_composition_validate_desc(&desc)==S_OK);
 desc.AlphaMode=DXGI_ALPHA_MODE_PREMULTIPLIED;assert(mad_composition_validate_desc(&desc)==S_OK);
 desc.AlphaMode=DXGI_ALPHA_MODE_STRAIGHT;assert(FAILED(mad_composition_validate_desc(&desc)));desc.AlphaMode=DXGI_ALPHA_MODE_IGNORE;
 desc.Width=0;assert(FAILED(mad_composition_validate_desc(&desc)));desc.Width=640;
 desc.SampleDesc.Count=4;assert(FAILED(mad_composition_validate_desc(&desc)));desc.SampleDesc.Count=1;
 desc.BufferCount=1;assert(FAILED(mad_composition_validate_desc(&desc)));desc.BufferCount=2;
 desc.SwapEffect=DXGI_SWAP_EFFECT_DISCARD;assert(FAILED(mad_composition_validate_desc(&desc)));desc.SwapEffect=DXGI_SWAP_EFFECT_FLIP_SEQUENTIAL;
 desc.Scaling=DXGI_SCALING_NONE;assert(FAILED(mad_composition_validate_desc(&desc)));desc.Scaling=DXGI_SCALING_STRETCH;
 struct mad_device dev={7};struct mad_queue queue={(HANDLE)1,&dev};struct mad_swapchain s={0};s.dev=&dev;s.queue=&queue;s.composition=TRUE;s.desc=desc;
 assert(mad_composition_bind_swapchain(&s,(HWND)0x33)==E_INVALIDARG&&!s.layer);
 window_pid=8;assert(mad_composition_bind_swapchain(&s,(HWND)0x11)==E_ACCESSDENIED&&!s.layer);window_pid=4;
 create_fail=1;assert(FAILED(mad_composition_bind_swapchain(&s,(HWND)0x11))&&!s.layer&&!s.hwnd);create_fail=0;
 assert(mad_composition_bind_swapchain(&s,(HWND)0x11)==S_OK&&s.hwnd==(HWND)0x11&&s.layer==117&&s.view==217&&created_hwnd==(HWND)0x11);
 int d=drained,a=apply_count;assert(mad_composition_bind_swapchain(&s,(HWND)0x11)==S_OK&&drained==d&&apply_count==a);
 assert(mad_composition_bind_swapchain(&s,(HWND)0x22)==E_INVALIDARG&&s.hwnd==(HWND)0x11);
 assert(mad_composition_bind_swapchain(&s,NULL)==S_OK&&!s.layer&&!s.view&&!s.hwnd&&released_view==217&&release_count==1&&drained>d);
 assert(mad_composition_bind_swapchain(&s,NULL)==S_OK&&release_count==1);
 assert(mad_composition_bind_swapchain(&s,(HWND)0x22)==S_OK&&s.layer==134);
 assert(mad_composition_bind_swapchain(&s,NULL)==S_OK&&!locks);
 alias_layer=1;assert(mad_composition_bind_swapchain(&s,(HWND)0x11)==S_OK);
 struct mad_swapchain other={0};other.dev=&dev;other.queue=&queue;other.composition=TRUE;other.desc=desc;
 assert(FAILED(mad_composition_bind_swapchain(&other,(HWND)0x22))&&!other.layer&&!other.view&&!other.hwnd);
 assert(s.hwnd==(HWND)0x11&&s.layer==100);
 assert(mad_composition_bind_swapchain(&s,NULL)==S_OK&&!g_composition_bound&&!locks);
 s.nbuf=2;s.index=0;s.presents=0;int old_apply=apply_count,old_release=release_count,old_drain=drained;
 EnterCriticalSection(&s.composition_lock);
 assert(mad_composition_present_unbound(&s)==S_OK&&s.index==1&&s.presents==1&&flushed==1&&drained==old_drain+1);
 assert(mad_composition_present_unbound(&s)==S_OK&&s.index==0&&s.presents==2&&flushed==2&&!s.layer);
 LeaveCriticalSection(&s.composition_lock);
 assert(apply_count==old_apply&&release_count==old_release);
 HWND h=(HWND)0x99;void *core=(void*)1;IDXGIOutput *output=(IDXGIOutput*)1;s.hwnd=(HWND)0x11;
 assert(FAILED(swap_GetHwnd((IDXGISwapChain4*)&s,&h))&&!h);
 assert(FAILED(swap_GetCoreWindow((IDXGISwapChain4*)&s,&IID_IMadeiraCompositionSurface,&core))&&!core);
 s.fs.Windowed=TRUE;assert(FAILED(swap_SetFullscreenState((IDXGISwapChain4*)&s,TRUE,NULL))&&s.fs.Windowed);
 assert(FAILED(swap_ResizeTarget((IDXGISwapChain4*)&s,NULL)));
 assert(FAILED(swap_GetContainingOutput((IDXGISwapChain4*)&s,&output))&&!output);
 struct mad_resource buffer={NULL,2,NULL,NULL};s.buffers[0]=&buffer;s.buffers[1]=NULL;s.nbuf=1;
 s.desc.Width=640;s.desc.Height=480;s.desc.BufferCount=2;int old_drain2=drained;
 assert(swap_ResizeBuffers((IDXGISwapChain4*)&s,2,384,256,DXGI_FORMAT_B8G8R8A8_UNORM,0)==DXGI_ERROR_INVALID_CALL);
 assert(!resized&&s.desc.Width==640&&s.desc.Height==480&&s.buffers[0]==&buffer&&!locks&&drained==old_drain2+1);
 buffer.refs=1;assert(swap_ResizeBuffers((IDXGISwapChain4*)&s,2,384,256,DXGI_FORMAT_B8G8R8A8_UNORM,0)==S_OK&&resized==1&&s.desc.Width==384&&!locks);
 s.composition=FALSE;assert(swap_GetHwnd((IDXGISwapChain4*)&s,&h)==S_OK&&h==(HWND)0x11);
 assert(swap_SetFullscreenState((IDXGISwapChain4*)&s,TRUE,NULL)==S_OK&&!s.fs.Windowed);
 assert(swap_ResizeTarget((IDXGISwapChain4*)&s,NULL)==S_OK);
 puts("PASS: real composition descriptor/bind logic, owned HWND, idempotence, cross-target reject, detach and failure");
}
'''
with tempfile.TemporaryDirectory(prefix='madeira-composition-surface-') as td:
    c = Path(td) / 'check.c'
    binary = Path(td) / 'check'
    c.write_text(code)
    subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-Wno-pragma-pack', '-Wno-ignored-attributes', '-fsanitize=address,undefined', '-fno-sanitize-recover=all', '-I', str(ROOT / 'wine/build-macos/include'), '-I', str(ROOT / 'wine/include'), '-I', str(ROOT / 'madeira-d3d12/src/pe'), str(c), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
