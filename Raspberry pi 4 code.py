import RPi.GPIO as GPIO
import time
import json
import math
import Adafruit_DHT
from Adafruit_DHT import Raspberry_Pi_2 as dht_driver
import smbus2
import paho.mqtt.client as mqtt
from RPLCD.i2c import CharLCD

# ==========================================
# 1. HiveMQ Cloud
# ==========================================
MQTT_HOST = ".s1.eu.hivemq.cloud"
MQTT_PORT = 8883

MQTT_USERNAME = "User_name"
MQTT_PASSWORD = "Password"

CLIENT_ID = "amaal-smart-helmet-pi4"

TELEMETRY_TOPIC = "so-m8k2/helmet/telemetry"
ALERTS_TOPIC = "so-m8k2/helmet/alerts"

# ==========================================
# 2. GPIO Pins (BCM numbering)
# ==========================================
IR_PIN = 17
SOUND_PIN = 22
MQ2_PIN = 21

SILENCE_BUTTON_PIN = 24
LED_BUTTON_PIN = 16

BUZZER_PIN = 18
LED_PIN = 25

# ==========================================
# 3. Sensor settings
# ==========================================
TEMP_LIMIT = 45.0

FALL_DELTA_G = 1.2
FALL_COOLDOWN = 3.0

SENSOR_INTERVAL = 0.1
TELEMETRY_INTERVAL = 2.0
DHT_INTERVAL = 2.0
BUTTON_DEBOUNCE = 0.05

IR_ACTIVE_LOW = True
SOUND_ACTIVE_LOW = True

# Your tested gas sensor logic:
# HIGH = No Gas detected
# LOW  = Gas detected
MQ2_ACTIVE_LOW = True

BUZZER_ACTIVE_LOW = False

# MPU6050 switch: keep False while the sensor is not working,
# set True once it shows up in "sudo i2cdetect -y 1" (0x68).
MPU_ENABLED = False

# ==========================================
# 4. GPIO initialization
# ==========================================
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

GPIO.setup(IR_PIN, GPIO.IN)
GPIO.setup(SOUND_PIN, GPIO.IN)

