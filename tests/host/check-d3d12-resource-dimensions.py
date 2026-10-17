#!/usr/bin/env python3
"""Exercise production resource entry points with a host-only Metal boundary.

The extracted allocation query, texture builder, complete resource creation
core, and committed/placed wrappers remain production code. Only external
Metal/COM/bookkeeping dependencies are replaced. This catches missing guards,
width truncation, stale output pointers, and Metal calls made before rejection;
it does not demonstrate that a game or real Metal device works.
"""
import pathlib
import re
import shutil
import subprocess
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "madeira-d3d12/src/pe/madeira_d3d12.c").read_text()


def function(name):
    pattern = re.compile(r"^static [^;{]*\b" + re.escape(name) + r"\([^;{]*\)\s*\{", re.M)
    match = pattern.search(SOURCE)
    if not match:
        raise RuntimeError("production function not found: " + name)
    depth = 1
    end = match.end()
    while depth:
        if SOURCE[end] == "{":
            depth += 1
        elif SOURCE[end] == "}":
            depth -= 1
        end += 1
    return SOURCE[match.start():end]


PRELUDE = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define STDMETHODCALLTYPE
typedef uint32_t UINT;
typedef uint64_t UINT64;
typedef int32_t HRESULT;
typedef long LONG;
typedef long long LONG64;
typedef size_t SIZE_T;
typedef int DXGI_FORMAT;
typedef int D3D12_RESOURCE_FLAGS;
typedef int D3D12_HEAP_TYPE;
typedef int D3D12_HEAP_FLAGS;
typedef int D3D12_RESOURCE_STATES;
typedef void *REFIID;
typedef uintptr_t obj_handle_t;
typedef struct mad_device ID3D12Device;
typedef struct mad_device ID3D12Device10;
typedef struct mad_resource ID3D12Resource;
typedef struct mad_memheap ID3D12Heap;
typedef struct { int unused; } D3D12_CLEAR_VALUE;
typedef struct { UINT Count, Quality; } DXGI_SAMPLE_DESC;
typedef struct { D3D12_HEAP_TYPE Type; } D3D12_HEAP_PROPERTIES;
typedef struct {
    int Dimension; UINT64 Alignment, Width; UINT Height;
    uint16_t DepthOrArraySize, MipLevels; DXGI_FORMAT Format;
    DXGI_SAMPLE_DESC SampleDesc; int Layout; D3D12_RESOURCE_FLAGS Flags;
} D3D12_RESOURCE_DESC;
typedef struct { D3D12_RESOURCE_DESC base; UINT mip_region[3]; } D3D12_RESOURCE_DESC1;
typedef struct { UINT64 SizeInBytes, Alignment; } D3D12_RESOURCE_ALLOCATION_INFO;
typedef struct { UINT64 Offset, Alignment, SizeInBytes; } D3D12_RESOURCE_ALLOCATION_INFO1;
#define D3D12_RESOURCE_DIMENSION_UNKNOWN 0
#define D3D12_RESOURCE_DIMENSION_BUFFER 1
#define D3D12_RESOURCE_DIMENSION_TEXTURE1D 2
#define D3D12_RESOURCE_DIMENSION_TEXTURE2D 3
#define D3D12_RESOURCE_DIMENSION_TEXTURE3D 4
#define D3D12_DEFAULT_RESOURCE_PLACEMENT_ALIGNMENT 65536ull
#define D3D12_DEFAULT_MSAA_RESOURCE_PLACEMENT_ALIGNMENT 4194304ull
#define D3D12_HEAP_TYPE_DEFAULT 1
#define D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET 1
#define D3D12_RESOURCE_FLAG_ALLOW_DEPTH_STENCIL 2
#define D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS 4
#define S_OK ((HRESULT)0)
#define E_INVALIDARG ((HRESULT)0x80070057u)
#define E_OUTOFMEMORY ((HRESULT)0x8007000eu)
#define E_NOTIMPL ((HRESULT)0x80004001u)
#define E_FAIL ((HRESULT)0x80004005u)
#define FAILED(hr) ((hr) < 0)
#define MEM_COMMIT 1
#define MEM_RESERVE 2
#define MEM_RELEASE 4
#define PAGE_READWRITE 1
#define MAD_HP_MAX_TEX (2ull << 20)
#define MAD_CAT_TEX_MSAA 0
#define MAD_CAT_TEX_DS 1
#define MAD_CAT_TEX_RT 2
#define MAD_CAT_TEX_UAV 3
#define MAD_CAT_TEX_PLAIN 4
#define MAD_CAT_BUF_PRIVATE 5
#define MAD_CAT_BUF_SHARED 6
enum WMTPixelFormat { WMTPixelFormatR32Uint, WMTPixelFormatR32Sint,
    WMTPixelFormatRG32Uint, WMTPixelFormatDepth32Float_Stencil8, TestRGBA8 };
