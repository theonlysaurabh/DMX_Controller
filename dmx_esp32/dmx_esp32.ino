// dmx_music_light.ino  (RGB + master version)
// Laptop sends 5-byte packets over USB serial: [0xFF, R, G, B, MASTER]
// Each value is 0-254. They are written to the DMX channels below.
//
// Wiring: ESP32 GPIO17 -> RS-485 DI ; DE+RE tied to the module's supply ;
//         A -> XLR pin 3 ; B -> XLR pin 2 ; GND -> XLR pin 1 and ESP32 GND.
//
// NOTE: save this in a folder named dmx_music_light (Arduino IDE requirement).

#include <Arduino.h>

#define DMX_TX      17
#define NUM_CH      32

#define CH_R        1
#define CH_G        2
#define CH_B        3
#define CH_MASTER   7      // master dimmer

uint8_t dmx[NUM_CH + 1];   // dmx[0] = start code, dmx[1..] = channels
HardwareSerial &DMX = Serial2;

void sendDMX() {
  DMX.updateBaudRate(90000);    // break: 0x00 at slow baud (~100 us low)
  DMX.write((uint8_t)0);
  DMX.flush();
  DMX.updateBaudRate(250000);   // data at DMX speed
  DMX.write(dmx, NUM_CH + 1);
  DMX.flush();
}

void setup() {
  Serial.begin(115200);
  DMX.begin(250000, SERIAL_8N2, -1, DMX_TX);
  memset(dmx, 0, sizeof(dmx));
}

void loop() {
  static int idx = -1;          // -1 = waiting for header
  static uint8_t buf[4];
  static unsigned long last = 0;

  while (Serial.available()) {
    uint8_t b = Serial.read();
    if (b == 0xFF) {            // header: start of a new packet
      idx = 0;
    } else if (idx >= 0) {
      buf[idx++] = b;
      if (idx == 4) {
        dmx[CH_R] = buf[0];
        dmx[CH_G] = buf[1];
        dmx[CH_B] = buf[2];
        dmx[CH_MASTER] = buf[3];
        idx = -1;
      }
    }
  }

  if (millis() - last >= 23) {  // ~40 DMX frames per second
    sendDMX();
    last = millis();
  }
}
