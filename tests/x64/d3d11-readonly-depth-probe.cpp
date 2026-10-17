/* V2 standalone D24 read-only depth attachment / ordinary sampling diagnostic.
 * Reader uses verified precompiled DXBC; VS/writer still use runtime D3DCompile.
 * Original runtime-reader v1 source/executable/evidence are archived separately.
 * LLVM MinGW: -std=c++17 -O2 -Wall -Wextra -Werror -static -ld3d11 -ld3dcompiler_47 -ldxgi
 * Host oracle: clang++ -std=c++17 -DREADONLY_DEPTH_ORACLE_ONLY this.cpp
 * Exit 0 = complete PASS, 1 = incomplete/API/I/O failure, 2 = complete mismatches.
 * This explores a GuJian hypothesis; it is neither a game fix nor device proof.
 */
#ifndef READONLY_DEPTH_ORACLE_ONLY
#define _WIN32_WINNT 0x0a00
#endif
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>

static constexpr unsigned side = 64;
#ifndef READONLY_DEPTH_ORACLE_ONLY
static constexpr unsigned frames = 16, batches = 6, channels = 4;
static constexpr float poison = -123.0f;
#endif
static constexpr float test_depths[] = {0.3125f, 0.6875f};
// One D24 integer code plus one float32 ULP at 1.0; fixed before desktop runs.
#ifndef READONLY_DEPTH_ORACLE_ONLY
static constexpr float tolerance = 1.0f / 16777215.0f + 0.00000011920928955078125f;
#endif

static float nominal_depth(unsigned x, unsigned y, unsigned pattern)
{
    return float(1 + ((3 * x + 5 * y + 2 * (x / 8) + x / 16 + y / 16 + 3 * pattern) % 7)) / 8.0f;
}

static float quantized_depth(unsigned x, unsigned y, unsigned pattern)
{
    const double value = nominal_depth(x, y, pattern);
    return float(std::floor(value * 16777215.0 + 0.5) / 16777215.0);
}

static float expected_sample(unsigned x, unsigned y, unsigned pattern, bool linear)
{
    if (!linear) return quantized_depth(x, y, pattern);
    const unsigned nx = std::min(x + 1, side - 1), ny = std::min(y + 1, side - 1);
    return quantized_depth(x, y, pattern) * 0.5625f +
           quantized_depth(nx, y, pattern) * 0.1875f +
           quantized_depth(x, ny, pattern) * 0.1875f +
           quantized_depth(nx, ny, pattern) * 0.0625f;
}

static bool accepted(unsigned x, unsigned y, unsigned pattern, unsigned draw)
{
    return test_depths[draw] <= quantized_depth(x, y, pattern);
}

static bool oracle_self_test()
{
    // Hand-derived literals check formula, pattern changes, two-depth acceptance,
    // bilinear weights and clamped edge behavior independently of HLSL.
    if (nominal_depth(0, 0, 0) != 0.125f || nominal_depth(1, 0, 0) != 0.5f ||
        nominal_depth(0, 1, 0) != 0.75f || nominal_depth(1, 1, 0) != 0.25f ||
        nominal_depth(8, 0, 0) != 0.75f || nominal_depth(0, 0, 1) != 0.5f ||
        nominal_depth(63, 63, 0) != 0.875f ||
        quantized_depth(0, 0, 0) != 0.12500001490116119f ||
        quantized_depth(1, 0, 0) != 0.5000000596046448f) return false;
    if (std::fabs(expected_sample(0, 0, 0, true) - 0.3203125298023224f) > 0.00000001f ||
        expected_sample(0, 0, 0, false) != quantized_depth(0, 0, 0) ||
        expected_sample(63, 63, 0, true) != quantized_depth(63, 63, 0)) return false;
    if (accepted(0, 0, 0, 0) || !accepted(1, 0, 0, 0) || accepted(1, 0, 0, 1) ||
        !accepted(0, 1, 0, 1)) return false;
    for (unsigned pattern = 0; pattern < 2; ++pattern) for (unsigned draw = 0; draw < 2; ++draw) {
        unsigned pass = 0;
        for (unsigned y = 0; y < side; ++y) for (unsigned x = 0; x < side; ++x)
            pass += accepted(x, y, pattern, draw);
        if (!pass || pass == side * side) return false;
    }
    return true;
}

