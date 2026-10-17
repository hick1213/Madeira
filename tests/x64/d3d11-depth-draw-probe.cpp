/* Standalone hardware D3D11 draw -> depth SRV probe. No WARP fallback.
 * Build on APFS with the repository's LLVM MinGW:
 * x86_64-w64-mingw32-clang++ -std=c++17 -O2 -Wall -Wextra -Werror -static
 *   d3d11-depth-draw-probe.cpp -ld3d11 -ld3dcompiler_47 -ldxgi -o probe.exe
 * Host oracle check: clang++ -std=c++17 -DDEPTH_DRAW_ORACLE_ONLY this.cpp
 * Exit: 0 = all checks passed; 1 = incomplete/API/I/O failure; 2 = mismatches.
 * Unique text + raw float32 atlas files: C:\madeira-shadow-trace-20261013.
 * CPU oracle checks do not establish hardware or device correctness.
 */
#ifndef DEPTH_DRAW_ORACLE_ONLY
#define _WIN32_WINNT 0x0a00
#endif
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <limits>

static constexpr unsigned side = 32;
#ifndef DEPTH_DRAW_ORACLE_ONLY
static constexpr unsigned planes = 4, frames = 32, batches = 12;
static constexpr unsigned atlas_width = side * planes, atlas_height = side * frames;
static constexpr float bias_scales[] = {0.0f, 0.1f, 1.0f};
static constexpr unsigned depth_slots[] = {10, 2}; // GuJian D16 cascade / D32 slot.
#endif
static constexpr float reference = 0.5f;
static constexpr float bias_clamp = -10.1f;

// DepthBias is zero for both formats. Microsoft D3D11 depth-bias definition:
// https://learn.microsoft.com/en-us/windows/win32/direct3d11/d3d10-graphics-programming-guide-output-merger-stage-depth-bias
// A negative clamp is a LOWER bound. MaxDepthSlope is abs(dz/dx)=0.25/32.
static float expected_depth(unsigned x, unsigned y, unsigned pattern, bool alpha, float scale)
{
    if (alpha && (((x / 4) + (y / 4) + pattern) & 1))
        return pattern ? 0.0625f : 0.9375f;
    const float ramp = (x + 0.5f) / 128.0f + (y + 0.5f) / 256.0f;
    const float z = pattern ? 0.75f - ramp : 0.25f + ramp;
    return z + std::max(bias_clamp, scale / 128.0f);
}

static bool valid_depth(float value)
{
    return std::isfinite(value) && value >= 0 && value <= 1;
}

// Raw Load is in the first 32 columns of this frame's atlas row group.
// Quarter-texel offsets have exactly representable bilinear weights. Clamp U/V
// repeats the edge texel. Compare reference AGAINST depth, before filtering.
static float expected_comparison(const float *raw, unsigned stride, unsigned x,
                                 unsigned y, bool shifted)
{
    const unsigned nx = shifted ? std::min(x + 1, side - 1) : x;
    const unsigned ny = shifted ? std::min(y + 1, side - 1) : y;
    const float values[] = {raw[y * stride + x], raw[y * stride + nx],
                            raw[ny * stride + x], raw[ny * stride + nx]};
    float result = 0;
    const float weights[] = {0.5625f, 0.1875f, 0.1875f, 0.0625f};
    for (unsigned i = 0; i < 4; ++i) {
        if (!valid_depth(values[i])) return std::numeric_limits<float>::quiet_NaN();
        result += (reference >= values[i] ? 1.0f : 0.0f) * weights[i];
    }
    return result;
}

