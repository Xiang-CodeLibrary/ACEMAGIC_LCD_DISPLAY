# ACEMAGIC S1 前面板 LCD + LED 控制工具

Python CLI 工具，用于控制 ACEMAGIC S1 小主机前面板的 320x170 TFT LCD 显示屏和 RGB LED 灯带。

![Python](https://img.shields.io/badge/Python-3.10+-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Platform](https://img.shields.io/badge/Platform-Linux-orange)

## 硬件原理

| 组件 | 接口 | 芯片 | 设备节点 |
|------|------|------|----------|
| LCD 显示屏 | USB HID | Holtek HT32 | `/dev/hidraw*` |
| LED 灯带 | USB 串口 | CH340 | `/dev/ttyUSB0` |

- **LCD**: VID=`0x04D9`, PID=`0xFD01`, 320x170 像素, RGB565 色深, 通过 USB Interrupt OUT 端点通信
- **LED**: 波特率 10000, 5 字节命令包, 支持彩虹/呼吸/循环等灯效

## 安装

### 1. 安装系统依赖

```bash
# Ubuntu / Debian
sudo apt-get install python3-usb python3-serial python3-pil python3-psutil

# 或使用 pip
pip install pyusb pyserial Pillow psutil
```

### 2. 配置 udev 规则（免 sudo）

```bash
sudo tee /etc/udev/rules.d/99-acemagic.rules << 'EOF'
SUBSYSTEM=="usb", ATTR{idVendor}=="04d9", ATTR{idProduct}=="fd01", MODE="0666"
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="04d9", ATTRS{idProduct}=="fd01", MODE="0666"
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", MODE="0666"
EOF
sudo udevadm control --reload-rules && sudo udevadm trigger
```

### 3. 下载工具

```bash
git clone https://github.com/Xiang-CodeLibrary/ACEMAGIC_LCD_DISPLAY.git
cd ACEMAGIC_LCD_DISPLAY
```

## 使用方法

### LCD 屏幕控制

```bash
# 清空屏幕
python3 s1ctl.py lcd clear

# 纯色填充 (RGB)
python3 s1ctl.py lcd fill 255 0 0          # 红色
python3 s1ctl.py lcd fill 0 255 0          # 绿色
python3 s1ctl.py lcd fill 0 0 255          # 蓝色

# 显示文字
python3 s1ctl.py lcd text "Hello World"
python3 s1ctl.py lcd text "第一行\n第二行" --fg 0 255 0 --bg 0 0 0 --size 28

# 竖屏模式显示文字
python3 s1ctl.py lcd text "竖屏" --portrait

# 显示图片 (自动缩放)
python3 s1ctl.py lcd image /path/to/photo.png
python3 s1ctl.py lcd image logo.jpg --portrait

# 切换显示方向
python3 s1ctl.py lcd orient landscape
python3 s1ctl.py lcd orient portrait

# 实时系统监控 (CPU / RAM / IP, 每秒刷新)
python3 s1ctl.py lcd sysinfo --interval 1
```

### LED 灯带控制

```bash
# 灯效模式
python3 s1ctl.py led rainbow                        # 彩虹
python3 s1ctl.py led breathing                      # 呼吸
python3 s1ctl.py led colorcycle                     # 颜色循环
python3 s1ctl.py led auto                           # 自动

# 调节亮度和速度 (1-5)
python3 s1ctl.py led rainbow --intensity 5 --speed 4

# 关闭 LED
python3 s1ctl.py led off

# 指定串口
python3 s1ctl.py led rainbow --port /dev/ttyUSB1
```

### 系统监控界面

`sysinfo` 模式会在屏幕上以竖屏布局显示三个圆角方框：

```
┌──────────┐
│   CPU    │
│   23%    │
└──────────┘
┌──────────┐
│   RAM    │
│   45%    │
└──────────┘
┌──────────┐
│ 192.168. │
│ 200.200  │
└──────────┘
```

启动时会自动开启 LED 彩虹灯效。

## 设置为系统服务（开机自启）

```bash
sudo tee /etc/systemd/system/s1lcd.service << 'EOF'
[Unit]
Description=ACEMAGIC S1 LCD Monitor
After=multi-user.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /path/to/s1ctl.py lcd sysinfo --interval 1
Restart=always
RestartSec=3
User=root

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable s1lcd.service
sudo systemctl start s1lcd.service
```

> 注意：将 `/path/to/s1ctl.py` 替换为实际路径。

### 服务管理命令

```bash
sudo systemctl status s1lcd     # 查看状态
sudo systemctl stop s1lcd       # 停止
sudo systemctl start s1lcd      # 启动
sudo systemctl restart s1lcd    # 重启
sudo systemctl disable s1lcd    # 取消开机自启
```

## 通信协议

### LCD 命令格式

每次 USB 写入 **4104 字节**（8 字节头 + 4096 字节数据）：

| 偏移 | 大小 | 说明 |
|------|------|------|
| 0 | 1 | 签名字节 `0x55` |
| 1 | 1 | 命令: `0xA1`=配置, `0xA2`=局部刷新, `0xA3`=全屏重绘 |
| 2 | 1 | 子命令 |
| 3-7 | 5 | 命令参数 |
| 8-4103 | 4096 | 数据区 (像素数据等) |

### 全屏重绘

320 x 170 x 2 = 108,800 字节，分 27 个 chunk 发送：
- Chunk 1-26: 各 4096 字节
- Chunk 27: 2304 字节

### 像素格式

RGB565 大端序（字节交换）：

```python
color = (R5 << 11) | (G6 << 5) | B5  # 5-6-5 位
bytes = [(color >> 8) & 0xFF, color & 0xFF]  # 大端序
```

### LED 命令格式

5 字节，波特率 10000，每字节间隔 5ms：

| 偏移 | 说明 |
|------|------|
| 0 | 签名 `0xFA` |
| 1 | 主题: `0x01`=彩虹, `0x02`=呼吸, `0x03`=循环, `0x04`=关闭, `0x05`=自动 |
| 2 | 亮度 (1-5, 反转: 1=最亮) |
| 3 | 速度 (1-5, 反转: 1=最快) |
| 4 | 校验和: (byte0 + byte1 + byte2 + byte3) & 0xFF |

## 致谢

协议逆向工程参考了以下开源项目：
- [tjaworski/AceMagic-S1-LED-TFT-Linux](https://github.com/tjaworski/AceMagic-S1-LED-TFT-Linux)
- [rojkov/s1display](https://github.com/rojkov/s1display)

## 许可证

MIT License
