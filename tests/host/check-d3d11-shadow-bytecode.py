#!/usr/bin/env python3
"""Execute real shader-cache creation and bounded diagnostic file writes.

SM50 initialization is the external compiler boundary; Config, SHA1, cache
locking/dedup and file I/O run actual source. All output stays in owned tempdirs.
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
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <map>
#include <memory>
#include <mutex>
#include <shared_mutex>
#include <sstream>
#include <string>
#include <vector>
#include "config/config.hpp"
#include "sha1/sha1_util.hpp"
namespace dxmt {
static std::vector<std::string> messages;
Config &Config::getInstance() { static Config value; return value; }
template<class... T> void host_log(T... values) {
  std::ostringstream stream; (stream << ... << values); messages.push_back(stream.str());
}
#define ERR(...) host_log(__VA_ARGS__)
using sm50_error_t = int;
using sm50_shader_t = int;
struct MTL_SHADER_REFLECTION {};
static unsigned initialized;
static int SM50Initialize(const void *p, uint32_t n, sm50_shader_t *shader,
                         MTL_SHADER_REFLECTION *, sm50_error_t *error) {
  if (!p || n < 4 || std::memcmp(p, "DXBC", 4)) { *error = 1; return 1; }
  uint32_t tag = 0;
  if (n >= 8) std::memcpy(&tag,static_cast<const uint8_t *>(p)+4,4);
  if (tag == 0xfeedbeef) { *error = 2; return 1; }
  *shader = int(++initialized); return 0;
}
static const char *SM50GetErrorMessageString(sm50_error_t) { return "invalid shader"; }
static void SM50FreeError(sm50_error_t) {}
"""

CLASS = r"""
class PipelineCache {
  struct CachedSM50Shader {
    CachedSM50Shader(PipelineCache *, sm50_shader_t, const Sha1Digest &, MTL_SHADER_REFLECTION &) {}
  };
  std::map<Sha1Digest, std::unique_ptr<CachedSM50Shader>,
           bool(*)(const Sha1Digest &, const Sha1Digest &)> shaders_{
    [](const Sha1Digest &a, const Sha1Digest &b) { return std::memcmp(a.data,b.data,20)<0; }};
  std::shared_mutex mutex_shares;
public:
"""

