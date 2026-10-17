// Standalone host Metal diagnostic. It does not load or modify Madeira.
// Build: xcrun swiftc -framework Metal depth-comparison-probe.swift -o probe
import Foundation
import Metal

struct ProbeFailure: Error, CustomStringConvertible {
    let description: String
    init(_ description: String) { self.description = description }
}

struct ProbeInput {
    var uv: SIMD2<Float>
    var reference: Float
    var padding: Float = 0
}

struct SampleCase {
    let coordinateName: String
    let referenceName: String
    let input: ProbeInput
}

struct Coordinate {
    let name: String
    let uv: SIMD2<Float>
    let linearInteriorWeight: Float
    let nearestInteriorWeight: Float
}

struct SamplerCase {
    let filterName: String
    let borderName: String
    let borderDepth: Float
    let linear: Bool
    let state: any MTLSamplerState
}

let shaderSource = """
#include <metal_stdlib>
using namespace metal;
struct ProbeInput { float2 uv; float reference; float padding; };
kernel void compare_depth(
    depth2d<float, access::sample> depth [[texture(0)]],
    sampler comparison [[sampler(0)]],
    device const ProbeInput *inputs [[buffer(0)]],
    device float4 *outputs [[buffer(1)]],
    constant uint &count [[buffer(2)]],
    uint index [[thread_position_in_grid]]) {
    if (index >= count) return;
    const ProbeInput input = inputs[index];
    float sampled = depth.sample_compare(comparison, input.uv, input.reference, level(0.0f));
    float stored = depth.read(uint2(1, 1));
    outputs[index] = float4(sampled, stored, input.reference, float(index));
}
"""

func bits(_ value: Float) -> String {
    String(format: "0x%08x", value.bitPattern)
}

func decimal(_ value: Float) -> String {
    String(format: "%.9g", Double(value))
}

func emitJSON(_ values: [String: Any]) throws {
    let data = try JSONSerialization.data(withJSONObject: values, options: [.sortedKeys])
    print(String(decoding: data, as: UTF8.self))
}

// D3D sample_c first quantizes/clamps the reference to the UNORM format.
// Use Double on the CPU and non-tie fixtures to keep this oracle independent
// of the Metal sampling operation being measured.
func d3dReference(_ reference: Float) -> UInt16 {
    let clamped = min(1.0, max(0.0, Double(reference)))
    return UInt16((clamped * 65535.0).rounded(.toNearestOrEven))
}

func expectedResult(
    reference: Float, storedBits: UInt16, storedFloat: Float,
    border: Float, interiorWeight: Float, quantize: Bool
) -> Float {
    let interiorPass: Bool
    let borderPass: Bool
    if quantize {
        let referenceBits = d3dReference(reference)
        interiorPass = referenceBits <= storedBits
        borderPass = referenceBits <= (border == 0 ? 0 : 65535)
    } else {
        interiorPass = reference <= storedFloat
        borderPass = reference <= border
    }
    return interiorWeight * (interiorPass ? 1 : 0) + (1 - interiorWeight) * (borderPass ? 1 : 0)
}

