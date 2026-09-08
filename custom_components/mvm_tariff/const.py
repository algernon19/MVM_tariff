"""Constants for the MVM Tariff Cost integration."""
from __future__ import annotations

DOMAIN = "mvm_tariff"

# --- P1 meter over MQTT ----------------------------------------------------
# Matches the jantenhove/esp8266_p1meter project's default MQTT_ROOT_TOPIC:
# it publishes plain-text Wh integers on "<root>/consumption_low_tarif" and
# "<root>/consumption_high_tarif" (the two DSMR tariff registers - total
# consumption is their sum, whether the meter actually uses one or both).
CONF_MQTT_ROOT_TOPIC = "mqtt_root_topic"
DEFAULT_MQTT_ROOT_TOPIC = "sensors/power/p1meter"
TOPIC_LOW = "consumption_low_tarif"
TOPIC_HIGH = "consumption_high_tarif"

CONSUMPTION_STATISTIC_NAME = "MVM Tarifa – Fogyasztás"

# --- alternative data source: import/export power sensors -------------------
# Instead of the two MQTT tariff registers, the live consumption can be
# integrated (trapezoidal rule, kW * elapsed hours) from a pair of existing HA
# power sensors - e.g. the ESP8266 P1 Meter's "Actual Power Consumption" /
# "Actual Return Delivery" entities. Import minus export gives the net kWh,
# so a solar/return-delivery setup nets against the tiered/D pricing.
CONF_DATA_SOURCE = "data_source"
DATA_SOURCE_MQTT = "mqtt"
DATA_SOURCE_POWER_SENSORS = "power_sensors"
DEFAULT_DATA_SOURCE = DATA_SOURCE_MQTT

CONF_IMPORT_POWER_ENTITY = "import_power_entity"
CONF_EXPORT_POWER_ENTITY = "export_power_entity"

# --- A1 tiered household tariff ---------------------------------------------
CONF_PRICE_LOW = "price_low"
CONF_PRICE_HIGH = "price_high"
CONF_ANNUAL_THRESHOLD = "annual_threshold_kwh"
# "monthly": the yearly allowance split across calendar months by day count
# (~210 kWh/month, matching each invoice); "yearly": one 2523 kWh bucket from
# 1 January (the figure the year-end reconciliation settles to).
CONF_ALLOWANCE_PERIOD = "allowance_period"
DEFAULT_PRICE_LOW = 36.39
DEFAULT_PRICE_HIGH = 70.104
DEFAULT_ANNUAL_THRESHOLD = 2523.0
DEFAULT_ALLOWANCE_PERIOD = "monthly"

COST_STATISTIC_ID = "mvm_tariff:cost_a1"
COST_STATISTIC_NAME = "MVM Tarifa – A1 költség"

# --- MVM "D" (dynamic, HUPX-based) tariff ----------------------------------
COST_D_STATISTIC_ID = "mvm_tariff:cost_d"
COST_D_STATISTIC_NAME = "MVM Tarifa – D költség"
ENERGY_CHARTS_PRICE_URL = "https://api.energy-charts.info/price"
# The MNB legacy SOAP service is only served over plain HTTP (the https URL 404s).
MNB_SOAP_URL = "http://www.mnb.hu/arfolyamok.asmx"

CONF_D_ENABLED = "d_enabled"
CONF_D_MERCHANT_FEE = "d_merchant_fee_huf_kwh"
CONF_D_TRANSMISSION_FEE = "d_transmission_fee_huf_kwh"
CONF_D_DISTRIBUTION_FEE = "d_distribution_fee_huf_kwh"
CONF_D_VAT_PERCENT = "d_vat_percent"
CONF_D_EUR_HUF = "d_eur_huf"
DEFAULT_D_ENABLED = False
DEFAULT_D_MERCHANT_FEE = 13.70
DEFAULT_D_TRANSMISSION_FEE = 4.84
DEFAULT_D_DISTRIBUTION_FEE = 18.56
DEFAULT_D_VAT_PERCENT = 27.0
DEFAULT_D_EUR_HUF = 0.0  # 0 = MNB daily rate, fetched automatically

SIGNAL_UPDATE = f"{DOMAIN}_update"

STORAGE_KEY = f"{DOMAIN}.state"
D_PRICE_STORAGE_KEY = f"{DOMAIN}.d_prices"

TIME_ZONE = "Europe/Budapest"