#ifdef READONLY_DEPTH_ORACLE_ONLY
int main()
{
    const bool ok = oracle_self_test();
    std::printf("READONLY-DEPTH HOST-ORACLE status=%s hardware-not-tested\n", ok ? "PASS" : "FAIL");
    return ok ? 0 : 1;
}
#else
#include <windows.h>
#include <d3d11.h>
#include <d3d11sdklayers.h>
#include <d3dcompiler.h>
#include <cstdarg>
#include <vector>
#include "d3d11-readonly-depth-precompiled-read.inc"

static FILE *log_file, *raw_file;
static bool io_ok = true;
static DWORD started;
static unsigned completed_batches, completed_frames, numeric_checks, numeric_failures;
static unsigned acceptance_checks, acceptance_failures, expected_pass, expected_reject;
static unsigned binding_checks, debug_warnings, debug_errors;
static const char *case_names[] = {"same-readonly-DSV-and-SRV", "original-detached-independent-identical-DSV-bound", "original-DSV-independent-SRV-snapshot"};

template<class T> struct Owned {
    T *p = nullptr;
    ~Owned() { if (p) p->Release(); }
    Owned() = default;
    Owned(const Owned &) = delete;
    Owned &operator=(const Owned &) = delete;
};

static void log_line(const char *format, ...)
{
    char line[1536];
    va_list args; va_start(args, format);
    const int size = vsnprintf(line, sizeof(line), format, args); va_end(args);
    if (size < 0 || unsigned(size) >= sizeof(line)) io_ok = false;
    OutputDebugStringA(line); fputs(line, stderr); fflush(stderr);
    if (log_file && (fputs(line, log_file) < 0 || fflush(log_file))) io_ok = false;
}

static bool checked(HRESULT hr, const char *operation)
{
    log_line("READONLY-DEPTH API operation=%s hr=%08lx\n", operation, (unsigned long)hr);
    return SUCCEEDED(hr);
}

static bool compile(const char *source, const char *entry, const char *target, ID3DBlob **blob)
{
    Owned<ID3DBlob> errors;
    HRESULT hr = D3DCompile(source, strlen(source), "madeira-readonly-depth-probe", nullptr,
        nullptr, entry, target, D3DCOMPILE_ENABLE_STRICTNESS, 0, blob, &errors.p);
    if (errors.p) log_line("READONLY-DEPTH shader-message=%.*s\n",
        int(std::min<size_t>(errors.p->GetBufferSize(), 900)), (const char *)errors.p->GetBufferPointer());
    return checked(hr, entry);
}

static bool within_budget()
{
    if (DWORD(GetTickCount() - started) < 120000) return true;
    log_line("READONLY-DEPTH FAIL overall-budget-exceeded\n"); return false;
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
    } while (DWORD(GetTickCount() - begin) < 10000 && within_budget());
    return hr;
}

static bool inspect_debug(ID3D11InfoQueue *queue)
{
    if (!queue) return true;
    const UINT64 count = queue->GetNumStoredMessagesAllowedByRetrievalFilter();
    for (UINT64 i = 0; i < count; ++i) {
        SIZE_T size = 0;
        if (FAILED(queue->GetMessage(i, nullptr, &size)) || size > 1048576) return false;
        std::vector<unsigned char> storage(size);
        auto *message = reinterpret_cast<D3D11_MESSAGE *>(storage.data());
        if (FAILED(queue->GetMessage(i, message, &size))) return false;
        if (message->Severity == D3D11_MESSAGE_SEVERITY_WARNING) ++debug_warnings;
        if (message->Severity <= D3D11_MESSAGE_SEVERITY_ERROR) ++debug_errors;
        if (i < 64) log_line("READONLY-DEPTH DEBUG severity=%u id=%u description=%.*s\n",
            unsigned(message->Severity), unsigned(message->ID),
            int(std::min<SIZE_T>(message->DescriptionByteLength, 1000)), message->pDescription);
    }
    const UINT64 discarded = queue->GetNumMessagesDiscardedByMessageCountLimit();
    log_line("READONLY-DEPTH DEBUG-END stored=%llu discarded=%llu warnings=%u errors=%u\n",
        (unsigned long long)count, (unsigned long long)discarded, debug_warnings, debug_errors);
    queue->ClearStoredMessages();
    return discarded == 0;
}

