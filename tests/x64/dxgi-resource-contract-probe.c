/* GPU API contracts for future games: resource reclaim and format/MSAA.
 * Build with the pinned x86_64-w64-mingw32-clang, -O2 -Wall -Wextra -Werror
 * -ldxguid -lkernel32. Run normally, --reclaim-only, or --msaa-only.
 * A terminal FAIL always exits nonzero. Unavailable features print SKIP.
 * This exercises real Wine/FEX/DXMT on-device; host tests cannot replace it.
 */
#define _WIN32_WINNT 0x0a00
#define COBJMACROS
#include <windows.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <stdarg.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

static unsigned failures, checks, skips;
static ID3D11Device *device;
static ID3D11DeviceContext *context;

static void log_line(const char *format, ...)
{
    char line[512];
    va_list args;
    va_start(args, format);
    vsnprintf(line, sizeof(line), format, args);
    va_end(args);
    OutputDebugStringA(line);
    fputs(line, stderr);
    fflush(stderr);
}

static void check(BOOL ok, const char *contract)
{
    log_line("DXGI-RESOURCE %s %s\n", ok ? "PASS" : "FAIL", contract);
    ++checks;
    if (!ok) ++failures;
}

static void skip(const char *reason)
{
    log_line("DXGI-RESOURCE SKIP %s\n", reason);
    ++skips;
}

static D3D11_TEXTURE2D_DESC texture_desc(DXGI_FORMAT format, UINT samples, UINT bind)
{
    D3D11_TEXTURE2D_DESC desc = {0};
    desc.Width = desc.Height = 16;
    desc.MipLevels = desc.ArraySize = 1;
    desc.Format = format;
    desc.SampleDesc.Count = samples;
    desc.Usage = D3D11_USAGE_DEFAULT;
    desc.BindFlags = bind;
    return desc;
}

static BOOL readback(ID3D11Texture2D *source, DXGI_FORMAT format, unsigned color)
{
    D3D11_TEXTURE2D_DESC desc;
    D3D11_MAPPED_SUBRESOURCE mapped = {0};
    ID3D11Texture2D *staging = NULL;
    HRESULT hr;
    BOOL matches = TRUE;
    DWORD start;
    UINT x, y, bytes_per_pixel;
    ID3D11Texture2D_GetDesc(source, &desc);
    desc.SampleDesc.Count = 1;
    desc.SampleDesc.Quality = 0;
    desc.Usage = D3D11_USAGE_STAGING;
    desc.BindFlags = desc.MiscFlags = 0;
    desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    if (format == DXGI_FORMAT_D32_FLOAT) desc.Format = DXGI_FORMAT_R32_TYPELESS;
    hr = ID3D11Device_CreateTexture2D(device, &desc, NULL, &staging);
    check(SUCCEEDED(hr) && staging, "readback staging texture creation");
    if (FAILED(hr) || !staging) return FALSE;
    ID3D11DeviceContext_CopyResource(context, (ID3D11Resource *)staging, (ID3D11Resource *)source);
    ID3D11DeviceContext_Flush(context);
    start = GetTickCount();
    do {
        hr = ID3D11DeviceContext_Map(context, (ID3D11Resource *)staging, 0, D3D11_MAP_READ,
                                   D3D11_MAP_FLAG_DO_NOT_WAIT, &mapped);
        if (hr != DXGI_ERROR_WAS_STILL_DRAWING) break;
        Sleep(1);
    } while ((DWORD)(GetTickCount() - start) < 10000);
    log_line("DXGI-RESOURCE readback format=%u hr=%#lx pitch=%u\n", format, (ULONG)hr, mapped.RowPitch);
    check(SUCCEEDED(hr) && mapped.pData, "GPU readback completes within 10 seconds");
    if (FAILED(hr) || !mapped.pData) {
        ID3D11Texture2D_Release(staging);
        return FALSE;
    }
    bytes_per_pixel = format == DXGI_FORMAT_R16G16B16A16_FLOAT ? 8 : 4;
    if (mapped.RowPitch < desc.Width * bytes_per_pixel) matches = FALSE;
    for (y = 0; matches && y < desc.Height; ++y) {
        const unsigned char *row = (const unsigned char *)mapped.pData + y * mapped.RowPitch;
        for (x = 0; x < desc.Width; ++x) {
            const unsigned char *pixel = row + x * bytes_per_pixel;
            if (format == DXGI_FORMAT_D32_FLOAT) {
                float value;
                memcpy(&value, pixel, sizeof(value));
                if (!(value >= 0.249f && value <= 0.251f)) matches = FALSE;
            } else if (format == DXGI_FORMAT_R16G16B16A16_FLOAT) {
                uint16_t expected[4] = {color == 0 ? 0x3c00 : 0, color == 1 ? 0x3c00 : 0, 0, 0x3c00};
                if (memcmp(pixel, expected, sizeof(expected))) matches = FALSE;
            } else {
                unsigned char expected[4] = {color == 0 ? 255 : 0, color == 1 ? 255 : 0, 0, 255};
                if (format == DXGI_FORMAT_B8G8R8A8_UNORM) {
                    unsigned char temp = expected[0];
                    expected[0] = expected[2];
                    expected[2] = temp;
                }
                if (memcmp(pixel, expected, sizeof(expected))) matches = FALSE;
            }
        }
    }
    ID3D11DeviceContext_Unmap(context, (ID3D11Resource *)staging, 0);
    ID3D11Texture2D_Release(staging);
    check(matches, "every readback pixel matches cleared contents");
    return matches;
}

