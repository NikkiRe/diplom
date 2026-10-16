package sysfs

// Таблицы единиц перенесены из прототипа research/new_topic/GAP2_artifacts/sysfs_probe.py без изменений.

// unitSpec — единица ABI ядра (после scale/offset), единица SenML (RFC 8428) и множитель ABI -> SenML.
type unitSpec struct {
	ABI   string
	SenML string
	K     float64
}

// iioUnits: тип канала IIO -> единицы. Источник: Documentation/ABI/testing/sysfs-bus-iio.
var iioUnits = map[string]unitSpec{
	"temp":             {"milli degC", "Cel", 1e-3},
	"humidityrelative": {"milli percent", "%RH", 1e-3},
	"pressure":         {"kPa", "Pa", 1e3},
	"illuminance":      {"lux", "lx", 1.0},
	"voltage":          {"mV", "V", 1e-3},
	"current":          {"mA", "A", 1e-3},
	"power":            {"mW", "W", 1e-3},
	"accel":            {"m/s^2", "m/s2", 1.0},
}

// iioNames: тип канала IIO -> имя свойства.
var iioNames = map[string]string{
	"temp": "temperature", "humidityrelative": "humidity", "pressure": "pressure",
	"illuminance": "illuminance", "voltage": "voltage", "current": "current",
	"power": "power", "accel": "acceleration",
}

// hwmonSpec: префикс атрибута hwmon -> единицы и имя величины. Источник: sysfs-class-hwmon.
type hwmonSpec struct {
	unitSpec
	Quantity string
}

var hwmonUnits = map[string]hwmonSpec{
	"temp":     {unitSpec{"millidegree Celsius", "Cel", 1e-3}, "temperature"},
	"in":       {unitSpec{"millivolt", "V", 1e-3}, "voltage"},
	"curr":     {unitSpec{"milliampere", "A", 1e-3}, "current"},
	"power":    {unitSpec{"microWatt", "W", 1e-6}, "power"},
	"humidity": {unitSpec{"milli-percent", "%RH", 1e-3}, "humidity"},
}

// w1ThermFamilies: код семейства 1-Wire -> чип (Documentation/w1/slaves/w1_therm.rst).
var w1ThermFamilies = map[string]string{
	"10": "DS18S20", "22": "DS1822", "28": "DS18B20", "3b": "DS1825", "42": "DS28EA00",
}