static bool verify_bindings(ID3D11DeviceContext *context, ID3D11RenderTargetView *rtv,
                            ID3D11DepthStencilView *dsv, ID3D11ShaderResourceView *srv)
{
    Owned<ID3D11RenderTargetView> actual_rtv;
    Owned<ID3D11DepthStencilView> actual_dsv;
    Owned<ID3D11ShaderResourceView> actual_srv;
    context->OMGetRenderTargets(1, &actual_rtv.p, &actual_dsv.p);
    context->PSGetShaderResources(3, 1, &actual_srv.p);
    ++binding_checks;
    if (actual_rtv.p != rtv || actual_dsv.p != dsv || actual_srv.p != srv) {
        log_line("READONLY-DEPTH FAIL binding-mismatch check=%u rtv=%d dsv=%d srv=%d\n",
            binding_checks, actual_rtv.p == rtv, actual_dsv.p == dsv, actual_srv.p == srv);
        return false;
    }
    return true;
}

static bool inspect_batch(ID3D11DeviceContext *context, ID3D11Texture2D *staging,
                           unsigned mode, unsigned filter)
{
    D3D11_MAPPED_SUBRESOURCE mapped = {};
    if (!checked(bounded_map(context, staging, &mapped), "MapBatchAtlas")) return false;
    const unsigned row_floats = side * channels;
    if (!mapped.pData || mapped.RowPitch < row_floats * sizeof(float)) {
        context->Unmap(staging, 0); return false;
    }
    std::vector<float> atlas(row_floats * side * frames);
    for (unsigned y = 0; y < side * frames; ++y)
        memcpy(atlas.data() + y * row_floats, (const char *)mapped.pData + y * mapped.RowPitch, row_floats * sizeof(float));
    context->Unmap(staging, 0);
    if (fwrite(atlas.data(), sizeof(float), atlas.size(), raw_file) != atlas.size() || fflush(raw_file)) return false;
    unsigned details = 0;
    const unsigned before_numeric = numeric_failures, before_acceptance = acceptance_failures;
    float max_error = 0;
    const unsigned points[][2] = {{0,0},{1,0},{0,1},{7,7},{8,7},{31,31},{32,31},{63,63}};
    for (unsigned frame = 0; frame < frames; ++frame) {
        const unsigned pattern = frame & 1;
        const float *raw = atlas.data() + frame * side * row_floats;
        for (unsigned y = 0; y < side; ++y) for (unsigned x = 0; x < side; ++x) {
            for (unsigned draw = 0; draw < 2; ++draw) {
                const bool pass = accepted(x, y, pattern, draw);
                pass ? ++expected_pass : ++expected_reject;
                const float *pair = raw + (y * side + x) * channels + draw * 2;
                ++acceptance_checks;
                const bool acceptance_ok = pass ? (pair[0] != poison && pair[1] != poison) : (pair[0] == poison && pair[1] == poison);
                if (!acceptance_ok) {
                    ++acceptance_failures;
                    if (details++ < 16) log_line("READONLY-DEPTH MISMATCH kind=acceptance frame=%u xy=%u,%u draw=%u expected-pass=%d raw=%.9g sample=%.9g\n",
                        frame, x, y, draw, pass, pair[0], pair[1]);
                }
                for (unsigned c = 0; c < 2; ++c) {
                    const float expected = pass ? (c ? expected_sample(x, y, pattern, filter != 0) : quantized_depth(x, y, pattern)) : poison;
                    const float error = std::fabs(pair[c] - expected);
                    ++numeric_checks;
                    if (std::isfinite(error)) max_error = std::max(max_error, error);
                    if (!std::isfinite(error) || error > (pass ? tolerance : 0.0f)) {
                        ++numeric_failures;
                        if (details++ < 16) log_line("READONLY-DEPTH MISMATCH kind=%s frame=%u xy=%u,%u draw=%u expected=%.9g actual=%.9g tolerance=%.9g\n",
                            c ? "ordinary-sample" : "load", frame, x, y, draw, expected, pair[c], pass ? tolerance : 0.0f);
                    }
                }
            }
        }
        for (const auto &point : points) {
            const unsigned x = point[0], y = point[1];
            const float *p = raw + (y * side + x) * channels;
            log_line("READONLY-DEPTH OBS batch=%u frame=%u pattern=%u xy=%u,%u depth=%.9g sample=%.9g pass0=%d pass1=%d raw0=%.9g sample0=%.9g raw1=%.9g sample1=%.9g\n",
                completed_batches, frame, pattern, x, y, quantized_depth(x,y,pattern), expected_sample(x,y,pattern,filter != 0),
                accepted(x,y,pattern,0), accepted(x,y,pattern,1), p[0],p[1],p[2],p[3]);
        }
        ++completed_frames;
    }
    log_line("READONLY-DEPTH BATCH-END batch=%u mode=%s filter=%s numeric-failures=%u acceptance-failures=%u max-error=%.9g\n",
        completed_batches, case_names[mode], filter ? "linear" : "point", numeric_failures - before_numeric,
        acceptance_failures - before_acceptance, max_error);
    ++completed_batches;
    return io_ok;
}

