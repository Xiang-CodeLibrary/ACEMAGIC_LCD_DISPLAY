#!/usr/bin/env python3
"""s1ctl - ACEMAGIC S1 前面板 LCD + LED 控制工具

LCD: 320x170 RGB565, 通过 USB HID (VID=0x04D9, PID=0xFD01) 控制
LED: RGB 灯带, 通过 USB 串口 CH340 (/dev/ttyUSB0) 控制, 波特率 10000
"""

import argparse
import struct
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
LCD_VID = 0x04D9
LCD_PID = 0xFD01
LCD_W, LCD_H = 320, 170
LCD_BPP = 2  # RGB565
FRAME_SIZE = LCD_W * LCD_H * LCD_BPP  # 108800
BUF_SIZE = 4104  # 8 header + 4096 data
CHUNK_DATA = 4096
TOTAL_CHUNKS = 27  # 26*4096 + 2304 = 108800

LED_BAUD = 10000
LED_SIG = 0xFA

SIG = 0x55
CMD_REFRESH = 0xA2  # 局部刷新
CMD_REDRAW = 0xA3   # 全屏重绘
CMD_CONFIG = 0xA1   # 配置命令
SUB_ORIENT = 0xF1
SUB_TIME = 0xF2
SUB_START = 0xF0
SUB_CONT = 0xF1
SUB_END = 0xF2

# LED 主题
LED_THEMES = {
    "rainbow": 0x01,
    "breathing": 0x02,
    "colorcycle": 0x03,
    "off": 0x04,
    "auto": 0x05,
}

# ---------------------------------------------------------------------------
# RGB565 转换
# ---------------------------------------------------------------------------
def rgb_to_565(r, g, b):
    """RGB888 -> RGB565 big-endian (byte-swapped)"""
    c = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
    return struct.pack(">H", c)


def image_to_rgb565(img, rotate=0):
    """PIL Image -> bytes (RGB565 big-endian), resize 到 320x170
    rotate: 旋转角度 (90=逆时针90°, 270=顺时针90°)
    """
    if rotate:
        img = img.rotate(rotate, expand=True)
    img = img.convert("RGB").resize((LCD_W, LCD_H))
    pixels = img.tobytes()  # RGBRGB...
    buf = bytearray(FRAME_SIZE)
    for i in range(LCD_W * LCD_H):
        off = i * 3
        r, g, b = pixels[off], pixels[off + 1], pixels[off + 2]
        buf[i * 2: i * 2 + 2] = rgb_to_565(r, g, b)
    return bytes(buf)


def solid_color_rgb565(r, g, b):
    """纯色填充 -> RGB565 帧"""
    px = rgb_to_565(r, g, b)
    return px * (LCD_W * LCD_H)


def text_to_rgb565(text, fg=(255, 255, 255), bg=(0, 0, 0), fontsize=24, portrait=False):
    """文字渲染 -> RGB565"""
    from PIL import Image, ImageDraw, ImageFont

    # 竖屏: 画布 170x320, 横屏: 320x170
    w, h = (LCD_H, LCD_W) if portrait else (LCD_W, LCD_H)
    img = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", fontsize)
    except OSError:
        try:
            font = ImageFont.truetype("/usr/share/fonts/TTF/DejaVuSans.ttf", fontsize)
        except OSError:
            font = ImageFont.load_default()

    lines = text.replace("\\n", "\n").split("\n")
    y = 10
    for line in lines:
        draw.text((10, y), line, fill=fg, font=font)
        y += fontsize + 4
    return image_to_rgb565(img, rotate=90 if portrait else 0)


# ---------------------------------------------------------------------------
# LCD USB 通信 (pyusb)
# ---------------------------------------------------------------------------
LCD_INTF = 1       # Interface 1 = LCD 控制
LCD_EP_OUT = 0x02  # Interrupt OUT endpoint

