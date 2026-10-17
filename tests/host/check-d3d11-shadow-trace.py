#!/usr/bin/env python3
"""Execute the production PreDraw trace hook and bounded ledger.

Config parsing, sampler BindingSet and the PreDraw prefix are real source.
Descriptor/texture getters and the finalized pipeline are host boundaries;
this verifies diagnostics, not shadow rendering or GPU synchronization.
"""

import argparse
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "dxmt/src"
TOKENS = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[{}]', re.DOTALL)


def definition(source: str, signature: str) -> str:
    start = source.index(signature)
    depth = 0
    for token in TOKENS.finditer(source, start):
        if token[0] == "{":
            depth += 1
        elif token[0] == "}":
            depth -= 1
            if depth == 0:
                return source[start:token.end()]
    raise ValueError(f"Unterminated definition: {signature}")


PRELUDE = r"""
#include <array>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <sstream>
#include <string>
#include <vector>
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <d3d11_3.h>
#define DXMT_NATIVE 1
#include "winemetal.h"
#include "config/config.hpp"
#include "dxmt_binding_set.hpp"
#include "sha1/sha1_util.hpp"
namespace dxmt {
static unsigned config_accesses, reads, record_calls;
Config &Config::getInstance() { ++config_accesses; static Config value; return value; }
static std::vector<std::string> log_lines;
template<class... T> void host_log(T... values) {
  std::ostringstream line; (line << ... << values); log_lines.push_back(line.str());
}
#define ERR(...) host_log(__VA_ARGS__)
template<class T> struct Rc {
  T *value;
  T *ptr() { return value; }
  T *operator->() { return value; }
};
struct Texture {
  WMTPixelFormat format;
  WMTPixelFormat pixelFormat() { ++reads; return format; }
  WMTPixelFormat pixelFormat(unsigned) { ++reads; return format; }
};
struct HostResource {
  unsigned references = 1;
  void AddRef() { ++references; ++reads; }
  void Release() { --references; ++reads; }
};
#define ID3D11Resource HostResource
struct D3D11ShaderResourceView {
  D3D11_SHADER_RESOURCE_VIEW_DESC1 desc{};
  Texture *tex;
  HostResource *resource;
  unsigned view = 23;
  Rc<Texture> texture() { ++reads; return {tex}; }
  unsigned viewId() { ++reads; return view; }
  void GetDesc1(D3D11_SHADER_RESOURCE_VIEW_DESC1 *out) { ++reads; *out = desc; }
  void GetResource(HostResource **out) { ++reads; *out = resource; if (resource) resource->AddRef(); }
};
struct SRVPointer {
  D3D11ShaderResourceView *value;
  D3D11ShaderResourceView *ptr() { return value; }
  D3D11ShaderResourceView *operator->() { return value; }
};
struct SRV_B { void *RawPointer = nullptr; SRVPointer SRV{nullptr}; };
template<> struct redunant_binding_trait<SRV_B> {
  static bool is_redunant(const SRV_B &a, const SRV_B &b) { return a.RawPointer == b.RawPointer; }
};
using SRVBindingSet = BindingSet<SRV_B, 128>;
struct Reflection {
  uint16_t SamplerSlotMask = 0;
  uint64_t SRVSlotMaskLo = 0, SRVSlotMaskHi = 0;
};
struct Shader {
  Sha1Digest digest{};
  Reflection refl{};
  const Sha1Digest &sha1() { ++reads; return digest; }
  Reflection &reflection() { ++reads; return refl; }
};
struct IMTLD3D11Shader {
  Shader *shader;
  Shader *GetManagedShader() { ++reads; return shader; }
};
struct ShaderPointer {
  IMTLD3D11Shader *value = nullptr;
  IMTLD3D11Shader *ptr() { return value; }
  ShaderPointer &operator=(IMTLD3D11Shader *p) { value = p; return *this; }
};
struct D3D11DepthStencilView {
  D3D11_DEPTH_STENCIL_VIEW_DESC desc{};
  Texture *tex;
  WMTPixelFormat format;
  unsigned view = 1;
  void GetDesc(D3D11_DEPTH_STENCIL_VIEW_DESC *out) { ++reads; *out = desc; }
  Rc<Texture> texture() { ++reads; return {tex}; }
  WMTPixelFormat pixelFormat() { ++reads; return format; }
  unsigned viewId() { ++reads; return view; }
};
struct IMTLD3D11RasterizerState {
  D3D11_RASTERIZER_DESC desc{};
  void GetDesc(D3D11_RASTERIZER_DESC *out) { ++reads; *out = desc; }
};
struct IMTLD3D11DepthStencilState {
  D3D11_DEPTH_STENCIL_DESC desc{};
  void GetDesc(D3D11_DEPTH_STENCIL_DESC *out) { ++reads; *out = desc; }
};
struct D3D11SamplerState {
  D3D11_SAMPLER_DESC desc{};
  void GetDesc(D3D11_SAMPLER_DESC *out) { ++reads; *out = desc; }
};
struct SAMPLER_B { void *RawPointer = nullptr; D3D11SamplerState *Sampler = nullptr; };
template<> struct redunant_binding_trait<SAMPLER_B> {
  static bool is_redunant(const SAMPLER_B &a, const SAMPLER_B &b) { return a.RawPointer == b.RawPointer; }
};
using SamplerBindingSet = BindingSet<SAMPLER_B, 16>;
enum class PipelineStage { Pixel = 0 };
enum class DrawCallStatus { Invalid, Ordinary };
"""

