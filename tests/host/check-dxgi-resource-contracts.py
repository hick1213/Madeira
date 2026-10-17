#!/usr/bin/env python3
"""Execute DXMT's production DXGI reclaim and format/MSAA decisions.

COM declarations, Metal hardware queries, and logging are host boundaries.
The methods, DXGI-to-Metal mapping, and capability tables run unchanged C++.
This checks API contracts, not Metal rendering or any game's compatibility.
Fixtures are created on the external project volume and removed on exit.
"""

import argparse
from pathlib import Path
import re
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
DXMT = ROOT / "dxmt/src"
TOKENS = re.compile(
    r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[{}]',
    re.DOTALL,
)


def definition(source: str, signature: str) -> str:
    """Copy a complete production definition, ignoring literal/comment braces."""
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
#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <map>
#include <stdexcept>
#include <type_traits>
#define WIN32_LEAN_AND_MEAN
#include <d3d11.h>
#include <dxgi1_2.h>
using WINBOOL = BOOL; // Wine's host headers use BOOL; llvm-mingw uses WINBOOL.
static_assert(sizeof(WINBOOL) == 4);
#define DXMT_NATIVE 1
#include "winemetal.h"
namespace WMT {
struct Device {
  unsigned generation = 7;
  bool native_bc = true;
  unsigned max_samples = 8;
  bool supportsFamily(WMTGPUFamily family) const {
    return family == WMTGPUFamilyApple7 ||
      (family == WMTGPUFamilyApple8 && generation >= 8) ||
      (family == WMTGPUFamilyApple9 && generation >= 9);
  }
  bool supportsBCTextureCompression() const { return native_bc; }
  bool supportsTextureSampleCount(uint8_t samples) const {
    return samples <= max_samples && (samples == 1 || samples == 2 || samples == 4 || samples == 8);
  }
};
}
struct Logger { static void warn(const char *) {} static void err(const char *) {} };
namespace str { template<typename... Args> const char *format(Args...) { return "host log boundary"; } }
using MTLD3DError = std::runtime_error;
#include "d3d11_private.h"
"""

ENUM_OPERATORS = r"""
constexpr FormatCapability operator|(FormatCapability a, FormatCapability b) {
  return static_cast<FormatCapability>(static_cast<int>(a) | static_cast<int>(b));
}
constexpr FormatCapability operator&(FormatCapability a, FormatCapability b) {
  return static_cast<FormatCapability>(static_cast<int>(a) & static_cast<int>(b));
}
constexpr bool any_bit_set(FormatCapability value) { return static_cast<int>(value) != 0; }
"""

CLASS_PREFIX = r"""
class ProductionDevice {
  WMT::Device metal_;
  FormatCapabilityInspector inspector_;
public:
  explicit ProductionDevice(WMT::Device metal) : metal_(metal) { inspector_.Inspect(metal); }
  WMT::Device GetMTLDevice() const { return metal_; }
  FormatCapability GetMTLPixelFormatCapability(WMTPixelFormat format) {
    auto it = inspector_.textureCapabilities.find(static_cast<WMTPixelFormat>(ORIGINAL_FORMAT(format)));
    return it == inspector_.textureCapabilities.end() ? FormatCapability::None : it->second;
  }
"""

DRIVER = r"""
static unsigned failures, checks;
static void check(bool ok, const char *message) {
  std::printf("DXGI-CONTRACT %s %s\n", ok ? "PASS" : "FAIL", message);
  ++checks;
  if (!ok) ++failures;
}
static void reclaim_contracts() {
  ProductionDevice device({});
  // Only opaque resource identity is used by the no-eviction implementation.
  uintptr_t identities[2] = {0x12345678, 0x87654321};
  IDXGIResource *resources[2] = {
    reinterpret_cast<IDXGIResource *>(&identities[0]),
    reinterpret_cast<IDXGIResource *>(&identities[1]),
  };
  struct Output { uint32_t before; WINBOOL discarded[2]; uint32_t after; } out;
  for (UINT count : {1u, 2u}) {
    out.before = out.after = 0x1234abcd;
    out.discarded[0] = 0x6b6b6b6b;
    out.discarded[1] = 0x77777777;
    check(device.OfferResources(count, resources, DXGI_OFFER_RESOURCE_PRIORITY_NORMAL) == S_OK,
          "valid OfferResources succeeds");
    HRESULT hr = device.ReclaimResources(count, resources, out.discarded);
    check(hr == S_OK, "valid ReclaimResources succeeds");
    for (UINT i = 0; i < count; ++i) {
      check(out.discarded[i] == FALSE || out.discarded[i] == TRUE,
            "S_OK initializes every requested discarded BOOL");
      check(out.discarded[i] == FALSE, "no-eviction implementation reports preserved contents");
    }
    check(out.before == 0x1234abcd && out.after == 0x1234abcd,
          "ReclaimResources preserves array outer canaries");
    if (count == 1) check(out.discarded[1] == 0x77777777, "one-resource call does not write second output");
    check(identities[0] == 0x12345678 && identities[1] == 0x87654321,
          "no-eviction calls do not modify resource contents");
    WINBOOL *optional = nullptr;
    // Preserve the real NULL call even in O2 mutation runs: an inlined mutant
    // that writes NULL otherwise has undefined behavior the optimizer can erase.
    asm volatile("" : "+r"(optional));
    check(device.OfferResources(count, resources, DXGI_OFFER_RESOURCE_PRIORITY_NORMAL) == S_OK &&
          device.ReclaimResources(count, resources, optional) == S_OK,
          "optional discarded output may be NULL");
  }
}
static void format_contracts() {
  const DXGI_FORMAT formats[] = {DXGI_FORMAT_R8G8B8A8_UNORM, DXGI_FORMAT_B8G8R8A8_UNORM,
    DXGI_FORMAT_R16G16B16A16_FLOAT, DXGI_FORMAT_D32_FLOAT, DXGI_FORMAT_BC1_UNORM, DXGI_FORMAT_BC7_UNORM};
  const UINT counts[] = {0, 1, 2, 4, 8, 16, 32, 33, 257, 258, 260, UINT_MAX};
  for (unsigned maximum : {4u, 8u}) {
    ProductionDevice device({7, true, maximum});
    for (DXGI_FORMAT format : formats) {
      UINT support = 0xcccccccc;
      check(device.CheckFormatSupport(format, &support) == S_OK,
            "known format query succeeds");
      for (UINT samples : counts) {
        UINT quality = 0xcccccccc;
        HRESULT hr = device.CheckMultisampleQualityLevels(format, samples, &quality);
        std::printf("DXGI-CONTRACT matrix format=%u samples=%u max=%u support=%#x hr=%#x quality=%u\n",
                    unsigned(format), samples, maximum, support, unsigned(hr), quality);
        check(quality != 0xcccccccc, "MSAA quality output is initialized");
        if (!samples) check(FAILED(hr) || !quality, "zero samples stays unsupported");
        if (samples > maximum) check(FAILED(hr) || !quality, "unsupported hardware sample count is not advertised");
        if (samples > 1 && !(support & D3D11_FORMAT_SUPPORT_MULTISAMPLE_RENDERTARGET))
          check(FAILED(hr) || !quality, "format without MSAA capability is not advertised as multisampled");
        if (samples == 1 || (samples > 1 && samples <= maximum &&
            (support & D3D11_FORMAT_SUPPORT_MULTISAMPLE_RENDERTARGET)))
          check(SUCCEEDED(hr) && quality > 0, "supported hardware and format combination stays supported");
      }
    }
  }
}
int main(int argc, char **argv) {
  if (argc == 1 || !std::strcmp(argv[1], "reclaim")) reclaim_contracts();
  if (argc == 1 || !std::strcmp(argv[1], "msaa")) format_contracts();
  std::printf("DXGI-CONTRACT %s checks=%u failures=%u\n", failures ? "FAIL" : "PASS", checks, failures);
  return failures ? 1 : 0;
}
"""


def host_source(device: str | None = None) -> str:
    if device is None:
        device = (DXMT / "d3d11/d3d11_device.cpp").read_text()
    formats = (DXMT / "dxmt/dxmt_format.cpp").read_text()
    header = (DXMT / "dxmt/dxmt_format.hpp").read_text()
    enum = definition(header, "enum class FormatCapability : int") + ";\n"
    inspector = definition(header, "class FormatCapabilityInspector") + ";\n"
    flags = definition(header, "enum MTL_DXGI_FORMAT_FLAG") + ";\n"
    desc = definition(header, "struct MTL_DXGI_FORMAT_DESC") + ";\n"
    constants = formats[formats.index("constexpr FormatCapability ALL_CAP"):
                        formats.index("void\nFormatCapabilityInspector::Inspect(")]
    inspect = definition(formats, "void\nFormatCapabilityInspector::Inspect(")
    mapping = definition(formats, "int32_t\nMTLQueryDXGIFormat(")
    methods = []
    for signature in (
        "HRESULT STDMETHODCALLTYPE\n      OfferResources(",
        "HRESULT STDMETHODCALLTYPE ReclaimResources(",
        "HRESULT STDMETHODCALLTYPE\n      CheckFormatSupport(",
        "HRESULT STDMETHODCALLTYPE CheckMultisampleQualityLevels(",
        "HRESULT STDMETHODCALLTYPE\n      CheckMultisampleQualityLevels1(",
    ):
        # Removing override only adapts the surrounding COM declaration.
        methods.append(definition(device, signature).replace(" override", ""))
    return (PRELUDE + enum + ENUM_OPERATORS + inspector + flags + desc + constants +
            "\n#define APPEND_CAP(format, caps) textureCapabilities[format] = textureCapabilities[format] | caps;\n" +
            inspect + mapping + CLASS_PREFIX + "\n".join(methods) + "\n};\n" + DRIVER)


def mutations(device: str) -> list[tuple[str, str]]:
    """Alter executed production methods, never the test oracle or disk source."""
    reclaim = definition(device, "HRESULT STDMETHODCALLTYPE ReclaimResources(")
    msaa = definition(device, "HRESULT STDMETHODCALLTYPE\n      CheckMultisampleQualityLevels1(")
    variants = []
    for name, method, before, after in (
        ("reclaim-no-write", reclaim, "pDiscarded[i] = FALSE;", "(void)pDiscarded[i];"),
        ("reclaim-first-only", reclaim, "i < NumResources;", "i < 1;"),
        ("reclaim-false-discard", reclaim, "pDiscarded[i] = FALSE;", "pDiscarded[i] = TRUE;"),
        ("reclaim-null-write", reclaim, "if (pDiscarded) {", "if (true) {"),
        ("msaa-ignore-format", msaa, "if (SampleCount > 1 &&", "if (false &&"),
        ("msaa-truncate-sample-count", msaa, "if (SampleCount > D3D11_MAX_MULTISAMPLE_SAMPLE_COUNT)",
         "if (false && SampleCount > D3D11_MAX_MULTISAMPLE_SAMPLE_COUNT)"),
        ("msaa-reject-single-bc", msaa, "if (SampleCount > 1 &&", "if (true &&"),
        ("msaa-ignore-hardware", msaa, "supportsTextureSampleCount(SampleCount)", "supportsTextureSampleCount(1)"),
    ):
        if method.count(before) != 1 or device.count(method) != 1:
            raise ValueError(f"Mutation requires a unique production anchor: {name}")
        variants.append((name, device.replace(method, method.replace(before, after))))
    return variants


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("reclaim", "msaa"), help="Run one contract independently")
    parser.add_argument("--optimized", action="store_true", help="Use optimized C++ instead of ASan/UBSan")
    parser.add_argument("--mutations", action="store_true", help="Prove the contracts reject executed regressions")
    args = parser.parse_args()
    if args.mutations and args.case:
        parser.error("--mutations requires the full contract suite")
    with tempfile.TemporaryDirectory(prefix=".dxgi-resource-contract-", dir=ROOT / "tests/host") as temporary:
        directory = Path(temporary)
        source = directory / "contracts.cpp"
        binary = directory / "contracts"
        production = (DXMT / "d3d11/d3d11_device.cpp").read_text()
        source.write_text(host_source(production))
        flags = ["-O2"] if args.optimized else [
            "-O1", "-g", "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
            "-fno-omit-frame-pointer",
        ]
        compile_command = [
            "clang++", "-std=c++20", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter",
            "-Wno-ignored-attributes", "-Wno-pragma-pack", "-Wno-microsoft-exception-spec",
            *flags, "-isystem", str(ROOT / "wine/build-macos/include"),
            "-isystem", str(ROOT / "wine/include"), "-I", str(DXMT / "winemetal"),
            "-I", str(DXMT / "d3d11"),
            str(source), "-o", str(binary),
        ]
        subprocess.run(compile_command, check=True, timeout=60)
        command = [str(binary)] + ([args.case] if args.case else [])
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        print(result.stdout, end="", flush=True)
        print(result.stderr, end="", file=sys.stderr, flush=True)
        if result.returncode or not args.mutations:
            return result.returncode
        for name, changed in mutations(production):
            source.write_text(host_source(changed))
            # A compile failure is an error, never a detected mutation.
            subprocess.run(compile_command, check=True, timeout=60)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            if not result.returncode:
                print(f"FAIL: executed mutation survived: {name}", flush=True)
                return 1
            print(f"PASS: executed mutation rejected: {name}", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