class LCDDevice:
    def __init__(self):
        self._dev = None
        self._ep = None

    def open(self):
        import usb.core
        import usb.util

        dev = usb.core.find(idVendor=LCD_VID, idProduct=LCD_PID)
        if dev is None:
            print("错误: 找不到 ACEMAGIC LCD 设备 (04d9:fd01)", file=sys.stderr)
            print("请检查 USB 连接, 或用 lsusb 确认设备存在", file=sys.stderr)
            sys.exit(1)

        # detach 所有接口的内核驱动
        for intf_num in range(3):
            try:
                if dev.is_kernel_driver_active(intf_num):
                    dev.detach_kernel_driver(intf_num)
            except (usb.core.USBError, NotImplementedError):
                pass

        try:
            dev.set_configuration()
        except usb.core.USBError:
            pass  # 已经是当前 configuration
        cfg = dev.get_active_configuration()
        intf = cfg[(LCD_INTF, 0)]
        ep = usb.util.find_descriptor(
            intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
        )
        if ep is None:
            print("错误: 找不到 LCD OUT endpoint", file=sys.stderr)
            sys.exit(1)

        self._dev = dev
        self._ep = ep
        return self

    def close(self):
        if self._dev:
            import usb.util
            usb.util.dispose_resources(self._dev)
            self._dev = None
            self._ep = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def _send(self, header8, data=b""):
        """发送 4104 字节: 8 header + 4096 data (通过 interrupt OUT)"""
        buf = bytearray(BUF_SIZE)
        buf[:len(header8)] = header8
        if data:
            buf[8: 8 + len(data)] = data[:CHUNK_DATA]
        self._ep.write(bytes(buf))

    def set_orientation(self, landscape=True):
        hdr = bytes([SIG, CMD_CONFIG, SUB_ORIENT, 0x01 if landscape else 0x02, 0, 0, 0, 0])
        self._send(hdr)

    def set_time(self):
        t = time.localtime()
        hdr = bytes([SIG, CMD_CONFIG, SUB_TIME, t.tm_hour, t.tm_min, t.tm_sec, 0, 0])
        self._send(hdr)

    def redraw(self, frame_bytes):
        """全屏重绘: 27 个 chunk"""
        assert len(frame_bytes) == FRAME_SIZE, f"帧大小应为 {FRAME_SIZE}, 实际 {len(frame_bytes)}"
        offset = 0
        for seq in range(1, TOTAL_CHUNKS + 1):
            if seq == 1:
                sub = SUB_START
            elif seq == TOTAL_CHUNKS:
                sub = SUB_END
            else:
                sub = SUB_CONT

            chunk_len = CHUNK_DATA if seq < TOTAL_CHUNKS else (FRAME_SIZE - offset)
            chunk_data = frame_bytes[offset: offset + chunk_len]

            # offset 和 length 是 big-endian
            off_hi = (offset >> 8) & 0xFF
            off_lo = offset & 0xFF
            len_hi = (chunk_len >> 8) & 0xFF
            len_lo = chunk_len & 0xFF

            hdr = bytes([SIG, CMD_REDRAW, sub, seq, off_hi, off_lo, len_hi, len_lo])
            self._send(hdr, chunk_data)
            offset += chunk_len

    def refresh_rect(self, x, y, w, h, data):
        """局部刷新一个矩形区域 (w*h*2 <= 4096)"""
        assert w * h * 2 <= CHUNK_DATA, f"区域太大: {w}x{h}={w*h*2} > 4096"
        # x, y 是 little-endian
        hdr = bytearray(8)
        hdr[0] = SIG
        hdr[1] = CMD_REFRESH
        struct.pack_into("<H", hdr, 2, x)
        struct.pack_into("<H", hdr, 4, y)
        hdr[6] = w
        hdr[7] = h
        self._send(bytes(hdr), data)

    def clear(self):
        """清屏 (黑色)"""
        self.redraw(solid_color_rgb565(0, 0, 0))


# ---------------------------------------------------------------------------
# LED 串口通信
# ---------------------------------------------------------------------------
class LEDDevice:
    def __init__(self, port="/dev/ttyUSB0"):
        self.port = port
        self._ser = None

    def open(self):
        import serial
        self._ser = serial.Serial(self.port, LED_BAUD, timeout=1)
        return self

    def close(self):
        if self._ser:
            self._ser.close()
            self._ser = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def send(self, theme, intensity=3, speed=3):
        """发送 5 字节 LED 命令, 每字节间隔 5ms"""
        # 反转 intensity 和 speed (用户 1-5, 线路 5-1)
        wire_int = min(5, max(1, 6 - intensity))
        wire_spd = min(5, max(1, 6 - speed))
        checksum = (LED_SIG + theme + wire_int + wire_spd) & 0xFF
        packet = bytes([LED_SIG, theme, wire_int, wire_spd, checksum])
        for b in packet:
            self._ser.write(bytes([b]))
            time.sleep(0.005)


# ---------------------------------------------------------------------------
# CLI 子命令实现
# ---------------------------------------------------------------------------
def cmd_lcd_clear(args):
    with LCDDevice() as lcd:
        lcd.set_orientation(True)
        lcd.clear()
        print("屏幕已清空")


def cmd_lcd_fill(args):
    frame = solid_color_rgb565(args.r, args.g, args.b)
    with LCDDevice() as lcd:
        lcd.set_orientation(True)
        lcd.redraw(frame)
        print(f"已填充颜色 ({args.r}, {args.g}, {args.b})")


