#!/usr/bin/env python3
"""Compile real HLSL with this checkout's vkd3d-shader; catch mixed cast grouping.

Native macOS prerequisites: clang, flex, bison 3, and an existing Wine build
with tools/widl/widl and generated include headers. All outputs stay in the
explicit build directory; no Wine DLL is built or staged.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess


ROOT = Path(__file__).resolve().parents[2]
VKD3D = ROOT / "wine/libs/vkd3d"
SHADER = VKD3D / "libs/vkd3d-shader"


def run(*args, **kwargs):
    subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def build_compiler(build, wine_build, bison):
    build.mkdir(parents=True, exist_ok=True)
    run(wine_build / "tools/widl/widl", "-h", "-H", build / "d3dcommon.h",
        "-I", ROOT / "wine/include", ROOT / "wine/include/d3dcommon.idl")
    generated = []
    for stem in ("hlsl", "preproc"):
        run(bison, "-d", "-o", build / f"{stem}.tab.c", SHADER / f"{stem}.y")
        run("flex", "-o", build / f"{stem}.yy.c", SHADER / f"{stem}.l")
        generated.extend([build / f"{stem}.tab.c", build / f"{stem}.yy.c"])
    sources = sorted(SHADER.glob("*.c")) + generated
    sources += [VKD3D / f"libs/vkd3d-common/{name}.c" for name in ("debug", "memory", "utf8")]
    sources.append(Path(__file__).with_name("hlsl-vector-casts-compiler.c"))
    includes = [build, VKD3D, VKD3D / "include", VKD3D / "include/private",
                SHADER, wine_build / "include", ROOT / "wine/include"]
    flags = ["-O0", "-g", "-fms-extensions", "-Wno-ignored-attributes",
             "-DWINE_NO_NAMELESS_EXTENSION", "-DLIBVKD3D_SHADER_SOURCE",
             "-DHAVE_PTHREAD_H=1", "-DHAVE_SYNC_ADD_AND_FETCH=1",
             "-DHAVE_SYNC_BOOL_COMPARE_AND_SWAP=1", "-DHAVE_ATOMIC_EXCHANGE_N=1"]
    flags += [f"-I{path}" for path in includes]

    def compile_source(source):
        output = build / f"{source.stem}.o"
        run("clang", *flags, "-c", source, "-o", output)
        return output

    with ThreadPoolExecutor(max_workers=4) as pool:
        objects = list(pool.map(compile_source, sources))
    compiler = build / "hlsl-vector-casts-compiler"
    run("clang", *objects, "-o", compiler)
    return compiler


def bits(value):
    return struct.unpack("<I", struct.pack("<f", value))[0]


def floating(value):
    return struct.unpack("<f", struct.pack("<I", value))[0]


def evaluate(assembly, position=(17.5, 31.5, 5.5, 1.0)):
    """Execute the straight-line arithmetic subset emitted by these fixtures.

    Registers contain raw 32-bit words, so MOV/asfloat never perform numeric
    conversion. Texture operations record their coordinates and return fixed
    values: this verifies emitted coordinate arithmetic, not GPU sampling.
    Unexpected instructions fail closed rather than being silently ignored.
    """
    registers = {"v0": [bits(x) for x in position], "cb0[0]": [0x3f800000, 0x40000000, 0, 0]}
    coordinates = {}

    def read(operand):
        if operand.startswith("l("):
            values = operand[2:-1].split(",")
            words = [bits(float(x)) if re.search(r"[.eE]", x) else int(x) & 0xffffffff for x in values]
            return words * 4 if len(words) == 1 else words
        match = re.fullmatch(r"(\w+(?:\[\d+\])?)(?:\.([xyzw]+))?", operand)
        assert match, f"Unsupported operand: {operand}"
        register, swizzle = match.groups()
        values = registers[register]
        return [values["xyzw".index(c)] for c in swizzle.ljust(4, swizzle[-1])] if swizzle else values[:]

    def write(operand, values):
        if operand == "null":
            return
        register, _, mask = operand.partition(".")
        mask = mask or ("x" if register == "oDepth" else "xyzw")
        target = registers.setdefault(register, [0] * 4)
        for component in mask:
            lane = "xyzw".index(component)
            target[lane] = values[lane] & 0xffffffff

    for line in assembly.splitlines():
        if line.startswith(("ps_", "dcl_")) or not line:
            continue
        op, _, operands = line.partition(" ")
        args = re.findall(r"l\([^)]*\)|[^,\s]+", operands)
        if op == "ret":
            break
        if op == "ld":
            value = read(args[1])
            coordinates["load"] = (value[0], value[1], value[3])
            write(args[0], [bits(0.25)] * 4)
        elif op == "sample":
            value = read(args[1])
            coordinates["sample"] = tuple(floating(x) for x in value[:2])
            write(args[0], [bits(0.5)] * 4)
        elif op in ("imul", "udiv"):
            a, b = read(args[2]), read(args[3])
            if op == "imul":
                result = [x * y for x, y in zip(a, b)]
                write(args[0], [x >> 32 for x in result])
                write(args[1], result)
            else:
                write(args[0], [x // y for x, y in zip(a, b)])
                write(args[1], [x % y for x, y in zip(a, b)])
        else:
            a = read(args[1])
            if op == "mov":
                result = a
            elif op in ("ftou", "ftoi"):
                result = [int(floating(x)) & 0xffffffff for x in a]
            elif op == "utof":
                result = [bits(float(x)) for x in a]
            elif op == "itof":
                result = [bits(float(x if x < 0x80000000 else x - 0x100000000)) for x in a]
            elif op in ("add", "div", "iadd"):
                b = read(args[2])
                if op == "iadd":
                    result = [x + y for x, y in zip(a, b)]
                elif op == "add":
                    result = [bits(floating(x) + floating(y)) for x, y in zip(a, b)]
                else:
                    # Inactive lanes may contain zero denominators; their values
                    # are not written. Match IEEE arithmetic for these lanes.
                    result = [bits(floating(x) / floating(y)) if floating(y) else bits(float("nan"))
                              for x, y in zip(a, b)]
            else:
                raise AssertionError(f"Unsupported instruction: {line}")
            write(args[0], result)
    return registers.get("o0", registers.get("oDepth")), coordinates


READER = """
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
"""


def check(compiler, build):
    # Hand-derived evaluator checks: immediate parsing, swizzles, raw MOV,
    # numeric conversion, and masked writes must agree before trusting it.
    sanity, _ = evaluate("""mov r0.xyzw, l(1.0, 2.0, 3.0, 4.0)