DRIVER = r"""
static unsigned checks, failures;
static void check(bool value, const char *message) {
  ++checks; if (!value) { ++failures; std::printf("BYTECODE FAIL %s\n", message); }
}
static std::vector<uint8_t> blob(uint32_t tag, size_t size=8) {
  std::vector<uint8_t> data(size, 0);
  std::memcpy(data.data(), "DXBC", 4); std::memcpy(data.data()+4, &tag, 4); return data;
}
static size_t count(const std::filesystem::path &path) {
  if (!std::filesystem::is_directory(path)) return 0;
  size_t result=0; for (const auto &p : std::filesystem::directory_iterator(path))
    if (p.path().extension()==".cso") ++result;
  return result;
}
static uint64_t bytes(const std::filesystem::path &path) {
  uint64_t result=0; for (const auto &p : std::filesystem::directory_iterator(path))
    if (p.path().extension()==".cso") result+=p.file_size();
  return result;
}
static bool logged(const char *part) {
  for (const auto &message : messages) if (message.find(part)!=std::string::npos) return true;
  return false;
}
int main(int argc, char **argv) {
  if (argc!=3) return 2;
  std::string mode=argv[1]; std::filesystem::path output=argv[2];
  if (mode!="off") Config::getInstance().setOption("d3d11.shadowBytecodePath", output.string());
  PipelineCache cache;
  auto data=blob(0x04030201);
  if (mode=="off") {
    check(cache.CreateShader(data.data(),data.size())!=nullptr,"default-off preserves valid shader creation");
    dxmt_shadow_dump_bytecode(Sha1Digest{},reinterpret_cast<const void *>(1),8);
    check(count(output)==0 && messages.empty(),"default-off writes/logs nothing or reads bytecode");
  } else if (mode=="write") {
    check(cache.CreateShader(data.data(),data.size())!=nullptr,"enabled dump preserves shader creation");
    auto name=output/"bd0838e4d784cd1ffa4fcd356e98fecc0f01c128.cso";
    std::ifstream file(name,std::ios::binary);
    std::vector<uint8_t> got((std::istreambuf_iterator<char>(file)),{});
    check(got==data,"valid initialized DXBC is written byte-for-byte to its SHA1 filename");
    file.close();
    check(count(output)==1,"one shader produces exactly one file");
    cache.CreateShader(data.data(),data.size());
    check(initialized==1 && count(output)==1,"cache duplicate is never reinitialized or dumped");
    std::filesystem::remove(name);
    PipelineCache second; second.CreateShader(data.data(),data.size());
    check(initialized==2 && count(output)==0,"cross-device attempted digest prevents re-dump after file removal");
  } else if (mode=="invalid") {
    data=blob(0xfeedbeef); // Intact DXBC magic, rejected by the compiler boundary.
    check(cache.CreateShader(data.data(),data.size())==nullptr,"invalid compiler input is rejected");
    check(count(output)==0,"failed shader initialization never writes data");
  } else if (mode=="filecap") {
    data=blob(1,1024*1024+1); cache.CreateShader(data.data(),data.size());
    check(count(output)==0,"file over1MiB is not written");
    data=blob(2,1024*1024); cache.CreateShader(data.data(),data.size());
    check(count(output)==1 && bytes(output)==1024*1024,"exact1MiB file is accepted");
  } else if (mode=="totalcap") {
    for (uint32_t i=0;i<33;++i) { data=blob(i,1024*1024); cache.CreateShader(data.data(),data.size()); }
    check(count(output)==32 && bytes(output)==32ull*1024*1024,"global dump size is capped at32MiB");
    PipelineCache second; data=blob(100,1024*1024); second.CreateShader(data.data(),data.size());
    check(count(output)==32,"byte budget spans multiple pipeline caches");
  } else if (mode=="countcap") {
    for (uint32_t i=0;i<1025;++i) { data=blob(i); cache.CreateShader(data.data(),data.size()); }
    check(count(output)==1024,"global dump count is capped at1024");
  } else if (mode=="error") {
    data=blob(1,1024*1024);
    for (uint32_t i=0;i<32;++i) { data=blob(i,1024*1024); check(cache.CreateShader(data.data(),data.size())!=nullptr,"write failure cannot fail shader creation"); }
    check(count(output)==0 && logged("open failed") && !logged("saved"),"missing-directory open failure reports an error");
    std::filesystem::create_directory(output);
    data=blob(40); cache.CreateShader(data.data(),data.size());
    check(count(output)==0,"failed attempts reserve the byte budget");
  } else if (mode=="existing") {
    for (uint32_t i=1;i<=2;++i) {
      data=blob(i);
      auto path=output/(Sha1HashState::compute(data.data(),data.size()).string()+".cso");
      { std::ofstream sentinel(path,std::ios::binary); sentinel<<"existing capture sentinel"; }
      if(i==2) std::filesystem::permissions(path,std::filesystem::perms::owner_write);
      check(cache.CreateShader(data.data(),data.size())!=nullptr,"existing capture never fails shader creation");
      std::filesystem::permissions(path,std::filesystem::perms::owner_all);
      std::ifstream file(path,std::ios::binary);
      std::string content((std::istreambuf_iterator<char>(file)),{});
      check(content=="existing capture sentinel",i==1?"readable existing capture is preserved":"unreadable writable capture is preserved");
    }
  } else if (mode=="guards") {
    uint8_t bad[8]={'N','O','P','E',0,0,0,0};
    dxmt_shadow_dump_bytecode(Sha1Digest{},bad,sizeof(bad));
    dxmt_shadow_dump_bytecode(Sha1Digest{},nullptr,8);
    dxmt_shadow_dump_bytecode(Sha1Digest{},"DXB",3);
    check(count(output)==0 && messages.empty(),"direct dump magic/null/short guards write/log nothing");
  } else return 2;
  std::printf("BYTECODE %s mode=%s checks=%u failures=%u\n",failures?"FAIL":"PASS",mode.c_str(),checks,failures);
  return failures?1:0;
}
"""


def host_source() -> str:
    source = (SRC / "d3d11/d3d11_pipeline_cache.cpp").read_text()
    config = (SRC / "util/config/config.cpp").read_text()
    pieces = [definition(config, signature) for signature in (
        "Config::Config()", "Config::~Config()", "void Config::setOption(", "std::string Config::getOptionValue(",
        "bool Config::parseOptionValue(\n    const std::string&  value,\n          std::string&  result)",
    )]
    dump = ""
    if "static void dxmt_shadow_dump_bytecode(" in source:
        dump = definition(source, "static void dxmt_shadow_dump_bytecode(")
    else:
        dump = "static void dxmt_shadow_dump_bytecode(const Sha1Digest &,const void *,uint32_t) {}\n"
    method = definition(source, "CachedSM50Shader *CreateShader(")
    return PRELUDE + "\n".join(pieces) + dump + CLASS + method + "\n};\n" + DRIVER + "\n}\nint main(int argc,char **argv) { return dxmt::main(argc,argv); }\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--optimized", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="madeira-shadow-bytecode-", dir="/private/tmp") as temporary:
        directory = Path(temporary)
        source, binary = directory / "contract.cpp", directory / "contract"
        source.write_text(host_source())
        flags = ["-O2"] if args.optimized else ["-O1", "-g", "-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
        subprocess.run(["clang++", "-std=c++20", "-Wall", "-Wextra", "-Werror", "-Wno-unused-function", *flags,
                        "-I", str(SRC / "util"), str(source), str(SRC / "util/sha1/sha1_util.cpp"),
                        "-x", "c++", str(SRC / "util/sha1/sha1.c"), "-o", str(binary)], check=True, timeout=60)
        failed = False
        for mode in ("off", "write", "invalid", "filecap", "totalcap", "countcap", "error", "existing", "guards"):
            output = directory / mode
            if mode != "error":
                output.mkdir()
            result = subprocess.run([str(binary), mode, str(output)], capture_output=True, text=True, timeout=30)
            print(result.stdout, end="", flush=True)
            print(result.stderr, end="", file=sys.stderr, flush=True)
            failed |= result.returncode != 0
        return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