def cmd_lcd_text(args):
    fg = tuple(args.fg) if args.fg else (255, 255, 255)
    bg = tuple(args.bg) if args.bg else (0, 0, 0)
    frame = text_to_rgb565(args.text, fg=fg, bg=bg, fontsize=args.size, portrait=args.portrait)
    with LCDDevice() as lcd:
        lcd.set_orientation(True)
        lcd.redraw(frame)
        print("文字已显示")


def cmd_lcd_image(args):
    from PIL import Image
    img = Image.open(args.path)
    frame = image_to_rgb565(img, rotate=90 if args.portrait else 0)
    with LCDDevice() as lcd:
        lcd.set_orientation(True)
        lcd.redraw(frame)
        print(f"图片已显示: {args.path}")


def cmd_lcd_orient(args):
    landscape = args.direction == "landscape"
    with LCDDevice() as lcd:
        lcd.set_orientation(landscape)
        print(f"方向已设置: {args.direction}")


def _get_ip():
    """获取第一个非 lo 网口的 IPv4 地址"""
    import psutil
    addrs = psutil.net_if_addrs()
    for iface, addr_list in addrs.items():
        if iface == "lo":
            continue
        for addr in addr_list:
            if addr.family.name == "AF_INET" and not addr.address.startswith("127."):
                return addr.address
    return "No IP"


def cmd_lcd_sysinfo(args):
    """实时系统信息循环显示 (竖屏, 大字风格)"""
    import psutil
    from PIL import Image, ImageDraw, ImageFont

    def load_font(name, size):
        paths = {
            "bold": [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
            ],
            "regular": [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/TTF/DejaVuSans.ttf",
            ],
            "mono": [
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
                "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
            ],
        }
        for path in paths.get(name, paths["regular"]):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
        return ImageFont.load_default()

    font_pct = load_font("bold", 40)    # 大数字
    font_label = load_font("bold", 22)  # 标签 CPU / RAM
    font_ip = load_font("bold", 22)    # IP 地址

    PW, PH = LCD_H, LCD_W  # 170 x 320
    interval = args.interval

    def draw_rounded_rect(draw, xy, radius, outline, width=2):
        """画圆角矩形边框"""
        x0, y0, x1, y1 = xy
        r = radius
        # 四个角的圆弧
        draw.arc([x0, y0, x0 + 2*r, y0 + 2*r], 180, 270, fill=outline, width=width)
        draw.arc([x1 - 2*r, y0, x1, y0 + 2*r], 270, 360, fill=outline, width=width)
        draw.arc([x0, y1 - 2*r, x0 + 2*r, y1], 90, 180, fill=outline, width=width)
        draw.arc([x1 - 2*r, y1 - 2*r, x1, y1], 0, 90, fill=outline, width=width)
        # 四条边
        draw.line([(x0 + r, y0), (x1 - r, y0)], fill=outline, width=width)
        draw.line([(x0 + r, y1), (x1 - r, y1)], fill=outline, width=width)
        draw.line([(x0, y0 + r), (x0, y1 - r)], fill=outline, width=width)
        draw.line([(x1, y0 + r), (x1, y1 - r)], fill=outline, width=width)

    # 启动 LED 彩虹灯效
    try:
        with LEDDevice() as led:
            led.send(LED_THEMES["rainbow"], intensity=4, speed=3)
    except Exception:
        pass

    with LCDDevice() as lcd:
        lcd.set_time()
        lcd.set_orientation(True)
        lcd.clear()
        lcd.set_time()
        print(f"系统信息监控中 (间隔 {interval}s), Ctrl+C 退出...")
        try:
            while True:
                lcd.set_time()
                cpu = psutil.cpu_percent(interval=0.1)
                mem = psutil.virtual_memory()
                ip = _get_ip()

                img = Image.new("RGB", (PW, PH), (5, 5, 15))
                draw = ImageDraw.Draw(img)
                cx = PW // 2
                box_margin = 10
                box_w = PW - box_margin * 2
                border_color = (50, 80, 130)
                r = 10
                gap = 8
                margin_y = 8

                # IP 框高度: 两行文字(22pt) + 上下留白 = 约 70
                ip_box_h = 70
                # CPU/RAM 框平分剩余高度
                big_box_h = (PH - margin_y * 2 - gap * 2 - ip_box_h) // 2

                # --- CPU 框 ---
                y = margin_y
                draw_rounded_rect(draw, [box_margin, y, box_margin + box_w, y + big_box_h], r, border_color)
                text_block = 22 + 6 + 40
                ty = y + (big_box_h - text_block) // 2
                draw.text((cx, ty), "CPU", fill=(60, 120, 220), font=font_label, anchor="mt")
                draw.text((cx, ty + 28), f"{cpu:.0f}%", fill=(0, 230, 80), font=font_pct, anchor="mt")

                # --- RAM 框 ---
                y += big_box_h + gap
                draw_rounded_rect(draw, [box_margin, y, box_margin + box_w, y + big_box_h], r, border_color)
                ty = y + (big_box_h - text_block) // 2
                draw.text((cx, ty), "RAM", fill=(60, 120, 220), font=font_label, anchor="mt")
                draw.text((cx, ty + 28), f"{mem.percent:.0f}%", fill=(0, 230, 80), font=font_pct, anchor="mt")

                # --- IP 框 ---
                y += big_box_h + gap
                draw_rounded_rect(draw, [box_margin, y, box_margin + box_w, y + ip_box_h], r, border_color)
                octets = ip.split(".")
                if len(octets) == 4:
                    line1 = f"{octets[0]}.{octets[1]}."
                    line2 = f"{octets[2]}.{octets[3]}"
                else:
                    line1 = ip
                    line2 = ""
                # 两行文字居中在框内
                ip_cy = y + ip_box_h // 2
                draw.text((cx, ip_cy - 13), line1, fill=(255, 255, 255), font=font_ip, anchor="mm")
                if line2:
                    draw.text((cx, ip_cy + 13), line2, fill=(255, 255, 255), font=font_ip, anchor="mm")

                lcd.redraw(image_to_rgb565(img, rotate=90))
                lcd.set_time()
                time.sleep(max(0, interval - 0.5))
                lcd.set_time()
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n已停止")


