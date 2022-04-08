import asyncio
import base64
import logging
import random
import time
from datetime import datetime, timedelta
from typing import List, MutableMapping, Optional, Tuple

from homeassistant.components.weather import WeatherEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, STATE_UNAVAILABLE, TEMP_CELSIUS
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import HomeAssistantType

from .const import (ATTR_API_KEY, ATTR_EXPIRES_AT, CONF_CONSUMER_KEY,
                    CONF_CONSUMER_SECRET, CONF_GEOLOCATION_ID, HTTP_OK)

logger = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=60)


async def async_setup_entry(
    hass: HomeAssistantType, config_entry: ConfigEntry, async_add_entities
) -> None:
    async_add_entities((SRFWeather(config_entry.data),), True)


API_URL = "https://api.srgssr.ch"

URL_OAUTH = API_URL + "/oauth/v1/accesstoken"

URL_FORECASTS = API_URL + "/srf-meteo/forecast/{geolocationId}"
URL_GEOLOCATION = API_URL + "/srf-meteo/geolocations"


def _check_client_credentials_response(d: dict) -> None:
    EXPECTED_KEYS = {"issued_at", "expires_in", "access_token"}

    if "issued_at" not in d:
        d["issued_at"] = int(time.time())

    missing = EXPECTED_KEYS - d.keys()
    if missing:
        logger.warning(
            f"received client credentials response with missing keys: {missing} ({d})"
        )
        raise ValueError("client credentials response missing keys", missing)


async def request_access_token(hass: HomeAssistantType, key: str, secret: str) -> dict:
    session = async_get_clientsession(hass)

    auth = base64.b64encode(f"{key}:{secret}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}"}
    params = {"grant_type": "client_credentials"}
    async with session.post(URL_OAUTH, params=params, headers=headers) as resp:
        data = await resp.json()

    _check_client_credentials_response(data)
    return data


async def _renew_api_key(hass: HomeAssistantType, data: MutableMapping) -> None:
    token_data = await request_access_token(
        hass, data[CONF_CONSUMER_KEY], data[CONF_CONSUMER_SECRET]
    )
    logger.debug("token data: %s", token_data)

    try:
        data[ATTR_EXPIRES_AT] = (
            int(token_data["expires_in"]) + int(token_data["issued_at"]) // 1000
        )
        data[ATTR_API_KEY] = token_data["access_token"]
    except Exception:
        logger.exception(
            "exception while parsing access token response: %s", token_data
        )
        raise


async def get_api_key(hass: HomeAssistantType, data: MutableMapping) -> str:
    try:
        expires_at = data[ATTR_EXPIRES_AT]
    except KeyError:
        renew = True
    else:
        renew = time.time() >= expires_at

    if renew:
        logger.info("Renewing API key")
        await _renew_api_key(hass, data)

    return data[ATTR_API_KEY]


async def _get(hass, api_data: dict, url: str, **kwargs) -> dict:
    session = async_get_clientsession(hass)
    api_key = await get_api_key(hass, api_data)
    weak_update(
        kwargs,
        "headers",
        {
            "Authorization": f"Bearer {api_key}",
        },
    )
    logger.debug("GET %s with %s", url, kwargs)
    async with session.get(url, **kwargs) as resp:
        if resp.status == HTTP_OK:
            logger.debug(
                "Rate-limit available %s, rate-limit reset will be on %s",
                resp.headers.get("x-ratelimit-available"),
                datetime.fromtimestamp(
                    int(resp.headers.get("x-ratelimit-reset-time", 0)) / 1000
                ),
            )
        data = await resp.json()
        logger.debug("response: %s", data)
        resp.raise_for_status()

    return data


async def get_geolocation_ids(hass, api_data: dict, latitude: float, longitude: float):
    coordinates = {"latitude": latitude, "longitude": longitude}
    data = await _get(hass, api_data, URL_GEOLOCATION, params=coordinates)
    logger.debug(data)
    return data