CLASS = r"""
struct Context {
  struct DSVPointer {
    D3D11DepthStencilView *value = nullptr;
    D3D11DepthStencilView *ptr() { return value; }
    DSVPointer &operator=(D3D11DepthStencilView *p) { value = p; return *this; }
  };
  struct State {
    struct { bool IndexBuffer = true; } InputAssembler;
    struct { DSVPointer DSV; IMTLD3D11DepthStencilState *DepthStencilState = nullptr; } OutputMerger;
    struct { IMTLD3D11RasterizerState *RasterizerState = nullptr; } Rasterizer;
    struct Stage { SamplerBindingSet Samplers; SRVBindingSet SRVs; ShaderPointer Shader; } stage;
    struct Stages {
      Stage *value;
      Stage &operator[](PipelineStage) { return *value; }
    } ShaderStages{&stage};
  } state_;
  IMTLD3D11RasterizerState *default_rasterizer_state = nullptr;
  IMTLD3D11DepthStencilState *default_depth_stencil_state = nullptr;
  D3D11ShadowTrace shadow_trace_;
  bool invalid = false;
  template<bool> DrawCallStatus FinalizeCurrentRenderPipeline() {
    return invalid ? DrawCallStatus::Invalid : DrawCallStatus::Ordinary;
  }
"""

