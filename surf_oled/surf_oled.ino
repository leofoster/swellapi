/*
 * surf_oled.ino (v2): ESP32 + I2C SSD1306 OLED surf display
 *
 * Reads a small JSON forecast (3-hour blocks) from:
 *     <API_URL>/esp/<ISLAND_KEY>?blocks=16
 * which is either the static GitHub Pages site (recommended) or the live API.
 * Payload format: see EspResponse in api.py. Layout and logic are mirrored
 * one-to-one in simulator/index.html, so check changes there first.
 *
 * What v2 fixes over v1:
 *   - A failed refresh no longer wipes good data. The last forecast stays up
 *     with a "!" in the corner until a refresh succeeds.
 *   - Time comes from NTP. "NOW" / "+6h" is computed on the device from each
 *     block's start time (e), and blocks already in the past are skipped, so
 *     an hour-old static file still shows correctly.
 *   - API_URL may include a path (needed for GitHub Pages) and http:// for
 *     a dev server on your LAN.
 *   - Failed polls back off 1, 2, 4, 8, 15 min instead of hammering.
 *
 * Setup:
 *   1. Copy secrets_example.h to secrets.h and fill it in.
 *   2. Library Manager: ArduinoJson (v7.x), Adafruit SSD1306, Adafruit GFX.
 *   3. Board: "ESP32 Dev Module". Wiring: SDA->21, SCL->22, VCC->3V3, GND->GND.
 *   4. Unsure of the panel? Run surf_oled_probe first.
 */

#include <WiFi.h>
#include <WiFiClient.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <time.h>

#include "secrets.h"      // WIFI_SSID, WIFI_PASS, API_URL, ISLAND_KEY

// ─── CONFIG ──────────────────────────────────────────────────────────────
#define MAX_BLOCKS    16                 // 16 x 3h = 48h held in memory

#define SCREEN_WIDTH  128
#define SCREEN_HEIGHT 64                 // 32 if the probe says 128x32
#define OLED_ADDR     0x3C               // some modules are 0x3D
#define PIN_SDA       21
#define PIN_SCL       22

const uint32_t REFRESH_MS   = 15UL * 60UL * 1000UL;   // normal poll interval
const uint32_t BACKOFF_MS[] = { 60000UL, 120000UL, 240000UL, 480000UL, 900000UL };
const uint32_t PAGE_MS      = 4000;                    // time on each block
const uint32_t STALE_S      = 3UL * 3600UL;            // data older than this gets "!"
const uint32_t BLOCK_S      = 3UL * 3600UL;

// Layout. Rows 0-15 are the yellow band on two-colour panels.
#if SCREEN_HEIGHT >= 64
  const int16_t Y_QUALITY = 0,  Y_TAG = 0,  Y_TIME = 8,
                Y_RULE    = 17, Y_BIG = 21, Y_DET  = 25,
                Y_WIND    = 40, Y_BAR = 54, BAR_H  = 8;
  const bool    COMPACT   = false;
#else
  const int16_t Y_QUALITY = 0,  Y_TAG = 0,  Y_TIME = 8,
                Y_RULE    = 15, Y_BIG = 16, Y_DET  = 16,   // two 8px lines: 16-23, 24-31
                Y_WIND    = 24, Y_BAR = 0,  BAR_H  = 0;
  const bool    COMPACT   = true;
#endif
const int16_t X_RIGHT = 86, X_DET = 56, X_WARN = 122;

Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, -1);

// ─── STATE ───────────────────────────────────────────────────────────────
struct Block {
  char     timeLabel[10];   // "Tue 03", island local time
  char     quality[10];     // FLAT / POOR / FAIR / GOOD / PUMPING
  char     swellDir[6];
  char     windDir[6];
  uint32_t start;           // unix seconds; 0 if the server sent a v1 payload
  int      hoursFromNow;    // v1 fallback when there is no clock
  float    swellHeight, swellPeriod, windSpeed, rating;
};

char     spotName[20]  = "Surf";
Block    blocks[MAX_BLOCKS];
uint8_t  blockCount    = 0;
uint32_t generatedAt   = 0;      // unix seconds of the data we hold
uint32_t lastPoll      = 0;
uint32_t lastPage      = 0;
uint8_t  page          = 0;
uint8_t  failStreak    = 0;
char     lastError[28] = "";

// ─── HELPERS ─────────────────────────────────────────────────────────────
static void copyStr(char *dst, size_t n, const char *src) {
  if (!src) { dst[0] = '\0'; return; }
  strncpy(dst, src, n - 1);
  dst[n - 1] = '\0';
}