class SRFWeather(WeatherEntity):
    def __init__(self, config: dict) -> None:
        self._config = config
        self._geolocation_id = config[CONF_GEOLOCATION_ID]
        self._api_data = dict(self._config)
        self.__update_loop_task = None

        self._forecast = []
        self._hourly_forecast = []
        self._state = None
        self._temperature = None
        self._wind_speed = None
        self._wind_bearing = None

        self._state_attrs = {}

    @property
    def should_poll(self) -> bool:
        return True

    @property
    def unique_id(self):
        return self._config[CONF_GEOLOCATION_ID]

    @property
    def name(self) -> Optional[str]:
        return self._config.get(CONF_NAME)

    @property
    def extra_state_attributes(self) -> dict:
        return self._state_attrs

    @property
    def state(self) -> Optional[str]:
        return self._state

    @property
    def temperature(self) -> Optional[float]:
        return self._temperature

    @property
    def temperature_unit(self):
        return TEMP_CELSIUS

    @property
    def pressure(self) -> Optional[float]:
        return None

    @property
    def humidity(self) -> Optional[float]:
        return None

    @property
    def visibility(self) -> Optional[float]:
        return None

    @property
    def wind_speed(self) -> Optional[float]:
        return self._wind_speed

    @property
    def wind_bearing(self) -> Optional[str]:
        return self._wind_bearing

    @property
    def forecast(self) -> List[dict]:
        return self._forecast

    @property
    def hourly_forecast(self) -> List[dict]:
        return self._hourly_forecast

    @property
    def attribution(self) -> str:
        return "SRF Schweizer Radio und Fernsehen"

    async def __update(self) -> None:
        url = URL_FORECASTS.format(geolocationId=self._geolocation_id)
        logger.debug("Updating using URL %s", url)
        data = await _get(self.hass, self._api_data, url)

        logger.debug(data)

        forecast = []
        now = datetime.now().astimezone().replace(minute=0, second=0, microsecond=0)

        # The API seems to provides 4 days worth of hourly (aka 60minutes) forcasts
        # and 7 days tripe-hour (aka hourly)/daily forcasts.

        # Get 12h hourly, fill today and the next day with triple-hour and daily
        # forcasts for the rest
        hourly_split = now + timedelta(hours=12)
        for raw_hour in data["forecast"]["60minutes"]:
            try:
                fdate, hour = parse_forecast_hour(raw_hour)
            except Exception:
                logger.warning(
                    f"failed to parse hourly forecast: {raw_hour}", exc_info=True
                )
                continue

            # Don't care about the past...
            if fdate < now:
                continue

            if fdate > hourly_split:
                break

            forecast.append(hour)

        triple_hour_split = (now + timedelta(days=2)).replace(hour=0)
        for raw_three_hour in data["forecast"]["hour"]:
            try:
                fdate, three_hour = parse_forecast_hour(raw_three_hour)
            except Exception:
                logger.warning(
                    f"failed to parse triple-hourly forecast: {raw_three_hour}",
                    exc_info=True,
                )
                continue

            if fdate <= hourly_split:
                continue

            if fdate > triple_hour_split:
                break

            forecast.append(three_hour)

        for raw_day in data["forecast"]["day"]:
            try:
                fdate, day = parse_forecast_day(raw_day)
            except Exception:
                logger.warning(
                    f"failed to parse daily forecast: {raw_day}", exc_info=True
                )
                continue

            if fdate <= triple_hour_split:
                continue

            forecast.append(day)

        self._forecast = forecast

        # Remove current "forecast" from forecast and use it as current weather
        forecastnow = forecast.pop(0)

        logger.debug(forecastnow)

        self._state = forecastnow["condition"]
        self._temperature = forecastnow["temperature"]
        self._wind_speed = forecastnow["wind_speed"]
        self._wind_bearing = deg_to_cardinal(forecastnow["wind_bearing"])

        self._state_attrs.update(
            wind_direction=forecastnow["wind_bearing"],
            symbol_id=forecastnow["symbol_id"],
            precipitation=forecastnow["precipitation"],
            precipitation_probability=forecastnow["precipitation_probability"],
        )

    async def async_update(self) -> None:
        """Get the latest data from SRF-Meteo API and updates the states."""
        try:
            await self.__update()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("failed to update weather")


def parse_forecast(forecast: dict) -> Tuple[datetime, dict]:
    date = datetime.fromisoformat(forecast["local_date_time"])

    symbol_id = int(forecast["SYMBOL_CODE"])
    condition = get_condition_from_symbol(symbol_id)
    precip_total = float(forecast["RRR_MM"])
    wind_speed = float(forecast["FF_KMH"])
    percip_probability = float(forecast["PROBPCP_PERCENT"])

    data = {
        "datetime": date.isoformat(),
        "condition": condition,
        "symbol_id": symbol_id,
        "precipitation": precip_total,
        "wind_speed": wind_speed,
        "precipitation_probability": percip_probability,
    }

    # For some unknown reason, wind bearing is sometimes missing
    if "DD_DEG" in forecast:
        data["wind_bearing"] = int(forecast["DD_DEG"])

    return (date, data)


def parse_forecast_day(day: dict) -> Tuple[datetime, dict]:
    date, data = parse_forecast(day)

    temp_high = float(day["TX_C"])
    temp_low = float(day["TN_C"])

    data.update(
        {
            "temperature": temp_high,
            "templow": temp_low,
        }
    )

    return (date, data)


def parse_forecast_hour(hour: dict) -> Tuple[datetime, dict]:
    date, data = parse_forecast(hour)

    temperature = float(hour["TTT_C"])

    data.update(
        {
            "temperature": temperature,
        }
    )

    return (date, data)


CARDINALS = (
    "N",
    "NNE",
    "NE",
    "ENE",
    "E",
    "ESE",
    "SE",
    "SSE",
    "S",
    "SSW",
    "SW",
    "WSW",
    "W",
    "WNW",
    "NW",
    "NNW",
)

DEG_HALF_CIRCLE = 180
DEG_FULL_CIRCLE = 2 * DEG_HALF_CIRCLE
_CARDINAL_DEGREE = DEG_FULL_CIRCLE / len(CARDINALS)


def deg_to_cardinal(deg: float) -> str:
    i = round((deg % DEG_FULL_CIRCLE) / _CARDINAL_DEGREE)
    return CARDINALS[i % len(CARDINALS)]