enum WMTTextureUsage { WMTTextureUsageShaderRead = 1, WMTTextureUsageShaderWrite = 2,
    WMTTextureUsageRenderTarget = 4, WMTTextureUsageShaderAtomic = 8,
    WMTTextureUsagePixelFormatView = 16 };
enum { WMTTextureType2DArray, WMTTextureType3D, WMTTextureType2DMultisampleArray,
    WMTResourceStorageModePrivate, WMTResourceStorageModeShared };
struct WMTTextureInfo {
    enum WMTPixelFormat pixel_format; UINT width, height, depth, array_length,
    mipmap_level_count, sample_count, type; enum WMTTextureUsage usage;
    UINT options; UINT64 gpu_resource_id;
};
struct WMTBufferInfo { UINT64 length; UINT options; struct { void *ptr; } memory; UINT64 gpu_address; };
struct mad_device { obj_handle_t mtl_device; struct { obj_handle_t heap; } theaps[1];
    LONG hp_textures, hp_fallbacks; int view_lock; };
struct mad_memheap { obj_handle_t mtl; struct {
    UINT64 SizeInBytes, Alignment; UINT Flags; D3D12_HEAP_PROPERTIES Properties;
} desc; };
struct mad_resource {
    void *vtbl; int refs; const void *iid; const char *name; UINT64 size;
    D3D12_HEAP_TYPE heap; D3D12_RESOURCE_DESC desc; struct mad_device *owner;
    obj_handle_t texture, buffer; struct mad_memheap *placed_heap;
    UINT hp_used, hp_heap; UINT64 hp_off, hp_size; UINT width, height;
    UINT64 gpu_resource_id; UINT tex_type, tex_pf, tex_mips, tex_layers, tex_depth;
    int is_depth, has_stencil; UINT samples; void *own_mem, *cpu; UINT64 gpu_address;
};
static int g_res_vtbl, IID_ID3D12Resource;
static LONG g_placed_in_heap, g_upload_swap_n;
static LONG64 g_upload_swap_bytes;
static UINT g_list_seq;
static unsigned metal_calls, buffer_calls, texture_calls, footprint_calls;
static struct WMTTextureInfo last_texture;
static UINT64 last_buffer_length;
#define d3d12_log(...) ((void)0)
#define InterlockedIncrement(p) (++*(p))
#define InterlockedExchangeAdd64(p,v) (*(p) += (v))
#define EnterCriticalSection(p) ((void)0)
#define LeaveCriticalSection(p) ((void)0)
#define ID3D12Heap_AddRef(p) ((void)0)
#define mad_placed_fallback(...) ((void)0)
#define mad_resident(...) ((void)0)
#define mad_acct(...) ((void)0)
#define mad_vmap_put_locked(...) ((void)0)
#define mad_hp_release(...) ((void)0)
#define mad_track(...) ((void)0)
#define mad_untrack(...) ((void)0)
#define NSObject_release(...) ((void)0)
#define VirtualAlloc(...) NULL
#define VirtualFree(...) ((void)0)
static int mad_upload_swap_on(void) { return 0; }
static int mad_hp_alloc(struct mad_device *d, UINT64 s, UINT64 a, UINT64 *o)
{ (void)d; (void)s; (void)a; (void)o; return -1; }
static HRESULT mad_creation_failure(struct mad_device *d, const char *why)
{ (void)d; (void)why; return E_FAIL; }
static HRESULT res_QI(ID3D12Resource *r, REFIID iid, void **out)
{ (void)iid; *out = r; return S_OK; }
static void res_Release(ID3D12Resource *r) { free(r); }
static int mad_map_texture_format(DXGI_FORMAT f, D3D12_RESOURCE_FLAGS flags,
    enum WMTPixelFormat *pf, int *depth)
{ (void)f; (void)flags; *pf = TestRGBA8; *depth = 0; return 1; }
static void mad_format_info(DXGI_FORMAT f, UINT *bytes, UINT *block)
{ (void)f; *bytes = 4; *block = 1; }
static void device_GetCopyableFootprints(ID3D12Device *dev, const D3D12_RESOURCE_DESC *d,
    UINT first, UINT count, UINT64 base, void *layouts, void *rows, void *sizes, UINT64 *total)
{ (void)dev; (void)first; (void)count; (void)base; (void)layouts; (void)rows;
  (void)sizes; ++footprint_calls; *total = d->Width * d->Height * 4; }