static uint32_t nowUnix() {               // 0 until NTP has synced
  time_t t = time(nullptr);
  return t > 1700000000 ? (uint32_t)t : 0;
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

// ─── WIFI + TIME ─────────────────────────────────────────────────────────
static bool ensureWifi() {
  if (WiFi.status() == WL_CONNECTED) return true;
  if (blockCount == 0) showStatus("Connecting WiFi", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) delay(250);
  if (WiFi.status() != WL_CONNECTED) {
    copyStr(lastError, sizeof(lastError), "wifi failed");
    return false;
  }
  configTime(0, 0, "pool.ntp.org", "time.google.com");   // UTC; labels come from the server
  return true;
}

// ─── FETCH + PARSE ───────────────────────────────────────────────────────
// Split API_URL into scheme / host / path prefix. Tolerates a missing scheme
// and trailing slashes, so "host", "https://host/" and "host/sub/" all work.
static void buildUrl(char *url, size_t n, char *host, size_t hn, bool &https) {
  const char *u = API_URL;
  https = true;
  if (strncmp(u, "https://", 8) == 0)     u += 8;
  else if (strncmp(u, "http://", 7) == 0) { u += 7; https = false; }

  char buf[128];
  copyStr(buf, sizeof(buf), u);
  size_t len = strlen(buf);
  while (len && buf[len - 1] == '/') buf[--len] = '\0';

  char *slash = strchr(buf, '/');
  const char *prefix = "";
  if (slash) { *slash = '\0'; prefix = slash + 1; }
  copyStr(host, hn, buf);

  snprintf(url, n, "%s://%s%s%s/esp/%s?blocks=%d",
           https ? "https" : "http", buf, *prefix ? "/" : "", prefix,
           ISLAND_KEY, MAX_BLOCKS);
}

// Fetch and parse into a scratch buffer; only replace live data on success.
static bool pollForecast() {
  if (!ensureWifi()) return false;

  char url[200], host[96];
  bool https;
  buildUrl(url, sizeof(url), host, sizeof(host), https);
  Serial.printf("GET %s\n", url);

  // Name the failure: a bad host otherwise surfaces as a generic -1.
  IPAddress ip;
  char hostOnly[96];
  copyStr(hostOnly, sizeof(hostOnly), host);
  char *colon = strchr(hostOnly, ':');           // strip :port for DNS
  if (colon) *colon = '\0';
  if (!WiFi.hostByName(hostOnly, ip)) {
    copyStr(lastError, sizeof(lastError), "DNS fail: bad host?");
    return false;
  }

  WiFiClientSecure tls;
  WiFiClient plain;
  tls.setInsecure();                              // public data; no cert pinning
  HTTPClient http;
  http.setConnectTimeout(20000);
  http.setTimeout(60000);                         // tolerates a Render cold start
  http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
  bool ok = https ? http.begin(tls, url) : http.begin(plain, url);
  if (!ok) { copyStr(lastError, sizeof(lastError), "bad URL"); return false; }

  int code = http.GET();
  if (code != HTTP_CODE_OK) {
    snprintf(lastError, sizeof(lastError), "HTTP %d", code);
    Serial.printf("GET failed: %d %s\n", code, HTTPClient::errorToString(code).c_str());
    http.end();
    return false;
  }

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, http.getStream());
  http.end();
  if (err) {
    snprintf(lastError, sizeof(lastError), "json %s", err.c_str());
    return false;
  }

  static Block fresh[MAX_BLOCKS];
  uint8_t n = 0;
  for (JsonObject b : doc["b"].as<JsonArray>()) {
    if (n >= MAX_BLOCKS) break;
    Block &v = fresh[n++];
    copyStr(v.timeLabel, sizeof(v.timeLabel), b["t"]  | "--");
    copyStr(v.quality,   sizeof(v.quality),   b["q"]  | "--");
    copyStr(v.swellDir,  sizeof(v.swellDir),  b["sd"] | "--");
    copyStr(v.windDir,   sizeof(v.windDir),   b["wd"] | "--");
    v.start        = b["e"]  | 0UL;
    v.hoursFromNow = b["hn"] | 0;
    v.swellHeight  = b["h"]  | 0.0f;
    v.swellPeriod  = b["p"]  | 0.0f;
    v.windSpeed    = b["w"]  | 0.0f;
    v.rating       = b["r"]  | 0.0f;
  }
  if (n == 0) { copyStr(lastError, sizeof(lastError), "empty forecast"); return false; }

  memcpy(blocks, fresh, sizeof(Block) * n);
  blockCount  = n;
  generatedAt = doc["gt"] | 0UL;
  copyStr(spotName, sizeof(spotName), doc["s"] | "Surf");
  lastError[0] = '\0';
  Serial.printf("ok: %u blocks for %s\n", n, spotName);
  return true;
}

