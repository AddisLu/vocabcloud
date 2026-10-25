"""產生選單列 template 圖示（單色＋透明，macOS 會依深淺色主題自動反轉）。

設計：雲朵（= Cloud）裡鏤空一個粗體 A（= 單字）。
  on  → 實心雲、鏤空 A
  off → 空心雲、實心 A（一眼分得出停用）
輸出 36×36 px（@2x，顯示為 18pt）與 288 px 預覽圖。
"""
from pathlib import Path

from AppKit import (NSAffineTransform, NSBezierPath, NSBitmapImageRep, NSColor, NSCompositingOperationDestinationOut, NSCompositingOperationSourceOver,
                    NSFont, NSFontAttributeName, NSForegroundColorAttributeName, NSGraphicsContext,
                    NSImage, NSMakeRect, NSPNGFileType, NSString)
from Foundation import NSMakePoint

OUT = Path(__file__).resolve().parent / "icons"
OUT.mkdir(exist_ok=True)


def cloud_path(s):
    """s = 畫布邊長。雲 = 三個圓 + 圓角底座，整體偏寬扁，底部留 margin。"""
    p = NSBezierPath.bezierPath()
    m = s * 0.06
    base_y = s * 0.26
    # 底座
    p.appendBezierPathWithRoundedRect_xRadius_yRadius_(
        NSMakeRect(m + s * 0.06, base_y, s - 2 * m - s * 0.12, s * 0.30), s * 0.15, s * 0.15)
    # 三個圓：左小、中大、右中
    for cx, cy, r in ((0.30, 0.46, 0.20), (0.53, 0.56, 0.27), (0.76, 0.46, 0.19)):
        p.appendBezierPathWithOvalInRect_(
            NSMakeRect(s * (cx - r), s * (cy - r), s * 2 * r, s * 2 * r))
    return p


def letter_attrs(s, color):
    return {NSFontAttributeName: NSFont.boldSystemFontOfSize_(s * 0.50),
            NSForegroundColorAttributeName: color}


def draw(s, on):
    img = NSImage.alloc().initWithSize_((s, s))
    img.lockFocus()
    NSGraphicsContext.currentContext().setShouldAntialias_(True)
    black = NSColor.blackColor()
    cloud = cloud_path(s)
    text = NSString.stringWithString_("A")
    attrs = letter_attrs(s, black)
    tw, th = text.sizeWithAttributes_(attrs)
    tx, ty = (s - tw) / 2 + s * 0.005, s * 0.21
    if on:
        black.setFill()
        cloud.fill()
        # 鏤空 A
        NSGraphicsContext.currentContext().setCompositingOperation_(NSCompositingOperationDestinationOut)
        text.drawAtPoint_withAttributes_(NSMakePoint(tx, ty), attrs)
    else:
        # 外框 = 實心雲 減去 以中心縮小一圈的雲（直接 stroke 會把三個圓的交疊線都畫出來）
        black.setFill()
        cloud.fill()
        inner = cloud_path(s)
        t = NSAffineTransform.transform()
        cx, cy = s * 0.53, s * 0.42
        t.translateXBy_yBy_(cx, cy)
        t.scaleBy_(0.80)
        t.translateXBy_yBy_(-cx, -cy)
        inner.transformUsingAffineTransform_(t)
        NSGraphicsContext.currentContext().setCompositingOperation_(NSCompositingOperationDestinationOut)
        inner.fill()
        NSGraphicsContext.currentContext().setCompositingOperation_(NSCompositingOperationSourceOver)
        text.drawAtPoint_withAttributes_(NSMakePoint(tx, ty), attrs)
    img.unlockFocus()
    return img


def save(img, path):
    tiff = img.TIFFRepresentation()
    rep = NSBitmapImageRep.imageRepWithData_(tiff)
    png = rep.representationUsingType_properties_(NSPNGFileType, None)
    png.writeToFile_atomically_(str(path), True)


if __name__ == "__main__":
    for on in (True, False):
        name = "on" if on else "off"
        save(draw(36, on), OUT / f"menubar_{name}@2x.png")
        save(draw(288, on), OUT / f"preview_{name}.png")
    print("icons written to", OUT)