static bool oracle_self_test()
{
    // Hand-derived literals, independent of the HLSL implementation.
    if (expected_depth(0, 0, 0, false, 0) != 0.255859375f ||
        expected_depth(0, 0, 1, false, 0) != 0.744140625f ||
        expected_depth(0, 0, 0, false, 1) != 0.263671875f ||
        std::fabs(expected_depth(0, 0, 0, false, 0.1f) - 0.256640625f) > 0.0000001f ||
        expected_depth(4, 0, 0, true, 1) != 0.9375f ||
        expected_depth(0, 0, 1, true, 0) != 0.0625f ||
        expected_depth(4, 0, 1, true, 0) != 0.712890625f) return false;
    float raw[side * side];
    std::fill(raw, raw + side * side, 1.0f);
    raw[0] = 0.25f; raw[1] = 0.75f; raw[side] = 0.75f; raw[side + 1] = 0.25f;
    raw[side * side - 1] = 0.25f;
    if (expected_comparison(raw, side, 0, 0, false) != 1.0f ||
        expected_comparison(raw, side, 1, 0, false) != 0.0f ||
        expected_comparison(raw, side, 0, 0, true) != 0.625f ||
        expected_comparison(raw, side, 31, 31, true) != 1.0f) return false;
    raw[0] = std::numeric_limits<float>::quiet_NaN();
    return std::isnan(expected_comparison(raw, side, 0, 0, true));
}

#ifdef DEPTH_DRAW_ORACLE_ONLY
int main()
{
    const bool ok = oracle_self_test();
    std::printf("DEPTH-DRAW HOST-ORACLE status=%s hardware-not-tested\n", ok ? "PASS" : "FAIL");
    return ok ? 0 : 1;
}
#else
#include <windows.h>
#include <d3d11.h>
#include <d3dcompiler.h>
#include <cstdarg>
#include <vector>

static FILE *log_file, *raw_file;
static bool io_ok = true;
static DWORD started;
static unsigned completed_batches, completed_frames, depth_checks, compare_checks;
static unsigned depth_failures, compare_failures, compare_skipped;

static void log_line(const char *format, ...)
{
    char line[1024];
    va_list args;
    va_start(args, format);
    const int size = vsnprintf(line, sizeof(line), format, args);
    va_end(args);
    if (size < 0 || (unsigned)size >= sizeof(line)) io_ok = false;
    OutputDebugStringA(line);
    fputs(line, stderr);
    fflush(stderr);
    if (log_file && (fputs(line, log_file) < 0 || fflush(log_file))) io_ok = false;
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
    log_line("DEPTH-DRAW API operation=%s hr=%08lx\n", operation, (unsigned long)hr);
    return SUCCEEDED(hr);
}

static bool compile(const char *source, const char *entry, const char *target, ID3DBlob **blob,
                    const D3D_SHADER_MACRO *defines = nullptr)
{
    Owned<ID3DBlob> errors;
    HRESULT hr = D3DCompile(source, strlen(source), "madeira-depth-draw-probe", defines,
                           nullptr, entry, target, D3DCOMPILE_ENABLE_STRICTNESS, 0, blob, &errors.p);
    if (errors.p) log_line("DEPTH-DRAW shader-message=%.*s\n",
        (int)std::min<size_t>(errors.p->GetBufferSize(), 700), (const char *)errors.p->GetBufferPointer());
    return checked(hr, entry);
}

static bool within_budget()
{
    if ((DWORD)(GetTickCount() - started) < 120000) return true;
    log_line("DEPTH-DRAW FAIL overall-budget-exceeded\n");
    return false;
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
    } while ((DWORD)(GetTickCount() - begin) < 10000 && within_budget());
    return hr;
}