static void reclaim_contracts(void)
{
    IDXGIDevice2 *dxgi = NULL;
    ID3D11Texture2D *textures[2] = {NULL, NULL};
    ID3D11RenderTargetView *views[2] = {NULL, NULL};
    IDXGIResource *resources[2] = {NULL, NULL};
    D3D11_TEXTURE2D_DESC desc = texture_desc(DXGI_FORMAT_R8G8B8A8_UNORM, 1, D3D11_BIND_RENDER_TARGET);
    struct { DWORD before; BOOL discarded[2]; DWORD after; } out;
    const FLOAT colors[2][4] = {{1, 0, 0, 1}, {0, 1, 0, 1}};
    HRESULT hr;
    unsigned i, count;
    hr = ID3D11Device_QueryInterface(device, &IID_IDXGIDevice2, (void **)&dxgi);
    if (hr == E_NOINTERFACE) { skip("IDXGIDevice2 unavailable; reclaim not tested"); return; }
    check(SUCCEEDED(hr) && dxgi, "IDXGIDevice2 QueryInterface");
    if (FAILED(hr) || !dxgi) return;
    for (i = 0; i < 2; ++i) {
        hr = ID3D11Device_CreateTexture2D(device, &desc, NULL, &textures[i]);
        check(SUCCEEDED(hr) && textures[i], "reclaim texture creation");
        if (FAILED(hr) || !textures[i]) goto cleanup;
        hr = ID3D11Device_CreateRenderTargetView(device, (ID3D11Resource *)textures[i], NULL, &views[i]);
        check(SUCCEEDED(hr) && views[i], "reclaim RTV creation");
        if (FAILED(hr) || !views[i]) goto cleanup;
        hr = ID3D11Texture2D_QueryInterface(textures[i], &IID_IDXGIResource, (void **)&resources[i]);
        check(SUCCEEDED(hr) && resources[i], "reclaim resource QueryInterface");
        if (FAILED(hr) || !resources[i]) goto cleanup;
    }
    for (count = 1; count <= 2; ++count) {
        for (i = 0; i < 2; ++i) ID3D11DeviceContext_ClearRenderTargetView(context, views[i], colors[i]);
        ID3D11DeviceContext_Flush(context);
        hr = IDXGIDevice2_OfferResources(dxgi, count, resources, DXGI_OFFER_RESOURCE_PRIORITY_NORMAL);
        log_line("DXGI-RESOURCE offer count=%u hr=%#lx\n", count, (ULONG)hr);
        if (hr == E_NOTIMPL || hr == DXGI_ERROR_UNSUPPORTED) {
            skip("OfferResources explicitly unsupported; reclaim not tested");
            goto cleanup;
        }
        check(hr == S_OK, "valid OfferResources succeeds");
        if (hr != S_OK) goto cleanup;
        out.before = out.after = 0x1234abcd;
        out.discarded[0] = 0x6b6b6b6b;
        out.discarded[1] = 0x77777777;
        hr = IDXGIDevice2_ReclaimResources(dxgi, count, resources, out.discarded);
        log_line("DXGI-RESOURCE reclaim count=%u hr=%#lx discarded=%#x,%#x\n",
                 count, (ULONG)hr, (unsigned)out.discarded[0], (unsigned)out.discarded[1]);
        check(hr == S_OK, "valid ReclaimResources succeeds");
        check(out.before == 0x1234abcd && out.after == 0x1234abcd, "reclaim outer canaries intact");
        if (count == 1) check(out.discarded[1] == 0x77777777, "reclaim respects requested count");
        if (hr != S_OK) goto cleanup;
        for (i = 0; i < count; ++i) {
            check(out.discarded[i] == FALSE || out.discarded[i] == TRUE, "reclaim initializes each BOOL output");
            if (out.discarded[i] == FALSE) readback(textures[i], DXGI_FORMAT_R8G8B8A8_UNORM, i);
            else if (out.discarded[i] == TRUE) skip("offered resource was discarded; preservation check inapplicable");
        }
        hr = IDXGIDevice2_OfferResources(dxgi, count, resources, DXGI_OFFER_RESOURCE_PRIORITY_NORMAL);
        check(hr == S_OK, "second valid OfferResources succeeds");
        if (hr != S_OK) goto cleanup;
        hr = IDXGIDevice2_ReclaimResources(dxgi, count, resources, NULL);
        check(hr == S_OK, "optional discarded output accepts NULL");
        if (hr != S_OK) goto cleanup;
        // With NULL discarded output, contents must be overwritten before use.
        for (i = 0; i < count; ++i) {
            ID3D11DeviceContext_ClearRenderTargetView(context, views[i], colors[i]);
            readback(textures[i], DXGI_FORMAT_R8G8B8A8_UNORM, i);
        }
    }
cleanup:
    for (i = 0; i < 2; ++i) {
        if (resources[i]) IDXGIResource_Release(resources[i]);
        if (views[i]) ID3D11RenderTargetView_Release(views[i]);
        if (textures[i]) ID3D11Texture2D_Release(textures[i]);
    }
    IDXGIDevice2_Release(dxgi);
}

