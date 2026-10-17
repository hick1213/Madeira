/* Actual GPU observations, not a host simulation. Build with pinned LLVM MinGW:
 * x86_64-w64-mingw32-clang++ -std=c++17 -O2 -Wall -Wextra -Werror
 *   -static d3d11-depth-comparison-probe.cpp -ld3d11 -ld3dcompiler_47 -ldxgi
 * This exercises Wine/FEX/DXMT on-device. Exit 0 means observations were read;
 * it does NOT mean every D3D comparison contract passed. No WARP fallback.
 * Log: C:\madeira-shadow-trace-20261013\d3d11-depth-comparison.txt
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
    log_line("DEPTH-PROBE API operation=%s hr=%08lx\n", operation, (unsigned long)hr);
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
    HRESULT hr = D3DCompile(source, strlen(source), "madeira-depth-probe", nullptr,
                           nullptr, "main", target, D3DCOMPILE_ENABLE_STRICTNESS, 0,
                           blob, &errors.p);
    // Shader source is fixed and contains no credentials or user data.
    if (errors.p) log_line("DEPTH-PROBE shader-message=%.*s\n",
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
    log_line("DEPTH-PROBE device feature-level=%04x\n", (unsigned)level);
    Owned<IDXGIDevice> dxgi;
    Owned<IDXGIAdapter> adapter;
    if (!checked(device.p->QueryInterface(__uuidof(IDXGIDevice), (void **)&dxgi.p), "QueryInterface-DXGI") ||
        !checked(dxgi.p->GetAdapter(&adapter.p), "GetAdapter")) return false;
    DXGI_ADAPTER_DESC adapter_desc = {};
    if (!checked(adapter.p->GetDesc(&adapter_desc), "GetAdapterDesc")) return false;
    char adapter_name[256] = {};
    WideCharToMultiByte(CP_UTF8, 0, adapter_desc.Description, -1, adapter_name, sizeof(adapter_name), nullptr, nullptr);
    log_line("DEPTH-PROBE adapter name=%s vendor=%04x device=%04x\n", adapter_name,
             adapter_desc.VendorId, adapter_desc.DeviceId);

    D3D11_TEXTURE2D_DESC depth_desc = {};
    depth_desc.Width = depth_desc.Height = 4;
    depth_desc.MipLevels = depth_desc.ArraySize = depth_desc.SampleDesc.Count = 1;
    depth_desc.Format = DXGI_FORMAT_R16_TYPELESS;
    depth_desc.Usage = D3D11_USAGE_DEFAULT;
    depth_desc.BindFlags = D3D11_BIND_DEPTH_STENCIL | D3D11_BIND_SHADER_RESOURCE;
    Owned<ID3D11Texture2D> depth;
    Owned<ID3D11DepthStencilView> dsv;
    Owned<ID3D11ShaderResourceView> srv;
    if (!checked(device.p->CreateTexture2D(&depth_desc, nullptr, &depth.p), "CreateDepthTexture")) return false;
    D3D11_DEPTH_STENCIL_VIEW_DESC dsv_desc = {};
    dsv_desc.Format = DXGI_FORMAT_D16_UNORM;
    dsv_desc.ViewDimension = D3D11_DSV_DIMENSION_TEXTURE2D;
    D3D11_SHADER_RESOURCE_VIEW_DESC srv_desc = {};
    srv_desc.Format = DXGI_FORMAT_R16_UNORM;
    srv_desc.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D;
    srv_desc.Texture2D.MipLevels = 1;
    if (!checked(device.p->CreateDepthStencilView(depth.p, &dsv_desc, &dsv.p), "CreateD16DSV") ||
        !checked(device.p->CreateShaderResourceView(depth.p, &srv_desc, &srv.p), "CreateR16SRV")) return false;
    const uint16_t requested_bits = 16384;
    const float q = (float)requested_bits / 65535.0f;
    context.p->ClearDepthStencilView(dsv.p, D3D11_CLEAR_DEPTH, q, 0);
    log_line("DEPTH-PROBE clear requested-bits=%u q=%.9g lsb=%.9g\n", requested_bits, q, 1.0f / 65535.0f);

    // Some Metal backends cannot map depth storage. Report that explicitly.
    D3D11_TEXTURE2D_DESC raw_desc = depth_desc;
    raw_desc.Usage = D3D11_USAGE_STAGING;
    raw_desc.BindFlags = 0;
    raw_desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    Owned<ID3D11Texture2D> raw;
    HRESULT raw_hr = device.p->CreateTexture2D(&raw_desc, nullptr, &raw.p);
    log_line("DEPTH-PROBE API operation=CreateRawDepthStaging hr=%08lx\n", (unsigned long)raw_hr);
    if (SUCCEEDED(raw_hr)) {
        context.p->CopyResource(raw.p, depth.p);
        context.p->Flush();
        D3D11_MAPPED_SUBRESOURCE mapped = {};
        raw_hr = bounded_map(context.p, raw.p, &mapped);
        log_line("DEPTH-PROBE API operation=MapRawDepth hr=%08lx\n", (unsigned long)raw_hr);
        if (SUCCEEDED(raw_hr)) {
            if (mapped.pData && mapped.RowPitch >= 8) {
                uint16_t first, last;
                memcpy(&first, mapped.pData, sizeof(first));
                memcpy(&last, (const char *)mapped.pData + 3 * mapped.RowPitch + 6, sizeof(last));
                log_line("DEPTH-PROBE RAW first=%u last=%u pitch=%u\n", first, last, mapped.RowPitch);
            } else log_line("DEPTH-PROBE SKIP raw-depth-invalid-mapped-layout\n");
            context.p->Unmap(raw.p, 0);
        } else log_line("DEPTH-PROBE SKIP raw-depth-map-unavailable\n");
    } else log_line("DEPTH-PROBE SKIP raw-depth-staging-unavailable\n");

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
        "Texture2D<float> depth : register(t0); SamplerComparisonState cmp : register(s0);"
        "cbuffer Params : register(b0) {float2 uv; float ref; float pad;}"
        "float main() : SV_Target {return depth.SampleCmpLevelZero(cmp,uv,ref);}",
        "Texture2D<float> depth : register(t0); SamplerComparisonState cmp : register(s0);"
        "cbuffer Params : register(b0) {float2 uv; float ref; float pad;}"
        "float main() : SV_Target {return depth.SampleCmp(cmp,uv,ref);}"
    };
    Owned<ID3DBlob> vs_code, ps_code[2];
    Owned<ID3D11VertexShader> vs;
    Owned<ID3D11PixelShader> ps[2];
    if (!compile(vs_source, "vs_5_0", &vs_code.p) ||
        !checked(device.p->CreateVertexShader(vs_code.p->GetBufferPointer(), vs_code.p->GetBufferSize(), nullptr, &vs.p),
                 "CreateVertexShader")) return false;
    for (unsigned i = 0; i < 2; ++i) {
        if (!compile(ps_sources[i], "ps_5_0", &ps_code[i].p) ||
            !checked(device.p->CreatePixelShader(ps_code[i].p->GetBufferPointer(), ps_code[i].p->GetBufferSize(), nullptr, &ps[i].p),
                     "CreatePixelShader")) return false;
    }
    D3D11_BUFFER_DESC buffer_desc = {};
    buffer_desc.ByteWidth = 16;
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
    context.p->PSSetShaderResources(0, 1, &srv.p);
    context.p->PSSetConstantBuffers(0, 1, &params.p);

    const float refs[] = {0, q - 0.51f / 65535.0f, q - 0.49f / 65535.0f, q,
                          q + 0.49f / 65535.0f, q + 0.51f / 65535.0f, 1, -0.01f, 1.01f};
    const char *names[] = {"zero", "q-minus-.51lsb", "q-minus-.49lsb", "q",
                           "q-plus-.49lsb", "q-plus-.51lsb", "one", "negative", "above-one"};
    const D3D11_FILTER filters[] = {D3D11_FILTER_COMPARISON_MIN_MAG_MIP_POINT,
                                    D3D11_FILTER_COMPARISON_MIN_MAG_LINEAR_MIP_POINT};
    for (unsigned filter = 0; filter < 2; ++filter) for (unsigned border = 0; border < 2; ++border) {
        D3D11_SAMPLER_DESC sampler_desc = {};
        sampler_desc.Filter = filters[filter];
        sampler_desc.AddressU = sampler_desc.AddressV = sampler_desc.AddressW = D3D11_TEXTURE_ADDRESS_BORDER;
        sampler_desc.ComparisonFunc = D3D11_COMPARISON_LESS_EQUAL;
        sampler_desc.MaxAnisotropy = 1;
        sampler_desc.MaxLOD = D3D11_FLOAT32_MAX;
        for (float &component : sampler_desc.BorderColor) component = (float)border;
        Owned<ID3D11SamplerState> sampler;
        if (!checked(device.p->CreateSamplerState(&sampler_desc, &sampler.p), "CreateComparisonSampler")) return false;
        context.p->PSSetSamplers(0, 1, &sampler.p);
        for (unsigned shader = 0; shader < 2; ++shader) for (unsigned outside = 0; outside < 2; ++outside)
            for (unsigned r = 0; r < sizeof(refs) / sizeof(refs[0]); ++r) {
                if ((DWORD)(GetTickCount() - started) >= 60000) {
                    log_line("DEPTH-PROBE FAIL overall-budget-exceeded\n"); return false;
                }
                const float values[4] = {outside ? -2.0f : 0.5f, outside ? -2.0f : 0.5f, refs[r], 0};
                context.p->UpdateSubresource(params.p, 0, nullptr, values, 0, 0);
                context.p->PSSetShader(ps[shader].p, nullptr, 0);
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
                    log_line("DEPTH-PROBE FAIL invalid-output-layout\n"); return false;
                }
                float result;
                memcpy(&result, mapped.pData, sizeof(result));
                context.p->Unmap(staging.p, 0);
                log_line("DEPTH-PROBE OBS shader=%s filter=%02x border=%u outside=%u case=%s ref=%.9g value=%.9g\n",
                         shader ? "SampleCmp" : "SampleCmpLevelZero", (unsigned)filters[filter], border,
                         outside, names[r], refs[r], result);
                if (!std::isfinite(result) || result < 0 || result > 1) {
                    log_line("DEPTH-PROBE FAIL comparison-output-not-in-zero-one-range\n"); return false;
                }
                ++observations;
            }
    }
    context.p->ClearState();
    return checked(device.p->GetDeviceRemovedReason(), "GetDeviceRemovedReason");
}

int main()
{
    started = GetTickCount();
    if (!CreateDirectoryA("C:\\madeira-shadow-trace-20261013", nullptr) && GetLastError() != ERROR_ALREADY_EXISTS)
        log_line("DEPTH-PROBE log-directory-unavailable error=%lu\n", GetLastError());
    log_file = fopen("C:\\madeira-shadow-trace-20261013\\d3d11-depth-comparison.txt", "wb");
    if (!log_file) log_line("DEPTH-PROBE file-log-unavailable\n");
    log_line("DEPTH-PROBE BEGIN version=1 raw-observations-only hardware-required\n");
    const bool ok = run();
    log_line("DEPTH-PROBE END status=%s observations=%u elapsed-ms=%lu\n",
             ok ? "OBSERVATIONS-COMPLETE" : "FAIL", observations, (DWORD)(GetTickCount() - started));
    if (log_file) fclose(log_file);
    return ok ? 0 : 1;
}