DRIVER = r"""
static unsigned checks, failures;
static void check(bool ok, const char *message) {
  ++checks; if (!ok) { ++failures; std::printf("SHADOW FAIL %s\n", message); }
}
static unsigned tuples() {
  unsigned count = 0;
  for (const auto &line : log_lines) if (line.find("[shadow-trace] tuple=") == 0) ++count;
  return count;
}
static bool contains(const char *text) {
  for (const auto &line : log_lines) if (line.find(text) != std::string::npos) return true;
  return false;
}
static int64_t last_tuple_field(const char *name) {
  const std::string prefix = std::string(name) + "=";
  for (auto line = log_lines.rbegin(); line != log_lines.rend(); ++line) {
    if (line->find("[shadow-trace] tuple=") != 0) continue;
    std::istringstream fields(*line);
    std::string field;
    while (fields >> field) {
      if (field.find(prefix) == 0) return std::stoll(field.substr(prefix.size()));
    }
    return -1;
  }
  return -1;
}
int main(int argc, char **argv) {
  if (argc != 3) return 2;
  const bool enabled = std::strcmp(argv[2], "0") != 0;
  Config &config = Config::getInstance();
  if (std::strcmp(argv[1], "default")) config.setOption("d3d11.shadowTrace", argv[1]);
  config_accesses = 0;
  Context context;
  context.PreDraw<false>();
  check(reads == 0 && tuples() == 0 && (enabled || log_lines.empty()),
        "null DSV is safe and produces no descriptor read/tuple");
  context.state_.InputAssembler.IndexBuffer = false;
  check(context.PreDraw<true>() == DrawCallStatus::Invalid && reads == 0 && tuples() == 0,
        "missing index buffer exits before diagnostic getters");
  context.state_.InputAssembler.IndexBuffer = true;
  Texture texture{WMTPixelFormatDepth32Float_Stencil8};
  D3D11DepthStencilView dsv{{}, &texture, WMTPixelFormatDepth32Float_Stencil8};
  dsv.desc.Format = DXGI_FORMAT_D24_UNORM_S8_UINT;
  dsv.desc.ViewDimension = D3D11_DSV_DIMENSION_TEXTURE2D;
  IMTLD3D11RasterizerState rasterizer;
  rasterizer.desc.DepthBias = 73;
  rasterizer.desc.SlopeScaledDepthBias = 1.5f;
  rasterizer.desc.DepthBiasClamp = 0.125f;
  D3D11SamplerState comparison, ordinary;
  comparison.desc.Filter = D3D11_FILTER_COMPARISON_MIN_MAG_MIP_LINEAR;
  comparison.desc.ComparisonFunc = D3D11_COMPARISON_LESS_EQUAL;
  comparison.desc.AddressU = D3D11_TEXTURE_ADDRESS_BORDER;
  comparison.desc.AddressV = D3D11_TEXTURE_ADDRESS_CLAMP;
  comparison.desc.AddressW = D3D11_TEXTURE_ADDRESS_WRAP;
  comparison.desc.BorderColor[0] = 0.25f;
  comparison.desc.BorderColor[1] = 0.5f;
  comparison.desc.BorderColor[2] = 0.75f;
  comparison.desc.BorderColor[3] = 1;
  ordinary.desc.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
  bool replaced = false;
  context.state_.stage.Samplers.bind(15, {&comparison, &comparison}, replaced);
  context.state_.stage.Samplers.bind(3, {&ordinary, &ordinary}, replaced);
  context.state_.stage.Samplers.bind(7, {nullptr, nullptr}, replaced);
  context.state_.stage.Samplers.bind(12, {&comparison, &comparison}, replaced);
  Shader managed;
  for (unsigned i = 0; i < 20; ++i) managed.digest.data[i] = uint8_t(i);
  managed.refl.SamplerSlotMask = (1u << 15) | (1u << 3) | (1u << 7);
  managed.refl.SRVSlotMaskHi = uint64_t(1) << 29; // SRV93, independently of sampler15.
  managed.refl.SRVSlotMaskLo = (uint64_t(1) << 1) | (uint64_t(1) << 5);
  IMTLD3D11Shader pixel{&managed};
  context.state_.stage.Shader = &pixel;
  Texture sampled{WMTPixelFormatDepth16Unorm};
  HostResource resource;
  D3D11ShaderResourceView srv{{}, &sampled, &resource};
  srv.desc.Format = DXGI_FORMAT_R16_UNORM;
  srv.desc.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2DARRAY;
  srv.desc.Texture2DArray.MostDetailedMip = 2;
  srv.desc.Texture2DArray.MipLevels = 3;
  srv.desc.Texture2DArray.FirstArraySlice = 4;
  srv.desc.Texture2DArray.ArraySize = 5;
  D3D11ShaderResourceView buffer{{}, nullptr, &resource};
  context.state_.stage.SRVs.bind(93, {&srv, {&srv}}, replaced);
  context.state_.stage.SRVs.bind(94, {&srv, {&srv}}, replaced); // Bound but unreferenced.
  context.state_.stage.SRVs.bind(1, {&buffer, {&buffer}}, replaced);
  context.state_.stage.SRVs.bind(5, {nullptr, {nullptr}}, replaced);
  context.state_.OutputMerger.DSV = &dsv;
  context.state_.Rasterizer.RasterizerState = &rasterizer;
  IMTLD3D11DepthStencilState depth_stencil;
  context.state_.OutputMerger.DepthStencilState = &depth_stencil;
  context.invalid = true;
  context.PreDraw<false>();
  check(reads == 0 && tuples() == 0, "invalid finalized pipeline does not trace");
  context.invalid = false;
  context.PreDraw<false>();
  if (!enabled) {
    check(reads == 0 && log_lines.empty(), "default/disabled trace has no object reads or logs");
    check(record_calls == 0, "disabled PreDraw never enters the diagnostic stack frame");
    check(config_accesses == 1, "disabled policy is cached once");
  } else {
    check(log_lines.size() >= 1 && log_lines[0] == "[shadow-trace] enabled=1",
          "enabled mode is reported");
    check(tuples() == 1, "first unique tuple logs once");
    check(contains("logical_name=D24_UNORM_S8_UINT") && contains("logical_dxgi=45") &&
          contains("physical_view_name=Depth32Float_Stencil8") && contains("physical_view_mtl=260"),
          "trace distinguishes logical D24 from physical D32S8");
    check(contains("depth_bias=73") && contains("slope_bias=1.5") && contains("bias_clamp=0.125"),
          "nonzero depth/slope/clamp bias is reported exactly");
    check(contains("sampler_slot=15") && contains("filter=149") && contains("comparison_func=4") &&
          contains("address_u=4") && contains("address_v=3") && contains("address_w=1"),
          "slot 15 comparison sampler and address modes are reported");
    check(!contains("sampler_slot=3") && !contains("sampler_slot=7"), "ordinary/null samplers are excluded");
    check(!contains("sampler_slot=12"), "unreferenced comparison sampler is excluded");
    check(contains("ps_sha1=000102030405060708090a0b0c0d0e0f10111213") &&
          contains("ps_sampler_mask=32904"), "Pixel shader digest and cached reflection mask are reported");
    check(contains("border_r=0.25") && contains("border_g=0.5") &&
          contains("border_b=0.75") && contains("border_a=1"), "actual comparison BorderRGBA is reported");
    check(contains("srv_slot=93") && contains("srv_logical_dxgi=56") &&
          contains("srv_physical_view_mtl=250") && contains("srv_view_id=23") &&
          contains("srv_first_mip=2") && contains("srv_mip_count=3") &&
          contains("srv_first_array_slice=4") && contains("srv_array_size=5"),
          "independently referenced high-slot texture SRV descriptor is reported");
    check(!contains("srv_slot=94") && !contains("srv_slot=1") && !contains("srv_slot=5"),
          "unused/buffer/null SRVs are excluded");
    check(contains("srv_resource_id=") && contains("srv_texture_id=") && contains("srv_id="),
          "SRV COM resource, stable texture, and view-object identities are present");
    check(contains((std::string("srv_resource_id=") + std::to_string(reinterpret_cast<uintptr_t>(&resource))).c_str()) &&
          contains((std::string("srv_texture_id=") + std::to_string(reinterpret_cast<uintptr_t>(&sampled))).c_str()) &&
          contains((std::string("srv_id=") + std::to_string(reinterpret_cast<uintptr_t>(&srv))).c_str()),
          "reported SRV identities match the actual bound objects");
    check(resource.references == 1, "resource identity getter balances COM references");
    check(contains("draw_seq=2"), "draw sequence includes preceding uninteresting valid draw");
    context.PreDraw<false>();
    check(tuples() == 1, "duplicate tuple is suppressed");
    dsv.view = 2; context.PreDraw<false>();
    check(tuples() == 2, "view identity participates in dedup");
    check(contains("draw_seq=4"), "duplicate draw increments sequence without consuming a tuple");
    rasterizer.desc.DepthBias = 0; context.PreDraw<false>();
    check(tuples() == 3, "changed bias participates in dedup");
    comparison.desc.ComparisonFunc = D3D11_COMPARISON_GREATER; context.PreDraw<false>();
    check(tuples() == 4, "changed comparison function participates in dedup");
    context.state_.Rasterizer.RasterizerState = nullptr;
    context.default_rasterizer_state = &rasterizer;
    context.PreDraw<false>();
    check(tuples() == 4, "default rasterizer fallback reports same tuple");
    context.default_rasterizer_state = nullptr; context.PreDraw<false>();
    check(tuples() == 5, "null rasterizer is safe");
    dsv.desc.Format = DXGI_FORMAT_D32_FLOAT;
    dsv.format = texture.format = WMTPixelFormatDepth32Float;
    context.PreDraw<false>();
    check(tuples() == 6 && contains("logical_name=D32_FLOAT") && contains("physical_view_name=Depth32Float"),
          "native D32 format change is reported");
    managed.digest.data[0] = 0xff; context.PreDraw<false>();
    check(tuples() == 7 && contains("ps_sha1=ff0102030405060708090a0b0c0d0e0f10111213"), "shader change participates in dedup");
    comparison.desc.BorderColor[0] = 0.125f; context.PreDraw<false>();
    check(tuples() == 8, "border color participates in dedup");
    srv.view = 24; context.PreDraw<false>();
    check(tuples() == 9 && contains("srv_view_id=24"), "SRV view identity participates in dedup");
    HostResource second;
    srv.resource = &second; context.PreDraw<false>();
    check(tuples() == 10 && second.references == 1, "SRV resource identity participates in dedup without retaining resource");
    context.state_.OutputMerger.DSV = nullptr; context.PreDraw<false>();
    check(tuples() == 11 && contains("dsv_present=0"), "comparison readers without a current DSV are traced");
    context.state_.OutputMerger.DSV = &dsv;
    for (unsigned i = 100; i < 340; ++i) { dsv.view = i; context.PreDraw<false>(); }
    check(tuples() == 224, "comparison tuple budget is independent and capped at224");
    context.state_.stage.Shader = nullptr;
    for (unsigned i = 1000; i < 1040; ++i) { dsv.view = i; context.PreDraw<false>(); }
    check(tuples() == 256 && contains("kind=depth-state"), "separate depth-state budget permits32 more tuples");
    const unsigned before = reads;
    context.state_.stage.Shader = &pixel;
    dsv.view = 999; context.PreDraw<false>();
    check(reads == before && tuples() == 256, "total cap stops descriptor reads and logs");
    check(config_accesses == 1, "enabled policy is cached once");
    unsigned modes = 0;
    for (const auto &line : log_lines) if (line == "[shadow-trace] enabled=1") ++modes;
    check(modes == 1, "enabled mode is logged once");
    Context fresh;
    fresh.state_.OutputMerger.DSV = &dsv;
    fresh.state_.stage.Shader = &pixel;
    fresh.state_.stage.Samplers.bind(15, {&comparison, &comparison}, replaced);
    fresh.state_.stage.SRVs.bind(93, {&srv, {&srv}}, replaced);
    fresh.PreDraw<false>();
    check(tuples() == 257, "fresh context has an independent lifetime-local ledger");

    Context state_context;
    state_context.state_.OutputMerger.DSV = &dsv;
    state_context.state_.stage.Shader = &pixel;
    state_context.state_.stage.Samplers.bind(15, {&comparison, &comparison}, replaced);
    unsigned state_tuples = tuples();
    state_context.PreDraw<false>();
    check(tuples() == ++state_tuples, "null depth-stencil state safely records D3D defaults");
    const struct { const char *name; int64_t expected; } defaults[] = {
      {"dsv_flags", 0}, {"depth_enable", 1}, {"depth_write_mask", 1}, {"depth_func", 2},
      {"stencil_enable", 0}, {"stencil_read_mask", 255}, {"stencil_write_mask", 255},
      {"stencil_front_fail", 1}, {"stencil_front_depth_fail", 1},
      {"stencil_front_pass", 1}, {"stencil_front_func", 8},
      {"stencil_back_fail", 1}, {"stencil_back_depth_fail", 1},
      {"stencil_back_pass", 1}, {"stencil_back_func", 8},
    };
    for (const auto &field : defaults) {
      check(last_tuple_field(field.name) == field.expected, field.name);
    }

    IMTLD3D11DepthStencilState default_depth;
    default_depth.desc = kDefaultDepthStencilDesc;
    default_depth.desc.DepthFunc = D3D11_COMPARISON_GREATER;
    default_depth.desc.StencilReadMask = 0x3c;
    default_depth.desc.StencilWriteMask = 0xa5;
    state_context.default_depth_stencil_state = &default_depth;
    state_context.PreDraw<false>();
    check(tuples() == ++state_tuples && last_tuple_field("depth_func") == 5 &&
          last_tuple_field("stencil_read_mask") == 60 && last_tuple_field("stencil_write_mask") == 165,
          "null binding uses effective default DSS and prints byte masks as integers");
    depth_stencil.desc = default_depth.desc;
    state_context.state_.OutputMerger.DepthStencilState = &depth_stencil;
    default_depth.desc.DepthFunc = D3D11_COMPARISON_EQUAL;
    state_context.PreDraw<false>();
    check(tuples() == state_tuples, "bound DSS takes precedence and equal descriptors deduplicate across objects");
    state_context.state_.OutputMerger.DepthStencilState = nullptr;
    state_context.PreDraw<false>();
    check(tuples() == ++state_tuples && last_tuple_field("depth_func") == 3,
          "unbinding switches to current default DSS descriptor");
    state_context.default_depth_stencil_state = nullptr;
    state_context.PreDraw<false>();
    check(tuples() == state_tuples, "missing default object falls back to the original D3D default tuple");
    depth_stencil.desc = kDefaultDepthStencilDesc;
    state_context.state_.OutputMerger.DepthStencilState = &depth_stencil;
    state_context.PreDraw<false>();
    check(tuples() == state_tuples, "explicit default descriptor deduplicates with null DSS");

    auto changed = [&](const char *field, int64_t expected) {
      state_context.PreDraw<false>();
      check(tuples() == ++state_tuples && last_tuple_field(field) == expected, field);
      state_context.PreDraw<false>();
      check(tuples() == state_tuples, "identical descriptor state remains deduplicated");
    };
    dsv.desc.Flags = D3D11_DSV_READ_ONLY_DEPTH; changed("dsv_flags", 1);
    dsv.desc.Flags |= D3D11_DSV_READ_ONLY_STENCIL; changed("dsv_flags", 3);
    depth_stencil.desc.DepthEnable = FALSE; changed("depth_enable", 0);
    depth_stencil.desc.DepthWriteMask = D3D11_DEPTH_WRITE_MASK_ZERO; changed("depth_write_mask", 0);
    depth_stencil.desc.DepthFunc = D3D11_COMPARISON_GREATER_EQUAL; changed("depth_func", 7);
    depth_stencil.desc.StencilEnable = TRUE; changed("stencil_enable", 1);
    depth_stencil.desc.StencilReadMask = 0x5a; changed("stencil_read_mask", 90);
    depth_stencil.desc.StencilWriteMask = 0xa5; changed("stencil_write_mask", 165);
    depth_stencil.desc.FrontFace.StencilFailOp = D3D11_STENCIL_OP_ZERO; changed("stencil_front_fail", 2);
    depth_stencil.desc.FrontFace.StencilDepthFailOp = D3D11_STENCIL_OP_REPLACE; changed("stencil_front_depth_fail", 3);
    depth_stencil.desc.FrontFace.StencilPassOp = D3D11_STENCIL_OP_INCR_SAT; changed("stencil_front_pass", 4);
    depth_stencil.desc.FrontFace.StencilFunc = D3D11_COMPARISON_EQUAL; changed("stencil_front_func", 3);
    depth_stencil.desc.BackFace.StencilFailOp = D3D11_STENCIL_OP_DECR_SAT; changed("stencil_back_fail", 5);
    depth_stencil.desc.BackFace.StencilDepthFailOp = D3D11_STENCIL_OP_INVERT; changed("stencil_back_depth_fail", 6);
    depth_stencil.desc.BackFace.StencilPassOp = D3D11_STENCIL_OP_INCR; changed("stencil_back_pass", 7);
    depth_stencil.desc.BackFace.StencilFunc = D3D11_COMPARISON_NOT_EQUAL; changed("stencil_back_func", 6);
  }
  check(dsv.tex == &texture && comparison.desc.AddressU == D3D11_TEXTURE_ADDRESS_BORDER &&
        context.state_.stage.Samplers.test_bound(15), "trace preserves bound render objects");
  std::printf("SHADOW %s option=%s checks=%u failures=%u\n", failures ? "FAIL" : "PASS", argv[1], checks, failures);
  return failures ? 1 : 0;
}
"""