static void multisample_case(DXGI_FORMAT format, UINT samples, UINT support, UINT quality)
{
    BOOL depth = format == DXGI_FORMAT_D32_FLOAT;
    BOOL compressed = format == DXGI_FORMAT_BC1_UNORM || format == DXGI_FORMAT_BC7_UNORM;
    UINT bind = depth ? D3D11_BIND_DEPTH_STENCIL :
                compressed ? D3D11_BIND_SHADER_RESOURCE :
                (support & D3D11_FORMAT_SUPPORT_RENDER_TARGET) ? D3D11_BIND_RENDER_TARGET : 0;
    D3D11_TEXTURE2D_DESC desc = texture_desc(format, samples, bind), actual;
    ID3D11Texture2D *texture = NULL, *resolved = NULL;
    ID3D11RenderTargetView *rtv = NULL;
    ID3D11DepthStencilView *dsv = NULL;
    ID3D11ShaderResourceView *srv = NULL;
    const FLOAT color[4] = {1, 0, 0, 1};
    HRESULT hr;
    if (!quality) { skip("format/sample combination not advertised; creation not required"); return; }
    check((support & D3D11_FORMAT_SUPPORT_TEXTURE2D) != 0, "quality query names a supported texture format");
    if (samples > 1) check(!compressed && (depth ? (support & D3D11_FORMAT_SUPPORT_DEPTH_STENCIL) :
                           (support & D3D11_FORMAT_SUPPORT_MULTISAMPLE_RENDERTARGET)),
                           "quality query agrees with format MSAA capability");
    hr = ID3D11Device_CreateTexture2D(device, &desc, NULL, &texture);
    log_line("DXGI-RESOURCE texture format=%u samples=%u hr=%#lx\n", format, samples, (ULONG)hr);
    check(SUCCEEDED(hr) && texture, "advertised format/sample texture creation");
    if (FAILED(hr) || !texture) goto cleanup;
    ID3D11Texture2D_GetDesc(texture, &actual);
    check(actual.Width == desc.Width && actual.Height == desc.Height && actual.Format == format &&
          actual.SampleDesc.Count == samples && actual.SampleDesc.Quality == 0,
          "created texture retains public descriptor");
    if (compressed) {
        hr = ID3D11Device_CreateShaderResourceView(device, (ID3D11Resource *)texture, NULL, &srv);
        check(SUCCEEDED(hr) && srv, "advertised compressed texture SRV creation");
        skip("compressed texture is not a render target; clear/resolve inapplicable");
        goto cleanup;
    }
    if (depth) {
        if (!(support & D3D11_FORMAT_SUPPORT_DEPTH_STENCIL)) {
            skip("format does not advertise depth attachment; DSV/clear inapplicable");
            goto cleanup;
        }
        hr = ID3D11Device_CreateDepthStencilView(device, (ID3D11Resource *)texture, NULL, &dsv);
        check(SUCCEEDED(hr) && dsv, "advertised depth texture DSV creation");
        if (FAILED(hr) || !dsv) goto cleanup;
        ID3D11DeviceContext_ClearDepthStencilView(context, dsv, D3D11_CLEAR_DEPTH, 0.25f, 0);
    } else {
        if (!(support & D3D11_FORMAT_SUPPORT_RENDER_TARGET)) {
            skip("format does not advertise color attachment; RTV/clear inapplicable");
            goto cleanup;
        }
        hr = ID3D11Device_CreateRenderTargetView(device, (ID3D11Resource *)texture, NULL, &rtv);
        check(SUCCEEDED(hr) && rtv, "advertised color texture RTV creation");
        if (FAILED(hr) || !rtv) goto cleanup;
        ID3D11DeviceContext_ClearRenderTargetView(context, rtv, color);
    }
    if (samples == 1) { readback(texture, format, 0); goto cleanup; }
    if (!(support & D3D11_FORMAT_SUPPORT_MULTISAMPLE_RESOLVE)) {
        skip("format does not advertise resolve; clear tested, resolve/readback inapplicable");
        goto cleanup;
    }
    desc.SampleDesc.Count = 1;
    hr = ID3D11Device_CreateTexture2D(device, &desc, NULL, &resolved);
    check(SUCCEEDED(hr) && resolved, "resolve destination creation");
    if (FAILED(hr) || !resolved) goto cleanup;
    ID3D11DeviceContext_ResolveSubresource(context, (ID3D11Resource *)resolved, 0,
                                         (ID3D11Resource *)texture, 0, format);
    readback(resolved, format, 0);
cleanup:
    if (srv) ID3D11ShaderResourceView_Release(srv);
    if (dsv) ID3D11DepthStencilView_Release(dsv);
    if (rtv) ID3D11RenderTargetView_Release(rtv);
    if (resolved) ID3D11Texture2D_Release(resolved);
    if (texture) ID3D11Texture2D_Release(texture);
}

