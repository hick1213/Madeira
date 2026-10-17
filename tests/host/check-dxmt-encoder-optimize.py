#!/usr/bin/env python3
"""Run the production encoder-optimizer gate and standalone pass emission.

Config parsing, the recorded-list traversal, optimizer call site, Clear case,
and ordinary multisample Resolve case are production source. Metal objects and
the optimizer relation function are spy boundaries. This checks the diagnostic
switch and preserved pass order/lifetimes, not optimizer or GPU correctness.
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
#include <algorithm>
#include <array>
#include <cassert>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <new>
#include <string>
#include <utility>
#include <vector>
#define DXMT_NATIVE 1
#include "winemetal.h"
#include "config/config.hpp"
namespace dxmt {
static unsigned config_accesses;
Config &Config::getInstance() { ++config_accesses; static Config value; return value; }
static std::vector<std::string> log_lines;
struct Logger { static void warn(const char *line) { log_lines.emplace_back(line); } };
"""

BOUNDARIES = r"""
enum class EncoderType { Null, Clear, Render, Resolve };
enum DXMT_ENCODER_LIST_OP { DXMT_ENCODER_LIST_OP_SYNCHRONIZE };
static unsigned destroyed;
struct EncoderData { EncoderData *next = nullptr; EncoderType type = EncoderType::Null; unsigned id = 0; };
struct Attachment { uint64_t value; uint64_t texture() const { return value; } };
struct ClearEncoderData : EncoderData {
  unsigned clear_dsv = 0;
  std::pair<double, uint32_t> depth_stencil = {0.375, 17};
  WMTClearColor color = {0.25, 0.5, 0.75, 1};
  unsigned array_length = 3, width = 96, height = 64;
  Attachment attachment{31};
  ~ClearEncoderData() { ++destroyed; }
};
struct ResolveEncoderData : EncoderData {
  Attachment src{41}, dst{42};
  ~ResolveEncoderData() { ++destroyed; }
};
static std::vector<WMTRenderPassInfo> passes;
static std::vector<std::string> events;
static unsigned ends;
constexpr auto WMTUTF8StringEncoding = 0;
namespace WMT {
struct String { static const char *string(const char *s, int) { return s; } };
void InitializeRenderPassInfo(WMTRenderPassInfo &info) { info = {}; }
struct Encoder {
  void setLabel(const char *label) { events.emplace_back(label); }
  void endEncoding() { ++ends; }
};
struct CommandBuffer {
  Encoder renderCommandEncoder(const WMTRenderPassInfo &info) {
    passes.push_back(info); return {};
  }
};
}
struct Context {
  EncoderData encoder_head;
  unsigned encoder_count_ = 0, relation_calls = 0;
  std::array<EncoderData *, 16> scratch{};
  void *allocate_cpu_heap(size_t size, size_t alignment) {
    assert(size <= sizeof(scratch) && alignment == alignof(EncoderData *));
    return scratch.data();
  }
  DXMT_ENCODER_LIST_OP checkEncoderRelation(EncoderData *, EncoderData *) {
    ++relation_calls; return DXMT_ENCODER_LIST_OP_SYNCHRONIZE;
  }
  void flush(WMT::CommandBuffer cmdbuf) {
"""

