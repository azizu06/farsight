// Original Farsight firmware. ELEGOO V4 pin assignments documented in README.
// No vendor control code or stock JSON protocol is used.
#include <Arduino.h>
#include <Servo.h>
#include <stdlib.h>
#include <string.h>

const uint8_t LEFT_PWM = 6, RIGHT_PWM = 5, LEFT_DIR = 8, RIGHT_DIR = 7;
const uint8_t STANDBY = 3, PAN_PIN = 10, TRIGGER = 13, ECHO = 12, STOP_BUTTON = 2;
const unsigned long MOTOR_WATCHDOG_MS = 300;
const unsigned long RANGE_PERIOD_MS = 100;
Servo panServo;
char line[48];
uint8_t lineLength = 0;
bool discardLine = false;
unsigned long partialAt = 0, lastMotorAt = 0, rangeAt = 0, panChangedAt = 0;
int leftMotor = 0, rightMotor = 0, panAngle = 90, rangeCm = -1;
bool motorLease = false;

void setMotors(int left, int right) {
  leftMotor = left;
  rightMotor = right;
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

bool forwardBlocked() {
  return rangeCm < 25 || millis() - rangeAt > 250 || abs(panAngle - 90) > 10
      || millis() - panChangedAt < 200;
}

void checkedMotors(int left, int right) {
  if (digitalRead(STOP_BUTTON) == LOW) {
    stopMotors();
    return;
  }
  if (left + right > 0 && forwardBlocked()) {
    // Remove only forward translation; turning and reverse stay available.
    const int translation = (left + right + 1) / 2;
    left -= translation;
    right -= translation;
  }
  setMotors(left, right);
}

void measureRange() {
  digitalWrite(TRIGGER, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIGGER, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIGGER, LOW);
  // Bounded at 12 ms; unlike the default pulseIn timeout this cannot stall
  // the 300 ms motor watchdog for a second when the echo cable is absent.
  const unsigned long pulse = pulseIn(ECHO, HIGH, 12000UL);
  rangeCm = pulse ? (int)(pulse / 58UL) : -1;
  rangeAt = millis();
  if (leftMotor + rightMotor > 0 && forwardBlocked()) {
    checkedMotors(leftMotor, rightMotor);
  }
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
      panChangedAt = millis();
      rangeCm = -1;
      panServo.write(panAngle);
      if (leftMotor + rightMotor > 0) checkedMotors(leftMotor, rightMotor);
    }
    Serial.println(F("OK"));
  } else if (text[0] == 'R' && finished(args)) {
    Serial.print(F("R "));
    Serial.println(rangeCm);
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
  pinMode(TRIGGER, OUTPUT);
  pinMode(ECHO, INPUT);
  pinMode(STOP_BUTTON, INPUT_PULLUP);
  stopMotors();
  panServo.attach(PAN_PIN);
  panServo.write(90);
  Serial.begin(115200);
}

void loop() {
  // Check before potentially blocking sensor IO and after every received byte.
  if (motorLease && millis() - lastMotorAt >= MOTOR_WATCHDOG_MS) stopMotors();
  if (digitalRead(STOP_BUTTON) == LOW) stopMotors();
  if (millis() - rangeAt >= RANGE_PERIOD_MS) measureRange();
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
