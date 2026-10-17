// Independent host API diagnostic. Run each case in a separate process:
// color-control, depth-control, compare-color-view, compare-uint-as-depth,
// view-depth-from-uint, view-depth-from-unorm; optional less/lessEqual argument.
import Foundation
import Metal

struct ColorProbeFailure: Error, CustomStringConvertible {
    let description: String
    init(_ description: String) { self.description = description }
}
struct ColorProbeInput {
    var directionArray: SIMD4<Float>
    var referencePad: SIMD4<Float>
}
func emit(_ object: [String: Any]) throws {
    let data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    FileHandle.standardOutput.write(data + Data([10]))
}
func number(_ value: Float) -> String { String(format: "%.9g", Double(value)) }
func hex(_ value: Float) -> String { String(format: "0x%08x", value.bitPattern) }

let colorShader = """
#include <metal_stdlib>
using namespace metal;
struct Input { float4 directionArray; float4 referencePad; };
kernel void ordinary_color(texturecube_array<float> image [[texture(0)]], sampler s [[sampler(0)]],
    device const Input *inputs [[buffer(0)]], device float4 *outputs [[buffer(1)]],
    constant uint &count [[buffer(2)]], uint index [[thread_position_in_grid]]) {
    if (index >= count) return;
    const Input input = inputs[index];
    float value = image.sample(s, input.directionArray.xyz, uint(input.directionArray.w), level(0.0f)).r;
    outputs[index] = float4(value, value, input.referencePad.x, input.directionArray.w);
}
kernel void depth_compare(depthcube_array<float> image [[texture(0)]], sampler s [[sampler(0)]],
    device const Input *inputs [[buffer(0)]], device float4 *outputs [[buffer(1)]],
    constant uint &count [[buffer(2)]], uint index [[thread_position_in_grid]]) {
    if (index >= count) return;
    const Input input = inputs[index];
    float value = image.sample_compare(s, input.directionArray.xyz, uint(input.directionArray.w),
                                      input.referencePad.x, level(0.0f));
    float ordinary = image.sample(s, input.directionArray.xyz, uint(input.directionArray.w), level(0.0f));
    outputs[index] = float4(value, ordinary, input.referencePad.x, input.directionArray.w);
}
"""

