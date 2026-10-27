import Foundation
import CoreAudio

func readAudio<T>(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector,
             _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal,
             _ element: AudioObjectPropertyElement = kAudioObjectPropertyElementMain,
             initial: T) -> T? {
    var address = AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: element)
    guard AudioObjectHasProperty(object, &address) else { return nil }
    var result = initial
    var size = UInt32(MemoryLayout<T>.size)
    let status = withUnsafeMutablePointer(to: &result) { pointer in
        AudioObjectGetPropertyData(object, &address, 0, nil, &size, UnsafeMutableRawPointer(pointer))
    }
    return status == noErr ? result : nil
}

var output: [String: Any] = [:]
for (label, selector) in [("normal_output", kAudioHardwarePropertyDefaultOutputDevice),
                          ("sound_effects_output", kAudioHardwarePropertyDefaultSystemOutputDevice)] {
    if let device: AudioObjectID = readAudio(AudioObjectID(kAudioObjectSystemObject), selector, initial: initialDevice()) {
        let name: CFString? = readAudio(device, kAudioObjectPropertyName, initial: CFStringCreateWithCString(nil, "", CFStringBuiltInEncodings.UTF8.rawValue)!)
        var info: [String: Any] = ["device_id": device, "name": (name as String?) ?? "unknown"]
        for element in UInt32(0)...UInt32(2) {
            if let mute: UInt32 = readAudio(device, kAudioDevicePropertyMute, kAudioObjectPropertyScopeOutput, element, initial: UInt32(0)) {
                info["mute_channel_\(element)"] = mute != 0
            }
            if let volume: Float32 = readAudio(device, kAudioDevicePropertyVolumeScalar, kAudioObjectPropertyScopeOutput, element, initial: Float32(0)) {
                info["volume_channel_\(element)"] = volume
            }
        }
        output[label] = info
    }
}
func initialDevice() -> AudioObjectID { kAudioObjectUnknown }
if let json = try? JSONSerialization.data(withJSONObject: output, options: [.prettyPrinted, .sortedKeys]),
   let text = String(data: json, encoding: .utf8) { print(text) }
