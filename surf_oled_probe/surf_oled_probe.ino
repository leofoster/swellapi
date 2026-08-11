/*
 * surf_oled_probe.ino — identify an unknown I2C OLED before wiring up the real
 * sketch. Upload, open Serial Monitor at 115200, and watch the panel.
 *
 * It prints every I2C address found, then tries to drive the panel as a
 * 128x64 SSD1306 and draws a ruler pattern. Read the result off the glass:
 *
 *   - Full test pattern, "64" row visible near the bottom -> 128x64. Use the
 *     main sketch as-is.
 *   - Pattern only fills the top quarter, or "32" is the last row you can see
 *     -> 128x32. Set SCREEN_HEIGHT to 32 in the main sketch.
 *   - Nothing at all, but Serial found a device -> not an SSD1306. It may be
 *     an SSD1327 (1.5in, 128x128 grayscale) or SH1106 (very common clone,
 *     needs the SH1106 library instead). Tell me the address and the physical
 *     size and I will adapt the sketch.
 */

#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

#define PIN_SDA 21
#define PIN_SCL 22

Adafruit_SSD1306 display(128, 64, &Wire, -1);

void setup() {
  Serial.begin(115200);
  delay(500);
  Wire.begin(PIN_SDA, PIN_SCL);

  Serial.println("\n--- I2C scan ---");
  uint8_t found = 0, addr = 0;
  for (uint8_t a = 1; a < 127; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) {
      Serial.printf("device at 0x%02X\n", a);
      if (!found) addr = a;
      found++;
    }
  }
  if (!found) {
    Serial.println("NO I2C DEVICES. Check SDA/SCL are not swapped and that");
    Serial.println("VCC is 3V3. Nothing else below will work.");
    return;
  }
  Serial.printf("%u device(s). Trying 0x%02X as SSD1306 128x64...\n", found, addr);

  if (!display.begin(SSD1306_SWITCHCAPVCC, addr)) {
    Serial.println("SSD1306 init FAILED — likely SH1106 or SSD1327, not SSD1306.");
    return;
  }
  Serial.println("SSD1306 init OK. Read the ruler off the screen.");

  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(1);

  // Row markers every 8px, labelled with their y coordinate.
  for (int16_t y = 0; y < 64; y += 8) {
    display.drawFastHLine(0, y, 8, SSD1306_WHITE);
    display.setCursor(12, y);
    display.print(y);
  }
  // Right edge marker: if you can see this, the panel really is 128 wide.
  display.drawFastVLine(127, 0, 64, SSD1306_WHITE);
  display.setCursor(60, 28);
  display.print("128 wide");
  display.setCursor(60, 44);
  display.print("64 tall?");
  display.display();
}

void loop() { delay(1000); }
