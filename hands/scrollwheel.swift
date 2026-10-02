import Foundation
import CoreGraphics
// usage: scrollwheel <x> <y> <dy> [steps]   dy<0 = scroll content up (finger swipe up), in pixels total
let a = CommandLine.arguments
let x = Double(a[1])!, y = Double(a[2])!, dy = Int32(a[3])!
let steps = a.count > 4 ? Int(a[4])! : 10
let src = CGEventSource(stateID: .hidSystemState)
let move = CGEvent(mouseEventSource: src, mouseType: .mouseMoved, mouseCursorPosition: CGPoint(x: x, y: y), mouseButton: .left)
move?.post(tap: .cghidEventTap)
usleep(80_000)
let per = dy / Int32(steps)
for _ in 0..<steps {
  if let e = CGEvent(scrollWheelEvent2Source: src, units: .pixel, wheelCount: 1, wheel1: per, wheel2: 0, wheel3: 0) {
    e.location = CGPoint(x: x, y: y)
    e.post(tap: .cghidEventTap)
  }
  usleep(16_000)
}
