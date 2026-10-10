// Original Farsight firmware. ELEGOO V4 pin assignments documented in README.
// No vendor control code or stock JSON protocol is used.
#include <Arduino.h>
#include <Servo.h>
#include <stdlib.h>
#include <string.h>

const uint8_t LEFT_PWM = 6, RIGHT_PWM = 5, LEFT_DIR = 8, RIGHT_DIR = 7;
const uint8_t STANDBY = 3, PAN_PIN = 10, STOP_BUTTON = 2;
const unsigned long MOTOR_WATCHDOG_MS = 300;
// No ultrasonic sensor or pan servo is mounted: obstacle stopping is the
// Jetson/iPhone path's job. R always reports -1 and P only moves an unused pin.
Servo panServo;
char line[48];
uint8_t lineLength = 0;
bool discardLine = false;
unsigned long partialAt = 0, lastMotorAt = 0;
int panAngle = 90;
bool motorLease = false;

void setMotors(int left, int right) {
  // Disable the bridge while changing direction to avoid a powered reversal.
  digitalWrite(STANDBY, LOW);
  digitalWrite(LEFT_DIR, left >= 0 ? HIGH : LOW);
  digitalWrite(RIGHT_DIR, right >= 0 ? HIGH : LOW);
  analogWrite(LEFT_PWM, abs(left));
  analogWrite(RIGHT_PWM, abs(right));
  if (left || right) digitalWrite(STANDBY, HIGH);
}

void stopMotors() {
  setMotors(0, 0);
  motorLease = false;
}

void checkedMotors(int left, int right) {
  if (digitalRead(STOP_BUTTON) == LOW) {
    stopMotors();
    return;
  }
  setMotors(left, right);
}

bool integer(char *&cursor, long &value) {
  while (*cursor == ' ') ++cursor;
  if (!*cursor) return false;
  char *end;
  value = strtol(cursor, &end, 10);
  if (end == cursor) return false;
  if (*end && *end != ' ' && *end != '\r') return false;
  cursor = end;
  return true;
}

bool finished(char *cursor) {
  while (*cursor == ' ' || *cursor == '\r') ++cursor;
  return !*cursor;
}

void command(char *text) {
  long a, b;
  char *args = text + 1;
  if (text[0] == 'M' && text[1] == ' ' && integer(args, a) && integer(args, b)
      && finished(args) && a >= -255 && a <= 255 && b >= -255 && b <= 255) {
    lastMotorAt = millis();
    motorLease = true;
    checkedMotors((int)a, (int)b);
    Serial.println(F("OK"));
  } else if (text[0] == 'P' && text[1] == ' ' && integer(args, a) && finished(args)
             && a >= 10 && a <= 170) {
    if (panAngle != a) {
      panAngle = (int)a;
      panServo.write(panAngle);
    }
    Serial.println(F("OK"));
  } else if (text[0] == 'R' && finished(args)) {
    Serial.println(F("R -1"));
  } else {
    stopMotors();
    Serial.println(F("ERR command"));
  }
}

void setup() {
  pinMode(STANDBY, OUTPUT);
  digitalWrite(STANDBY, LOW);
  pinMode(LEFT_PWM, OUTPUT);
  pinMode(RIGHT_PWM, OUTPUT);
  pinMode(LEFT_DIR, OUTPUT);
  pinMode(RIGHT_DIR, OUTPUT);
  pinMode(STOP_BUTTON, INPUT_PULLUP);
  stopMotors();
  panServo.attach(PAN_PIN);
  panServo.write(90);
  Serial.begin(115200);
}

void loop() {
  // Check every loop and after every received byte.
  if (motorLease && millis() - lastMotorAt >= MOTOR_WATCHDOG_MS) stopMotors();
  if (digitalRead(STOP_BUTTON) == LOW) stopMotors();
  if (lineLength && millis() - partialAt > 50) {
    stopMotors();
    lineLength = 0;
    discardLine = true;
  }
  // A bounded byte budget prevents a serial flood starving the watchdog.
  uint8_t budget = 64;
  while (Serial.available() && budget--) {
    if (motorLease && millis() - lastMotorAt >= MOTOR_WATCHDOG_MS) stopMotors();
    const char c = (char)Serial.read();
    if (c == '\n') {
      if (!discardLine && lineLength) {
        line[lineLength] = '\0';
        command(line);
      }
      lineLength = 0;
      discardLine = false;
    } else if (!discardLine) {
      if (lineLength >= sizeof(line) - 1 || c == '\0') {
        stopMotors();
        lineLength = 0;
        discardLine = true;
        Serial.println(F("ERR line"));
      } else {
        line[lineLength++] = c;
        partialAt = millis();
      }
    }
  }
}
