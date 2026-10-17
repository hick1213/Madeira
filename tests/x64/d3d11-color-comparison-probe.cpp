/* Actual GPU observations, not a host simulation. Build with pinned LLVM MinGW:
 * x86_64-w64-mingw32-clang++ -std=c++17 -O2 -Wall -Wextra -Werror
 *   -static d3d11-color-comparison-probe.cpp -ld3d11 -ld3dcompiler_47 -ldxgi
 * This exercises Wine/FEX/DXMT on-device. Exit 0 means observations were read;
 * it does NOT mean every D3D comparison contract passed. No WARP fallback.
 * Log: C:\madeira-shadow-trace-20261013\d3d11-color-comparison.txt
 */
#define _WIN32_WINNT 0x0a00
#include <windows.h>
#include <d3d11.h>
#include <d3dcompiler.h>
#include <cstdint>
#include <cstdio>
#include <cstdarg>
#include <cstring>
#include <cmath>

static FILE *log_file;
static DWORD started;
static unsigned observations;

static void log_line(const char *format, ...)
{
    char line[768];
    va_list args;
    va_start(args, format);
    vsnprintf(line, sizeof(line), format, args);
    va_end(args);
    OutputDebugStringA(line);
    fputs(line, stderr);
    fflush(stderr);
    if (log_file) { fputs(line, log_file); fflush(log_file); }
}

template<class T> struct Owned {
    T *p = nullptr;
    ~Owned() { if (p) p->Release(); }
    Owned() = default;
    Owned(const Owned &) = delete;
    Owned &operator=(const Owned &) = delete;
};

static bool checked(HRESULT hr, const char *operation)
{
    log_line("COLOR-PROBE API operation=%s hr=%08lx\n", operation, (unsigned long)hr);
    return SUCCEEDED(hr);
}

static HRESULT bounded_map(ID3D11DeviceContext *context, ID3D11Resource *resource,
                           D3D11_MAPPED_SUBRESOURCE *mapped)
{
    const DWORD begin = GetTickCount();
    HRESULT hr;
    do {
        hr = context->Map(resource, 0, D3D11_MAP_READ, D3D11_MAP_FLAG_DO_NOT_WAIT, mapped);
        if (hr != DXGI_ERROR_WAS_STILL_DRAWING) return hr;
        Sleep(1);
    } while ((DWORD)(GetTickCount() - begin) < 2000 &&
             (DWORD)(GetTickCount() - started) < 60000);
    return hr;
}

static bool compile(const char *source, const char *target, ID3DBlob **blob)
{
    Owned<ID3DBlob> errors;
    HRESULT hr = D3DCompile(source, strlen(source), "madeira-color-probe", nullptr,
                           nullptr, "main", target, D3DCOMPILE_ENABLE_STRICTNESS, 0,
                           blob, &errors.p);
    // Shader source is fixed and contains no credentials or user data.
    if (errors.p) log_line("COLOR-PROBE shader-message=%.*s\n",
                          (int)errors.p->GetBufferSize(), (const char *)errors.p->GetBufferPointer());
    return checked(hr, target);
}