static void multisample_contracts(void)
{
    const DXGI_FORMAT formats[] = {DXGI_FORMAT_R8G8B8A8_UNORM, DXGI_FORMAT_B8G8R8A8_UNORM,
        DXGI_FORMAT_R16G16B16A16_FLOAT, DXGI_FORMAT_D32_FLOAT, DXGI_FORMAT_BC1_UNORM, DXGI_FORMAT_BC7_UNORM};
    const UINT counts[] = {0, 1, 2, 4, 8, 16, 32, 33, 257, 258, 260, UINT_MAX};
    unsigned i, j;
    for (i = 0; i < sizeof(formats) / sizeof(formats[0]); ++i) {
        UINT support = 0xcccccccc;
        HRESULT format_hr = ID3D11Device_CheckFormatSupport(device, formats[i], &support);
        log_line("DXGI-RESOURCE format=%u hr=%#lx support=%#x\n", formats[i], (ULONG)format_hr, support);
        if (FAILED(format_hr)) { skip("format explicitly unavailable; matrix row not tested"); continue; }
        check(support != 0xcccccccc, "successful format query initializes support output");
        for (j = 0; j < sizeof(counts) / sizeof(counts[0]); ++j) {
            UINT quality = 0xcccccccc;
            HRESULT hr = ID3D11Device_CheckMultisampleQualityLevels(device, formats[i], counts[j], &quality);
            log_line("DXGI-RESOURCE matrix format=%u samples=%u hr=%#lx quality=%u support=%#x\n",
                     formats[i], counts[j], (ULONG)hr, quality, support);
            if (FAILED(hr)) { skip("format/sample quality query explicitly unavailable"); continue; }
            check(quality != 0xcccccccc, "successful quality query initializes output");
            if (quality == 0xcccccccc) continue;
            if (!counts[j] || counts[j] > D3D11_MAX_MULTISAMPLE_SAMPLE_COUNT) {
                check(quality == 0, "invalid sample count is not advertised through integer truncation");
                continue;
            }
            multisample_case(formats[i], counts[j], support, quality);
        }
    }
}