static bool inspect_batch(ID3D11DeviceContext *context, ID3D11Texture2D *staging,
                          unsigned format, unsigned alpha, unsigned bias_index)
{
    D3D11_MAPPED_SUBRESOURCE mapped = {};
    if (!checked(bounded_map(context, staging, &mapped), "MapAtlasAfterBatch")) return false;
    if (!mapped.pData || mapped.RowPitch < atlas_width * sizeof(float)) {
        context->Unmap(staging, 0);
        log_line("DEPTH-DRAW FAIL invalid-atlas-layout\n");
        return false;
    }
    std::vector<float> atlas(atlas_width * atlas_height);
    for (unsigned y = 0; y < atlas_height; ++y) {
        const void *row = (const char *)mapped.pData + y * mapped.RowPitch;
        memcpy(atlas.data() + y * atlas_width, row, atlas_width * sizeof(float));
    }
    context->Unmap(staging, 0);
    if (fwrite(atlas.data(), sizeof(float), atlas.size(), raw_file) != atlas.size() || fflush(raw_file)) {
        log_line("DEPTH-DRAW FAIL raw-file-write\n");
        return false;
    }
    const float tolerance = format ? 0.000001f : 1.0f / 65535.0f + 0.000001f;
    const unsigned previous_depth = depth_failures, previous_compare = compare_failures;
    const unsigned previous_skipped = compare_skipped;
    unsigned detailed_failures = 0;
    float max_depth_error = 0, max_compare_error = 0;
    const unsigned points[][2] = {{0, 0}, {3, 3}, {4, 3}, {7, 7}, {8, 7}, {15, 15}, {16, 15}, {31, 31}};
    for (unsigned frame = 0; frame < frames; ++frame) {
        const unsigned pattern = frame & 1;
        const float *raw = atlas.data() + frame * side * atlas_width;
        for (unsigned y = 0; y < side; ++y) for (unsigned x = 0; x < side; ++x) {
            const float observed = raw[y * atlas_width + x];
            const float expected = expected_depth(x, y, pattern, alpha != 0, bias_scales[bias_index]);
            const float error = std::fabs(observed - expected);
            ++depth_checks;
            if (std::isfinite(error)) max_depth_error = std::max(max_depth_error, error);
            if (!valid_depth(observed) || error > tolerance) {
                ++depth_failures;
                if (detailed_failures++ < 16) log_line(
                    "DEPTH-DRAW MISMATCH kind=write frame=%u xy=%u,%u expected=%.9g actual=%.9g tolerance=%.9g\n",
                    frame, x, y, expected, observed, tolerance);
            }
            for (unsigned plane = 1; plane < planes; ++plane) {
                const float value = raw[y * atlas_width + plane * side + x];
                const float expected_cmp = expected_comparison(raw, atlas_width, x, y, plane != 1);
                if (!std::isfinite(expected_cmp)) { ++compare_skipped; continue; }
                ++compare_checks;
                const float compare_error = std::fabs(value - expected_cmp);
                if (std::isfinite(compare_error)) max_compare_error = std::max(max_compare_error, compare_error);
                if (!valid_depth(value) || compare_error > 0.0001f) {
                    ++compare_failures;
                    if (detailed_failures++ < 16) log_line(
                        "DEPTH-DRAW MISMATCH kind=compare frame=%u plane=%u xy=%u,%u expected-from-load=%.9g actual=%.9g\n",
                        frame, plane, x, y, expected_cmp, value);
                }
            }
        }
        for (const auto &point : points) {
            const unsigned x = point[0], y = point[1];
            log_line("DEPTH-DRAW OBS batch=%u frame=%u pattern=%u xy=%u,%u raw=%.9g expected-depth=%.9g cmp-center=%.9g expected-center=%.9g cmp-shift-lz=%.9g cmp-shift=%.9g expected-shift=%.9g\n",
                completed_batches, frame, pattern, x, y, raw[y * atlas_width + x],
                expected_depth(x, y, pattern, alpha != 0, bias_scales[bias_index]),
                raw[y * atlas_width + side + x], expected_comparison(raw, atlas_width, x, y, false),
                raw[y * atlas_width + 2 * side + x], raw[y * atlas_width + 3 * side + x],
                expected_comparison(raw, atlas_width, x, y, true));
        }
        ++completed_frames;
    }
    log_line("DEPTH-DRAW BATCH-END batch=%u depth-failures=%u compare-failures=%u compare-skipped=%u max-depth-error=%.9g max-compare-error=%.9g\n",
        completed_batches, depth_failures - previous_depth, compare_failures - previous_compare,
        compare_skipped - previous_skipped, max_depth_error, max_compare_error);
    ++completed_batches;
    return io_ok;
}

