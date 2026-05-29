from __future__ import annotations

import logging

from pypolestar import PolestarApi

log = logging.getLogger(__name__)


def _patch_pypolestar_schema() -> None:
    """Drop `chargingStatus` from the BatteryV2 GraphQL query.

    Polestar removed the field on 2026-05-21, breaking every poll. Until
    pypolestar ships a fix upstream, we rewrite the query at import time and
    make the battery model's from_dict tolerant of the missing key. Charging
    status is then derived in our own fetcher from charging-power / connector
    state.
    """
    try:
        from gql import gql
        from pypolestar import api as _pp_api
        from pypolestar import graphql as _pp_gql
        from pypolestar import models as _pp_models
    except Exception:
        log.exception("pypolestar import failed; cannot apply schema patch")
        return

    new_query = gql(
        """
        query CarTelematicsV2($vins: [String!]!) {
            carTelematicsV2(vins: $vins) {
                health {
                    vin
                    brakeFluidLevelWarning
                    daysToService
                    distanceToServiceKm
                    engineCoolantLevelWarning
                    oilLevelWarning
                    serviceWarning
                    timestamp { seconds nanos }
                }
                battery {
                    vin
                    batteryChargeLevelPercentage
                    estimatedChargingTimeToFullMinutes
                    estimatedDistanceToEmptyKm
                    timestamp { seconds nanos }
                }
                odometer {
                    vin
                    odometerMeters
                    timestamp { seconds nanos }
                }
            }
        }
        """
    )
    _pp_gql.QUERY_TELEMATICS_V2 = new_query
    _pp_api.QUERY_TELEMATICS_V2 = new_query

    BatteryData = getattr(_pp_models, "CarBatteryData", None)
    if BatteryData is not None:
        original_from_dict = BatteryData.from_dict.__func__

        @classmethod
        def from_dict_safe(cls, data):
            if isinstance(data, dict) and "chargingStatus" not in data:
                data = {**data, "chargingStatus": "CHARGING_STATUS_UNSPECIFIED"}
            return original_from_dict(cls, data)

        BatteryData.from_dict = from_dict_safe

    # Pull extra battery fields the proto exposes but pypolestar's
    # `_parse_battery` drops on the floor (total energy consumption variants,
    # per-mode averages, miles, charger power status). We attach them to the
    # frozen `GrpcBatteryData` via object.__setattr__ so dataclasses.asdict()
    # still sees only the declared fields.
    try:
        from pypolestar import grpc_client as _pp_grpc
    except Exception:
        _pp_grpc = None
    if _pp_grpc is not None and hasattr(_pp_grpc, "_parse_battery"):
        _orig_parse = _pp_grpc._parse_battery

        def _none_if_zero(v):
            return v if (v is not None and v != 0) else None

        def _parse_battery_with_extras(b):
            result = _orig_parse(b)
            extras = {
                "avg_consumption_kwh_per_100km_automatic":
                    _none_if_zero(getattr(b, "average_energy_consumption_kwh_per_100_km_automatic", 0)),
                "avg_consumption_kwh_per_100km_since_charge":
                    _none_if_zero(getattr(b, "average_energy_consumption_kwh_per_100_km_since_charge", 0)),
                "total_energy_consumption_wh":
                    _none_if_zero(getattr(b, "total_energy_consumption_wh", 0)),
                "total_energy_consumption_wh_automatic":
                    _none_if_zero(getattr(b, "total_energy_consumption_wh_automatic", 0)),
                "total_energy_consumption_wh_since_charge":
                    _none_if_zero(getattr(b, "total_energy_consumption_wh_since_charge", 0)),
                "estimated_distance_to_empty_miles":
                    _none_if_zero(getattr(b, "estimated_distance_to_empty_miles", 0)),
                "charger_power_status_raw":
                    getattr(b, "charger_power_status", 0),
            }
            for k, v in extras.items():
                object.__setattr__(result, k, v)
            object.__setattr__(result, "_raw_proto_repr", str(b))
            return result

        _pp_grpc._parse_battery = _parse_battery_with_extras

    log.warning("Applied pypolestar schema patch: dropped BatteryV2.chargingStatus; widened gRPC battery decode")


_patch_pypolestar_schema()


class PolestarClient:
    """Thin wrapper around pypolestar.PolestarApi.

    pypolestar manages its own OAuth/PKCE flow and token refresh in-process.
    We just hold the instance and the resolved VIN list for the rest of the app.
    """

    def __init__(self, email: str, password: str, vins: list[str] | None = None) -> None:
        self._email = email
        self._password = password
        self._configured_vins = vins or []
        self._api: PolestarApi | None = None
        self._vins: list[str] = []

    async def connect(self) -> None:
        # Tear down a previous instance before replacing it (reconnect path),
        # otherwise its httpx session / tokens leak.
        if self._api is not None:
            for closer in ("async_logout", "aclose", "close"):
                fn = getattr(self._api, closer, None)
                if fn is None:
                    continue
                try:
                    res = fn()
                    if hasattr(res, "__await__"):
                        await res
                except Exception:  # noqa: BLE001
                    log.debug("Closing previous PolestarApi via %s failed", closer, exc_info=True)
                break
            self._api = None

        self._api = PolestarApi(
            username=self._email,
            password=self._password,
            vins=self._configured_vins or None,
        )
        await self._api.async_init()

        if self._configured_vins:
            self._vins = list(self._configured_vins)
        else:
            # pypolestar discovers the account's cars in async_init() and exposes
            # them via get_available_vins() (also as the `available_vins` set).
            self._vins = list(self._api.get_available_vins())

        if not self._vins:
            log.warning("No VINs available after async_init; check credentials/account")
        else:
            log.info("Polestar client ready for VINs: %s", self._vins)

    @property
    def api(self) -> PolestarApi:
        if self._api is None:
            raise RuntimeError("PolestarClient.connect() not called")
        return self._api

    @property
    def connected(self) -> bool:
        return self._api is not None

    @property
    def vins(self) -> list[str]:
        return list(self._vins)

    async def refresh(self, vin: str) -> None:
        # update_vehicle=True refreshes the CarInformationData payload, which
        # carries `battery` (voltage/capacity/modules/cells) and `torque`.
        # Without it those fields stay `None`, robbing us of the real battery
        # capacity for consumption calculations.
        await self.api.update_latest_data(
            vin=vin,
            update_vehicle=True,
            update_telematics=True,
            update_grpc=True,
        )