static bool run()
{
    Owned<ID3D11Device> device;
    Owned<ID3D11DeviceContext> context;
    D3D_FEATURE_LEVEL level;
    const D3D_FEATURE_LEVEL levels[] = {D3D_FEATURE_LEVEL_11_0};
    HRESULT hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, D3D11_CREATE_DEVICE_DEBUG,
        levels, 1, D3D11_SDK_VERSION, &device.p, &level, &context.p);
    log_line("READONLY-DEPTH API operation=D3D11CreateDevice-hardware-debug hr=%08lx\n", (unsigned long)hr);
    if (FAILED(hr)) {
        // A missing/unsupported debug layer must not select software rendering.
        if (device.p) { device.p->Release(); device.p = nullptr; }
        if (context.p) { context.p->Release(); context.p = nullptr; }
        if (!checked(D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0,
            levels, 1, D3D11_SDK_VERSION, &device.p, &level, &context.p), "D3D11CreateDevice-hardware-no-debug")) return false;
    }
    Owned<ID3D11InfoQueue> debug;
    hr = device.p->QueryInterface(__uuidof(ID3D11InfoQueue), (void **)&debug.p);
    log_line("READONLY-DEPTH DEBUG availability=%s hr=%08lx\n", SUCCEEDED(hr) ? "available" : "unavailable", (unsigned long)hr);
    if (debug.p && !checked(debug.p->SetMessageCountLimit(4096), "SetDebugMessageCountLimit")) return false;
    Owned<IDXGIDevice> dxgi; Owned<IDXGIAdapter> adapter;
    DXGI_ADAPTER_DESC ad = {};
    if (!checked(device.p->QueryInterface(__uuidof(IDXGIDevice), (void **)&dxgi.p), "QueryDXGI") ||
        !checked(dxgi.p->GetAdapter(&adapter.p), "GetAdapter") || !checked(adapter.p->GetDesc(&ad), "GetAdapterDesc")) return false;
    char name[256] = {};
    WideCharToMultiByte(CP_UTF8, 0, ad.Description, -1, name, sizeof(name), nullptr, nullptr);
    log_line("READONLY-DEPTH DEVICE feature-level=%04x adapter=%s vendor=%04x device=%04x\n", unsigned(level), name, ad.VendorId, ad.DeviceId);

    const char *source = R"(