static bool run()
{
    Owned<ID3D11Device> device;
    Owned<ID3D11DeviceContext> context;
    D3D_FEATURE_LEVEL level;
    const D3D_FEATURE_LEVEL levels[] = {D3D_FEATURE_LEVEL_11_0};
    if (!checked(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0,
        levels, 1, D3D11_SDK_VERSION, &device.p, &level, &context.p), "D3D11CreateDevice-hardware")) return false;
    Owned<IDXGIDevice> dxgi;
    Owned<IDXGIAdapter> adapter;
    if (!checked(device.p->QueryInterface(__uuidof(IDXGIDevice), (void **)&dxgi.p), "QueryInterface-DXGI") ||
        !checked(dxgi.p->GetAdapter(&adapter.p), "GetAdapter")) return false;
    DXGI_ADAPTER_DESC adapter_desc = {};
    if (!checked(adapter.p->GetDesc(&adapter_desc), "GetAdapterDesc")) return false;
    char adapter_name[256] = {};
    WideCharToMultiByte(CP_UTF8, 0, adapter_desc.Description, -1, adapter_name, sizeof(adapter_name), nullptr, nullptr);
    log_line("DEPTH-DRAW DEVICE feature-level=%04x adapter=%s vendor=%04x device=%04x\n",
        (unsigned)level, adapter_name, adapter_desc.VendorId, adapter_desc.DeviceId);

    // Oversized triangle covers every 32x32 pixel center, away from edge rules.
    // Two immutable constant buffers select opposite depth patterns. VertexID
    // excludes StartVertexLocation, so Draw's start offset cannot select them.
    const char *writer_source = R"(
cbuffer Pattern : register(b0) { uint pattern; uint3 padding; };
struct V { float4 p : SV_Position; nointerpolation uint pattern : TEXCOORD0; };
V vs(uint id : SV_VertexID) {
    uint vertex = id % 3;
    float2 uv = vertex == 0 ? float2(0,0) : (vertex == 1 ? float2(2,0) : float2(0,2));
    float ramp = uv.x * 0.25 + uv.y * 0.125;
    V o; o.p = float4(uv.x * 2 - 1, 1 - uv.y * 2, pattern ? 0.75 - ramp : 0.25 + ramp, 1);
    o.pattern = pattern; return o;
}
void empty_ps(V i) { }
void discard_ps(V i) {
    uint2 pixel = uint2(i.p.xy);
    if (((pixel.x / 4 + pixel.y / 4 + i.pattern) & 1) != 0) discard;
}
 )";
    const char *reader_source = R"(