// ─── WHICH BLOCKS TO SHOW ────────────────────────────────────────────────
// A block is current or future if it has not ended yet. Without a clock
// (NTP not synced, or a v1 payload) everything is shown, as v1 did.
static bool blockVisible(const Block &v, uint32_t now) {
  return now == 0 || v.start == 0 || v.start + BLOCK_S > now;
}

static int nextVisible(int from, uint32_t now) {
  for (int i = 0; i < blockCount; i++) {
    int idx = (from + i) % blockCount;
    if (blockVisible(blocks[idx], now)) return idx;
  }
  return -1;
}

// ─── RENDER ──────────────────────────────────────────────────────────────
static void drawBlock(const Block &v, uint32_t now, bool warn) {
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);

  display.setTextSize(2);
  display.setCursor(0, Y_QUALITY);
  display.print(v.quality);

  display.setTextSize(1);
  display.setCursor(X_RIGHT, Y_TAG);
  int hrs = v.hoursFromNow;
  if (now && v.start) hrs = v.start > now ? (int)((v.start - now + 1799) / 3600) : 0;
  if (hrs <= 0) display.print(" NOW");
  else          display.printf(" +%dh", hrs);
  display.setCursor(X_RIGHT, Y_TIME);
  display.print(v.timeLabel);

  if (warn) { display.setCursor(X_WARN, Y_TAG); display.print("!"); }

  display.drawFastHLine(0, Y_RULE, SCREEN_WIDTH, SSD1306_WHITE);

  if (COMPACT) {
    display.setCursor(0, Y_BIG);
    display.printf("%.1fm %.0fs %s", v.swellHeight, v.swellPeriod, v.swellDir);
    display.setCursor(0, Y_WIND);
    display.printf("W %.0fmph %s  %d%%", v.windSpeed, v.windDir, (int)(v.rating * 100));
  } else {
    display.setTextSize(2);
    display.setCursor(0, Y_BIG);
    display.printf("%.1fm", v.swellHeight);
    display.setTextSize(1);
    display.setCursor(X_DET, Y_DET);
    display.printf("%.0fs %s", v.swellPeriod, v.swellDir);

    display.setCursor(0, Y_WIND);
    display.printf("WND %.0fmph %s", v.windSpeed, v.windDir);

    int16_t barW = (int16_t)(v.rating * SCREEN_WIDTH);
    if (barW < 2) barW = 2;
    if (barW > SCREEN_WIDTH) barW = SCREEN_WIDTH;
    display.drawRect(0, Y_BAR, SCREEN_WIDTH, BAR_H, SSD1306_WHITE);
    display.fillRect(0, Y_BAR, barW, BAR_H, SSD1306_WHITE);
  }
  display.display();
}

static void drawPage() {
  uint32_t now = nowUnix();
  int idx = blockCount ? nextVisible(page, now) : -1;
  if (idx < 0) {
    showStatus(blockCount ? "Forecast expired" : "No forecast data",
               lastError[0] ? lastError : "retrying...");
    return;
  }
  page = idx;
  bool stale = lastError[0] || (now && generatedAt && now - generatedAt > STALE_S);
  drawBlock(blocks[idx], now, stale);
}

static uint32_t pollInterval() {
  if (failStreak == 0) return REFRESH_MS;
  uint8_t i = failStreak - 1;
  if (i > 4) i = 4;
  return BACKOFF_MS[i];
}

// ─── MAIN ────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  Wire.begin(PIN_SDA, PIN_SCL);
  if (!display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR)) {
    Serial.println("SSD1306 not found: check wiring / try address 0x3D");
    while (true) delay(1000);
  }
  showStatus("SURFMOD v2", "booting...");

  failStreak = pollForecast() ? 0 : 1;
  if (failStreak) Serial.printf("poll failed: %s\n", lastError);
  lastPoll = lastPage = millis();
  page = 0;
  drawPage();
}

void loop() {
  uint32_t ms = millis();

  if (ms - lastPoll >= pollInterval()) {
    if (blockCount == 0) showStatus("Updating...", spotName);
    if (pollForecast()) {
      failStreak = 0;
      page = 0;                          // restart at NOW after fresh data
    } else {
      if (failStreak < 255) failStreak++;
      Serial.printf("poll failed (%u in a row): %s\n", failStreak, lastError);
    }
    lastPoll = lastPage = millis();
    drawPage();
  }

  if (ms - lastPage >= PAGE_MS) {
    page = blockCount ? (page + 1) % blockCount : 0;
    lastPage = ms;
    drawPage();
  }
  delay(50);
}
