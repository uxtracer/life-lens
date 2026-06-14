// life_lens 智能相框 — ESP32 示例固件
// ---------------------------------------------------------------------------
// 作用:连上家里 Wi-Fi,每隔 N 秒向 life_lens 的相框接口 GET 一张已缩放好的
//       JPEG,解码后铺到屏幕上,实现照片轮播。
//
// 依赖(Arduino 库管理器里装):
//   - TFT_eSPI        (Bodmer)  —— 驱动 SPI TFT 屏;**必须按你的屏在 User_Setup.h 里配好引脚**
//   - TJpg_Decoder    (Bodmer)  —— JPEG 解码
//   （WiFi / HTTPClient 是 ESP32 核心自带,无需另装）
//
// 适配你的硬件:
//   1. 改下面的 WIFI_SSID / WIFI_PASS / LENS_HOST / LENS_PORT
//   2. 把 SCREEN_W / SCREEN_H 改成你屏幕的分辨率
//   3. TFT_eSPI 的屏幕引脚在它自己的 User_Setup.h 配(不同板子不一样,见该库文档)
//
// 服务端准备:在 life_lens 配置页打开「🖼️ 智能相框」开关(默认关)。
//   接口默认轮播收藏照片;想换主题在配置页填,或在 URL 上加 ?theme=海边。
// ---------------------------------------------------------------------------

#include <WiFi.h>
#include <HTTPClient.h>
#include <TFT_eSPI.h>
#include <TJpg_Decoder.h>

// ====== 改这里 ======
const char* WIFI_SSID = "你的WiFi名字";
const char* WIFI_PASS = "你的WiFi密码";
const char* LENS_HOST = "192.168.1.100";    // 跑 life_lens 那台机器的局域网 IP(在配置页「智能相框」卡片里能看到)
const uint16_t LENS_PORT = 7878;

const int SCREEN_W = 320;     // 你的屏幕宽
const int SCREEN_H = 240;     // 你的屏幕高
const char* THEME  = "";      // 留空 = 用服务端默认(收藏 / 配置页设的主题);也可写 "海边"
const char* FIT    = "cover"; // cover=裁剪铺满屏 | contain=完整不裁(可能留黑边)
const uint32_t INTERVAL_MS = 30000;   // 每张停留 30 秒
// ====================

TFT_eSPI tft = TFT_eSPI();

// TJpg_Decoder 每解出一块 16x16/8x8 像素就回调一次,直接画到屏上
bool drawBlock(int16_t x, int16_t y, uint16_t w, uint16_t h, uint16_t* bitmap) {
  if (y >= tft.height()) return false;     // 超出屏幕底部就停
  tft.pushImage(x, y, w, h, bitmap);
  return true;
}

void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  tft.fillScreen(TFT_BLACK);
  tft.setCursor(8, 8);
  tft.setTextColor(TFT_WHITE);
  tft.print("连接 Wi-Fi ...");
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) {
    delay(300);
    tft.print(".");
  }
}

// 拉一张图到内存,再交给解码器画出来。成功返 true。
bool showNextPhoto() {
  connectWiFi();
  if (WiFi.status() != WL_CONNECTED) return false;

  // 组装 URL:/api/frame/next?w=..&h=..&mode=..[&theme=..]
  String url = "http://" + String(LENS_HOST) + ":" + String(LENS_PORT) +
               "/api/frame/next?w=" + String(SCREEN_W) +
               "&h=" + String(SCREEN_H) + "&mode=" + FIT;
  if (strlen(THEME) > 0) {
    url += "&theme=";
    url += THEME;   // 简单主题(ASCII)直接拼;中文主题建议在服务端配置页设,免去 URL 编码
  }

  HTTPClient http;
  http.begin(url);
  int code = http.GET();
  if (code != HTTP_CODE_OK) {
    http.end();
    tft.fillScreen(TFT_BLACK);
    tft.setCursor(8, 8);
    tft.setTextColor(TFT_RED);
    tft.print("HTTP ");
    tft.print(code);   // 403=相框开关没开 / 404=照片池为空
    return false;
  }

  int len = http.getSize();
  if (len <= 0 || len > 200 * 1024) {   // 防御:异常大小不收
    http.end();
    return false;
  }

  uint8_t* buf = (uint8_t*) malloc(len);
  if (!buf) { http.end(); return false; }

  // 把整张 JPEG 读进内存
  WiFiClient* stream = http.getStreamPtr();
  int got = 0;
  uint32_t t0 = millis();
  while (got < len && millis() - t0 < 10000) {
    if (stream->available()) {
      got += stream->readBytes(buf + got, len - got);
    } else {
      delay(1);
    }
  }
  http.end();

  bool ok = false;
  if (got == len) {
    tft.fillScreen(TFT_BLACK);
    TJpgDec.drawJpg(0, 0, buf, len);   // 解码 + 通过 drawBlock 回调铺屏
    ok = true;
  }
  free(buf);
  return ok;
}

void setup() {
  Serial.begin(115200);
  tft.init();
  tft.setRotation(1);                 // 按你屏幕方向调(0~3)
  tft.fillScreen(TFT_BLACK);

  // TJpg_Decoder 配置:小端字节序 + 注册回调
  TJpgDec.setSwapBytes(true);
  TJpgDec.setCallback(drawBlock);

  connectWiFi();
  showNextPhoto();
}

void loop() {
  delay(INTERVAL_MS);
  showNextPhoto();
}