Texture2D<float> depth : register(DEPTH_SLOT);
SamplerComparisonState compare_sampler : register(s0);
float read_ps(float4 position : SV_Position) : SV_Target {
    uint plane = uint(position.x) / 32;
    uint2 pixel = uint2(position.xy) % 32;
    if (plane == 0) return depth.Load(int3(pixel, 0));
    float2 uv = (float2(pixel) + 0.5 + (plane > 1 ? 0.25 : 0.0)) / 32.0;
    if (plane == 3) return depth.SampleCmp(compare_sampler, uv, 0.5);
    return depth.SampleCmpLevelZero(compare_sampler, uv, 0.5);
}
 )";
    Owned<ID3DBlob> vs_code, empty_code, discard_code;
    Owned<ID3D11VertexShader> vs;
    Owned<ID3D11PixelShader> writer_ps[2], reader_ps[2];
    if (!compile(writer_source, "vs", "vs_5_0", &vs_code.p) ||
        !compile(writer_source, "empty_ps", "ps_5_0", &empty_code.p) ||
        !compile(writer_source, "discard_ps", "ps_5_0", &discard_code.p) ||
        !checked(device.p->CreateVertexShader(vs_code.p->GetBufferPointer(), vs_code.p->GetBufferSize(), nullptr, &vs.p), "CreateVS") ||
        !checked(device.p->CreatePixelShader(empty_code.p->GetBufferPointer(), empty_code.p->GetBufferSize(), nullptr, &writer_ps[0].p), "CreateEmptyPS") ||
        !checked(device.p->CreatePixelShader(discard_code.p->GetBufferPointer(), discard_code.p->GetBufferSize(), nullptr, &writer_ps[1].p), "CreateDiscardPS")) return false;
    for (unsigned format = 0; format < 2; ++format) {
        Owned<ID3DBlob> reader_code;
        const D3D_SHADER_MACRO defines[] = {{"DEPTH_SLOT", format ? "t2" : "t10"}, {nullptr, nullptr}};
        if (!compile(reader_source, "read_ps", "ps_5_0", &reader_code.p, defines) ||
            !checked(device.p->CreatePixelShader(reader_code.p->GetBufferPointer(), reader_code.p->GetBufferSize(),
                 nullptr, &reader_ps[format].p), "CreateReaderPS")) return false;
    }

    D3D11_TEXTURE2D_DESC output_desc = {};
    output_desc.Width = atlas_width; output_desc.Height = atlas_height;
    output_desc.MipLevels = output_desc.ArraySize = output_desc.SampleDesc.Count = 1;
    output_desc.Format = DXGI_FORMAT_R32_FLOAT;
    output_desc.Usage = D3D11_USAGE_DEFAULT;
    output_desc.BindFlags = D3D11_BIND_RENDER_TARGET;
    Owned<ID3D11Texture2D> output, staging;
    Owned<ID3D11RenderTargetView> rtv;
    if (!checked(device.p->CreateTexture2D(&output_desc, nullptr, &output.p), "CreateR32Atlas") ||
        !checked(device.p->CreateRenderTargetView(output.p, nullptr, &rtv.p), "CreateR32AtlasRTV")) return false;
    output_desc.Usage = D3D11_USAGE_STAGING; output_desc.BindFlags = 0;
    output_desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    if (!checked(device.p->CreateTexture2D(&output_desc, nullptr, &staging.p), "CreateR32Staging")) return false;

    Owned<ID3D11RasterizerState> raster[3];
    for (unsigned i = 0; i < 3; ++i) {
        D3D11_RASTERIZER_DESC desc = {};
        desc.FillMode = D3D11_FILL_SOLID; desc.CullMode = D3D11_CULL_NONE;
        desc.DepthClipEnable = TRUE; desc.SlopeScaledDepthBias = bias_scales[i];
        desc.DepthBiasClamp = bias_clamp;
        if (!checked(device.p->CreateRasterizerState(&desc, &raster[i].p), "CreateRasterizer")) return false;
    }
    Owned<ID3D11DepthStencilState> depth_write, depth_off;
    D3D11_DEPTH_STENCIL_DESC state_desc = {};
    state_desc.DepthEnable = TRUE; state_desc.DepthWriteMask = D3D11_DEPTH_WRITE_MASK_ALL;
    state_desc.DepthFunc = D3D11_COMPARISON_ALWAYS;
    if (!checked(device.p->CreateDepthStencilState(&state_desc, &depth_write.p), "CreateDepthWriteState")) return false;
    state_desc.DepthEnable = FALSE; state_desc.DepthWriteMask = D3D11_DEPTH_WRITE_MASK_ZERO;
    if (!checked(device.p->CreateDepthStencilState(&state_desc, &depth_off.p), "CreateDepthOffState")) return false;
    D3D11_SAMPLER_DESC sampler_desc = {};
    sampler_desc.Filter = D3D11_FILTER_COMPARISON_MIN_MAG_LINEAR_MIP_POINT; // 148 / 0x94
    sampler_desc.AddressU = sampler_desc.AddressV = sampler_desc.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    sampler_desc.ComparisonFunc = D3D11_COMPARISON_GREATER_EQUAL; // 7
    sampler_desc.MaxAnisotropy = 1; sampler_desc.MaxLOD = D3D11_FLOAT32_MAX;
    Owned<ID3D11SamplerState> sampler;
    if (!checked(device.p->CreateSamplerState(&sampler_desc, &sampler.p), "CreateGEComparisonSampler")) return false;
    context.p->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    context.p->VSSetShader(vs.p, nullptr, 0);
    context.p->PSSetSamplers(0, 1, &sampler.p);
    Owned<ID3D11Buffer> patterns[2];
    for (unsigned pattern = 0; pattern < 2; ++pattern) {
        const unsigned values[4] = {pattern, 0, 0, 0};
        D3D11_BUFFER_DESC desc = {};
        desc.ByteWidth = sizeof(values); desc.Usage = D3D11_USAGE_IMMUTABLE;
        desc.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
        D3D11_SUBRESOURCE_DATA data = {}; data.pSysMem = values;
        if (!checked(device.p->CreateBuffer(&desc, &data, &patterns[pattern].p), "CreateImmutablePattern")) return false;
    }

    for (unsigned format = 0; format < 2; ++format) {
        D3D11_TEXTURE2D_DESC desc = {};
        desc.Width = desc.Height = side; desc.MipLevels = desc.ArraySize = desc.SampleDesc.Count = 1;
        desc.Format = format ? DXGI_FORMAT_R32_TYPELESS : DXGI_FORMAT_R16_TYPELESS;
        desc.Usage = D3D11_USAGE_DEFAULT;
        desc.BindFlags = D3D11_BIND_DEPTH_STENCIL | D3D11_BIND_SHADER_RESOURCE;
        Owned<ID3D11Texture2D> depth;
        Owned<ID3D11DepthStencilView> dsv;
        Owned<ID3D11ShaderResourceView> srv;
        D3D11_DEPTH_STENCIL_VIEW_DESC dd = {};
        dd.Format = format ? DXGI_FORMAT_D32_FLOAT : DXGI_FORMAT_D16_UNORM;
        dd.ViewDimension = D3D11_DSV_DIMENSION_TEXTURE2D;
        D3D11_SHADER_RESOURCE_VIEW_DESC sd = {};
        sd.Format = format ? DXGI_FORMAT_R32_FLOAT : DXGI_FORMAT_R16_UNORM;
        sd.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D; sd.Texture2D.MipLevels = 1;
        if (!checked(device.p->CreateTexture2D(&desc, nullptr, &depth.p), "CreateDepthTexture") ||
            !checked(device.p->CreateDepthStencilView(depth.p, &dd, &dsv.p), "CreateDSV") ||
            !checked(device.p->CreateShaderResourceView(depth.p, &sd, &srv.p), "CreateDepthSRV")) return false;
        for (unsigned alpha = 0; alpha < 2; ++alpha) for (unsigned bias = 0; bias < 3; ++bias) {
            log_line("DEPTH-DRAW BATCH-BEGIN batch=%u format=%s writer=%s slope-bias=%.9g clamp=%.9g depth-bias=0 frames=%u slot=t%u filter=148 func=7 address=clamp reference=%.9g\n",
                completed_batches, format ? "D32_FLOAT" : "D16_UNORM", alpha ? "alpha-discard" : "empty",
                bias_scales[bias], bias_clamp, frames, depth_slots[format], reference);
            const float poison[4] = {-123, -123, -123, -123};
            context.p->ClearRenderTargetView(rtv.p, poison);
            for (unsigned frame = 0; frame < frames; ++frame) {
                if (!within_budget()) return false;
                const unsigned pattern = frame & 1;
                ID3D11ShaderResourceView *null_srv = nullptr;
                context.p->PSSetShaderResources(depth_slots[format], 1, &null_srv);
                context.p->OMSetRenderTargets(0, nullptr, dsv.p);
                context.p->ClearDepthStencilView(dsv.p, D3D11_CLEAR_DEPTH, pattern ? 0.0625f : 0.9375f, 0);
                context.p->RSSetState(raster[bias].p);
                context.p->OMSetDepthStencilState(depth_write.p, 0);
                D3D11_VIEWPORT viewport = {0, 0, (FLOAT)side, (FLOAT)side, 0, 1};
                context.p->RSSetViewports(1, &viewport);
                context.p->VSSetConstantBuffers(0, 1, &patterns[pattern].p);
                context.p->PSSetShader(writer_ps[alpha].p, nullptr, 0);
                context.p->Draw(3, 0);
                // Explicit DSV unbind precedes the same texture's SRV bind.
                context.p->OMSetRenderTargets(1, &rtv.p, nullptr);
                context.p->OMSetDepthStencilState(depth_off.p, 0);
                context.p->RSSetState(raster[0].p);
                viewport.Width = (FLOAT)atlas_width; viewport.TopLeftY = (FLOAT)(frame * side);
                context.p->RSSetViewports(1, &viewport);
                context.p->PSSetShaderResources(depth_slots[format], 1, &srv.p);
                context.p->PSSetShader(reader_ps[format].p, nullptr, 0);
                context.p->Draw(3, 0);
            }
            ID3D11ShaderResourceView *null_srv = nullptr;
            context.p->PSSetShaderResources(depth_slots[format], 1, &null_srv);
            context.p->OMSetRenderTargets(0, nullptr, nullptr);
            context.p->CopyResource(staging.p, output.p);
            // Deliberately only once after all 32 draws + samples, never per frame.
            context.p->Flush();
            if (!inspect_batch(context.p, staging.p, format, alpha, bias)) return false;
        }
    }
    context.p->ClearState();
    return checked(device.p->GetDeviceRemovedReason(), "GetDeviceRemovedReason");
}

