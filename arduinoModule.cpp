#include <SPI.h>
#include <MFRC522.h>
#include <WiFiNINA.h>

// =====================================================
// USER SETTINGS
// =====================================================
char ssid[] = "YOUR_WIFI_NAME";
char pass[] = "YOUR_WIFI_PASSWORD";

// Your computer's LAN IP address running FastAPI backend
char server[] = "192.168.1.100";
const int serverPort = 8000;

// Unique ID for this seat/check-in module
String seatId = "A12";

// =====================================================
// PIN DEFINITIONS
// =====================================================
// RFID
#define RFID_SS_PIN    2
#define RFID_RST_PIN   3

// LEDs
#define GREEN_LED      4
#define YELLOW_LED     5
#define RED_LED        6

// Checkout button
#define BUTTON_PIN     7

// =====================================================
// OBJECTS
// =====================================================
MFRC522 rfid(RFID_SS_PIN, RFID_RST_PIN);

// =====================================================
// TIMING
// =====================================================
unsigned long lastPollMs = 0;
const unsigned long pollIntervalMs = 3000;   // poll backend every 3 sec

unsigned long lastBlinkMs = 0;
const unsigned long warningBlinkInterval = 300;

// =====================================================
// WIFI STATE
// =====================================================
int wifiStatus = WL_IDLE_STATUS;

// =====================================================
// SEAT STATE
// =====================================================
enum SeatState {
  STATE_OPEN,
  STATE_RESERVED_NO_SHOW,
  STATE_OCCUPIED,
  STATE_OCCUPIED_WARNING
};

SeatState currentState = STATE_OPEN;
bool warningBlinkOn = false;

// =====================================================
// LED HELPERS
// =====================================================
void allLightsOff() {
  digitalWrite(GREEN_LED, LOW);
  digitalWrite(YELLOW_LED, LOW);
  digitalWrite(RED_LED, LOW);
}

void setOpenLight() {
  digitalWrite(GREEN_LED, HIGH);
  digitalWrite(YELLOW_LED, LOW);
  digitalWrite(RED_LED, LOW);
}

void setReservedLight() {
  digitalWrite(GREEN_LED, LOW);
  digitalWrite(YELLOW_LED, HIGH);
  digitalWrite(RED_LED, LOW);
}

void setOccupiedLight() {
  digitalWrite(GREEN_LED, LOW);
  digitalWrite(YELLOW_LED, LOW);
  digitalWrite(RED_LED, HIGH);
}

void flashDeniedRed() {
  // Flash red a few times, then return to strong red
  digitalWrite(GREEN_LED, LOW);
  digitalWrite(YELLOW_LED, LOW);

  for (int i = 0; i < 4; i++) {
    digitalWrite(RED_LED, HIGH);
    delay(200);
    digitalWrite(RED_LED, LOW);
    delay(200);
  }

  digitalWrite(RED_LED, HIGH);
}

void applyState(SeatState s) {
  currentState = s;

  switch (s) {
    case STATE_OPEN:
      setOpenLight();
      break;

    case STATE_RESERVED_NO_SHOW:
      setReservedLight();
      break;

    case STATE_OCCUPIED:
      setOccupiedLight();
      break;

    case STATE_OCCUPIED_WARNING:
      // blinking handled separately in loop
      digitalWrite(GREEN_LED, LOW);
      digitalWrite(YELLOW_LED, LOW);
      digitalWrite(RED_LED, HIGH);
      warningBlinkOn = true;
      lastBlinkMs = millis();
      break;
  }
}

// =====================================================
// BUTTON / RFID HELPERS
// =====================================================
bool isCheckoutMode() {
  // INPUT_PULLUP means pressed = LOW
  return digitalRead(BUTTON_PIN) == LOW;
}

String readUidString() {
  String uid = "";
  for (byte i = 0; i < rfid.uid.size; i++) {
    if (i > 0) uid += "-";
    uid += String(rfid.uid.uidByte[i], DEC);
  }
  return uid;
}

// For MVP only. In a real system the backend should map RFID UID to a real student account.
String fakeUserFromUid(String uid) {
  return "user_" + uid;
}

// =====================================================
// WIFI
// =====================================================
bool connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) {
    return true;
  }

  Serial.print("Connecting to WiFi");

  while (WiFi.status() != WL_CONNECTED) {
    wifiStatus = WiFi.begin(ssid, pass);
    delay(3000);
    Serial.print(".");
  }

  Serial.println();
  Serial.println("Connected to WiFi.");
  Serial.print("Board IP: ");
  Serial.println(WiFi.localIP());

  return true;
}

// =====================================================
// HTTP HELPERS
// =====================================================
String httpPost(String path, String jsonBody) {
  WiFiClient client;
  String response = "";

  if (!client.connect(server, serverPort)) {
    Serial.println("POST connection failed");
    return "";
  }

  client.println("POST " + path + " HTTP/1.1");
  client.println("Host: " + String(server));
  client.println("Content-Type: application/json");
  client.print("Content-Length: ");
  client.println(jsonBody.length());
  client.println("Connection: close");
  client.println();
  client.print(jsonBody);

  unsigned long timeout = millis();
  while (client.connected() && millis() - timeout < 5000) {
    while (client.available()) {
      char c = client.read();
      response += c;
      timeout = millis();
    }
  }

  client.stop();
  return response;
}