# MQ2 DO is connected through ONE diode (1N4148 or 1N4007):
#   stripe (cathode) toward the MQ2 DO pin, other end (anode) to MQ2_PIN.
# The diode blocks the 5 V of the MQ2 module, so the Raspberry Pi internal
# pull-up must be enabled: no gas -> pin reads HIGH, gas -> the module pulls it LOW.
GPIO.setup(MQ2_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

GPIO.setup(
    SILENCE_BUTTON_PIN,
    GPIO.IN,
    pull_up_down=GPIO.PUD_UP
)

GPIO.setup(
    LED_BUTTON_PIN,
    GPIO.IN,
    pull_up_down=GPIO.PUD_UP
)

GPIO.setup(BUZZER_PIN, GPIO.OUT, initial=GPIO.LOW)
GPIO.setup(LED_PIN, GPIO.OUT, initial=GPIO.LOW)

# ==========================================
# 5. LCD I2C initialization
# ==========================================
LCD_ADDRESS = 0x27  # Change to 0x3F if detected

lcd = CharLCD(
    "PCF8574",
    LCD_ADDRESS,
    port=1,
    cols=16,
    rows=2
)

def update_lcd(line1, line2=""):
    # Keep each line within 16 characters
    lcd.cursor_pos = (0, 0)
    lcd.write_string(str(line1)[:16].ljust(16))

    lcd.cursor_pos = (1, 0)
    lcd.write_string(str(line2)[:16].ljust(16))


def show_helmet_off():
    update_lcd("HELMET: OFF", "SYSTEM: STOPPED")


# ==========================================
# 6. DHT11 initialization
# ==========================================
DHT_SENSOR = Adafruit_DHT.DHT11
DHT_PIN = 4               # GPIO4 (BCM) - same as your working test
DHT_MAX_FAILS = 5         # failed reads in a row before dropping the old value

dht_fail_count = 0


# ==========================================
# 7. MPU6050 initialization
# ==========================================
MPU_ADDRESS = 0x68
MPU_ACCEL_REGISTER = 0x3B

i2c_bus = smbus2.SMBus(1)

if MPU_ENABLED:
    try:
        i2c_bus.write_byte_data(
            MPU_ADDRESS,
            0x6B,
            0x00
        )
    except OSError as error:
        # Do not crash the whole helmet because of the MPU
        print("MPU6050 not responding, fall detection disabled:", error)
        MPU_ENABLED = False
else:
    print("MPU6050 disabled (MPU_ENABLED = False)")

# ==========================================
# 8. MQTT initialization
# ==========================================
mqtt_connected = False

def on_connect(client, userdata, flags, reason_code, properties):
    global mqtt_connected

    if reason_code == 0:
        mqtt_connected = True
        print("MQTT connected")
    else:
        mqtt_connected = False
        print("MQTT connection failed:", reason_code)


def on_disconnect(
    client, userdata, disconnect_flags, reason_code, properties
):
    global mqtt_connected
    mqtt_connected = False
    print("MQTT disconnected")


client = mqtt.Client(
    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    client_id=CLIENT_ID
)

client.username_pw_set(
    MQTT_USERNAME,
    MQTT_PASSWORD
)

client.tls_set()
client.on_connect = on_connect
client.on_disconnect = on_disconnect

client.connect_async(
    MQTT_HOST,
    MQTT_PORT,
    keepalive=60
)

client.loop_start()


def publish_json(topic, data):
    if mqtt_connected:
        client.publish(
            topic,
            json.dumps(data),
            qos=0
        )


def send_alert(alert_type, message,
               temperature=None, acceleration=None):

    alert = {
        "type": alert_type,
        "message": message,
        "time": time.strftime("%Y-%m-%d %H:%M:%S")
    }

    if temperature is not None:
        alert["temp"] = round(temperature, 2)

    if acceleration is not None:
        alert["accel"] = round(acceleration, 2)

    publish_json(ALERTS_TOPIC, alert)
    print("ALERT:", message)


# ==========================================
# 9. Helper functions
# ==========================================
def sensor_active(pin, active_low):
    value = GPIO.input(pin)

    if active_low:
        return value == GPIO.LOW

    return value == GPIO.HIGH


def read_mpu_acceleration():
    # MPU disabled -> no reading, the rest of the code skips fall detection
    if not MPU_ENABLED:
        return None

    data = i2c_bus.read_i2c_block_data(
        MPU_ADDRESS,
        MPU_ACCEL_REGISTER,
        6
    )

    def signed_value(high, low):
        value = (high << 8) | low

        if value >= 32768:
            value -= 65536

        return value

    ax = signed_value(data[0], data[1]) / 16384.0
    ay = signed_value(data[2], data[3]) / 16384.0
    az = signed_value(data[4], data[5]) / 16384.0

    return math.sqrt(ax * ax + ay * ay + az * az)


def set_buzzer(enabled):
    if BUZZER_ACTIVE_LOW:
        GPIO.output(
            BUZZER_PIN,
            GPIO.LOW if enabled else GPIO.HIGH
        )
    else:
        GPIO.output(
            BUZZER_PIN,
            GPIO.HIGH if enabled else GPIO.LOW
        )


# ==========================================
# 10. Button debounce
# ==========================================
class DebouncedButton:
    def __init__(self, pin):
        self.pin = pin
        self.last_raw = GPIO.input(pin)
        self.stable_state = self.last_raw
        self.last_change = time.monotonic()

    def pressed(self):
        raw = GPIO.input(self.pin)
        now = time.monotonic()

        if raw != self.last_raw:
            self.last_raw = raw
            self.last_change = now

        if (
            raw != self.stable_state
            and now - self.last_change >= BUTTON_DEBOUNCE
        ):
            self.stable_state = raw

            if self.stable_state == GPIO.LOW:
                return True

        return False


silence_button = DebouncedButton(SILENCE_BUTTON_PIN)
led_button = DebouncedButton(LED_BUTTON_PIN)

# ==========================================
# 11. Runtime variables
# ==========================================
led_state = False
silenced = False
fall_latched = False

temperature = None
humidity = None
acceleration = None

last_dht_read = 0.0
last_telemetry = 0.0
last_fall_time = 0.0

previous_magnitude = None
previous_fire = False
previous_gas = False

last_status = None
last_lcd_update = 0.0

# ==========================================
# 12. Main program
# ==========================================
print("Smart Helmet started")
show_helmet_off()

try:
    while True:
        now = time.monotonic()

        # Independent LED button
        if led_button.pressed():
            led_state = not led_state

            GPIO.output(
                LED_PIN,
                GPIO.HIGH if led_state else GPIO.LOW
            )

            print("LED:", "ON" if led_state else "OFF")

        # ----------------------------------
        # Detect helmet worn / removed
        # ----------------------------------
        helmet_worn = sensor_active(
            IR_PIN,
            IR_ACTIVE_LOW
        )

        if not helmet_worn:
            # Stop the active monitoring functions
            # while keeping the IR check running.
            temperature = None
            humidity = None
            acceleration = None
            previous_magnitude = None

            fall_latched = False
            silenced = False
            previous_fire = False
            previous_gas = False

            set_buzzer(False)

            # Silence button has no active alarm to mute here.
            # LED button remains independent.

            if (
                last_status != "NOT WORN"
                or now - last_lcd_update >= 1.0
            ):
                show_helmet_off()
                last_lcd_update = now

            last_status = "NOT WORN"

            if now - last_telemetry >= TELEMETRY_INTERVAL:
                publish_json(
                    TELEMETRY_TOPIC,
                    {
                        "worn": 0,
                        "status": "NOT WORN",
                        "noise": 0,
                        "gas": 0
                    }
                )

                last_telemetry = now

            time.sleep(SENSOR_INTERVAL)
            continue

        # ----------------------------------
        # Helmet is worn: system active
        # ----------------------------------
        if last_status == "NOT WORN":
            print("Helmet worn - system active")

            temperature = None
            humidity = None
            acceleration = None
            previous_magnitude = None

            last_dht_read = 0.0
            dht_fail_count = 0
            previous_fire = False
            previous_gas = False
            fall_latched = False
            silenced = False

        # Read digital sensors
        sound_active = sensor_active(
            SOUND_PIN,
            SOUND_ACTIVE_LOW
        )

        # Same active-LOW logic as your test code
        gas_active = sensor_active(
            MQ2_PIN,
            MQ2_ACTIVE_LOW
        )

        # ----------------------------------
        # DHT11 temperature and humidity
        # ----------------------------------
        if now - last_dht_read >= DHT_INTERVAL:
            # Same call as your working test code
            # Call the Pi 2/3/4 driver directly (skips platform detection)
            try:
                new_humidity, new_temp = dht_driver.read(
                    DHT_SENSOR,
                    DHT_PIN
                )
            except (RuntimeError, OSError) as error:
                print("DHT11 driver error:", error)
                new_humidity, new_temp = None, None

            if new_temp is not None and new_humidity is not None:
                temperature = float(new_temp)
                humidity = float(new_humidity)
                dht_fail_count = 0
            else:
                dht_fail_count += 1
                print("DHT11 read failed:", dht_fail_count)

                if dht_fail_count >= DHT_MAX_FAILS:
                    # Do not keep using an old value if the sensor is dead
                    temperature = None
                    humidity = None

            # Take the time AFTER the read (it can take a moment)
            last_dht_read = time.monotonic()

        fire_active = (
            temperature is not None
            and temperature > TEMP_LIMIT
        )

        # ----------------------------------
        # MPU6050 fall heuristic
        # (skipped automatically when MPU_ENABLED = False)
        # ----------------------------------
        try:
            acceleration = read_mpu_acceleration()

            if acceleration is not None:
                if previous_magnitude is not None:
                    delta_g = abs(
                        acceleration - previous_magnitude
                    )

                    if (
                        not fall_latched
                        and delta_g >= FALL_DELTA_G
                        and now - last_fall_time >= FALL_COOLDOWN
                    ):
                        fall_latched = True
                        last_fall_time = now

                        send_alert(
                            "fall",
                            "Fall detected",
                            temperature=temperature,
                            acceleration=delta_g
                        )

                previous_magnitude = acceleration

        except OSError as error:
            print("MPU6050 read error:", error)

        # ----------------------------------
        # Fire alert: once per event
        # ----------------------------------
        if fire_active and not previous_fire:
            send_alert(
                "fire",
                "Fire detected",
                temperature=temperature,
                acceleration=acceleration
            )

        previous_fire = fire_active

        # ----------------------------------
        # Gas alert: once per event
        # ----------------------------------
        if gas_active and not previous_gas:
            send_alert(
                "gas",
                "Gas leakage detected",
                temperature=temperature,
                acceleration=acceleration
            )

        previous_gas = gas_active

        # ----------------------------------
        # Choose status and LCD message
        # ----------------------------------
        if fire_active:
            status = "FIRE"
            lcd_line1 = "FIRE ALERT"
            lcd_line2 = (
                f"Temp: {temperature:.1f} C"
            )

        elif gas_active:
            status = "GAS"
            lcd_line1 = "GAS DETECTED"
            lcd_line2 = (
                f"Temp: {temperature:.1f} C"
                if temperature is not None
                else "Check gas sensor"
            )

        elif fall_latched:
            status = "FALL"
            lcd_line1 = "FALL DETECTED"
            lcd_line2 = (
                f"Accel: {acceleration:.1f} g"
                if acceleration is not None
                else "Check worker"
            )

        elif sound_active:
            status = "NOISE"
            lcd_line1 = "LOUD NOISE"
            lcd_line2 = "Noise detected"

        else:
            status = "SAFE"
            lcd_line1 = "HELMET: ON"

            if temperature is None:
                lcd_line2 = "System: ACTIVE"
            else:
                lcd_line2 = f"Temp: {temperature:.1f} C"

        # ----------------------------------
        # Alarm and silence button
        # ----------------------------------
        alarm_active = (
            fire_active
            or gas_active
            or fall_latched
            or sound_active
        )

        if silence_button.pressed():
            silenced = True

            # Acknowledge the current latched fall.
            fall_latched = False

            print("Alarm silenced")

            # Recalculate fall status after acknowledgement.
            if not fire_active and not gas_active:
                if sound_active:
                    status = "NOISE"
                    lcd_line1 = "LOUD NOISE"
                    lcd_line2 = "Noise detected"
                else:
                    status = "SAFE"
                    lcd_line1 = "HELMET: ON"
                    lcd_line2 = (
                        f"Temp: {temperature:.1f} C"
                        if temperature is not None
                        else "System: ACTIVE"
                    )

            alarm_active = (
                fire_active
                or gas_active
                or fall_latched
                or sound_active
            )

        if not alarm_active:
            silenced = False

        set_buzzer(
            alarm_active and not silenced
        )

        # ----------------------------------
        # Update LCD
        # ----------------------------------
        if (
            status != last_status
            or now - last_lcd_update >= 1.0
        ):
            update_lcd(lcd_line1, lcd_line2)
            last_lcd_update = now

        # ----------------------------------
        # Publish telemetry for existing flow
        # ----------------------------------
        if now - last_telemetry >= TELEMETRY_INTERVAL:
            telemetry = {
                "worn": 1,
                "status": status,
                "noise": 100 if sound_active else 0,
                "gas": 100 if gas_active else 0
            }

            if temperature is not None:
                telemetry["temp"] = round(
                    temperature, 2
                )

            if humidity is not None:
                telemetry["humidity"] = round(
                    humidity, 2
                )

            if acceleration is not None:
                telemetry["accel"] = round(
                    acceleration, 2
                )

            publish_json(
                TELEMETRY_TOPIC,
                telemetry
            )

            print(telemetry)
            last_telemetry = now

        last_status = status
        time.sleep(SENSOR_INTERVAL)

except KeyboardInterrupt:
    print("\nSmart Helmet stopped")

finally:
    try:
        set_buzzer(False)
        GPIO.output(LED_PIN, GPIO.LOW)
    except Exception:
        pass

    try:
        show_helmet_off()
        lcd.close()
    except Exception:
        pass

    try:
        i2c_bus.close()
    except Exception:
        pass

    try:
        client.loop_stop()
        client.disconnect()
    except Exception:
        pass

    GPIO.cleanup()
    print("GPIO cleaned up")