def cmd_led(args):
    theme = LED_THEMES.get(args.effect, 0x04)
    with LEDDevice(port=args.port) as led:
        led.send(theme, intensity=args.intensity, speed=args.speed)
        print(f"LED: {args.effect} (亮度={args.intensity}, 速度={args.speed})")


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        prog="s1ctl",
        description="ACEMAGIC S1 前面板 LCD + LED 控制工具",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # --- lcd ---
    lcd_p = sub.add_parser("lcd", help="LCD 屏幕控制")
    lcd_sub = lcd_p.add_subparsers(dest="action", required=True)

    # lcd clear
    lcd_sub.add_parser("clear", help="清空屏幕").set_defaults(func=cmd_lcd_clear)

    # lcd fill R G B
    p = lcd_sub.add_parser("fill", help="纯色填充")
    p.add_argument("r", type=int, help="红 0-255")
    p.add_argument("g", type=int, help="绿 0-255")
    p.add_argument("b", type=int, help="蓝 0-255")
    p.set_defaults(func=cmd_lcd_fill)

    # lcd text "..."
    p = lcd_sub.add_parser("text", help="显示文字")
    p.add_argument("text", help="要显示的文字 (\\n 换行)")
    p.add_argument("--fg", type=int, nargs=3, metavar=("R", "G", "B"), help="前景色")
    p.add_argument("--bg", type=int, nargs=3, metavar=("R", "G", "B"), help="背景色")
    p.add_argument("--size", type=int, default=24, help="字号 (默认 24)")
    p.add_argument("--portrait", action="store_true", help="竖屏模式")
    p.set_defaults(func=cmd_lcd_text)

    # lcd image <path>
    p = lcd_sub.add_parser("image", help="显示图片")
    p.add_argument("path", help="图片路径")
    p.add_argument("--portrait", action="store_true", help="竖屏模式")
    p.set_defaults(func=cmd_lcd_image)

    # lcd orient
    p = lcd_sub.add_parser("orient", help="设置方向")
    p.add_argument("direction", choices=["landscape", "portrait"], help="方向")
    p.set_defaults(func=cmd_lcd_orient)

    # lcd sysinfo
    p = lcd_sub.add_parser("sysinfo", help="实时系统信息")
    p.add_argument("--interval", type=float, default=2, help="刷新间隔秒数 (默认 2)")
    p.set_defaults(func=cmd_lcd_sysinfo)

    # --- led ---
    led_p = sub.add_parser("led", help="LED 灯带控制")
    led_p.add_argument("effect", choices=list(LED_THEMES.keys()), help="灯效")
    led_p.add_argument("--intensity", type=int, default=3, choices=range(1, 6), help="亮度 1-5 (默认 3)")
    led_p.add_argument("--speed", type=int, default=3, choices=range(1, 6), help="速度 1-5 (默认 3)")
    led_p.add_argument("--port", default="/dev/ttyUSB0", help="串口路径 (默认 /dev/ttyUSB0)")
    led_p.set_defaults(func=cmd_led)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