String httpGet(String path) {
  WiFiClient client;
  String response = "";

  if (!client.connect(server, serverPort)) {
    Serial.println("GET connection failed");
    return "";
  }

  client.println("GET " + path + " HTTP/1.1");
  client.println("Host: " + String(server));
  client.println("Connection: close");
  client.println();

  unsigned long timeout = millis();
  while (client.connected() && millis() - timeout < 5000) {
    while (client.available()) {
      char c = client.read();
      response += c;
      timeout = millis();
    }
  }

  client.stop();
  return response;
}

String getHttpBody(String rawResponse) {
  int idx = rawResponse.indexOf("\r\n\r\n");
  if (idx == -1) return "";
  return rawResponse.substring(idx + 4);
}

// =====================================================
// BACKEND COMMUNICATION
// =====================================================
void sendTap(String uid, String action) {
  String userId = fakeUserFromUid(uid);

  String body = "{";
  body += "\"seat_id\":\"" + seatId + "\",";
  body += "\"rfid_uid\":\"" + uid + "\",";
  body += "\"user_id\":\"" + userId + "\",";
  body += "\"action\":\"" + action + "\"";
  body += "}";

  Serial.println("Sending tap:");
  Serial.println(body);

  String raw = httpPost("/tap", body);
  String resp = getHttpBody(raw);

  Serial.println("Tap response:");
  Serial.println(resp);

  if (resp.length() == 0) {
    Serial.println("Empty response from backend");
    return;
  }

  // Denied cases
  if (resp.indexOf("\"ok\":false") >= 0) {
    if (resp.indexOf("denied_occupied") >= 0 ||
        resp.indexOf("denied_reserved_for_someone_else") >= 0 ||
        resp.indexOf("not_owner") >= 0) {
      flashDeniedRed();
      applyState(STATE_OCCUPIED);
      return;
    }

    if (resp.indexOf("no_active_session") >= 0) {
      applyState(STATE_OPEN);
      return;
    }

    return;
  }

  // Success cases
  if (resp.indexOf("checked_out") >= 0) {
    applyState(STATE_OPEN);
  } 
  else if (resp.indexOf("checked_in") >= 0 ||
           resp.indexOf("reservation_checked_in") >= 0 ||
           resp.indexOf("already_checked_in") >= 0) {
    applyState(STATE_OCCUPIED);
  }
}

void pollSeatState() {
  String raw = httpGet("/seat/" + seatId);
  String resp = getHttpBody(raw);

  Serial.println("State response:");
  Serial.println(resp);

  if (resp.length() == 0) {
    return;
  }

  if (resp.indexOf("\"state\":\"OPEN\"") >= 0) {
    applyState(STATE_OPEN);
  } 
  else if (resp.indexOf("\"state\":\"RESERVED_NO_SHOW\"") >= 0) {
    applyState(STATE_RESERVED_NO_SHOW);
  } 
  else if (resp.indexOf("\"state\":\"OCCUPIED_WARNING\"") >= 0) {
    applyState(STATE_OCCUPIED_WARNING);
  } 
  else if (resp.indexOf("\"state\":\"OCCUPIED\"") >= 0) {
    applyState(STATE_OCCUPIED);
  }
}

// =====================================================
// RFID HANDLING
// =====================================================
void handleRFIDTap() {
  if (!rfid.PICC_IsNewCardPresent()) return;
  if (!rfid.PICC_ReadCardSerial()) return;

  String uid = readUidString();

  Serial.print("RFID UID: ");
  Serial.println(uid);

  if (isCheckoutMode()) {
    Serial.println("Checkout mode active");
    sendTap(uid, "checkout");
  } else {
    Serial.println("Normal check-in mode");
    sendTap(uid, "checkin");
  }

  rfid.PICC_HaltA();
  rfid.PCD_StopCrypto1();

  delay(1000); // simple debounce / prevent double reads
}

// =====================================================
// WARNING BLINK HANDLING
// =====================================================
void handleWarningBlink() {
  if (currentState != STATE_OCCUPIED_WARNING) return;

  unsigned long now = millis();
  if (now - lastBlinkMs >= warningBlinkInterval) {
    lastBlinkMs = now;
    warningBlinkOn = !warningBlinkOn;

    digitalWrite(GREEN_LED, LOW);
    digitalWrite(YELLOW_LED, LOW);
    digitalWrite(RED_LED, warningBlinkOn ? HIGH : LOW);
  }
}

// =====================================================
// SETUP
// =====================================================
void setup() {
  Serial.begin(115200);
  while (!Serial) {}

  pinMode(GREEN_LED, OUTPUT);
  pinMode(YELLOW_LED, OUTPUT);
  pinMode(RED_LED, OUTPUT);

  pinMode(BUTTON_PIN, INPUT_PULLUP);

  allLightsOff();

  SPI.begin();
  rfid.PCD_Init();

  Serial.println("RFID initialized");

  connectWiFi();

  applyState(STATE_OPEN);

  Serial.println("Seat module started");
}

// =====================================================
// LOOP
// =====================================================
void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
  }

  if (millis() - lastPollMs >= pollIntervalMs) {
    lastPollMs = millis();
    pollSeatState();
  }

  handleRFIDTap();
  handleWarningBlink();
}