DRIVER = r"""
static unsigned checks, failures;
static void check(bool ok, const char *message) {
  ++checks;
  if (!ok) { ++failures; std::printf("ENCODER FAIL %s\n", message); }
}
int main(int argc, char **argv) {
  if (argc != 3) return 2;
  const bool enabled = std::strcmp(argv[2], "0") != 0;
  Config &config = Config::getInstance();
  if (std::strcmp(argv[1], "default")) config.setOption("dxmt.encoderOptimize", argv[1]);
  config_accesses = 0;
  Context empty;
  empty.flush({});
  check(empty.relation_calls == 0 && passes.empty() && events.empty() && config_accesses == 0 && log_lines.empty(),
        "empty submission performs no optimization, configuration read or emission");
  EncoderData single_render;
  single_render.type = EncoderType::Render;
  Context single;
  single.encoder_head.next = &single_render;
  single.encoder_count_ = 1;
  single.flush({});
  check(single.relation_calls == 0 && events == std::vector<std::string>{"Render"} &&
        config_accesses == 0 && log_lines.empty(), "single pass emits without optimizer work");
  for (unsigned iteration = 0; iteration < 2; ++iteration) {
    // Heap storage is explicitly released after production destructors run.
    auto *color = new ClearEncoderData;
    auto *depth = new ClearEncoderData;
    auto *resolve = new ResolveEncoderData;
    EncoderData render;
    color->type = depth->type = EncoderType::Clear;
    render.type = EncoderType::Render;
    resolve->type = EncoderType::Resolve;
    depth->clear_dsv = 3;
    color->next = &render; render.next = depth; depth->next = resolve;
    Context context;
    context.encoder_head.next = color;
    context.encoder_count_ = 4;
    passes.clear(); events.clear(); ends = destroyed = 0;
    context.flush({});
    check(context.relation_calls == (enabled ? 3u : 0u), "gate controls only optimizer calls");
    check(events == std::vector<std::string>{"ClearPass", "Render", "ClearPass", "ResolvePass"},
          "recorded pass emission order is preserved");
    check(passes.size() == 3 && ends == 3, "every standalone clear and resolve is encoded and ended");
    check(destroyed == 3, "standalone encoder resources are destroyed exactly once");
    if (passes.size() == 3) {
      const auto &c = passes[0];
      check(c.colors[0].texture == 31 && c.colors[0].load_action == WMTLoadActionClear &&
            c.colors[0].store_action == WMTStoreActionStore && c.colors[0].clear_color.b == 0.75 &&
            c.render_target_array_length == 3, "color clear preserves attachment, value and store");
      const auto &d = passes[1];
      check(d.depth.texture == 31 && d.depth.load_action == WMTLoadActionClear &&
            d.depth.store_action == WMTStoreActionStore && d.depth.clear_depth == 0.375 &&
            d.stencil.texture == 31 && d.stencil.load_action == WMTLoadActionClear &&
            d.stencil.store_action == WMTStoreActionStore && d.stencil.clear_stencil == 17 &&
            d.render_target_width == 96 && d.render_target_height == 64 && d.render_target_array_length == 3,
            "depth and stencil clear preserve dimensions, values and stores");
      const auto &r = passes[2];
      check(r.colors[0].texture == 41 && r.colors[0].resolve_texture == 42 &&
            r.colors[0].load_action == WMTLoadActionLoad &&
            r.colors[0].store_action == WMTStoreActionStoreAndMultisampleResolve,
            "ordinary resolve remains a standalone resolve pass");
    }
    check(config_accesses == 1, "mode is read only once across flushes");
    config.setOption("dxmt.encoderOptimize", enabled ? "0" : "1");
    ::operator delete(color); ::operator delete(depth); ::operator delete(resolve);
  }
  check(log_lines == (enabled ? std::vector<std::string>{} :
        std::vector<std::string>{"[encoder-optimize] enabled=0 (diagnostic; preserving recorded passes)"}),
        "only disabled diagnostic mode is logged, once");
  std::printf("ENCODER %s option=%s enabled=%u checks=%u failures=%u\n",
              failures ? "FAIL" : "PASS", argv[1], unsigned(enabled), checks, failures);
  return failures ? 1 : 0;
}
}
int main(int argc, char **argv) { return dxmt::main(argc, argv); }
"""


def host_source() -> str:
    context = (SRC / "dxmt/dxmt_context.cpp").read_text()
    config = (SRC / "util/config/config.cpp").read_text()
    definitions = [definition(config, signature) for signature in (
        "Config::Config()", "Config::~Config()", "void Config::setOption(",
        "std::string Config::getOptionValue(",
        "bool Config::parseOptionValue(\n    const std::string&  value,\n          int32_t&      result)",
    )]
    helper = ""
    if "static bool\nencoderOptimizationEnabled()" in context:
        helper = definition(context, "static bool\nencoderOptimizationEnabled()")
    threshold = re.search(r"constexpr unsigned kEncoderOptimizerThreshold = \d+;", context)
    if threshold is None:
        raise ValueError("Encoder optimizer threshold was not found")
    flush = definition(context, "ArgumentEncodingContext::flushCommands(")
    gather = flush[flush.index("  unsigned encoder_count ="):flush.index("  std::unique_ptr<VisibilityResultReadback>")]
    clear = flush[flush.index("    case EncoderType::Clear:"):flush.index("    case EncoderType::Resolve:")]
    resolve_start = flush.index("      {\n        WMTRenderPassInfo info;\n        WMT::InitializeRenderPassInfo(info);\n        info.colors[0].texture = data->src.texture();")
    resolve_end = flush.index("    /* MADEIRA (WOW64_DESIGN.md section 7.11): the Direct3D 9 StretchRect", resolve_start)
    resolve = "    case EncoderType::Resolve: {\n      auto data = static_cast<ResolveEncoderData *>(current);\n" + flush[resolve_start:resolve_end]
    iteration = flush[flush.index("  while (encoder_index) {"):flush.index("    case EncoderType::Render: {")]
    iteration += '    case EncoderType::Render: events.emplace_back("Render"); break;\n'
    iteration += clear + resolve + "    default: break;\n    }\n    --encoder_index;\n  }\n"
    return PRELUDE + "\n".join(definitions) + helper + threshold[0] + BOUNDARIES + gather + iteration + "  }\n};\n" + DRIVER


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--optimized", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="madeira-encoder-optimize-", dir="/private/tmp") as temporary:
        directory = Path(temporary)
        source, binary = directory / "contract.cpp", directory / "contract"
        source.write_text(host_source())
        flags = ["-O2"] if args.optimized else ["-O1", "-g", "-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
        subprocess.run(["clang++", "-std=c++20", "-Wall", "-Wextra", "-Werror", *flags,
                        "-I", str(SRC / "winemetal"), "-I", str(SRC / "util"),
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
