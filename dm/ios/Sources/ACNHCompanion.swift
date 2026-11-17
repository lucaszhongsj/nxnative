// Native Apple presentation of Eden Duo's live ACNH module outputs.
import SwiftUI
import Combine
import CoreGraphics
#if os(iOS)
import UIKit
#endif

func companionText(_ key: String) -> String {
    let main = Bundle.main
    // The panel is Chinese only: the games it accompanies are played in Chinese, so its own words do not follow DM_LANG or the system
    // language. In the shared shell the panel lives in a game framework, and Bundle.main is the shell app, which has no Companion.strings:
    // ask the framework's own bundle first, then the app's.
    for base in [Bundle(for: CompanionModel.self), main] {
        var bundle = base
        if let path = base.path(forResource: "zh-Hans", ofType: "lproj"), let translated = Bundle(path: path) { bundle = translated }
        let text = bundle.localizedString(forKey: key, value: "", table: "Companion")
        if !text.isEmpty { return text }
    }
    return key
}
enum CompanionPage: Int, CaseIterable {
    case map = 1, bag, critters, diy, today, phone
    var title: String { companionText(["Map", "Pockets", "Critterpedia", "DIY", "Today", "NookPhone"][rawValue - 1]) }
    var symbol: String { ["map", "bag", "fish", "hammer", "sun.max", "apps.iphone"][rawValue - 1] }
}
struct CompanionPixels { let data: Data; let width: Int; let height: Int }
struct CompanionMarker: Identifiable {
    let id: String
    let key: String
    let x: Double
    let y: Double
    let box: Double
}
@MainActor final class CompanionModel: ObservableObject {
    typealias Reader = () -> [String: Any]
    typealias Sender = (String, Int64) -> Void
    typealias ImageLoader = (String, @escaping (Data?, Int, Int) -> Void) -> Void
    @Published var values: [String: Any] = [:]
    @Published var page = CompanionPage.map
    @Published var imageEpoch = 0
    private var pendingPage: CompanionPage?   // a pick the adapter has not confirmed yet: its page's keys are not in the snapshot until it does
    @Published var islandName = ""
    @Published var controlsHidden = false
    @Published var externalController = false
    @Published var dockInsets = EdgeInsets()
    @Published private(set) var presented = true
    var uiActivity: ((Int, Bool) -> Void)?
    private var nativeActivity = false
    private let read: Reader
    private let send: Sender
    let load: ImageLoader
    private var timer: Timer?
    private var interactionSamples: AnyCancellable?
    // The module's adapter, reached through the C bridge in companion_apple.h. This file joins the target only where the profile has
    // companion.json (build_ipa.sh), which is also the only build that links the adapter; a test injects reader / sender / images and
    // gets no engine.
    private let native: UnsafeMutableRawPointer?
    init(reader: Reader? = nil, sender: Sender? = nil, images: ImageLoader? = nil) {
        let engine: UnsafeMutableRawPointer? = reader == nil ? dm_companion_engine() : nil
        native = engine
        read = reader ?? { engine.map { dm_companion_engine_snapshot($0) as? [String: Any] ?? [:] } ?? [:] }
        send = sender ?? { name, value in if let engine { dm_companion_engine_action(engine, name, value) } }
        load = images ?? { key, completion in
            guard let engine else { completion(nil, 0, 0); return }
            dm_companion_engine_image(engine, key) { data, w, h in completion(data, Int(w), Int(h)) }
        }
        if engine != nil {
            interactionSamples = NotificationCenter.default.publisher(for: Notification.Name("DmCompanionInteractionSampled"))
                .sink { [weak self] _ in Task { @MainActor in self?.poll() } }
        }
        if let engine { dm_companion_engine_start(engine) }
        poll()
        schedulePolling()
    }
    private func schedulePolling() {
        timer?.invalidate();timer=nil
        guard presented else { return }
        let interval:TimeInterval = [.map,.bag,.phone].contains(page) ? 0.2 : 0.5
        timer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in Task { @MainActor in self?.poll() } }
        if let timer { RunLoop.main.add(timer, forMode: .common) }
    }
    deinit { timer?.invalidate() }
    func poll() {
        guard presented else { return }
        let next = read()
        if !(next as NSDictionary).isEqual(to: values) { values = next }
        // A page pick takes effect when the adapter reports it: until then the snapshot still holds the old page's keys, and
        // drawing the new page would show empty fields.
        if let pending = pendingPage, (next["ui.page"] as? NSNumber)?.intValue == pending.rawValue {
            pendingPage = nil
            if page != pending { page = pending; updateNativeActivity(); schedulePolling() }
        }
        let name=text("island.name")
        if !name.isEmpty && name != islandName { islandName = name }
        updateNativeActivity()
    }
    func visible(_ value: Bool) {
        if !value && page == .bag { action("bag_cancel", 0) }
        presented=value
        if let native { dm_companion_engine_set_visible(native, value) }
        schedulePolling();if value {poll()}
    }
    func setPresentation(controlsHidden: Bool, physicalController: Bool, presented: Bool) {
        if self.controlsHidden != controlsHidden { self.controlsHidden = controlsHidden }
        if externalController != physicalController { externalController = physicalController }
        // The root controller reports presentation independently of SwiftUI appearance.
        // Route transitions through the owner of polling and native visibility.
        if self.presented != presented { visible(presented) }
    }
    func select(_ next: CompanionPage) { if page == .bag && next != .bag { action("bag_cancel", 0) }; pendingPage = next; action("ui_open", next.rawValue);schedulePolling();poll() }
    // The panel's bottom bar double tap: Tap is the layout in which the panel itself takes the touches, 3D the one with the controls.
    func toggleLayout() { dm_pad_layout_set(dm_pad_layout() == 2 ? 1 : 2) }
    func updateNativeActivity(force: Bool = false) {
        let busy = page == .bag && (number("bag.native.open") != 0 || number("bag.busy") != 0)
            || page == .phone && (values["pdrv.idle"] as? NSNumber)?.intValue == 0
        if force || busy != nativeActivity {nativeActivity=busy;uiActivity?(4,busy)}
    }
    func action(_ name: String, _ value: Int) { send(name, Int64(value)) }
    func pocketToken(_ slot: Int) -> String { text("bag.\(slot).token") }
    private func validToken(_ token: String) -> Bool { token.utf8.count == 16 && token.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) } }
    func movePocket(from: Int, to: Int, expected: String) -> Bool {
        let fresh = read()
        let slots = min(40, max(0, (fresh["bag.slots"] as? NSNumber)?.intValue ?? 0))
        guard from >= 0, to >= 0, from < slots, to < slots,
              (fresh["bag.can_write"] as? NSNumber)?.boolValue == true,
              (fresh["bag.busy"] as? NSNumber)?.boolValue != true,
              validToken(expected), fresh["bag.\(from).token"] as? String == expected,
              let target = fresh["bag.\(to).token"] as? String, validToken(target) else { return false }
        send("bag_move_checked_" + expected + "_" + target, Int64(from * 64 + to));return true
    }
    func usePocket(_ slot: Int) {
        let token = pocketToken(slot);guard slot >= 0, slot < count("bag.slots", max: 40), number("bag.can_use") != 0, validToken(token) else { return }
        send("bag_use_" + token, Int64(slot))
    }
    func pocketCommand(_ index: Int) {
        let slot = number("bag.native.slot"), token = text("bag.native.token")
        guard number("bag.native.open") != 0, number("bag.busy") == 0, slot >= 0, slot < 40, index >= 0, index < count("bag.native.count", max: 7), validToken(token) else { return }
        send("bag_command_\(slot)_" + token, Int64(number("bag.native.\(index).id") * 8 + index))
    }
    func text(_ key: String, _ fallback: String = "") -> String {
        let value = values[key] as? String ?? fallback
        // The native text sits beside decoded app/item icons. Nintendo's private icon
        // codepoints have no glyph in the system font and must not become empty squares.
        return String(String.UnicodeScalarView(value.unicodeScalars.filter { $0.properties.generalCategory != .privateUse }))
    }
    func number(_ key: String) -> Int { (values[key] as? NSNumber)?.intValue ?? 0 }
    func decimal(_ key: String) -> Double { (values[key] as? NSNumber)?.doubleValue ?? 0 }
    func count(_ key: String, max limit: Int = 1000) -> Int { min(max(0, number(key)), limit) }
    var ready: Bool { number("acnh.ready") != 0 }
    var turnipPrice: Int {
        guard let slot = values["kabu.now"] as? NSNumber else { return 0 }
        if slot.intValue < 0 { return max(0, number("kabu.sun")) }
        return slot.intValue < 12 ? max(0, number("kabu.\(slot.intValue)")) : 0
    }
    func questMiles(_ index: Int) -> Int {
        let prefix = "nmp.\(index)."
        let multiplier = number(prefix + "x5") != 0 ? 5 : text(prefix + "card").contains("&bonus=1") ? 2 : 1
        return number(prefix + "miles") * multiplier
    }
    var markers: [CompanionMarker] {
        values.keys.filter { $0.hasPrefix("map.ib.") && $0.hasSuffix(".k0") }.sorted().compactMap { key in
            let stem = String(key.dropLast(2))
            guard let image = values[key] as? String, !image.isEmpty else { return nil }
            let parts = key.split(separator: ".")
            return CompanionMarker(id: key, key: image, x: decimal(stem + "x") / 10, y: decimal(stem + "y") / 10, box: parts.count > 2 ? Double(parts[2]) ?? 64 : 64)
        }
    }
}