add r0.xy, r0.yxxx, l(0.5, 0.25, 0.0, 0.0)
mov o0.xyzw, r0.xyzw
ret
""")
    assert sanity == [0x40200000, 0x3fa00000, 0x40400000, 0x40800000]
    sanity, _ = evaluate("""ftou r0.xy, v0.xyxx
utof r1.xy, r0.xyxx
mov o0.xy, r0.xyxx
mov o0.zw, r1.xxxy
ret
""")
    assert sanity == [17, 31, 0x41880000, 0x41f80000]
    prefix = "float4 main(float4 p : SV_Position) : SV_Target { uint2 v = uint2(p.xy);"
    suffix = "return float4(asfloat(i), f); }"
    cases = [
        ("exact-reader", READER, "read_ps", [0x3e800000, 0x3f000000, 0x3e800000, 0x3f000000]),
        ("int-then-float", prefix + "int2 i=int2(v); float2 f=float2(v);" + suffix,
         "main", [17, 31, 0x41880000, 0x41f80000]),
        ("float-then-int", prefix + "float2 f=float2(v); int2 i=int2(v);" + suffix,
         "main", [17, 31, 0x41880000, 0x41f80000]),
        ("float-widths", "float4 main(float4 p : SV_Position) : SV_Target { uint3 v=uint3(p.xyz);"
         "float x=float(v.x); float2 yz=float2(v.yz); return float4(x,yz,1); }",
         "main", [0x41880000, 0x41f80000, 0x40a00000, 0x3f800000]),
        ("int-widths", "float4 main(float4 p : SV_Position) : SV_Target {"
         "int x=int(p.x); int2 yz=int2(p.yz); return asfloat(int4(x,yz,1)); }",
         "main", [17, 31, 5, 1]),
        ("scalar-no-load", "float4 main(float4 p : SV_Position) : SV_Target {"
         "uint v=uint(p.x); float f=float(v); return float4(f,f,f,1); }",
         "main", [0x41880000, 0x41880000, 0x41880000, 0x3f800000]),
        ("cbuffer-control", "cbuffer Values { uint2 v; }; float4 main() : SV_Target {"
         "int2 i=int2(v);float2 f=float2(v);return float4(asfloat(i), f); }",
         "main", [0x3f800000, 0x40000000, 0x4e7e0000, 0x4e800000]),
    ]
    report = []
    for name, source, entry, expected in cases:
        path = build / f"{name}.hlsl"
        path.write_text(source)
        asm = path.with_suffix(".asm")
        cso = path.with_suffix(".cso")
        run(compiler, path, cso, asm, entry)
        assembly = asm.read_text()
        actual, coordinates = evaluate(assembly)
        errors = []
        if actual != expected:
            errors.append(f"output words {actual}, expected {expected}")
        if name == "exact-reader":
            if coordinates.get("load") != (17, 31, 0):
                errors.append(f"Load coordinates {coordinates.get('load')}, expected (17, 31, 0)")
            if coordinates.get("sample") != (0.27734375, 0.49609375):
                errors.append(f"Sample coordinates {coordinates.get('sample')}, expected (0.27734375, 0.49609375)")
        if name in ("float-widths", "int-widths"):
            op = "utof" if name == "float-widths" else "ftoi"
            conversions = re.findall(rf"^{op} (\w+)\.([xyzw]+),", assembly, re.MULTILINE)
            if len(conversions) != 1 or len(conversions[0][1]) != 3:
                errors.append(f"Same-base scalar/vector casts did not combine: {conversions}")
        row = {"name": name, "pass": not errors, "errors": errors, "output_words": actual,
               "coordinates": coordinates, "cso_size": cso.stat().st_size,
               "cso_sha256": hashlib.sha256(cso.read_bytes()).hexdigest()}
        report.append(row)
        print(f"{'FAIL' if errors else 'PASS'} {name}: {'; '.join(errors) if errors else 'numeric results and conversion lanes preserved'}")
    (build / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    assert all(row["pass"] for row in report), "emitted HLSL bytecode failed numeric regression"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--wine-build", type=Path, required=True)
    parser.add_argument("--bison", type=Path, default=ROOT / "toolchains/host-tools/bison/3.8.2/bin/bison")
    args = parser.parse_args()
    compiler = build_compiler(args.build_dir, args.wine_build, args.bison)
    check(compiler, args.build_dir)


if __name__ == "__main__":
    main()