func runProbe() throws {
    guard MemoryLayout<ProbeInput>.stride == 16 else { throw ProbeFailure("Unexpected input ABI stride") }
    guard let device = MTLCreateSystemDefaultDevice() else { throw ProbeFailure("No host Metal device available") }
    guard let queue = device.makeCommandQueue() else { throw ProbeFailure("Cannot create Metal command queue") }
    try emitJSON([
        "kind": "environment", "device": device.name, "registryID": String(device.registryID),
        "os": ProcessInfo.processInfo.operatingSystemVersionString,
        "unifiedMemory": device.hasUnifiedMemory, "pixelFormat": "depth16Unorm",
        "pixelFormatRaw": MTLPixelFormat.depth16Unorm.rawValue,
        "sdk": ProcessInfo.processInfo.environment["MADEIRA_PROBE_SDK"] ?? "unspecified",
        "comparison": "lessEqual", "textureWrite": "render-pass depth clear/store",
        "rawReadback": "depth-only texture to shared buffer blit, no depth/stencil option"
    ])
    let options = MTLCompileOptions()
    options.mathMode = .safe
    let library = try device.makeLibrary(source: shaderSource, options: options)
    guard let function = library.makeFunction(name: "compare_depth") else {
        throw ProbeFailure("Metal compare_depth function unavailable")
    }
    let pipeline = try device.makeComputePipelineState(function: function)
    let descriptor = MTLTextureDescriptor.texture2DDescriptor(
        pixelFormat: .depth16Unorm, width: 4, height: 4, mipmapped: false
    )
    descriptor.storageMode = .private
    descriptor.usage = [.renderTarget, .shaderRead]
    descriptor.hazardTrackingMode = .tracked
    guard let texture = device.makeTexture(descriptor: descriptor),
          let raw = device.makeBuffer(length: 256 * 4, options: .storageModeShared) else {
        throw ProbeFailure("Cannot allocate depth16 texture or raw readback buffer")
    }
    let coordinates = [
        Coordinate(name: "interior", uv: SIMD2(0.5, 0.5), linearInteriorWeight: 1, nearestInteriorWeight: 1),
        Coordinate(name: "outside-left", uv: SIMD2(-1, 0.5), linearInteriorWeight: 0, nearestInteriorWeight: 0),
        Coordinate(name: "outside-right", uv: SIMD2(2, 0.5), linearInteriorWeight: 0, nearestInteriorWeight: 0),
        Coordinate(name: "edge-left", uv: SIMD2(0, 0.5), linearInteriorWeight: 0.5, nearestInteriorWeight: 1),
        Coordinate(name: "edge-right", uv: SIMD2(1, 0.5), linearInteriorWeight: 0.5, nearestInteriorWeight: 0),
        Coordinate(name: "corner", uv: SIMD2(0, 0), linearInteriorWeight: 0.25, nearestInteriorWeight: 1)
    ]
    var samplers: [SamplerCase] = []
    for linear in [false, true] {
        for white in [false, true] {
            let sampler = MTLSamplerDescriptor()
            sampler.minFilter = linear ? .linear : .nearest
            sampler.magFilter = linear ? .linear : .nearest
            sampler.mipFilter = .nearest
            sampler.sAddressMode = .clampToBorderColor
            sampler.tAddressMode = .clampToBorderColor
            sampler.rAddressMode = .repeat
            sampler.borderColor = white ? .opaqueWhite : .opaqueBlack
            sampler.compareFunction = .lessEqual
            sampler.normalizedCoordinates = true
            sampler.lodMinClamp = 0
            sampler.lodMaxClamp = 0
            guard let state = device.makeSamplerState(descriptor: sampler) else {
                throw ProbeFailure("Cannot create comparison/border sampler")
            }
            samplers.append(SamplerCase(
                filterName: linear ? "linear-mip-point-0x94" : "nearest-control",
                borderName: white ? "opaqueWhite" : "opaqueBlack", borderDepth: white ? 1 : 0,
                linear: linear, state: state
            ))
        }
    }
    var total = 0, d3dMatches = 0, rawMatches = 0, neither = 0
    var interiorD3DMismatches = 0, outsideD3DMismatches = 0
    for requestedBits in [UInt16(0), 16384, 32768, 49152, 65535] {
        let q = Float(requestedBits) / 65535
        // A nominal n/65535 clear can round just below the target value when
        // converted to Float (observed n=16384 writing 16383 on Apple M3).
        // Move only the clear input one Float ULP upward, then verify all raw
        // stored UInt16 pixels. Comparisons still use q, not this clear input.
        let clearDepth = (requestedBits == 0 || requestedBits == 65535) ? Double(q) : Double(q.nextUp)
        let lsb = Float(1) / 65535
        let references: [(String, Float)] = [
            ("q-minus-0.51-LSB", q - 0.51 * lsb), ("q-minus-0.49-LSB", q - 0.49 * lsb),
            ("q", q), ("q-plus-0.49-LSB", q + 0.49 * lsb), ("q-plus-0.51-LSB", q + 0.51 * lsb),
            ("minus-0.01", -0.01), ("plus-1.01", 1.01), ("zero", 0), ("one", 1)
        ]
        let cases = coordinates.flatMap { coordinate in
            references.map { name, reference in
                SampleCase(coordinateName: coordinate.name, referenceName: name,
                           input: ProbeInput(uv: coordinate.uv, reference: reference))
            }
        }
        let inputs = cases.map(\.input)
        guard let input = inputs.withUnsafeBufferPointer({ data in
            device.makeBuffer(bytes: data.baseAddress!, length: data.count * MemoryLayout<ProbeInput>.stride,
                              options: .storageModeShared)
        }), let command = queue.makeCommandBuffer() else { throw ProbeFailure("Cannot allocate probe inputs/command") }
        command.label = "D16 clear/read/compare n=\(requestedBits)"
        let renderPass = MTLRenderPassDescriptor()
        renderPass.depthAttachment.texture = texture
        renderPass.depthAttachment.loadAction = .clear
        renderPass.depthAttachment.storeAction = .store
        renderPass.depthAttachment.clearDepth = clearDepth
        guard let render = command.makeRenderCommandEncoder(descriptor: renderPass) else {
            throw ProbeFailure("Cannot encode depth16 clear render pass")
        }
        render.endEncoding()
        guard let blit = command.makeBlitCommandEncoder() else { throw ProbeFailure("Cannot encode raw depth readback") }
        blit.copy(from: texture, sourceSlice: 0, sourceLevel: 0, sourceOrigin: MTLOrigin(x: 0, y: 0, z: 0),
                  sourceSize: MTLSize(width: 4, height: 4, depth: 1), to: raw,
                  destinationOffset: 0, destinationBytesPerRow: 256, destinationBytesPerImage: 256 * 4)
        blit.endEncoding()
        var outputs: [any MTLBuffer] = []
        for sampler in samplers {
            guard let output = device.makeBuffer(length: cases.count * 16, options: .storageModeShared),
                  let compute = command.makeComputeCommandEncoder() else { throw ProbeFailure("Cannot encode compute sampling") }
            outputs.append(output)
            compute.setComputePipelineState(pipeline)
            compute.setTexture(texture, index: 0)
            compute.setSamplerState(sampler.state, index: 0)
            compute.setBuffer(input, offset: 0, index: 0)
            compute.setBuffer(output, offset: 0, index: 1)
            var count = UInt32(cases.count)
            compute.setBytes(&count, length: MemoryLayout<UInt32>.size, index: 2)
            let width = min(32, pipeline.maxTotalThreadsPerThreadgroup)
            compute.dispatchThreadgroups(MTLSize(width: (cases.count + width - 1) / width, height: 1, depth: 1),
                                         threadsPerThreadgroup: MTLSize(width: width, height: 1, depth: 1))
            compute.endEncoding()
        }
        command.commit()
        command.waitUntilCompleted()
        guard command.status == .completed, command.error == nil else {
            throw ProbeFailure("Metal command failed: \(command.status.rawValue) \(String(describing: command.error))")
        }
        var pixels: [UInt16] = []
        for y in 0..<4 {
            for x in 0..<4 { pixels.append(raw.contents().load(fromByteOffset: y * 256 + x * 2, as: UInt16.self)) }
        }
        try emitJSON(["kind": "depth-write", "requestedBits": Int(requestedBits),
                      "nominalDepthDouble": Double(requestedBits) / 65535.0,
                      "clearDepthDouble": clearDepth, "actualUInt16Pixels": pixels.map(Int.init)])
        guard pixels.allSatisfy({ $0 == requestedBits }) else { throw ProbeFailure("Depth clear did not write requested D16 bits") }
        for (samplerIndex, sampler) in samplers.enumerated() {
            let values = outputs[samplerIndex].contents().bindMemory(to: SIMD4<Float>.self, capacity: cases.count)
            for (index, sample) in cases.enumerated() {
                let result = values[index]
                guard result.z.bitPattern == sample.input.reference.bitPattern && result.w == Float(index),
                      result.y.isFinite && abs(result.y - q) < lsb / 4 else {
                    throw ProbeFailure("GPU input/readback ABI or stored-depth validation failed")
                }
                let coordinate = coordinates[index / references.count]
                let weight = sampler.linear ? coordinate.linearInteriorWeight : coordinate.nearestInteriorWeight
                let d3d = expectedResult(reference: sample.input.reference, storedBits: requestedBits, storedFloat: result.y,
                                         border: sampler.borderDepth, interiorWeight: weight, quantize: true)
                let rawExpected = expectedResult(reference: sample.input.reference, storedBits: requestedBits, storedFloat: result.y,
                                                border: sampler.borderDepth, interiorWeight: weight, quantize: false)
                let d3dMatch = abs(result.x - d3d) < 0.000001
                let rawMatch = abs(result.x - rawExpected) < 0.000001
                total += 1; d3dMatches += d3dMatch ? 1 : 0; rawMatches += rawMatch ? 1 : 0
                neither += !d3dMatch && !rawMatch ? 1 : 0
                if !d3dMatch && coordinate.name == "interior" { interiorD3DMismatches += 1 }
                if !d3dMatch && coordinate.name.hasPrefix("outside") { outsideD3DMismatches += 1 }
                try emitJSON([
                    "kind": "comparison", "storedBits": Int(requestedBits), "storedFloat": decimal(result.y),
                    "storedFloatBits": bits(result.y), "filter": sampler.filterName, "border": sampler.borderName,
                    "coordinate": sample.coordinateName, "u": decimal(sample.input.uv.x), "v": decimal(sample.input.uv.y),
                    "referenceCase": sample.referenceName, "referenceFloat": decimal(result.z), "referenceFloatBits": bits(result.z),
                    "d3dReferenceBits": Int(d3dReference(sample.input.reference)), "metalResult": decimal(result.x),
                    "d3dExpected": decimal(d3d), "rawExpected": decimal(rawExpected),
                    "matchesD3D": d3dMatch, "matchesRawFloat": rawMatch
                ])
            }
        }
    }
    try emitJSON([
        "kind": "summary", "execution": "completed", "cases": total, "matchesD3D": d3dMatches,
        "matchesRawFloat": rawMatches, "matchesNeither": neither,
        "interiorD3DMismatches": interiorD3DMismatches, "outsideD3DMismatches": outsideD3DMismatches,
        "scope": "Host GPU API semantics only; not translated AIR, Gujian, or iPad runtime proof"
    ])
}

do {
    try runProbe()
} catch {
    fputs("DEPTH-PROBE BLOCKED/FAILED: \(error)\n", stderr)
    exit(1)
}