int main()
{
    started = GetTickCount();
    FILETIME time; GetSystemTimeAsFileTime(&time);
    const unsigned long pid = GetCurrentProcessId();
    char stem[256], text_path[280], raw_path[280];
    snprintf(stem, sizeof(stem), "C:\\madeira-shadow-trace-20261013\\d3d11-depth-draw-%08lx%08lx-%lu",
        time.dwHighDateTime, time.dwLowDateTime, pid);
    snprintf(text_path, sizeof(text_path), "%s.txt", stem);
    snprintf(raw_path, sizeof(raw_path), "%s.f32", stem);
    if (!CreateDirectoryA("C:\\madeira-shadow-trace-20261013", nullptr) && GetLastError() != ERROR_ALREADY_EXISTS) {
        log_line("DEPTH-DRAW FAIL create-log-directory error=%lu\n", GetLastError()); return 1;
    }
    log_file = fopen(text_path, "wb"); raw_file = fopen(raw_path, "wb");
    if (!log_file || !raw_file) {
        log_line("DEPTH-DRAW FAIL open-output-files\n");
        if (log_file) fclose(log_file);
        if (raw_file) fclose(raw_file);
        return 1;
    }
    log_line("DEPTH-DRAW BEGIN version=1 run=%08lx%08lx-%lu hardware-required frames-per-batch=%u expected-batches=%u expected-depth-checks=%u expected-compare-checks=%u\n",
        time.dwHighDateTime, time.dwLowDateTime, pid, frames, batches, batches * frames * side * side,
        batches * frames * side * side * (planes - 1));
    log_line("DEPTH-DRAW FILE text=%s raw=%s\n", text_path, raw_path);
    log_line("DEPTH-DRAW RAW-LAYOUT header=8-u32-le magic=44524457 version=1 width=%u height=%u batches=%u frames=%u run-high-low data=float32-le-row-major planes=Load,CmpCenterLZ,CmpQuarterLZ,CmpQuarter batch-order=format-alpha-bias\n",
        atlas_width, atlas_height, batches, frames);
    const uint32_t header[] = {0x44524457, 1, atlas_width, atlas_height, batches, frames,
                              time.dwHighDateTime, time.dwLowDateTime};
    bool ok = fwrite(header, sizeof(header), 1, raw_file) == 1;
    if (!oracle_self_test()) { log_line("DEPTH-DRAW FAIL host-oracle-self-test\n"); ok = false; }
    if (ok) ok = run();
    if (fflush(raw_file)) io_ok = false;
    if (fclose(raw_file)) io_ok = false;
    raw_file = nullptr;
    const bool complete = ok && io_ok && completed_batches == batches && completed_frames == batches * frames &&
        depth_checks == batches * frames * side * side &&
        compare_checks + compare_skipped == batches * frames * side * side * (planes - 1);
    int status = complete ? ((depth_failures || compare_failures || compare_skipped) ? 2 : 0) : 1;
    log_line("DEPTH-DRAW END version=1 run=%08lx%08lx-%lu status=%s exit=%d batches=%u frames=%u depth-checks=%u depth-failures=%u compare-checks=%u compare-failures=%u compare-skipped=%u elapsed-ms=%lu\n",
        time.dwHighDateTime, time.dwLowDateTime, pid, status == 0 ? "PASS" : status == 2 ? "CHECKS-FAILED" : "INCOMPLETE",
        status, completed_batches, completed_frames, depth_checks, depth_failures, compare_checks,
        compare_failures, compare_skipped, (DWORD)(GetTickCount() - started));
    const int close_status = fclose(log_file);
    if (!io_ok || close_status) status = 1;
    return status;
}
#endif
