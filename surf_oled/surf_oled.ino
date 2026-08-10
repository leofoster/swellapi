/*
 * surf_oled.ino — ESP32 + I2C SSD1306 OLED surf display
 *
 * Polls the SURFMOD API's microcontroller endpoint:
 *     GET https://<host>/esp/<island>?blocks=8
 * which returns 3-hour-averaged blocks (~950 bytes), and cycles through them:
 * NOW -> +3h -> +6h -> ... re-fetching every 15 minutes.
 *
 * NOTE ON COLOR: this panel is I2C (GND/VCC/SCL/SDA), i.e. an SSD1306. It has
 * no addressable color, so green-for-good / red-for-bad is not possible. The
 * rating is instead encoded three ways: the word itself, a proportional bar,
 * and (on the common yellow-top/blue-bottom panels) by sitting in the top 16
 * rows, which are physically yellow.
 *
 * Setup:
 *   1. Copy secrets_example.h to secrets.h and fill in your details.
 *   2. Library Manager: ArduinoJson (v7.x), Adafruit SSD1306, Adafruit GFX.
 *   3. Wiring: SDA -> GPIO21, SCL -> GPIO22, VCC -> 3V3, GND -> GND.
 */

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

#include "secrets.h"      // WIFI_SSID, WIFI_PASS, API_HOST, ISLAND_KEY

// ─── CONFIG ──────────────────────────────────────────────────────────────
#define NUM_BLOCKS    8                  // 8 x 3h = 24h ahead (API max is 16)

#define SCREEN_WIDTH  128
#define SCREEN_HEIGHT 64                 // set to 32 if the probe says 128x32
#define OLED_ADDR     0x3C               // some modules are 0x3D
#define PIN_SDA       21
#define PIN_SCL       22

const uint32_t REFRESH_MS = 15UL * 60UL * 1000UL;   // re-poll every 15 min
const uint32_t RETRY_MS   = 60UL * 1000UL;          // after a failed poll
const uint32_t PAGE_MS    = 4000;                   // time on each 3h block

// Layout. Rows 0-15 are the yellow band on two-color panels, so the rating
// word goes there.
#if SCREEN_HEIGHT >= 64
  const int16_t Y_QUALITY = 0,  Y_TAG  = 0,  Y_TIME = 8,
                Y_RULE    = 17, Y_BIG  = 21, Y_DET  = 25,
                Y_WIND    = 40, Y_BAR  = 54, BAR_H  = 8;
  const int16_t X_RIGHT   = 86, X_DET  = 56;
  const bool    COMPACT   = false;
#else
  // 128x32: no room for the large height digits or the bar.
  const int16_t Y_QUALITY = 0,  Y_TAG  = 0,  Y_TIME = 8,
                Y_RULE    = 16, Y_BIG  = 18, Y_DET  = 18,
                Y_WIND    = 25, Y_BAR  = 0,  BAR_H  = 0;
  const int16_t X_RIGHT   = 86, X_DET  = 56;
  const bool    COMPACT   = true;
#endif

Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, -1);

// ─── STATE ───────────────────────────────────────────────────────────────
struct Block {
  char  timeLabel[10];   // "Tue 03"
  char  quality[10];     // FLAT / POOR / FAIR / GOOD / PUMPING
  char  swellDir[6];
  char  windDir[6];
  int   hoursFromNow;
  float swellHeight;
  float swellPeriod;
  float windSpeed;
  float rating;          // 0.0 - 1.0
};

char     spotName[20]  = "Surf";
Block    blocks[NUM_BLOCKS];
uint8_t  blockCount    = 0;
bool     haveData      = false;
uint32_t lastPoll      = 0;
uint32_t lastPage      = 0;
uint8_t  page          = 0;
char     lastError[28] = "";

// ─── HELPERS ─────────────────────────────────────────────────────────────
static void copyStr(char *dst, size_t n, const char *src) {
  if (!src) { dst[0] = '\0'; return; }
  strncpy(dst, src, n - 1);
  dst[n - 1] = '\0';
}

static void showStatus(const char *line1, const char *line2) {
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 0);
  display.print(line1);
  if (line2) { display.setCursor(0, 12); display.print(line2); }
  display.display();
}

// ─── WIFI ────────────────────────────────────────────────────────────────
static bool ensureWifi() {
  if (WiFi.status() == WL_CONNECTED) return true;

  showStatus("Connecting WiFi", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) delay(250);

  if (WiFi.status() != WL_CONNECTED) {
    copyStr(lastError, sizeof(lastError), "wifi failed");
    return false;
  }
  return true;
}