static bool run()
{
    Owned<ID3D11Device> device;
    Owned<ID3D11DeviceContext> context;
    D3D_FEATURE_LEVEL level;
    const D3D_FEATURE_LEVEL levels[] = {D3D_FEATURE_LEVEL_11_0};
    if (!checked(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0,
                                  levels, 1, D3D11_SDK_VERSION, &device.p, &level,
                                  &context.p), "D3D11CreateDevice-hardware")) return false;
    log_line("COLOR-PROBE device feature-level=%04x\n", (unsigned)level);
    Owned<IDXGIDevice> dxgi;
    Owned<IDXGIAdapter> adapter;
    if (!checked(device.p->QueryInterface(__uuidof(IDXGIDevice), (void **)&dxgi.p), "QueryInterface-DXGI") ||
        !checked(dxgi.p->GetAdapter(&adapter.p), "GetAdapter")) return false;
    DXGI_ADAPTER_DESC adapter_desc = {};
    if (!checked(adapter.p->GetDesc(&adapter_desc), "GetAdapterDesc")) return false;
    char adapter_name[256] = {};
    WideCharToMultiByte(CP_UTF8, 0, adapter_desc.Description, -1, adapter_name, sizeof(adapter_name), nullptr, nullptr);
    log_line("COLOR-PROBE adapter name=%s vendor=%04x device=%04x\n", adapter_name,
             adapter_desc.VendorId, adapter_desc.DeviceId);

    // CPU initialization supplies distinct, known texels for all 48 faces.
    uint16_t face_pixels[48][16];
    D3D11_SUBRESOURCE_DATA face_data[48] = {};
    for (unsigned slice = 0; slice < 48; ++slice) {
        const uint16_t bits = (uint16_t)(4096 + (slice / 6) * 4096 + (slice % 6) * 257);
        for (uint16_t &pixel : face_pixels[slice]) pixel = bits;
        face_data[slice].pSysMem = face_pixels[slice];
        face_data[slice].SysMemPitch = 8;
    }
    D3D11_TEXTURE2D_DESC resource_desc = {};
    resource_desc.Width = resource_desc.Height = 4;
    resource_desc.MipLevels = resource_desc.SampleDesc.Count = 1;
    resource_desc.ArraySize = 48;
    resource_desc.Format = DXGI_FORMAT_R16_TYPELESS;
    resource_desc.Usage = D3D11_USAGE_DEFAULT;
    resource_desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    resource_desc.MiscFlags = D3D11_RESOURCE_MISC_TEXTURECUBE;
    Owned<ID3D11Texture2D> cube, color2d, depth2d;
    Owned<ID3D11ShaderResourceView> cube_srv, color2d_srv, depth2d_srv;
    if (!checked(device.p->CreateTexture2D(&resource_desc, face_data, &cube.p), "Create48FaceR16ColorCube")) return false;
    D3D11_SHADER_RESOURCE_VIEW_DESC view_desc = {};
    view_desc.Format = DXGI_FORMAT_R16_UNORM;
    view_desc.ViewDimension = D3D11_SRV_DIMENSION_TEXTURECUBEARRAY;
    view_desc.TextureCubeArray.NumCubes = 8;
    view_desc.TextureCubeArray.MipLevels = 1;
    if (!checked(device.p->CreateShaderResourceView(cube.p, &view_desc, &cube_srv.p), "CreateR16CubeArraySRV")) return false;

    uint16_t control_pixels[16];
    const uint16_t control_bits = 16384;
    for (uint16_t &pixel : control_pixels) pixel = control_bits;
    D3D11_SUBRESOURCE_DATA control_data = {};
    control_data.pSysMem = control_pixels;
    control_data.SysMemPitch = 8;
    resource_desc.ArraySize = 1;
    resource_desc.MiscFlags = 0;
    if (!checked(device.p->CreateTexture2D(&resource_desc, &control_data, &color2d.p), "CreateR16Color2D")) return false;
    view_desc = {};
    view_desc.Format = DXGI_FORMAT_R16_UNORM;
    view_desc.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D;
    view_desc.Texture2D.MipLevels = 1;
    if (!checked(device.p->CreateShaderResourceView(color2d.p, &view_desc, &color2d_srv.p), "CreateR16Color2DSRV")) return false;
    resource_desc.BindFlags |= D3D11_BIND_DEPTH_STENCIL;
    if (!checked(device.p->CreateTexture2D(&resource_desc, nullptr, &depth2d.p), "CreateR16Depth2D")) return false;
    Owned<ID3D11DepthStencilView> dsv;
    D3D11_DEPTH_STENCIL_VIEW_DESC dsv_desc = {};
    dsv_desc.Format = DXGI_FORMAT_D16_UNORM;
    dsv_desc.ViewDimension = D3D11_DSV_DIMENSION_TEXTURE2D;
    if (!checked(device.p->CreateDepthStencilView(depth2d.p, &dsv_desc, &dsv.p), "CreateD16DSV") ||
        !checked(device.p->CreateShaderResourceView(depth2d.p, &view_desc, &depth2d_srv.p), "CreateR16Depth2DSRV")) return false;
    context.p->ClearDepthStencilView(dsv.p, D3D11_CLEAR_DEPTH, (float)control_bits / 65535.0f, 0);
    log_line("COLOR-PROBE inputs format=R16_TYPELESS srv=R16_UNORM cubes=8 faces=48 texels-per-face=16 control-bits=%u\n", control_bits);

    D3D11_TEXTURE2D_DESC color_desc = {};
    color_desc.Width = color_desc.Height = 1;
    color_desc.MipLevels = color_desc.ArraySize = color_desc.SampleDesc.Count = 1;
    color_desc.Format = DXGI_FORMAT_R32_FLOAT;
    color_desc.Usage = D3D11_USAGE_DEFAULT;
    color_desc.BindFlags = D3D11_BIND_RENDER_TARGET;
    Owned<ID3D11Texture2D> color, staging;
    Owned<ID3D11RenderTargetView> rtv;
    if (!checked(device.p->CreateTexture2D(&color_desc, nullptr, &color.p), "CreateR32Target") ||
        !checked(device.p->CreateRenderTargetView(color.p, nullptr, &rtv.p), "CreateR32RTV")) return false;
    color_desc.Usage = D3D11_USAGE_STAGING;
    color_desc.BindFlags = 0;
    color_desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    if (!checked(device.p->CreateTexture2D(&color_desc, nullptr, &staging.p), "CreateR32Staging")) return false;

    const char *vs_source = "float4 main(uint id : SV_VertexID) : SV_Position {"
                            "float2 p = id == 0 ? float2(-1,-1) : (id == 1 ? float2(-1,3) : float2(3,-1));"
                            "return float4(p,0,1);}";
    const char *ps_sources[] = {
        "TextureCubeArray<float> tex : register(t0); SamplerComparisonState cmp : register(s0);"
        "cbuffer Params : register(b0) {float4 coord; float ref; float3 pad;}"
        "float main() : SV_Target {return tex.SampleCmpLevelZero(cmp,coord,ref);}",
        "Texture2D<float> tex : register(t0); SamplerComparisonState cmp : register(s0);"
        "cbuffer Params : register(b0) {float4 coord; float ref; float3 pad;}"
        "float main() : SV_Target {return tex.SampleCmpLevelZero(cmp,coord.xy,ref);}",
        "TextureCubeArray<float> tex : register(t0); SamplerState samp : register(s0);"
        "cbuffer Params : register(b0) {float4 coord; float ref; float3 pad;}"
        "float main() : SV_Target {return tex.SampleLevel(samp,coord,0);}"
    };
    Owned<ID3DBlob> vs_code, ps_code[3];
    Owned<ID3D11VertexShader> vs;
    Owned<ID3D11PixelShader> ps[3];
    if (!compile(vs_source, "vs_5_0", &vs_code.p) ||
        !checked(device.p->CreateVertexShader(vs_code.p->GetBufferPointer(), vs_code.p->GetBufferSize(), nullptr, &vs.p),
                 "CreateVertexShader")) return false;
    for (unsigned i = 0; i < 3; ++i) {
        if (!compile(ps_sources[i], "ps_5_0", &ps_code[i].p) ||
            !checked(device.p->CreatePixelShader(ps_code[i].p->GetBufferPointer(), ps_code[i].p->GetBufferSize(), nullptr, &ps[i].p),
                     "CreatePixelShader")) return false;
    }
    D3D11_BUFFER_DESC buffer_desc = {};
    buffer_desc.ByteWidth = 32;
    buffer_desc.Usage = D3D11_USAGE_DEFAULT;
    buffer_desc.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    Owned<ID3D11Buffer> params;
    if (!checked(device.p->CreateBuffer(&buffer_desc, nullptr, &params.p), "CreateParams")) return false;
    D3D11_RASTERIZER_DESC raster_desc = {};
    raster_desc.FillMode = D3D11_FILL_SOLID;
    raster_desc.CullMode = D3D11_CULL_NONE;
    raster_desc.DepthClipEnable = TRUE;
    Owned<ID3D11RasterizerState> raster;
    if (!checked(device.p->CreateRasterizerState(&raster_desc, &raster.p), "CreateRasterizer")) return false;
    const D3D11_VIEWPORT viewport = {0, 0, 1, 1, 0, 1};
    context.p->RSSetState(raster.p);
    context.p->RSSetViewports(1, &viewport);
    context.p->OMSetRenderTargets(1, &rtv.p, nullptr);
    context.p->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    context.p->VSSetShader(vs.p, nullptr, 0);
    context.p->PSSetConstantBuffers(0, 1, &params.p);

    const float directions[6][3] = {{1,0,0}, {-1,0,0}, {0,1,0}, {0,-1,0}, {0,0,1}, {0,0,-1}};
    struct CubeIndex { int requested; unsigned expected_clamped; };
    // Array coordinates clamp to the view's eight cubes, not its 48 faces.
    // No GetDimensions/resinfo.z interpretation is needed for these expectations.
    const CubeIndex cubes[] = {{-1, 0}, {0, 0}, {1, 1}, {7, 7}, {8, 7}, {9, 7}, {47, 7}};
    const float offsets[] = {-0.51f, -0.49f, 0, 0.49f, 0.51f};
    const D3D11_FILTER filters[] = {D3D11_FILTER_COMPARISON_MIN_MAG_MIP_POINT,
                                    D3D11_FILTER_COMPARISON_MIN_MAG_LINEAR_MIP_POINT};
    const D3D11_COMPARISON_FUNC functions[] = {D3D11_COMPARISON_LESS, D3D11_COMPARISON_LESS_EQUAL};

    auto observe = [&](ID3D11ShaderResourceView *view, ID3D11PixelShader *shader,
                       const float *values, const char *kind, int cube_index, int expected_clamped_cube,
                       unsigned face, unsigned bits, unsigned filter, unsigned function,
                       float offset) -> bool {
        if ((DWORD)(GetTickCount() - started) >= 60000) {
            log_line("COLOR-PROBE FAIL overall-budget-exceeded\n"); return false;
        }
        context.p->PSSetShaderResources(0, 1, &view);
        context.p->UpdateSubresource(params.p, 0, nullptr, values, 0, 0);
        context.p->PSSetShader(shader, nullptr, 0);
        const float poison[4] = {-123, -123, -123, -123};
        context.p->ClearRenderTargetView(rtv.p, poison);
        context.p->Draw(3, 0);
        context.p->CopyResource(staging.p, color.p);
        context.p->Flush();
        D3D11_MAPPED_SUBRESOURCE mapped = {};
        HRESULT hr = bounded_map(context.p, staging.p, &mapped);
        if (!checked(hr, "MapR32Observation")) return false;
        if (!mapped.pData || mapped.RowPitch < sizeof(float)) {
            context.p->Unmap(staging.p, 0);
            log_line("COLOR-PROBE FAIL invalid-output-layout\n"); return false;
        }
        float result;
        memcpy(&result, mapped.pData, sizeof(result));
        context.p->Unmap(staging.p, 0);
        log_line("COLOR-PROBE OBS kind=%s filter=%02x func=%u cube=%d expected-clamped-cube=%d face=%u bits=%u coord=%.3g,%.3g,%.3g,%.3g offset-lsb=%.3g ref=%.9g value=%.9g\n",
                 kind, filter, function, cube_index, expected_clamped_cube, face, bits, values[0], values[1], values[2], values[3],
                 offset, values[4], result);
        if (!std::isfinite(result) || result < 0 || result > 1) {
            log_line("COLOR-PROBE FAIL output-not-in-zero-one-range\n"); return false;
        }
        ++observations;
        return true;
    };

    // Noncomparison samples independently observe the uploaded cube/view data.
    D3D11_SAMPLER_DESC sample_desc = {};
    sample_desc.Filter = D3D11_FILTER_MIN_MAG_MIP_POINT;
    sample_desc.AddressU = sample_desc.AddressV = sample_desc.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    sample_desc.MaxAnisotropy = 1;
    sample_desc.ComparisonFunc = D3D11_COMPARISON_NEVER;
    sample_desc.MaxLOD = D3D11_FLOAT32_MAX;
    Owned<ID3D11SamplerState> ordinary_sampler;
    if (!checked(device.p->CreateSamplerState(&sample_desc, &ordinary_sampler.p), "CreateOrdinarySampler")) return false;
    context.p->PSSetSamplers(0, 1, &ordinary_sampler.p);
    // Retain the 18 in-range observations and add 24 OOB array-index controls.
    for (const CubeIndex &cube_index : cubes) for (unsigned face = 0; face < 6; ++face) {
        const unsigned bits = 4096 + cube_index.expected_clamped * 4096 + face * 257;
        const float values[8] = {directions[face][0], directions[face][1], directions[face][2],
                                 (float)cube_index.requested, (float)bits / 65535.0f, 0, 0, 0};
        const char *kind = cube_index.requested < 0 || cube_index.requested > 7 ? "cube-sample-oob" : "cube-sample";
        if (!observe(cube_srv.p, ps[2].p, values, kind, cube_index.requested, (int)cube_index.expected_clamped,
                     face, bits, 0, 0, 0)) return false;
    }

    for (unsigned filter = 0; filter < 2; ++filter) for (unsigned function = 0; function < 2; ++function) {
        D3D11_SAMPLER_DESC sampler_desc = sample_desc;
        sampler_desc.Filter = filters[filter];
        sampler_desc.ComparisonFunc = functions[function];
        Owned<ID3D11SamplerState> sampler;
        if (!checked(device.p->CreateSamplerState(&sampler_desc, &sampler.p), "CreateComparisonSampler")) return false;
        context.p->PSSetSamplers(0, 1, &sampler.p);
        for (const CubeIndex &cube_index : cubes) for (unsigned face = 0; face < 6; ++face) for (float offset : offsets) {
            const unsigned bits = 4096 + cube_index.expected_clamped * 4096 + face * 257;
            const float ref = (float)bits / 65535.0f + offset / 65535.0f;
            const float values[8] = {directions[face][0], directions[face][1], directions[face][2],
                                     (float)cube_index.requested, ref, 0, 0, 0};
            if (!observe(cube_srv.p, ps[0].p, values, "cube-compare", cube_index.requested,
                         (int)cube_index.expected_clamped, face, bits,
                         (unsigned)filters[filter], (unsigned)functions[function], offset)) return false;
        }
        for (float offset : offsets) {
            const float ref = (float)control_bits / 65535.0f + offset / 65535.0f;
            const float values[8] = {0.5f, 0.5f, 0, 0, ref, 0, 0, 0};
            if (!observe(color2d_srv.p, ps[1].p, values, "color2d-compare", 0, -1, 0, control_bits,
                         (unsigned)filters[filter], (unsigned)functions[function], offset) ||
                !observe(depth2d_srv.p, ps[1].p, values, "depth2d-compare", 0, -1, 0, control_bits,
                         (unsigned)filters[filter], (unsigned)functions[function], offset)) return false;
        }
    }
    context.p->ClearState();
    return checked(device.p->GetDeviceRemovedReason(), "GetDeviceRemovedReason");
}

int main()
{
    started = GetTickCount();
    if (!CreateDirectoryA("C:\\madeira-shadow-trace-20261013", nullptr) && GetLastError() != ERROR_ALREADY_EXISTS)
        log_line("COLOR-PROBE log-directory-unavailable error=%lu\n", GetLastError());
    log_file = fopen("C:\\madeira-shadow-trace-20261013\\d3d11-color-comparison.txt", "wb");
    if (!log_file) log_line("COLOR-PROBE file-log-unavailable\n");
    log_line("COLOR-PROBE BEGIN version=2 expected-observations=922 raw-observations-only hardware-required\n");
    const bool ok = run();
    log_line("COLOR-PROBE END status=%s observations=%u elapsed-ms=%lu\n",
             ok ? "OBSERVATIONS-COMPLETE" : "FAIL", observations, (DWORD)(GetTickCount() - started));
    if (log_file) fclose(log_file);
    return ok ? 0 : 1;
}