int main(int argc, char **argv)
{
    typedef HRESULT (WINAPI *CreateDeviceFn)(IDXGIAdapter *, D3D_DRIVER_TYPE, HMODULE, UINT,
        const D3D_FEATURE_LEVEL *, UINT, UINT, ID3D11Device **, D3D_FEATURE_LEVEL *, ID3D11DeviceContext **);
    HMODULE module;
    CreateDeviceFn create;
    D3D_FEATURE_LEVEL feature = 0;
    HRESULT hr;
    BOOL reclaim = TRUE, msaa = TRUE;
    if (argc == 2 && !strcmp(argv[1], "--reclaim-only")) msaa = FALSE;
    else if (argc == 2 && !strcmp(argv[1], "--msaa-only")) reclaim = FALSE;
    else if (argc != 1) { log_line("DXGI-RESOURCE FAIL unknown arguments\n"); return 2; }
    module = LoadLibraryA("d3d11.dll");
    check(module != NULL, "load d3d11.dll");
    if (!module) return 1;
    create = (CreateDeviceFn)(void *)GetProcAddress(module, "D3D11CreateDevice");
    check(create != NULL, "resolve D3D11CreateDevice");
    if (!create) { FreeLibrary(module); return 1; }
    hr = create(NULL, D3D_DRIVER_TYPE_HARDWARE, NULL, 0, NULL, 0, D3D11_SDK_VERSION,
                &device, &feature, &context);
    log_line("DXGI-RESOURCE device hr=%#lx feature=%#x\n", (ULONG)hr, feature);
    check(SUCCEEDED(hr) && device && context, "create real hardware D3D11 device and context");
    if (SUCCEEDED(hr) && device && context) {
        if (reclaim) reclaim_contracts();
        if (msaa) multisample_contracts();
        check(ID3D11Device_GetDeviceRemovedReason(device) == S_OK, "device remains usable after contracts");
    }
    if (context) { ID3D11DeviceContext_ClearState(context); ID3D11DeviceContext_Flush(context); ID3D11DeviceContext_Release(context); }
    if (device) ID3D11Device_Release(device);
    FreeLibrary(module);
    log_line("DXGI-RESOURCE %s checks=%u failures=%u skipped=%u\n", failures ? "FAIL" : "PASS", checks, failures, skips);
    return failures ? 1 : 0;
}