cbuffer Params : register(b0) { uint pattern; float z; uint2 padding; };
Texture2D<float> depth : register(t3);
SamplerState ordinary_sampler : register(s0);
float4 vs(uint id : SV_VertexID) : SV_Position {
    float2 uv = id == 0 ? float2(0,0) : (id == 1 ? float2(2,0) : float2(0,2));
    return float4(uv.x * 2 - 1, 1 - uv.y * 2, z, 1);
}
float write_ps(float4 p : SV_Position) : SV_Depth {
    uint2 v = uint2(p.xy);
    return float(1 + ((3*v.x + 5*v.y + 2*(v.x/8) + v.x/16 + v.y/16 + 3*pattern) % 7)) / 8.0;
}
float4 read_ps(float4 p : SV_Position) : SV_Target {
    uint2 v = uint2(p.xy);
    float a = depth.Load(int3(v, 0));
    float b = depth.Sample(ordinary_sampler, (float2(v) + 0.75) / 64.0);
    return float4(a, b, a, b);
}
)";
    Owned<ID3DBlob> vs_code, write_code;
    Owned<ID3D11VertexShader> vs;
    Owned<ID3D11PixelShader> writer, reader;
    log_line("READONLY-DEPTH READER source=precompiled-desktop-DXBC bytes=%u sha256=d349694d68529b6f122a388ab63809306e633aaee51f59c2d68a2ec04da7feb4 runtime-reader-compilation=disabled\n", unsigned(sizeof(precompiled_read_ps)));
    if (!compile(source,"vs","vs_5_0",&vs_code.p) || !compile(source,"write_ps","ps_5_0",&write_code.p) ||
        !checked(device.p->CreateVertexShader(vs_code.p->GetBufferPointer(),vs_code.p->GetBufferSize(),nullptr,&vs.p),"CreateVS") ||
        !checked(device.p->CreatePixelShader(write_code.p->GetBufferPointer(),write_code.p->GetBufferSize(),nullptr,&writer.p),"CreateWriterPS") ||
        !checked(device.p->CreatePixelShader(precompiled_read_ps,sizeof(precompiled_read_ps),nullptr,&reader.p),"CreatePrecompiledReaderPS")) return false;

    Owned<ID3D11Texture2D> depth[3];
    Owned<ID3D11DepthStencilView> writable[2], readonly_dsv[2];
    Owned<ID3D11ShaderResourceView> srv[3];
    D3D11_TEXTURE2D_DESC td = {};
    td.Width = td.Height = side; td.MipLevels = td.ArraySize = td.SampleDesc.Count = 1;
    td.Format = DXGI_FORMAT_R24G8_TYPELESS; td.Usage = D3D11_USAGE_DEFAULT;
    td.BindFlags = D3D11_BIND_DEPTH_STENCIL | D3D11_BIND_SHADER_RESOURCE;
    for (unsigned i = 0; i < 3; ++i) {
        D3D11_DEPTH_STENCIL_VIEW_DESC dd = {};
        dd.Format = DXGI_FORMAT_D24_UNORM_S8_UINT; dd.ViewDimension = D3D11_DSV_DIMENSION_TEXTURE2D;
        D3D11_SHADER_RESOURCE_VIEW_DESC sd = {};
        sd.Format = DXGI_FORMAT_R24_UNORM_X8_TYPELESS; sd.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D; sd.Texture2D.MipLevels = 1;
        if (!checked(device.p->CreateTexture2D(&td,nullptr,&depth[i].p),"CreateDepthTexture") ||
            !checked(device.p->CreateShaderResourceView(depth[i].p,&sd,&srv[i].p),"CreateR24SRV")) return false;
        if (i < 2) {
            if (!checked(device.p->CreateDepthStencilView(depth[i].p,&dd,&writable[i].p),"CreateWritableDSV")) return false;
            dd.Flags = D3D11_DSV_READ_ONLY_DEPTH | D3D11_DSV_READ_ONLY_STENCIL;
            if (!checked(device.p->CreateDepthStencilView(depth[i].p,&dd,&readonly_dsv[i].p),"CreateReadonlyDSV")) return false;
            D3D11_DEPTH_STENCIL_VIEW_DESC actual = {}; readonly_dsv[i].p->GetDesc(&actual);
            if (actual.Flags != dd.Flags) return false;
            log_line("READONLY-DEPTH DSV texture=%u flags=%u format=%u\n", i, actual.Flags, unsigned(actual.Format));
        }
    }
    Owned<ID3D11Texture2D> output, atlas, staging;
    Owned<ID3D11RenderTargetView> rtv;
    td.Format = DXGI_FORMAT_R32G32B32A32_FLOAT; td.BindFlags = D3D11_BIND_RENDER_TARGET;
    if (!checked(device.p->CreateTexture2D(&td,nullptr,&output.p),"CreateOutput") ||
        !checked(device.p->CreateRenderTargetView(output.p,nullptr,&rtv.p),"CreateOutputRTV")) return false;
    td.Height = side * frames;
    if (!checked(device.p->CreateTexture2D(&td,nullptr,&atlas.p),"CreateAtlas")) return false;
    td.Usage = D3D11_USAGE_STAGING; td.BindFlags = 0; td.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    if (!checked(device.p->CreateTexture2D(&td,nullptr,&staging.p),"CreateStaging")) return false;
    Owned<ID3D11DepthStencilState> write_state, test_state;
    D3D11_DEPTH_STENCIL_DESC ds = {};
    ds.DepthEnable = TRUE; ds.DepthWriteMask = D3D11_DEPTH_WRITE_MASK_ALL; ds.DepthFunc = D3D11_COMPARISON_ALWAYS;
    if (!checked(device.p->CreateDepthStencilState(&ds,&write_state.p),"CreateDepthWriteState")) return false;
    ds.DepthWriteMask = D3D11_DEPTH_WRITE_MASK_ZERO; ds.DepthFunc = D3D11_COMPARISON_LESS_EQUAL;
    if (!checked(device.p->CreateDepthStencilState(&ds,&test_state.p),"CreateDepthTestNoWriteState")) return false;
    Owned<ID3D11RasterizerState> raster;
    D3D11_RASTERIZER_DESC rs = {};
    rs.FillMode = D3D11_FILL_SOLID; rs.CullMode = D3D11_CULL_NONE; rs.DepthClipEnable = TRUE;
    if (!checked(device.p->CreateRasterizerState(&rs,&raster.p),"CreateRasterizer")) return false;
    Owned<ID3D11BlendState> masks[2];
    Owned<ID3D11SamplerState> samplers[2];
    for (unsigned i = 0; i < 2; ++i) {
        D3D11_BLEND_DESC blend = {};
        blend.RenderTarget[0].RenderTargetWriteMask = i ? (D3D11_COLOR_WRITE_ENABLE_BLUE | D3D11_COLOR_WRITE_ENABLE_ALPHA) :
            (D3D11_COLOR_WRITE_ENABLE_RED | D3D11_COLOR_WRITE_ENABLE_GREEN);
        if (!checked(device.p->CreateBlendState(&blend,&masks[i].p),"CreatePairWriteMask")) return false;
        D3D11_SAMPLER_DESC sd = {};
        sd.Filter = i ? D3D11_FILTER_MIN_MAG_LINEAR_MIP_POINT : D3D11_FILTER_MIN_MAG_MIP_POINT;
        sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
        sd.ComparisonFunc = D3D11_COMPARISON_NEVER; sd.MaxAnisotropy = 1; sd.MaxLOD = D3D11_FLOAT32_MAX;
        if (!checked(device.p->CreateSamplerState(&sd,&samplers[i].p),"CreateOrdinarySampler")) return false;
    }
    Owned<ID3D11Buffer> constants[2][2];
    for (unsigned pattern = 0; pattern < 2; ++pattern) for (unsigned draw = 0; draw < 2; ++draw) {
        struct Params { unsigned pattern; float z; unsigned padding[2]; } values = {pattern,test_depths[draw],{0,0}};
        D3D11_BUFFER_DESC bd = {}; bd.ByteWidth = sizeof(values); bd.Usage = D3D11_USAGE_IMMUTABLE; bd.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
        D3D11_SUBRESOURCE_DATA data = {}; data.pSysMem = &values;
        if (!checked(device.p->CreateBuffer(&bd,&data,&constants[pattern][draw].p),"CreateImmutableParams")) return false;
    }
    context.p->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    context.p->VSSetShader(vs.p,nullptr,0); context.p->RSSetState(raster.p);
    D3D11_VIEWPORT viewport = {0,0,float(side),float(side),0,1}; context.p->RSSetViewports(1,&viewport);
    for (unsigned mode = 0; mode < 3; ++mode) for (unsigned filter = 0; filter < 2; ++filter) {
        log_line("READONLY-DEPTH BATCH-BEGIN batch=%u mode=%s filter=%s depth-func=LESS_EQUAL depth-write=ZERO stencil=disabled slot=t3 frames=%u\n",
            completed_batches,case_names[mode],filter ? "linear" : "point",frames);
        context.p->PSSetSamplers(0,1,&samplers[filter].p);
        for (unsigned frame = 0; frame < frames; ++frame) {
            if (!within_budget()) return false;
            const unsigned pattern = frame & 1;
            ID3D11ShaderResourceView *null_srv = nullptr; context.p->PSSetShaderResources(3,1,&null_srv);
            context.p->OMSetBlendState(nullptr,nullptr,0xffffffff);
            context.p->OMSetDepthStencilState(write_state.p,0);
            context.p->VSSetConstantBuffers(0,1,&constants[pattern][0].p);
            context.p->PSSetConstantBuffers(0,1,&constants[pattern][0].p);
            context.p->PSSetShader(writer.p,nullptr,0);
            // Detached control is independently rendered, never copied from the
            // original; the same-resource case needs no pre-sampling copy/flush.
            for (unsigned i = 0; i < (mode == 1 ? 2u : 1u); ++i) {
                context.p->OMSetRenderTargets(0,nullptr,writable[i].p);
                context.p->ClearDepthStencilView(writable[i].p,D3D11_CLEAR_DEPTH | D3D11_CLEAR_STENCIL,1,0);
                context.p->Draw(3,0);
            }
            context.p->OMSetRenderTargets(0,nullptr,nullptr);
            if (mode == 2) context.p->CopyResource(depth[2].p,depth[0].p);
            const float clear[4] = {poison,poison,poison,poison}; context.p->ClearRenderTargetView(rtv.p,clear);
            ID3D11DepthStencilView *active_dsv = readonly_dsv[mode == 1 ? 1 : 0].p;
            ID3D11ShaderResourceView *active_srv = srv[mode == 2 ? 2 : 0].p;
            context.p->OMSetDepthStencilState(test_state.p,0);
            context.p->OMSetRenderTargets(1,&rtv.p,active_dsv);
            context.p->PSSetShaderResources(3,1,&active_srv);
            if (!verify_bindings(context.p,rtv.p,active_dsv,active_srv)) return false;
            context.p->PSSetShader(reader.p,nullptr,0);
            // Both draws are consecutive on the same attachments. Channel masks
            // preserve each draw independently; there is no copy/Map/Flush here.
            for (unsigned draw = 0; draw < 2; ++draw) {
                context.p->VSSetConstantBuffers(0,1,&constants[pattern][draw].p);
                context.p->OMSetBlendState(masks[draw].p,nullptr,0xffffffff);
                context.p->Draw(3,0);
            }
            context.p->PSSetShaderResources(3,1,&null_srv);
            context.p->OMSetRenderTargets(0,nullptr,nullptr);
            context.p->CopySubresourceRegion(atlas.p,0,0,frame*side,0,output.p,0,nullptr);
        }
        context.p->CopyResource(staging.p,atlas.p); context.p->Flush();
        if (!inspect_batch(context.p,staging.p,mode,filter) || !inspect_debug(debug.p)) return false;
    }
    context.p->ClearState();
    return checked(device.p->GetDeviceRemovedReason(),"GetDeviceRemovedReason");
}