def host_source() -> str:
    source = (SRC / "d3d11/d3d11_context_impl.cpp").read_text()
    states = (SRC / "d3d11/d3d11_state_object.hpp").read_text()
    config = (SRC / "util/config/config.cpp").read_text()
    definitions = [definition(config, signature) for signature in (
        "Config::Config()", "Config::~Config()", "void Config::setOption(",
        "std::string Config::getOptionValue(",
        "bool Config::parseOptionValue(\n    const std::string&  value,\n          int32_t&      result)",
    )]
    helper = "struct D3D11ShadowTrace {};\n"
    if "class D3D11ShadowTrace" in source:
        trace = definition(source, "class D3D11ShadowTrace")
        # Entry instrumentation only: retain the executed production body to
        # detect calling the large diagnostic stack frame while disabled.
        anchor = "    if (!dxmt_shadow_trace_enabled()"
        if trace.count(anchor) != 1:
            raise ValueError("Expected one Record entry guard")
        trace = trace.replace(anchor, "    ++record_calls;\n" + anchor)
        helper = (definition(source, "inline bool dxmt_shadow_trace_enabled()") + "\n" +
                  trace + ";\n")
    start = source.index("template <bool IndexedDraw>\n  DrawCallStatus\n  PreDraw() {")
    # Execute the exact production prefix through the diagnostic hook. Later
    # draw encoding is outside the diagnostic's contract and remains on device.
    prefix = source[start:source.index("    /* ml1254: draw census */", start)]
    default_depth = definition(states, "constexpr D3D11_DEPTH_STENCIL_DESC kDefaultDepthStencilDesc") + ";\n"
    return PRELUDE + "\n".join(definitions) + default_depth + helper + CLASS + prefix + "return status;\n}\n};\n" + DRIVER + "\n}\nint main(int argc, char **argv) { return dxmt::main(argc, argv); }\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--optimized", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="madeira-shadow-trace-", dir="/private/tmp") as temporary:
        directory = Path(temporary)
        source, binary = directory / "contract.cpp", directory / "contract"
        source.write_text(host_source())
        flags = ["-O2"] if args.optimized else ["-O1", "-g", "-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
        subprocess.run(["clang++", "-std=c++20", "-Wall", "-Wextra", "-Werror", "-Wno-parentheses",
                        "-Wno-ignored-attributes", "-Wno-pragma-pack", "-Wno-microsoft-exception-spec", *flags,
                        "-isystem", str(ROOT / "wine/build-macos/include"), "-isystem", str(ROOT / "wine/include"),
                        "-I", str(SRC / "winemetal"), "-I", str(SRC / "util"), "-I", str(SRC / "dxmt"),
                        str(source), str(SRC / "util/sha1/sha1_util.cpp"), "-x", "c++",
                        str(SRC / "util/sha1/sha1.c"), "-o", str(binary)], check=True, timeout=60)
        failed = False
        for value, enabled in (("default", "0"), ("0", "0"), ("1", "1"), ("invalid", "0")):
            result = subprocess.run([str(binary), value, enabled], capture_output=True, text=True, timeout=20)
            print(result.stdout, end="", flush=True)
            print(result.stderr, end="", file=sys.stderr, flush=True)
            failed |= result.returncode != 0
        return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
