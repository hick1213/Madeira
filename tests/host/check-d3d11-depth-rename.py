#!/usr/bin/env python3
"""Execute production depth clear with real Config lookup/parsing.

Metal encoding, resource pools and COM ownership are narrow host boundaries.
ClearDepthStencilView, the policy helper, and format planar flags run actual
source; this verifies the diagnostic switch, not game rendering.
"""

from pathlib import Path
import argparse
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
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <functional>
#include <sstream>
#include <string>
#include <utility>
#include <vector>
#define DXMT_NATIVE 1
#include "winemetal.h"
#include "config/config.hpp"
namespace dxmt {
using UINT = unsigned;
using FLOAT = float;
using UINT8 = uint8_t;
static unsigned config_accesses;
Config &Config::getInstance() { ++config_accesses; static Config value; return value; }
static std::vector<std::string> log_lines;
template<class... T> void host_log(T... values) {
  std::ostringstream line; (line << ... << values); log_lines.push_back(line.str());
}
#define ERR(...) host_log(__VA_ARGS__)
"""

BOUNDARIES = r"""
template<class T> struct Rc {
  T *value;
  T *ptr() { return value; }
  T *operator->() { return value; }
};
template<class T> Rc<T> forward_rc(Rc<T> &value) { return value; }
static std::string events;
struct Texture {
  WMTPixelFormat format;
  unsigned renames = 0;
  WMTPixelFormat pixelFormat() { return format; }
  void rename(unsigned allocation) {
    if (allocation != 701) throw "wrong allocation";
    ++renames; events += "R";
  }
};
struct RenamableTexturePool {
  unsigned nexts = 0;
  unsigned getNext(uint64_t sequence) {
    if (sequence != 42) throw "wrong sequence";
    ++nexts; events += "N"; return 701;
  }
};
struct D3D11DepthStencilView {
  struct Props { UINT RenderTargetArrayLength = 3; } props;
  Rc<Texture> tex;
  Rc<RenamableTexturePool> pool;
  Props &description() { return props; }
  Rc<Texture> texture() { return tex; }
  Rc<RenamableTexturePool> renamable() { return pool; }
  unsigned viewId() { return 17; }
};
struct ArgumentEncodingContext {
  Texture *cleared = nullptr;
  UINT view = 0, length = 0, flags = 0;
  FLOAT depth = 0;
  UINT8 stencil = 0;
  unsigned clears = 0;
  uint64_t currentSeqId() { events += "S"; return 42; }
  void clearDepthStencil(Rc<Texture> texture, UINT v, UINT l, UINT f, FLOAT d, UINT8 s) {
    cleared = texture.ptr(); view = v; length = l; flags = f; depth = d; stencil = s;
    ++clears; events += "C";
  }
};
struct Context {
  std::function<void(ArgumentEncodingContext &)> queued;
  unsigned invalidations = 0;
  void InvalidateCurrentPass() { ++invalidations; events += "I"; }
  template<class F> void EmitOP(F operation) { queued = std::move(operation); events += "E"; }
"""

DRIVER = r"""
static unsigned checks, failures;
static void check(bool ok, const char *message) {
  ++checks; if (!ok) { ++failures; std::printf("DEPTH FAIL %s\n", message); }
}
int main(int argc, char **argv) {
  if (argc != 3) return 2;
  const bool enabled = std::strcmp(argv[2], "0") != 0;
  Config &config = Config::getInstance();
  if (std::strcmp(argv[1], "default")) config.setOption("d3d11.depthRename", argv[1]);
  config_accesses = 0;
  struct Case { UINT flags; WMTPixelFormat format; bool pool, rename; };
  const Case cases[] = {
    {0, WMTPixelFormatDepth32Float_Stencil8, true, false},
    {3, WMTPixelFormatDepth32Float_Stencil8, true, true},
    {1, WMTPixelFormatDepth32Float_Stencil8, true, false},
    {2, WMTPixelFormatDepth32Float_Stencil8, true, false},
    {7, WMTPixelFormatDepth32Float_Stencil8, true, true},
    {3, WMTPixelFormatDepth32Float_Stencil8, false, false},
    {1, WMTPixelFormatDepth32Float, true, true},
    {2, WMTPixelFormatStencil8, true, true},
    {2, WMTPixelFormatDepth32Float, true, false},
  };
  for (const auto &c : cases) {
    events.clear();
    Texture texture{c.format};
    RenamableTexturePool pool;
    D3D11DepthStencilView view{{3}, {&texture}, {c.pool ? &pool : nullptr}};
    Context context;
    ArgumentEncodingContext encoder;
    context.ClearDepthStencilView(&view, c.flags, 0.375f, 239);
    check(encoder.clears == 0 && texture.renames == 0, "encoding is deferred");
    if (!c.flags) {
      check(!context.queued && context.invalidations == 0 && events.empty(), "zero flags preserve existing no-op");
      continue;
    }
    const unsigned accesses_before_encode = config_accesses;
    // A queued operation must use the captured policy. Re-read config here is a bug.
    config.setOption("d3d11.depthRename", enabled ? "0" : "1");
    context.queued(encoder);
    check(config_accesses == accesses_before_encode, "GPU encoding performs no Config access");
    check(encoder.clears == 1 && encoder.cleared == &texture, "clear always executes once on same texture");
    check(encoder.view == 17 && encoder.length == 3 && encoder.flags == (c.flags & 3) &&
          encoder.depth == 0.375f && encoder.stencil == 239, "clear arguments are unchanged");
    check(context.invalidations == 1, "clear invalidates current pass exactly once");
    const bool renamed = enabled && c.rename;
    check(texture.renames == unsigned(renamed) && pool.nexts == unsigned(renamed), "policy gates only eligible rename");
    check(events == (renamed ? "IESNRC" : "IEC"), "rename/clear ordering is preserved");
    check(config_accesses == 1, "policy is cached once outside GPU encoding");
  }
  check(log_lines.size() == 1 && log_lines[0] == std::string("[depth-rename] enabled=") + (enabled ? "1" : "0"),
        "effective mode is logged once");
  std::printf("DEPTH %s option=%s enabled=%u checks=%u failures=%u\n",
              failures ? "FAIL" : "PASS", argv[1], unsigned(enabled), checks, failures);
  return failures ? 1 : 0;
}
"""


def host_source() -> str:
    context = (SRC / "d3d11/d3d11_context_impl.cpp").read_text()
    config = (SRC / "util/config/config.cpp").read_text()
    definitions = [definition(config, signature) for signature in (
        "Config::Config()", "Config::~Config()", "void Config::setOption(",
        "std::string Config::getOptionValue(",
        "bool Config::parseOptionValue(\n    const std::string&  value,\n          int32_t&      result)",
    )]
    policy = ""
    if "inline bool dxmt_depth_rename_enabled()" in context:
        policy = definition(context, "inline bool dxmt_depth_rename_enabled()")
    flags = definition((SRC / "dxmt/dxmt_format.cpp").read_text(), "uint32_t\nDepthStencilPlanarFlags(")
    clear = definition(context, "void\n  ClearDepthStencilView(D3D11DepthStencilView *")
    return PRELUDE + "\n".join(definitions) + policy + flags + BOUNDARIES + clear + "\n};\n" + DRIVER + "\n}\nint main(int argc, char **argv) { return dxmt::main(argc, argv); }\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--optimized", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="madeira-depth-rename-", dir="/private/tmp") as temporary:
        directory = Path(temporary)
        source, binary = directory / "contract.cpp", directory / "contract"
        source.write_text(host_source())
        flags = ["-O2"] if args.optimized else ["-O1", "-g", "-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
        subprocess.run(["clang++", "-std=c++20", "-Wall", "-Wextra", "-Werror",
                        *flags, "-I", str(SRC / "winemetal"), "-I", str(SRC / "util"),
                        str(source), "-o", str(binary)], check=True, timeout=60)
        failed = False
        for value, enabled in (("default", "1"), ("0", "0"), ("1", "1"), ("invalid", "1"), ("", "1"), ("-1", "1")):
            result = subprocess.run([str(binary), value, enabled], capture_output=True, text=True, timeout=20)
            print(result.stdout, end="", flush=True)
            print(result.stderr, end="", file=sys.stderr, flush=True)
            failed |= result.returncode != 0
        return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
