import AppKit
let main = NSScreen.main!
let primaryH = NSScreen.screens[0].frame.height
var out: [String] = []
for (i, s) in NSScreen.screens.enumerated() {
  let f = s.frame
  // convert Cocoa (origin bottom-left) to CG top-left global coords
  let topY = primaryH - (f.origin.y + f.height)
  out.append("{\"id\":\(i+1),\"name\":\"\(s.localizedName)\",\"x\":\(Int(f.origin.x)),\"y\":\(Int(topY)),\"w\":\(Int(f.width)),\"h\":\(Int(f.height)),\"scale\":\(s.backingScaleFactor),\"main\":\(s == main)}")
}
print("[" + out.joined(separator: ",") + "]")