# maps the symbol reported by the API to the Material Design icon names used by Home Assistant.
# Sadly this isn't bijective because the API reports lots of weirdly specific states.
# The comments contain the description of each symbol id as reported by SRG SSR.
SYMBOL_STATE_MAP = {
    1: "sunny",  # sonnig
    2: "fog",  # Nebelbänke
    3: "partlycloudy",  # teils sonnig
    4: "rainy",  # Regenschauer
    5: "lightning-rainy",  # Regenegenschauer mit Gewitter
    6: "snowy",  # Schneeschauer
    7: "snowy-rainy",  # sonnige Abschnitte und einige Gewitter mit Schnee (undocumented)
    8: "snowy-rainy",  # Schneeregenschauer
    9: "snowy-rainy",  # wechselhaft mit Schneeregenschauern und Gewittern (undocumented)
    10: "sunny",  # ziemlich sonnig
    11: "partlycloudy",  # sonnig, aber auch einzelne Schauer (undocumented)
    12: "sunny",  # sonnig und nur einzelne Gewitter (undocumented)
    13: "sunny",  # sonnig und nur einzelne Schneeschauer (undocumented)
    14: "sunny",  # sonnig, einzelne Schneeschauer, dazwischen sogar Blitz und Donner (undocumented)
    15: "sunny",  # sonnig und nur einzelne Schauer, vereinzelt auch Flocken (undocumented)
    16: "sunny",  # oft sonnig, nur einzelne gewittrige Schauer, teils auch Flocken (undocumented)
    17: "fog",  # Nebel
    18: "cloudy",  # stark bewölkt (undocumented)
    19: "cloudy",  # bedeckt
    20: "rainy",  # regnerisch
    21: "snowy",  # Schneefall
    22: "snowy-rainy",  # Schneeregen
    23: "pouring",  # Dauerregen (undocumented)
    24: "snowy",  # starker Schneefall (undocumented)
    25: "rainy",  # Regenschauer26: "lightning",  # stark bewölkt und einige Gewitter
    26: "lightning",  # stark bewölkt und einige Gewitter (undocumented)
    27: "snowy",  # trüb mit einigen Schneeschauern (undocumented)
    28: "cloudy",  # stark bewölkt, Schneeschauer, dazwischen Blitz und Donner (undocumented)
    29: "snowy-rainy",  # ab und zu Schneeregen (undocumented)
    30: "snowy-rainy",  # Schneeregen, einzelne Gewitter (undocumented)
    -1: "clear-night",  # klar
    -2: "fog",  # Nebelbänke
    -3: "cloudy",  # Wolken: Sandsturm
    -4: "rainy",  # Regenschauer
    -5: "lightning-rainy",  # Regenschauer mit Gewitter
    -6: "snowy",  # Schneeschauer
    -7: "snowy",  # einige Gewitter mit Schnee (undocumented)
    -8: "snowy-rainy",  # Schneeregenschauer
    -9: "lightning-rainy",  # wechselhaft mit Schneeregenschauern und Gewittern (undocumented)
    -10: "partlycloudy",  # klare Abschnitte
    -11: "rainy",  # einzelne Schauer (undocumented)
    -12: "lightning",  # einzelne Gewitter (undocumented)
    -13: "snowy",  # einzelne Schneeschauer (undocumented)
    -14: "snowy",  # einzelne Schneeschauer, dazwischen sogar Blitz und Donner (undocumented)
    -15: "snowy-rainy",  # einzelne Schauer, vereinzelt auch Flocken (undocumented)
    -16: "partlycloudy",  # oft sonnig, nur einzelne gewittrige Schauer, teils auch Flocken (undocumented)
    -17: "fog",  # Nebel
    -18: "cloudy",  # stark bewölkt (undocumented)
    -19: "cloudy",  # bedeckt
    -20: "rainy",  # regnerisch
    -21: "snowy",  # Schneefall
    -22: "snowy-rainy",  # Schneeregen
    -23: "pouring",  # Dauerregen (undocumented)
    -24: "snowy",  # starker Schneefall (undocumented)
    -25: "rainy",  # Regenschauer
    -26: "lightning",  # stark bewölkt und einige Gewitter (undocumented)
    -27: "rainy",  # trüb mit einigen Schneeschauern (undocumented)
    -28: "lightning-rainy",  # stark bewölkt, Schneeschauer, dazwischen Blitz und Donner (undocumented)
    -29: "snowy-rainy",  # ab und zu Schneeregen (undocumented)
    -30: "snowy-rainy",  # Schneeregen, einzelne Gewitter (undocumented)
}


def get_condition_from_symbol(symbol_id: int):
    condition = SYMBOL_STATE_MAP.get(symbol_id)
    if condition is None:
        logger.warning("No condition entry for symbol id {}".format(symbol_id))
        condition = STATE_UNAVAILABLE
    return condition


def weak_update(d: MutableMapping, key: str, value: MutableMapping) -> None:
    try:
        existing = d[key]
    except KeyError:
        pass
    else:
        value.update(existing)

    d[key] = value
