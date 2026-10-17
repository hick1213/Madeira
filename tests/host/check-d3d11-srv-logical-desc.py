#!/usr/bin/env python3
"""Execute production D3D11 texture SRV creation and descriptor normalization.

Only COM ownership, format/hardware queries, and Metal view creation are host
boundaries. The creation method, normalization, default expansion, descriptor
layouts and GetDesc downgrade run real source. This is not GPU/device proof.
Compilation uses an automatically removed internal APFS temporary directory.
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
    raise ValueError(f"Unterminated production definition: {signature}")


PRELUDE = r"""
#include <cstdint>
#include <climits>
#include <cstdio>
#include <cstring>
#include <cassert>
#include <type_traits>
#define WIN32_LEAN_AND_MEAN
#include <d3d11_3.h>
#define DXMT_NATIVE 1
#include "winemetal.h"
#define ERR(...) ((void)0)
#define D3D11_ASSERT(...) assert(__VA_ARGS__)
using TextureViewKey = unsigned;
struct MTLD3D11Device { int GetMTLDevice() { return 0; } };
"""

BOUNDARIES = r"""
struct Texture {
  WMTTextureType type;
  unsigned views = 0;
  TextureViewDescriptor physical{};
  WMTTextureType textureType() { return type; }
  WMTPixelFormat pixelFormat() { return WMTPixelFormatRGBA8Unorm; }
  TextureViewKey createView(const TextureViewDescriptor &desc) {
    physical = desc; return ++views;
  }
};
// This test uses one ordinary color format. Format lookup is the hardware boundary.
static HRESULT MTLQueryDXGIFormat(int, DXGI_FORMAT format, MTL_DXGI_FORMAT_DESC &out) {
  if (format != DXGI_FORMAT_R8G8B8A8_UNORM) return E_FAIL;
  out.PixelFormat = WMTPixelFormatRGBA8Unorm; return S_OK;
}
template<class R, class V> HRESULT ExtractEntireResourceViewDescription(const R *, V *);
template<class V> HRESULT InitializeAndNormalizeViewDescriptor(
  MTLD3D11Device *, unsigned, unsigned, Texture *, V &, TextureViewDescriptor &);
