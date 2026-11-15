// Copy this file to secrets.h and fill in your values.
// secrets.h is gitignored so your WiFi password never reaches the repo.
#pragma once

#define WIFI_SSID  "your-wifi-name"     // 2.4 GHz only: the ESP32 cannot see 5 GHz
#define WIFI_PASS  "your-wifi-password"

// Base URL of the forecast data. Scheme optional, path allowed, no trailing slash needed.
//   GitHub Pages (recommended): "https://<github-user>.github.io/swellapi"
//   Live API on Render:         "https://<your-service>.onrender.com"
//   Local dev on your network:  "http://192.168.1.20:8000"
#define API_URL    "https://<github-user>.github.io/swellapi"

// "bermuda" or "newquay"
#define ISLAND_KEY "bermuda"
