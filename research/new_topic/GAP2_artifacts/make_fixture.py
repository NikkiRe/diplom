"""Синтетическое дерево /sys + /proc для проверки sysfs_probe.py без платы.

Имена путей и атрибутов взяты из ABI ядра (Documentation/ABI/testing/
sysfs-bus-iio, sysfs-class-hwmon, sysfs-driver-w1_therm, sysfs-bus-usb,
sysfs-firmware-ofw), из исходников драйверов (bmp280-core.c: каналы BME280 с
PROCESSED+RAW+SCALE; dht11.c: только PROCESSED; v4l2-dev.c: атрибуты name и
index) и из документации Raspberry Pi (оверлеи i2c-sensor, dht11, w1-gpio;
узел /proc/device-tree/hat, который прошивка заполняет из EEPROM HAT).

ВАЖНО: значения датчиков, адреса, серийные номера, VID:PID и строки HAT
выдуманы. Это фикстура для тестов разбора, а не запись с реальной платы.
Запись с реальной платы делается командой `umockdev-record` или
`tar` нужных каталогов /sys (см. README.md).

Запуск: python3 make_fixture.py fixture_root
"""
from __future__ import annotations

import os
import shutil
import sys


def w(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def link(target_abs: str, linkpath: str) -> None:
    """Относительная симлинка, как в настоящем /sys."""
    os.makedirs(os.path.dirname(linkpath), exist_ok=True)
    rel = os.path.relpath(target_abs, os.path.dirname(linkpath))
    if os.path.lexists(linkpath):
        os.remove(linkpath)
    os.symlink(rel, linkpath)


def build(root: str) -> None:
    if os.path.exists(root):
        shutil.rmtree(root)
    S = os.path.join(root, "sys")
    dev = os.path.join(S, "devices")

    # --- I2C-1 + BME280 (dtoverlay=i2c-sensor,bme280), драйвер bmp280 -> IIO
    i2c1 = os.path.join(dev, "platform", "soc", "1f00074000.i2c", "i2c-1")
    w(os.path.join(i2c1, "name"), "Synopsys DesignWare I2C adapter\n")
    cl = os.path.join(i2c1, "1-0076")
    w(os.path.join(cl, "name"), "bme280\n")
    iio0 = os.path.join(cl, "iio:device0")
    w(os.path.join(iio0, "name"), "bme280\n")
    # PROCESSED (_input) и RAW+SCALE одновременно, как в bme280_channels[]
    w(os.path.join(iio0, "in_temp_input"), "22510\n")            # m°C
    w(os.path.join(iio0, "in_temp_raw"), "2251\n")
    w(os.path.join(iio0, "in_temp_scale"), "10\n")
    w(os.path.join(iio0, "in_temp_oversampling_ratio"), "2\n")
    w(os.path.join(iio0, "in_humidityrelative_input"), "41230\n")  # m%
    w(os.path.join(iio0, "in_humidityrelative_raw"), "42220\n")
    w(os.path.join(iio0, "in_humidityrelative_scale"), "0.976562500\n")
    w(os.path.join(iio0, "in_pressure_input"), "101.325000000\n")  # kPa
    w(os.path.join(iio0, "in_pressure_raw"), "25939200\n")
    w(os.path.join(iio0, "in_pressure_scale"), "0.000003906\n")
    link(iio0, os.path.join(S, "bus", "iio", "devices", "iio:device0"))
    link(cl, os.path.join(S, "bus", "i2c", "devices", "1-0076"))
    link(i2c1, os.path.join(S, "bus", "i2c", "devices", "i2c-1"))

    # --- DHT22 по GPIO4 (dtoverlay=dht11,gpiopin=4), драйвер dht11 -> IIO
    dht = os.path.join(dev, "platform", "dht11@4")
    iio1 = os.path.join(dht, "iio:device1")
    w(os.path.join(iio1, "name"), "dht11\n")
    w(os.path.join(iio1, "in_temp_input"), "23100\n")
    w(os.path.join(iio1, "in_humidityrelative_input"), "44800\n")
    link(iio1, os.path.join(S, "bus", "iio", "devices", "iio:device1"))

    # --- Канал АЦП без _input: значение = (raw + offset) * scale
    adc = os.path.join(dev, "platform", "soc", "1f00074000.i2c", "i2c-1", "1-0048")
    w(os.path.join(adc, "name"), "ads1015\n")
    iio2 = os.path.join(adc, "iio:device2")
    w(os.path.join(iio2, "name"), "ads1015\n")
    w(os.path.join(iio2, "in_voltage0_raw"), "1650\n")
    w(os.path.join(iio2, "in_voltage0_scale"), "2.000000000\n")   # mV на отсчёт
    w(os.path.join(iio2, "in_voltage0_offset"), "-25\n")
    link(iio2, os.path.join(S, "bus", "iio", "devices", "iio:device2"))
    link(adc, os.path.join(S, "bus", "i2c", "devices", "1-0048"))

    # --- I2C-клиент без драйвера IIO/hwmon (объявлен через new_device,
    #     драйвер не привязан): ОС знает только адрес и имя.
    raw = os.path.join(i2c1, "1-0040")
    w(os.path.join(raw, "name"), "dummy\n")
    link(raw, os.path.join(S, "bus", "i2c", "devices", "1-0040"))

    # --- hwmon: температура SoC (класс hwmon, единицы m°C)
    tz = os.path.join(dev, "virtual", "thermal", "thermal_zone0")
    hw0 = os.path.join(tz, "hwmon0")
    w(os.path.join(hw0, "name"), "cpu_thermal\n")
    w(os.path.join(hw0, "temp1_input"), "48300\n")
    link(hw0, os.path.join(S, "class", "hwmon", "hwmon0"))

    # --- 1-Wire: DS18B20 (семейство 0x28), dtoverlay=w1-gpio
    m1 = os.path.join(dev, "w1_bus_master1")
    w(os.path.join(m1, "w1_master_slave_count"), "1\n")
    ds = os.path.join(m1, "28-0316a2795cff")
    w(os.path.join(ds, "name"), "28-0316a2795cff\n")
    w(os.path.join(ds, "temperature"), "21437\n")   # 1/1000 °C
    w(os.path.join(ds, "resolution"), "12\n")
    w(os.path.join(ds, "conv_time"), "750\n")       # мс при 12 битах (w1_therm.c)
    link(m1, os.path.join(S, "bus", "w1", "devices", "w1_bus_master1"))
    link(ds, os.path.join(S, "bus", "w1", "devices", "28-0316a2795cff"))

    # --- USB: UVC-камера + USB-UART (CH340)
    usb = os.path.join(dev, "platform", "axi", "1000120000.pcie", "usb1")
    w(os.path.join(usb, "idVendor"), "1d6b\n")      # корневой хаб Linux Foundation
    w(os.path.join(usb, "idProduct"), "0002\n")
    w(os.path.join(usb, "product"), "xHCI Host Controller\n")
    cam = os.path.join(usb, "1-1")
    w(os.path.join(cam, "idVendor"), "046d\n")
    w(os.path.join(cam, "idProduct"), "0825\n")
    w(os.path.join(cam, "product"), "Webcam C270\n")
    w(os.path.join(cam, "manufacturer"), "Logitech\n")
    camif = os.path.join(cam, "1-1:1.0")
    for idx, vn in ((0, "video0"), (1, "video1")):
        vd = os.path.join(camif, "video4linux", vn)
        w(os.path.join(vd, "name"), "UVC Camera (046d:0825)\n")
        w(os.path.join(vd, "index"), f"{idx}\n")
        link(vd, os.path.join(S, "class", "video4linux", vn))
    ser = os.path.join(usb, "1-2")
    w(os.path.join(ser, "idVendor"), "1a86\n")
    w(os.path.join(ser, "idProduct"), "7523\n")
    w(os.path.join(ser, "product"), "USB Serial\n")
    tty = os.path.join(ser, "1-2:1.0", "ttyUSB0")
    w(os.path.join(tty, "uevent"), "MAJOR=188\nMINOR=0\nDEVNAME=ttyUSB0\n")
    link(tty, os.path.join(S, "class", "tty", "ttyUSB0"))
    for d in (usb, cam, ser):
        link(d, os.path.join(S, "bus", "usb", "devices", os.path.basename(d)))

    # --- GPIO-чип (символьное устройство /dev/gpiochip0; линии - через ioctl)
    g = os.path.join(dev, "platform", "axi", "1000120000.pcie", "1f000d0000.gpio",
                     "gpiochip0")
    os.makedirs(g, exist_ok=True)
    link(g, os.path.join(S, "bus", "gpio", "devices", "gpiochip0"))

    # --- HAT EEPROM -> узел /hat в живом дереве устройств
    hat = os.path.join(root, "proc", "device-tree", "hat")
    w(os.path.join(hat, "vendor"), "ACME Technology Company\x00")
    w(os.path.join(hat, "product"), "Special Sensor Board\x00")
    w(os.path.join(hat, "product_id"), "0x0001\x00")
    w(os.path.join(hat, "product_ver"), "0x0001\x00")
    w(os.path.join(hat, "uuid"), "00000000-0000-0000-0000-000000000001\x00")


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "fixture_root")
    print("ok")