int main()
{
    started = GetTickCount();
    FILETIME time; GetSystemTimeAsFileTime(&time);
    const unsigned long pid = GetCurrentProcessId();
    char stem[256], text_path[280], raw_path[280];
    snprintf(stem,sizeof(stem),"C:\\madeira-shadow-trace-20261013\\d3d11-readonly-depth-precompiled-%08lx%08lx-%lu",time.dwHighDateTime,time.dwLowDateTime,pid);
    snprintf(text_path,sizeof(text_path),"%s.txt",stem); snprintf(raw_path,sizeof(raw_path),"%s.f32",stem);
    if (!CreateDirectoryA("C:\\madeira-shadow-trace-20261013",nullptr) && GetLastError() != ERROR_ALREADY_EXISTS) return 1;
    log_file = fopen(text_path,"wb"); raw_file = fopen(raw_path,"wb");
    if (!log_file || !raw_file) {
        log_line("READONLY-DEPTH FAIL open-output-files\n");
        if (log_file) fclose(log_file);
        if (raw_file) fclose(raw_file);
        return 1;
    }
    log_line("READONLY-DEPTH BEGIN version=2 run=%08lx%08lx-%lu hardware-required expected-batches=%u frames-per-batch=%u expected-numeric-checks=%u expected-acceptance-checks=%u tolerance=%.9g\n",
        time.dwHighDateTime,time.dwLowDateTime,pid,batches,frames,batches*frames*side*side*channels,batches*frames*side*side*2,tolerance);
    log_line("READONLY-DEPTH FILE text=%s raw=%s\n",text_path,raw_path);
    log_line("READONLY-DEPTH RAW-LAYOUT header=12-u32-le magic=52445356 version=2 width=%u height=%u batches=%u frames=%u channels=4 draws=2 run-high-low-pid reserved=0 data=float32-le-interleaved-RGBA batch-order=mode-filter RG=draw0-load-sample BA=draw1-load-sample poison=-123\n",side,side*frames,batches,frames);
    const uint32_t header[] = {0x52445356,2,side,side*frames,batches,frames,channels,2,time.dwHighDateTime,time.dwLowDateTime,uint32_t(pid),0};
    bool ok = fwrite(header,sizeof(header),1,raw_file) == 1;
    if (!oracle_self_test()) { log_line("READONLY-DEPTH FAIL host-oracle-self-test\n"); ok = false; }
    if (ok) ok = run();
    if (fflush(raw_file)) io_ok = false;
    if (fclose(raw_file)) io_ok = false;
    raw_file = nullptr;
    const bool complete = ok && io_ok && completed_batches == batches && completed_frames == batches*frames &&
        numeric_checks == batches*frames*side*side*channels && acceptance_checks == batches*frames*side*side*2 &&
        binding_checks == batches*frames && expected_pass && expected_reject;
    int status = complete ? ((numeric_failures || acceptance_failures || debug_warnings || debug_errors) ? 2 : 0) : 1;
    log_line("READONLY-DEPTH END version=2 run=%08lx%08lx-%lu status=%s exit=%d batches=%u frames=%u binding-checks=%u numeric-checks=%u numeric-failures=%u acceptance-checks=%u acceptance-failures=%u expected-pass=%u expected-reject=%u debug-warnings=%u debug-errors=%u elapsed-ms=%lu\n",
        time.dwHighDateTime,time.dwLowDateTime,pid,status == 0 ? "PASS" : status == 2 ? "CHECKS-FAILED" : "INCOMPLETE",status,
        completed_batches,completed_frames,binding_checks,numeric_checks,numeric_failures,acceptance_checks,acceptance_failures,
        expected_pass,expected_reject,debug_warnings,debug_errors,DWORD(GetTickCount()-started));
    const int close_status = fclose(log_file);
    if (!io_ok || close_status) status = 1;
    return status;
}
#endif