"""

CLASS = r"""
// COM allocation is replaced with a host object. Descriptor copying is the same
// operation as TResourceViewBase's constructor; GetDesc runs its real downgrade.
struct HostView {
  struct tag {
    using DESC = D3D11_SHADER_RESOURCE_VIEW_DESC;
    using DESC1 = D3D11_SHADER_RESOURCE_VIEW_DESC1;
  };
  D3D11_SHADER_RESOURCE_VIEW_DESC1 desc;
HOST_VIEW_METHODS
};
#define ID3D11ShaderResourceView1 HostView
template<class T> T *ref(T *p) { return p; }
struct tag_texture_2d { using DESC1 = D3D11_TEXTURE2D_DESC1; };
template<class tag_texture> struct DeviceTexture {
  typename tag_texture::DESC1 desc;
  MTLD3D11Device *m_parent;
  uint32_t mip_bias_;
  struct Pointer {
    Texture *value;
    Texture *ptr() { return value; }
    Texture *operator->() { return value; }
  } underlying_texture_;
  struct TextureSRV : HostView {
    TextureSRV(TextureViewKey, const D3D11_SHADER_RESOURCE_VIEW_DESC1 *p,
               DeviceTexture *, MTLD3D11Device *) { desc = *p; }
  };
"""

DRIVER = r"""
static unsigned checks, failures;
static const char *current;
static void check(bool ok, const char *message) {
  ++checks;
  if (!ok) { ++failures; std::printf("SRV FAIL %s: %s\n", current, message); }
}
struct Case {
  const char *name;
  D3D11_SRV_DIMENSION dimension;
  WMTTextureType type;
  UINT arraySize, bias, most, count, first, arrays;
  UINT wantMost, wantCount, wantArrays, physicalMost, physicalCount, physicalArrays;
  bool defaultDesc;
};
static void run(const Case &c) {
  current = c.name;
  MTLD3D11Device parent;
  Texture texture{c.type};
  D3D11_TEXTURE2D_DESC1 resource{};
  resource.Width = resource.Height = 1024;
  resource.MipLevels = 11;
  resource.ArraySize = c.arraySize;
  resource.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
  resource.SampleDesc.Count = 1;
  if (c.type == WMTTextureTypeCube || c.type == WMTTextureTypeCubeArray)
    resource.MiscFlags = D3D11_RESOURCE_MISC_TEXTURECUBE;
  DeviceTexture<tag_texture_2d> device{resource, &parent, c.bias, {&texture}};
  D3D11_SHADER_RESOURCE_VIEW_DESC1 input{};
  input.Format = resource.Format;
  input.ViewDimension = c.dimension;
  // The real D3D union shares most/count and array first/size across these views.
  input.Texture2DArray.MostDetailedMip = c.most;
  input.Texture2DArray.MipLevels = c.count;
  input.Texture2DArray.FirstArraySlice = c.first;
  input.Texture2DArray.ArraySize = c.arrays;
  const auto saved = input;
  const auto *requested = c.defaultDesc ? nullptr : &input;
  check(device.CreateShaderResourceView(requested, nullptr) == S_FALSE,
        "validation-only succeeds without a view");
  check(texture.views == 0, "validation-only never allocates Metal view");
  HostView *view = nullptr;
  check(device.CreateShaderResourceView(requested, &view) == S_OK && view,
        "creation succeeds");
  if (!view) return;
  D3D11_SHADER_RESOURCE_VIEW_DESC1 got{};
  D3D11_SHADER_RESOURCE_VIEW_DESC legacy{};
  view->GetDesc1(&got);
  view->GetDesc(&legacy);
  check(got.Texture2D.MostDetailedMip == c.wantMost, "GetDesc1 preserves logical most mip");
  check(got.Texture2D.MipLevels == c.wantCount, "GetDesc1 returns finite logical mip count");
  check(legacy.Texture2D.MostDetailedMip == c.wantMost, "GetDesc preserves logical most mip");
  check(legacy.Texture2D.MipLevels == c.wantCount, "GetDesc returns finite logical mip count");
  check(got.Format == resource.Format && got.ViewDimension == c.dimension,
        "logical format and dimension are preserved");
  if (c.dimension == D3D11_SRV_DIMENSION_TEXTURE2DARRAY ||
      c.dimension == D3D11_SRV_DIMENSION_TEXTURECUBEARRAY) {
    check(got.Texture2DArray.ArraySize == c.wantArrays, "GetDesc1 normalizes logical array count");
    check(legacy.Texture2DArray.ArraySize == c.wantArrays, "GetDesc normalizes logical array count");
    check(got.Texture2DArray.FirstArraySlice == c.first, "GetDesc1 preserves first array slice");
  }
  check(texture.physical.firstMiplevel == c.physicalMost, "physical most mip uses bias");
  check(texture.physical.miplevelCount == c.physicalCount, "physical mip count uses bias");
  check(texture.physical.arraySize == c.physicalArrays, "physical array size stays correct");
  check(texture.views == 1, "creation allocates exactly one Metal view");
  check(!std::memcmp(&input, &saved, sizeof(input)), "caller input descriptor is unchanged");
  check(device.desc.Width == 1024 && device.desc.Height == 1024 && device.desc.MipLevels == 11,
        "resource dimensions and mip count stay logical");
  delete view;
  std::printf("SRV CASE %s complete\n", c.name);
}
int main() {
  const Case cases[] = {
    {"2d-all-bias0", D3D11_SRV_DIMENSION_TEXTURE2D, WMTTextureType2D, 1,0,0,UINT_MAX,0,1, 0,11,1,0,11,1,false},
    {"2d-all-bias3", D3D11_SRV_DIMENSION_TEXTURE2D, WMTTextureType2D, 1,3,0,UINT_MAX,0,1, 0,11,1,0,8,1,false},
    {"2d-subset-all-bias3", D3D11_SRV_DIMENSION_TEXTURE2D, WMTTextureType2D, 1,3,5,UINT_MAX,0,1, 5,6,1,2,6,1,false},
    {"2d-cross-bias-all", D3D11_SRV_DIMENSION_TEXTURE2D, WMTTextureType2D, 1,3,1,UINT_MAX,0,1, 1,10,1,0,8,1,false},
    {"2d-finite-cross-bias", D3D11_SRV_DIMENSION_TEXTURE2D, WMTTextureType2D, 1,3,1,5,0,1, 1,5,1,0,3,1,false},
    {"2d-finite-above-bias", D3D11_SRV_DIMENSION_TEXTURE2D, WMTTextureType2D, 1,3,5,4,0,1, 5,4,1,2,4,1,false},
    {"2d-null-bias0", D3D11_SRV_DIMENSION_TEXTURE2D, WMTTextureType2D, 1,0,0,0,0,1, 0,11,1,0,11,1,true},
    {"2d-null-bias3", D3D11_SRV_DIMENSION_TEXTURE2D, WMTTextureType2D, 1,3,0,0,0,1, 0,11,1,0,8,1,true},
    {"array-all-bias0", D3D11_SRV_DIMENSION_TEXTURE2DARRAY, WMTTextureType2DArray, 7,0,2,UINT_MAX,2,UINT_MAX, 2,9,5,2,9,5,false},
    {"array-all-bias3", D3D11_SRV_DIMENSION_TEXTURE2DARRAY, WMTTextureType2DArray, 7,3,2,UINT_MAX,2,UINT_MAX, 2,9,5,0,8,5,false},
    {"array-null-bias3", D3D11_SRV_DIMENSION_TEXTURE2DARRAY, WMTTextureType2DArray, 7,3,0,0,0,7, 0,11,7,0,8,7,true},
    {"cube-all-bias3", D3D11_SRV_DIMENSION_TEXTURECUBE, WMTTextureTypeCube, 6,3,1,UINT_MAX,0,1, 1,10,1,0,8,6,false},
    {"cube-null-bias3", D3D11_SRV_DIMENSION_TEXTURECUBE, WMTTextureTypeCube, 6,3,0,0,0,1, 0,11,1,0,8,6,true},
    {"cubearray-all-bias3", D3D11_SRV_DIMENSION_TEXTURECUBEARRAY, WMTTextureTypeCubeArray, 18,3,4,UINT_MAX,6,UINT_MAX, 4,7,2,1,7,12,false},
    {"cubearray-null-bias3", D3D11_SRV_DIMENSION_TEXTURECUBEARRAY, WMTTextureTypeCubeArray, 18,3,0,0,0,3, 0,11,3,0,8,18,true},
  };
  for (const auto &c : cases) run(c);
  std::printf("SRV %s checks=%u failures=%u\n", failures ? "FAIL" : "PASS", checks, failures);
  return failures ? 1 : 0;
}
"""


def host_source(production: str) -> str:
    resource = (SRC / "d3d11/d3d11_resource.hpp").read_text()
    helper = (SRC / "d3d11/d3d11_resource_helper.cpp").read_text()
    normalizer = (SRC / "d3d11/d3d11_resource_view_helper.cpp").read_text()
    method = definition(production, "HRESULT\n  STDMETHODCALLTYPE\n  CreateShaderResourceView(")
    return (PRELUDE +
            definition((SRC / "dxmt/dxmt_texture.hpp").read_text(), "struct TextureViewDescriptor") + ";\n" +
            definition((SRC / "dxmt/dxmt_format.hpp").read_text(), "struct MTL_DXGI_FORMAT_DESC") + ";\n" +
            BOUNDARIES +
            definition(resource, "template <typename RESOURCE_DESC, typename VIEW_DESC>\nHRESULT ExtractEntireResourceViewDescription(const RESOURCE_DESC *pResourceDesc,\n                                             const VIEW_DESC *pViewDescIn,") + "\n" +
            definition(helper, "template <>\nHRESULT ExtractEntireResourceViewDescription<D3D11_TEXTURE2D_DESC1,\n                                             D3D11_SHADER_RESOURCE_VIEW_DESC1>") + "\n" +
            definition(normalizer, "void FixDepthStencilFormat(") + "\n" +
            definition(normalizer, "template <>\nHRESULT\nInitializeAndNormalizeViewDescriptor(") + "\n" +
            definition(normalizer, "void DowngradeViewDescription(const D3D11_SHADER_RESOURCE_VIEW_DESC1 &src,") + "\n" +
            CLASS.replace("HOST_VIEW_METHODS",
                          definition(resource, "void STDMETHODCALLTYPE GetDesc(tag::DESC *pDesc)").replace(" final", "") + "\n" +
                          definition(resource, "void STDMETHODCALLTYPE GetDesc1(tag::DESC1 *pDesc)")) +
            method.replace(" override", "") + "\n};\n" + DRIVER)


def mutations(production: str) -> list[tuple[str, str]]:
    """Mutate executed source only; keep independent literal test expectations."""
    start = production.index("    // Expand sentinel counts in the logical descriptor")
    end = production.index("    /* ml675: TWO descriptors.", start)
    logical = production[start:end]
    return [
        ("omit-logical-normalization", production[:start] + production[end:]),
        ("cache-physical-description", production.replace(
            "new TextureSRV(key, &finalDesc, this, this->m_parent)",
            "new TextureSRV(key, &physDesc, this, this->m_parent)")),
        ("normalize-logical-with-physical-mips", production.replace(logical, logical.replace(
            "this->desc.MipLevels, arraySize", "this->desc.MipLevels - mip_bias_, arraySize"))),
        # Keep cube array sizes divisible by six so this mutation reaches the
        # descriptor assertions instead of aborting on an invalid cube fixture.
        ("normalize-logical-with-shortened-array", production.replace(logical, logical.replace(
            "this->desc.MipLevels, arraySize",
            "this->desc.MipLevels, arraySize > 6 ? arraySize - 6 : arraySize"))),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--optimized", action="store_true")
    parser.add_argument("--mutations", action="store_true")
    args = parser.parse_args()
    production = (SRC / "d3d11/d3d11_texture_device.cpp").read_text()
    with tempfile.TemporaryDirectory(prefix="madeira-srv-contract-", dir="/private/tmp") as temporary:
        directory = Path(temporary)
        source, binary = directory / "contract.cpp", directory / "contract"
        source.write_text(host_source(production))
        flags = ["-O2"] if args.optimized else ["-O1", "-g", "-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
        compile_command = ["clang++", "-std=c++20", "-Wall", "-Wextra", "-Werror",
                        "-Wno-unused-parameter", "-Wno-ignored-attributes", "-Wno-pragma-pack",
                        "-Wno-microsoft-exception-spec", *flags,
                        "-isystem", str(ROOT / "wine/build-macos/include"),
                        "-isystem", str(ROOT / "wine/include"), "-I", str(SRC / "winemetal"),
                        str(source), "-o", str(binary)]
        subprocess.run(compile_command, check=True, timeout=60)
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
        print(result.stdout, end="", flush=True)
        print(result.stderr, end="", file=sys.stderr, flush=True)
        if result.returncode or not args.mutations:
            return result.returncode
        for name, changed in mutations(production):
            if changed == production:
                raise ValueError(f"Mutation anchor did not change source: {name}")
            source.write_text(host_source(changed))
            subprocess.run(compile_command, check=True, timeout=60)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            if not result.returncode:
                print(f"SRV FAIL mutation survived: {name}", flush=True)
                return 1
            summary = re.search(r"^SRV FAIL checks=\d+ failures=[1-9]\d*$", result.stdout, re.MULTILINE)
            if result.returncode != 1 or summary is None:
                print(f"SRV FAIL unexpected mutation execution failure: {name}; exit={result.returncode}", flush=True)
                print(result.stdout, end="", flush=True)
                print(result.stderr, end="", file=sys.stderr, flush=True)
                return 1
            print(f"SRV PASS executed mutation rejected: {name}", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