struct ACNHCompanionView: View {
    @ObservedObject var model: CompanionModel
    var body: some View { EdenCompanionView(model: model) }
}

#if os(iOS)
@MainActor private weak var activeCompanionModel: CompanionModel?

// front_ios.m: whether the band is shown, a physical controller is in use, and the pad is hidden, and the safe-area parts of the
// band its content must avoid (home indicator, notch). The front end owns these; the renderer only reacts to them.
@MainActor @_cdecl("dm_companion_ui_state")
public func dmCompanionUIState(_ hidden: Int32, _ physical: Int32, _ presented: Int32) {
    guard let model = activeCompanionModel else { return }
    model.setPresentation(controlsHidden: hidden != 0, physicalController: physical != 0, presented: presented != 0)
}

@MainActor @_cdecl("dm_companion_ui_dock_insets")
public func dmCompanionUIDockInsets(_ left: Double, _ right: Double, _ bottom: Double) {
    guard let model = activeCompanionModel else { return }
    let insets = EdgeInsets(top: 0, leading: left, bottom: bottom, trailing: right)
    if model.dockInsets != insets { model.dockInsets = insets }
}

// The front end embeds this hosting controller's view as the band under the picture (front_ios.m), with the touch pad floating above it. It inherits the
// game root's orientation policy; no canvas rotation is needed: the view is laid out by the root, not presented full screen.
@MainActor @_cdecl("dm_companion_view_controller")
public func dmCompanionViewController() -> UnsafeMutableRawPointer {
    let model = CompanionModel()
    activeCompanionModel = model
    // The band reaches the screen's bottom edge: the bar's background covers it, its text stays above the home indicator.
    let controller = DmHostingController(rootView: ACNHCompanionView(model: model).ignoresSafeArea(.container, edges: .bottom))
    controller.view.backgroundColor = .clear
    controller.view.clipsToBounds = true
    return Unmanaged.passRetained(controller).toOpaque()
}
#endif
