# ESP32 智能相框

把带 SPI 屏的 ESP32 做成 life_lens 照片相框:连家里 Wi-Fi,定时拉一张已缩放好的 JPEG 轮播显示。**照片字节只在局域网内传,不出家门。**

## 服务端(life_lens)准备

1. 在配置页打开「🖼️ 智能相框」开关(默认关,opt-in)。
2. 默认轮播**收藏照片**;想换主题就在卡片里填,或在固件 URL 上加 `?theme=...`。主题分三种:
   - **留空** → 收藏照片
   - **填人名**(已在「面孔」里命名的人)→ 出这个人**全部**照片(按人脸,不靠描述提没提到名字)
   - **填场景/物品**(如「海边」「蛋糕」)→ 按内容搜索
   卡片会显示当前主题识别成了"人物"还是"内容",以及池子有多少张。
3. 卡片会显示设备该访问的地址,形如 `http://192.168.x.x:7878/api/frame/next`。

> 安全:这是和「问相册」**完全独立**的接口,各自独立开关、互不影响。局域网设备只能取图(只读),改不了任何配置;关掉开关设备立即无法访问。

## 接口速查

所有端点在 `/api/frame/` 下,需要相框开关已开(否则 403):

| 端点 | 返回 | 说明 |
|---|---|---|
| `GET /api/frame/next` | `image/jpeg` | **主接口**:轮播下一张。一次 GET 直接出图,元信息在响应头 |
| `GET /api/frame/photo/{id}` | `image/jpeg` | 取指定照片 id |
| `GET /api/frame/playlist` | JSON | id 列表 + 描述(给自己管播放顺序的固件) |
| `GET /api/frame/info` | JSON | 当前主题 + 照片池大小(自测用) |

`next` / `photo` 的查询参数:

- `w`, `h` — 目标尺寸(默认 480,按你屏幕分辨率传)
- `mode` — `contain`(完整不裁,可能留边)或 `cover`(裁剪铺满)
- `theme` — 临时指定主题(留空用服务端默认);中文主题建议在配置页设,免 URL 编码
- `quality` — JPEG 质量 40~95(默认 82)

`next` 的响应头(可选读取,显示标题用):

- `X-Photo-Id` — 照片 id
- `X-Photo-Date` — 拍摄时间
- `X-Photo-Caption` — AI 生成的描述(**URL 编码**,中文需 decode)

## 烧录 `esp32_photo_frame.ino`

1. Arduino 库管理器装:**TFT_eSPI**(Bodmer)、**TJpg_Decoder**(Bodmer)。
2. 按你的屏在 **TFT_eSPI 的 `User_Setup.h`** 里配好引脚/驱动(不同板子不一样,见该库文档)。
3. 改 `.ino` 顶部的 `WIFI_SSID` / `WIFI_PASS` / `LENS_HOST` / `LENS_PORT` / `SCREEN_W` / `SCREEN_H`。
4. 选对开发板烧录。

固件逻辑很简单:连 Wi-Fi → 每 `INTERVAL_MS` 毫秒 GET 一次 `next` → 整张读进内存 → TJpg_Decoder 解码铺屏。服务端已把图缩放到 `w×h`,ESP32 不用自己缩放,内存压力小。

## 排错

- **屏幕显示 `HTTP 403`**:服务端相框开关没开,去配置页打开。
- **`HTTP 404`**:照片池为空(没有收藏照片,或该主题没匹配到)。换主题或先在 life_lens 里收藏几张。
- **花屏 / 颜色不对**:`TJpgDec.setSwapBytes(true)` 试着改成 `false`;或调 `tft.setRotation()`。
- **连不上**:确认 ESP32 和跑 life_lens 的机器在**同一个** Wi-Fi;`LENS_HOST` 填对方局域网 IP(配置页卡片里有)。
- **内存不足**:把 `w/h` 调小、`quality` 调低,单张 JPEG 越小越稳。