static void MTLDevice_heapTextureSizeAndAlign(obj_handle_t dev, const struct WMTTextureInfo *ti,
    UINT64 *size, UINT64 *align)
{ (void)dev; ++metal_calls; ++texture_calls; last_texture = *ti; *size = 65536; *align = 65536; }
static void MTLDevice_heapBufferSizeAndAlign(obj_handle_t dev, UINT64 len, UINT options,
    UINT64 *size, UINT64 *align)
{ (void)dev; (void)options; ++metal_calls; ++buffer_calls; last_buffer_length = len;
  *size = len; *align = 65536; }
static obj_handle_t MTLDevice_newTexture(obj_handle_t dev, struct WMTTextureInfo *ti)
{ (void)dev; ++metal_calls; ++texture_calls; last_texture = *ti; return 1; }
static obj_handle_t MTLHeap_newTextureAtOffset(obj_handle_t heap, struct WMTTextureInfo *ti, UINT64 off)
{ (void)heap; (void)off; return MTLDevice_newTexture(0, ti); }
static obj_handle_t MTLDevice_newBuffer(obj_handle_t dev, struct WMTBufferInfo *info)
{ (void)dev; ++metal_calls; ++buffer_calls; last_buffer_length = info->length; return 1; }
static obj_handle_t MTLHeap_newBufferAtOffset(obj_handle_t heap, struct WMTBufferInfo *info, UINT64 off)
{ (void)heap; (void)off; return MTLDevice_newBuffer(0, info); }
'''

HARNESS = r'''
static unsigned failures;
#define CHECK(c, label) do { if (!(c)) { ++failures; printf("FAIL %s: %s\n", label, #c); } } while (0)
static void reset(void) { metal_calls = buffer_calls = texture_calls = footprint_calls = 0; }
static D3D12_RESOURCE_DESC desc(int dim, UINT64 width, UINT height, UINT depth) {
    D3D12_RESOURCE_DESC d = {0};
    d.Dimension = dim; d.Width = width; d.Height = height; d.DepthOrArraySize = depth;
    d.MipLevels = 1; d.Format = dim == 1 ? 0 : 28; d.SampleDesc.Count = 1;
    return d;
}
static void run_case(const char *label, D3D12_RESOURCE_DESC d, int valid) {
    struct mad_device dev = {0};
    D3D12_HEAP_PROPERTIES hp = { D3D12_HEAP_TYPE_DEFAULT };
    struct mad_memheap heap = {0};
    D3D12_RESOURCE_ALLOCATION_INFO ai;
    void *out;
    HRESULT hr;
    if (!valid) {
        struct WMTTextureInfo ti; enum WMTPixelFormat pf; int depth;
        reset();
        CHECK(mad_texinfo_from_desc(&d, &ti, &pf, &depth) == 0, label);
        CHECK(metal_calls == 0, label);
    }
    heap.mtl = 1; heap.desc.SizeInBytes = 1ull << 40; heap.desc.Properties = hp;
    reset();
    device_GetResourceAllocationInfo(&dev, &ai, 0, 1, &d);
    if (valid) {
        CHECK(ai.SizeInBytes != UINT64_MAX && ai.SizeInBytes > 0, label);
        CHECK(metal_calls > 0, label);
    } else {
        CHECK(ai.SizeInBytes == UINT64_MAX, label);
        CHECK(ai.Alignment == (d.SampleDesc.Count > 1 ? 4194304ull : 65536ull), label);
        CHECK(metal_calls == 0 && footprint_calls == 0, label);
    }
    for (int placed = 0; placed < 2; ++placed) {
        reset(); out = (void *)(uintptr_t)0xdeadbeef;
        hr = placed ? device_CreatePlacedResource(&dev, &heap, 0, &d, 0, NULL, NULL, &out)
                    : device_CreateCommittedResource(&dev, &hp, 0, &d, 0, NULL, NULL, &out);
        if (valid) {
            CHECK(hr == S_OK && out != NULL && metal_calls > 0, label);
            if (d.Dimension == 1) {
                CHECK(buffer_calls > 0 && texture_calls == 0 && last_buffer_length == d.Width, label);
            } else {
                CHECK(texture_calls > 0 && buffer_calls == 0 && last_texture.width == d.Width, label);
                CHECK(last_texture.height == d.Height, label);
            }
        } else {
            CHECK(hr == E_INVALIDARG, label);
            CHECK(out == NULL, label);
            CHECK(metal_calls == 0, label);
        }
    }
}
static void texture_case(const char *label, D3D12_RESOURCE_DESC d, UINT type,
    UINT layers, UINT depth, UINT mips, UINT samples) {
    struct mad_device dev = {0}; struct WMTTextureInfo ti;
    D3D12_HEAP_PROPERTIES hp = { D3D12_HEAP_TYPE_DEFAULT };
    enum WMTPixelFormat pf; int is_depth; void *out = NULL;
    CHECK(mad_texinfo_from_desc(&d, &ti, &pf, &is_depth) == 1, label);
    CHECK(ti.type == type && ti.array_length == layers && ti.depth == depth &&
          ti.mipmap_level_count == mips && ti.sample_count == samples, label);
    reset();
    CHECK(device_CreateCommittedResource(&dev, &hp, 0, &d, 0, NULL, NULL, &out) == S_OK, label);
    CHECK(last_texture.type == type && last_texture.array_length == layers &&
          last_texture.depth == depth && last_texture.mipmap_level_count == mips &&
          last_texture.sample_count == samples, label);
}
static void allocation_case(const char *label, const D3D12_RESOURCE_DESC *ds,
    UINT count, UINT64 want_size, UINT64 want_align, int valid) {
    struct mad_device dev = {0}; D3D12_RESOURCE_ALLOCATION_INFO ai;
    D3D12_RESOURCE_ALLOCATION_INFO1 info[10]; D3D12_RESOURCE_DESC1 ds1[10];
    for (UINT i = 0; ds && i < count; ++i) ds1[i].base = ds[i];
    for (int version = 0; version <= 2; ++version) {
        reset(); memset(info, 0xa5, sizeof info);
        if (version == 0) device_GetResourceAllocationInfo(&dev, &ai, 0, count, ds);
        else if (version == 1) device_GetResourceAllocationInfo1(&dev, &ai, 0, count, ds, info);
        else device_GetResourceAllocationInfo2(&dev, &ai, 0, count, ds ? ds1 : NULL, info);
        CHECK(ai.SizeInBytes == want_size && ai.Alignment == want_align, label);
        CHECK(valid ? metal_calls > 0 : metal_calls == 0 && footprint_calls == 0, label);
        if (valid && version != 0 && count == 2) {
            CHECK(info[0].Offset == 0 && info[0].SizeInBytes == 65536 && info[0].Alignment == 65536, label);
            CHECK(info[1].Offset == 65536 && info[1].SizeInBytes == 65536 && info[1].Alignment == 65536, label);
        }
    }
}
int main(int argc, char **argv) {
    D3D12_RESOURCE_DESC d;
    /* Isolate the currently missing base-API NULL guard from the other checks. */
    if (argc > 1 && strcmp(argv[1], "--null-base") == 0) {
        struct mad_device dev = {0}; D3D12_RESOURCE_ALLOCATION_INFO ai;
        reset(); device_GetResourceAllocationInfo(&dev, &ai, 0, 1, NULL);
        CHECK(ai.SizeInBytes == UINT64_MAX && ai.Alignment == 65536 && metal_calls == 0,
              "NULL base allocation descriptors with nonzero count");
        return failures ? 1 : 0;
    }
    run_case("valid buffer", desc(1, 4096, 1, 1), 1);
    run_case("64-bit buffer is not truncated", desc(1, 1ull << 32, 1, 1), 1);
    run_case("valid 1D array", desc(2, 64, 1, 3), 1);
    texture_case("1D builder and Metal descriptor", desc(2, 64, 1, 3), WMTTextureType2DArray, 3, 1, 1, 1);
    run_case("valid 2D array", desc(3, 1160, 800, 2), 1);
    texture_case("2D builder and Metal descriptor", desc(3, 1160, 800, 2), WMTTextureType2DArray, 2, 1, 1, 1);
    run_case("valid 3D", desc(4, 64, 32, 8), 1);
    texture_case("3D builder and Metal descriptor", desc(4, 64, 32, 8), WMTTextureType3D, 1, 8, 1, 1);
    d = desc(3, 64, 32, 1); d.MipLevels = 0;
    run_case("zero mip levels remains valid", d, 1);
    texture_case("zero mip levels retains existing one-mip conversion", d, WMTTextureType2DArray, 1, 1, 1, 1);
    d.MipLevels = 3;
    texture_case("explicit mip count is preserved", d, WMTTextureType2DArray, 1, 1, 3, 1);
    d = desc(3, 64, 32, 1); d.SampleDesc.Count = 4;
    run_case("valid MSAA remains valid", d, 1);
    texture_case("4x MSAA builder and Metal descriptor", d, WMTTextureType2DMultisampleArray, 1, 1, 1, 4);
    run_case("zero width and height", desc(3, 0, 0, 1), 0);
    for (int dim = 1; dim <= 4; ++dim) {
        run_case("zero width", desc(dim, 0, 1, 1), 0);
        run_case("zero height", desc(dim, 64, 0, 1), 0);
        run_case("zero depth/array size", desc(dim, 64, 1, 0), 0);
        if (dim != 1) {
            run_case("texture width truncates to zero", desc(dim, 1ull << 32, 1, 1), 0);
            run_case("texture width truncates to nonzero", desc(dim, (1ull << 32) + 1, 1, 1), 0);
        }
    }
    run_case("buffer height must be one", desc(1, 64, 2, 1), 0);
    run_case("buffer array size must be one", desc(1, 64, 1, 2), 0);
    run_case("1D height must be one", desc(2, 64, 2, 1), 0);
    run_case("unknown dimension", desc(0, 64, 32, 1), 0);
    run_case("unrecognized dimension", desc(99, 64, 32, 1), 0);
    d = desc(3, 0, 0, 1); d.SampleDesc.Count = 4;
    run_case("invalid MSAA reports MSAA alignment", d, 0);
    /* Validate the whole array before querying Metal for an earlier valid item. */
    {
        struct mad_device dev = {0}; D3D12_RESOURCE_ALLOCATION_INFO ai;
        D3D12_RESOURCE_DESC ds[] = { desc(3, 64, 32, 1), desc(3, 0, 0, 1) };
        reset(); device_GetResourceAllocationInfo(&dev, &ai, 0, 2, ds);
        CHECK(ai.SizeInBytes == UINT64_MAX && metal_calls == 0, "invalid later allocation descriptor");
        ds[0].SampleDesc.Count = 4;
        reset(); device_GetResourceAllocationInfo(&dev, &ai, 0, 2, ds);
        CHECK(ai.SizeInBytes == UINT64_MAX && ai.Alignment == 4194304ull && metal_calls == 0,
              "MSAA in allocation array determines failure alignment");
    }
    {
        D3D12_RESOURCE_DESC ds[] = { desc(3, 64, 32, 1), desc(3, 64, 32, 1) };
        allocation_case("valid multi-resource allocation offsets", ds, 2, 131072, 65536, 1);
        ds[1].Width = 0;
        allocation_case("invalid later descriptor cannot wrap allocation sentinel", ds, 2, UINT64_MAX, 65536, 0);
        ds[0].SampleDesc.Count = 4;
        allocation_case("earlier MSAA determines invalid later alignment", ds, 2, UINT64_MAX, 4194304, 0);
        ds[0] = desc(3, 0, 0, 1); ds[1] = desc(3, 64, 32, 1);
        ds[1].SampleDesc.Count = 4;
        allocation_case("invalid first descriptor scans later MSAA before rejection", ds, 2, UINT64_MAX, 4194304, 0);
    }
    {
        D3D12_RESOURCE_DESC ds[9];
        for (UINT i = 0; i < 9; ++i) ds[i] = desc(3, 64, 32, 1);
        ds[8].Height = 0;
        allocation_case("Info2 heap descriptor conversion also preflights all entries", ds, 9, UINT64_MAX, 65536, 0);
    }
    {
        struct mad_device dev = {0}; D3D12_RESOURCE_ALLOCATION_INFO ai;
        reset(); device_GetResourceAllocationInfo1(&dev, &ai, 0, 1, NULL, NULL);
        CHECK(ai.SizeInBytes == UINT64_MAX && ai.Alignment == 65536 && metal_calls == 0,
              "NULL Info1 descriptors with nonzero count");
        reset(); device_GetResourceAllocationInfo2(&dev, &ai, 0, 1, NULL, NULL);
        CHECK(ai.SizeInBytes == UINT64_MAX && ai.Alignment == 65536 && metal_calls == 0,
              "NULL Info2 descriptors with nonzero count");
    }
    printf("resource dimensions: %u failures\n", failures);
    return failures ? 1 : 0;
}
'''


def main():
    cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    if not cc:
        raise SystemExit("FAIL: a host C compiler is required")
    names = ["mad_align", "mad_clamp_sample_count"]
    # These helpers do not exist before the fix, so the same harness gives RED.
    for name in ("mad_resource_dimensions_valid", "mad_invalid_resource_desc_log", "mad_resource_descs_valid"):
        if re.search(r"^static [^;{]*\b" + name + r"\(", SOURCE, re.M):
            names.append(name)
    names += ["mad_refuse_log", "mad_texinfo_from_desc", "device_GetResourceAllocationInfo",
              "mad_create_resource_at", "mad_create_resource", "device_CreateCommittedResource",
              "device_CreatePlacedResource", "mad_desc1_to_desc", "device_GetResourceAllocationInfo1",
              "device_GetResourceAllocationInfo2"]
    with tempfile.TemporaryDirectory(prefix="madeira-resource-dimensions-") as temp:
        c_file = pathlib.Path(temp) / "resource-dimensions.c"
        binary = pathlib.Path(temp) / "resource-dimensions"
        c_file.write_text(PRELUDE + "\n" + "\n\n".join(function(n) for n in names) + HARNESS)
        subprocess.run([cc, "-std=c99", "-Werror=implicit-function-declaration", "-o", str(binary), str(c_file)], check=True)
        results = [subprocess.run([str(binary), *args], check=False)
                   for args in ([], ["--null-base"])]
        for result in results:
            if result.returncode:
                print("FAIL resource dimensions harness exit:", result.returncode)
        raise SystemExit(1 if any(result.returncode for result in results) else 0)


if __name__ == "__main__":
    main()