// ─── FETCH + PARSE ───────────────────────────────────────────────────────
static bool pollForecast() {
  if (!ensureWifi()) return false;

  char url[160];
  snprintf(url, sizeof(url), "https://%s/esp/%s?blocks=%d",
           API_HOST, ISLAND_KEY, NUM_BLOCKS);

  WiFiClientSecure client;
  client.setInsecure();          // no cert pinning; public forecast data only
  client.setTimeout(45);         // seconds — Render free tier cold-starts slowly

  HTTPClient http;
  http.setTimeout(45000);
  http.setReuse(false);
  if (!http.begin(client, url)) {
    copyStr(lastError, sizeof(lastError), "http begin fail");
    return false;
  }

  int code = http.GET();
  if (code != HTTP_CODE_OK) {
    snprintf(lastError, sizeof(lastError), "HTTP %d", code);
    http.end();
    return false;
  }

  // /esp returns ~950 bytes for 8 blocks, so the whole body fits in RAM.
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, http.getStream());
  http.end();

  if (err) {
    snprintf(lastError, sizeof(lastError), "json %s", err.c_str());
    return false;
  }

  copyStr(spotName, sizeof(spotName), doc["s"] | "Surf");

  blockCount = 0;
  for (JsonObject b : doc["b"].as<JsonArray>()) {
    if (blockCount >= NUM_BLOCKS) break;
    Block &v = blocks[blockCount];

    copyStr(v.timeLabel, sizeof(v.timeLabel), b["t"]  | "--");
    copyStr(v.quality,   sizeof(v.quality),   b["q"]  | "--");
    copyStr(v.swellDir,  sizeof(v.swellDir),  b["sd"] | "--");
    copyStr(v.windDir,   sizeof(v.windDir),   b["wd"] | "--");
    v.hoursFromNow = b["hn"] | 0;
    v.swellHeight  = b["h"]  | 0.0f;
    v.swellPeriod  = b["p"]  | 0.0f;
    v.windSpeed    = b["w"]  | 0.0f;
    v.rating       = b["r"]  | 0.0f;
    blockCount++;
  }

  if (blockCount == 0) {
    copyStr(lastError, sizeof(lastError), "empty forecast");
    return false;
  }
  lastError[0] = '\0';
  return true;
}

// ─── RENDER ──────────────────────────────────────────────────────────────
static void drawBlock(const Block &v) {
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);

  // Rating word, large, in the top band
  display.setTextSize(2);
  display.setCursor(0, Y_QUALITY);
  display.print(v.quality);

  // Which 3h block this is, and its clock time
  display.setTextSize(1);
  display.setCursor(X_RIGHT, Y_TAG);
  if (v.hoursFromNow == 0) display.print(" NOW");
  else                     display.printf(" +%dh", v.hoursFromNow);
  display.setCursor(X_RIGHT, Y_TIME);
  display.print(v.timeLabel);

  display.drawFastHLine(0, Y_RULE, SCREEN_WIDTH, SSD1306_WHITE);

  if (COMPACT) {
    // Two tight lines: swell, then wind.
    display.setTextSize(1);
    display.setCursor(0, Y_BIG);
    display.printf("%.1fm %.0fs %s", v.swellHeight, v.swellPeriod, v.swellDir);
    display.setCursor(0, Y_WIND);
    display.printf("W %.0fmph %s  %d%%",
                   v.windSpeed, v.windDir, (int)(v.rating * 100));
  } else {
    // Swell height large, period + direction beside it
    display.setTextSize(2);
    display.setCursor(0, Y_BIG);
    display.printf("%.1fm", v.swellHeight);
    display.setTextSize(1);
    display.setCursor(X_DET, Y_DET);
    display.printf("%.0fs %s", v.swellPeriod, v.swellDir);

    display.setCursor(0, Y_WIND);
    display.printf("WND %.0fmph %s", v.windSpeed, v.windDir);

    // Proportional rating bar — the stand-in for green/red
    int16_t barW = (int16_t)(v.rating * SCREEN_WIDTH);
    if (barW < 2) barW = 2;
    if (barW > SCREEN_WIDTH) barW = SCREEN_WIDTH;
    display.drawRect(0, Y_BAR, SCREEN_WIDTH, BAR_H, SSD1306_WHITE);
    display.fillRect(0, Y_BAR, barW, BAR_H, SSD1306_WHITE);
  }

  display.display();
}

static void drawPage() {
  if (haveData && blockCount > 0) {
    if (page >= blockCount) page = 0;
    drawBlock(blocks[page]);
  } else {
    showStatus("No forecast data", lastError[0] ? lastError : "retrying...");
  }
}

// ─── MAIN ────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  Wire.begin(PIN_SDA, PIN_SCL);

  if (!display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR)) {
    Serial.println("SSD1306 not found — check wiring / try address 0x3D");
    while (true) delay(1000);
  }
  showStatus("SURFMOD", "booting...");

  haveData = pollForecast();
  if (!haveData) Serial.printf("poll failed: %s\n", lastError);

  lastPoll = millis();
  lastPage = millis();
  page     = 0;
  drawPage();
}

void loop() {
  uint32_t now = millis();

  if (now - lastPoll >= (haveData ? REFRESH_MS : RETRY_MS)) {
    showStatus("Updating...", spotName);
    haveData = pollForecast();
    if (!haveData) Serial.printf("poll failed: %s\n", lastError);
    lastPoll = millis();
    page     = 0;                  // restart the cycle at NOW after a refresh
    lastPage = millis();
    drawPage();
  }

  if (now - lastPage >= PAGE_MS) {
    page     = (blockCount > 0) ? (page + 1) % blockCount : 0;
    lastPage = now;
    drawPage();
  }

  delay(50);
}