func runColorProbe() throws {
    guard CommandLine.arguments.count >= 2 else { throw ColorProbeFailure("Missing isolated case argument") }
    let mode = CommandLine.arguments[1]
    let lessEqual = CommandLine.arguments.count >= 3 && CommandLine.arguments[2] == "lessEqual"
    let cases = ["color-control", "depth-control", "compare-color-view", "compare-uint-as-depth",
                 "view-depth-from-uint", "view-depth-from-unorm"]
    guard cases.contains(mode) else { throw ColorProbeFailure("Unknown case: \(mode)") }
    guard MemoryLayout<ColorProbeInput>.stride == 32 else { throw ColorProbeFailure("Unexpected input ABI") }
    guard let device = MTLCreateSystemDefaultDevice(), let queue = device.makeCommandQueue() else {
        throw ColorProbeFailure("Host Metal device/queue unavailable")
    }
    let n: UInt16 = 32768
    let q = Float(n) / 65535
    try emit(["kind": "environment", "case": mode, "device": device.name,
              "os": ProcessInfo.processInfo.operatingSystemVersionString,
              "sdk": ProcessInfo.processInfo.environment["MADEIRA_PROBE_SDK"] ?? "unspecified",
              "validationRequested": ProcessInfo.processInfo.environment["MTL_DEBUG_LAYER"] ?? "unspecified",
              "comparison": lessEqual ? "lessEqual" : "less", "cubes": 8, "faces": 48,
              "requestedUInt16": Int(n), "scope": "Host public API only, not DXMT AIR or game branch proof"])
    let depthControl = mode == "depth-control"
    let format: MTLPixelFormat = depthControl ? .depth16Unorm :
        (mode == "view-depth-from-unorm" ? .r16Unorm : .r16Uint)
    let descriptor = MTLTextureDescriptor()
    descriptor.textureType = .typeCubeArray
    descriptor.pixelFormat = format
    descriptor.width = 4; descriptor.height = 4; descriptor.arrayLength = 8
    descriptor.mipmapLevelCount = 1
    descriptor.storageMode = depthControl ? .private : .shared
    descriptor.usage = depthControl ? [.shaderRead, .renderTarget, .pixelFormatView] : [.shaderRead, .pixelFormatView]
    descriptor.hazardTrackingMode = .tracked
    try emit(["kind": "phase", "phase": "before-texture-create", "basePixelFormat": format.rawValue])
    guard let base = device.makeTexture(descriptor: descriptor) else { throw ColorProbeFailure("CubeArray texture allocation failed") }
    if depthControl {
        guard let command = queue.makeCommandBuffer(),
              let raw = device.makeBuffer(length: 1024, options: .storageModeShared) else {
            throw ColorProbeFailure("Depth clear/readback allocation failed")
        }
        for slice in 0..<48 {
            let pass = MTLRenderPassDescriptor()
            pass.depthAttachment.texture = base
            pass.depthAttachment.slice = slice
            pass.depthAttachment.loadAction = .clear
            pass.depthAttachment.storeAction = .store
            pass.depthAttachment.clearDepth = Double(q.nextUp)
            guard let render = command.makeRenderCommandEncoder(descriptor: pass) else { throw ColorProbeFailure("Depth face clear failed") }
            render.endEncoding()
        }
        guard let blit = command.makeBlitCommandEncoder() else { throw ColorProbeFailure("Depth readback encoder failed") }
        blit.copy(from: base, sourceSlice: 0, sourceLevel: 0, sourceOrigin: MTLOrigin(x: 0, y: 0, z: 0),
                  sourceSize: MTLSize(width: 4, height: 4, depth: 1), to: raw,
                  destinationOffset: 0, destinationBytesPerRow: 256, destinationBytesPerImage: 1024)
        blit.endEncoding(); command.commit(); command.waitUntilCompleted()
        guard command.status == .completed && command.error == nil else { throw ColorProbeFailure("Depth clear command failed: \(String(describing: command.error))") }
        let stored = raw.contents().load(as: UInt16.self)
        try emit(["kind": "write", "method": "48 depth render clears", "rawFirstPixelUInt16": Int(stored)])
        guard stored == n else { throw ColorProbeFailure("Depth control did not store target D16 value") }
    } else {
        let values = [UInt16](repeating: n, count: 16)
        values.withUnsafeBytes { data in
            for slice in 0..<48 {
                base.replace(region: MTLRegionMake2D(0, 0, 4, 4), mipmapLevel: 0, slice: slice,
                             withBytes: data.baseAddress!, bytesPerRow: 8, bytesPerImage: 32)
            }
        }
        var readback = [UInt16](repeating: 0, count: 16)
        readback.withUnsafeMutableBytes { data in
            base.getBytes(data.baseAddress!, bytesPerRow: 8, bytesPerImage: 32,
                          from: MTLRegionMake2D(0, 0, 4, 4), mipmapLevel: 0, slice: 0)
        }
        try emit(["kind": "write", "method": "48 CPU color slice replaces", "rawUInt16Pixels": readback.map(Int.init)])
        guard readback.allSatisfy({ $0 == n }) else { throw ColorProbeFailure("Color texture CPU readback mismatch") }
    }
    var texture: any MTLTexture = base
    if mode == "view-depth-from-uint" || mode == "view-depth-from-unorm" {
        try emit(["kind": "phase", "phase": "before-depth-view", "sourcePixelFormat": base.pixelFormat.rawValue,
                  "requestedViewPixelFormat": MTLPixelFormat.depth16Unorm.rawValue])
        let view = base.makeTextureView(pixelFormat: .depth16Unorm, textureType: .typeCubeArray,
                                        levels: 0..<1, slices: 0..<48)
        try emit(["kind": "view-result", "returnedTexture": view != nil,
                  "viewPixelFormat": view?.pixelFormat.rawValue ?? 0])
        return
    }
    if mode == "color-control" || mode == "compare-color-view" {
        try emit(["kind": "phase", "phase": "before-unorm-view", "sourcePixelFormat": base.pixelFormat.rawValue])
        guard let view = base.makeTextureView(pixelFormat: .r16Unorm, textureType: .typeCubeArray,
                                             levels: 0..<1, slices: 0..<48) else {
            throw ColorProbeFailure("R16Uint -> R16Unorm color view unavailable")
        }
        texture = view
    }
    try emit(["kind": "texture", "basePixelFormat": base.pixelFormat.rawValue,
              "boundPixelFormat": texture.pixelFormat.rawValue, "textureType": texture.textureType.rawValue,
              "arrayLength": texture.arrayLength])
    let options = MTLCompileOptions(); options.mathMode = .safe
    let library = try device.makeLibrary(source: colorShader, options: options)
    let ordinary = mode == "color-control"
    guard let function = library.makeFunction(name: ordinary ? "ordinary_color" : "depth_compare") else {
        throw ColorProbeFailure("Probe shader unavailable")
    }
    let pipeline = try device.makeComputePipelineState(function: function)
    let samplerDesc = MTLSamplerDescriptor()
    samplerDesc.minFilter = .linear; samplerDesc.magFilter = .linear; samplerDesc.mipFilter = .nearest
    samplerDesc.sAddressMode = .clampToEdge; samplerDesc.tAddressMode = .clampToEdge
    samplerDesc.rAddressMode = .repeat
    samplerDesc.compareFunction = ordinary ? .never : (lessEqual ? .lessEqual : .less)
    guard let sampler = device.makeSamplerState(descriptor: samplerDesc) else { throw ColorProbeFailure("Sampler unavailable") }
    let refs: [(String, Float)] = [("q-0.51LSB", q - 0.51 / 65535), ("q-0.49LSB", q - 0.49 / 65535),
                                ("q", q), ("q+0.49LSB", q + 0.49 / 65535), ("q+0.51LSB", q + 0.51 / 65535),
                                ("zero", 0), ("one", 1)]
    let directions: [SIMD3<Float>] = [SIMD3(1,0,0), SIMD3(0,1,0), SIMD3(0,0,1)]
    var inputs: [ColorProbeInput] = []
    for cube in [0, 1, 7] {
        for direction in directions {
            for (_, reference) in refs {
                inputs.append(ColorProbeInput(directionArray: SIMD4(direction.x,direction.y,direction.z,Float(cube)),
                                              referencePad: SIMD4(reference,0,0,0)))
            }
        }
    }
    guard let input = inputs.withUnsafeBufferPointer({ data in
        device.makeBuffer(bytes: data.baseAddress!, length: inputs.count * 32, options: .storageModeShared)
    }), let output = device.makeBuffer(length: inputs.count * 16, options: .storageModeShared),
          let command = queue.makeCommandBuffer(), let compute = command.makeComputeCommandEncoder() else {
        throw ColorProbeFailure("Compute resources unavailable")
    }
    try emit(["kind": "phase", "phase": "before-bind", "shaderTextureDeclaration": ordinary ? "texturecube_array<float>" : "depthcube_array<float>"])
    compute.setComputePipelineState(pipeline)
    compute.setTexture(texture, index: 0); compute.setSamplerState(sampler, index: 0)
    compute.setBuffer(input, offset: 0, index: 0); compute.setBuffer(output, offset: 0, index: 1)
    var count = UInt32(inputs.count); compute.setBytes(&count, length: 4, index: 2)
    compute.dispatchThreadgroups(MTLSize(width: (inputs.count + 31) / 32, height: 1, depth: 1),
                                 threadsPerThreadgroup: MTLSize(width: 32, height: 1, depth: 1))
    compute.endEncoding()
    try emit(["kind": "phase", "phase": "before-commit"])
    command.commit(); command.waitUntilCompleted()
    guard command.status == .completed && command.error == nil else { throw ColorProbeFailure("Compute command failed: \(String(describing: command.error))") }
    let result = output.contents().bindMemory(to: SIMD4<Float>.self, capacity: inputs.count)
    var mismatches = 0, nonBooleanResults = 0
    for index in inputs.indices {
        let ref = inputs[index].referencePad.x
        guard result[index].z.bitPattern == ref.bitPattern && result[index].w == inputs[index].directionArray.w else {
            throw ColorProbeFailure("GPU input/output ABI or execution verification failed")
        }
        let refBits = UInt16((min(1.0,max(0.0,Double(ref))) * 65535).rounded(.toNearestOrEven))
        let expected: Float = ordinary ? q : ((lessEqual ? refBits <= n : refBits < n) ? 1 : 0)
        // Every face has identical data and sampling stays at face centers,
        // so comparison filtering must return exactly zero or one here.
        if !ordinary && result[index].x != 0 && result[index].x != 1 { nonBooleanResults += 1 }
        let matches = ordinary ? (result[index].x.isFinite && abs(result[index].x - expected) <= 0.000001) : result[index].x == expected
        if !matches { mismatches += 1 }
        try emit(["kind": "sample", "cube": Int(inputs[index].directionArray.w), "referenceCase": refs[index % refs.count].0,
                  "reference": number(ref), "referenceBits": hex(ref), "metalResult": number(result[index].x),
                  "ordinarySample": number(result[index].y), "expectedD3D": number(expected)])
    }
    try emit(["kind": "summary", "execution": "completed", "case": mode, "comparisons": inputs.count,
              "d3dModelMismatches": mismatches,
              "nonBooleanComparisonResults": nonBooleanResults,
              "apiValidationRequested": ProcessInfo.processInfo.environment["MTL_DEBUG_LAYER"] == "1",
              "boundPixelFormat": texture.pixelFormat.rawValue,
              "note": "Observed host public API behavior only; not DXMT AIR, iPad, or game branch proof"])
}

do { try runColorProbe() }
catch {
    fputs("COLOR-PROBE BLOCKED/FAILED: \(error)\n", stderr)
    exit(1)
}
