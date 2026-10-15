"""
The OpenADR 3 fetch-events automation type.

A polling schedule is a FlexMeasures automation of this type on a VEN client's asset:
FlexMeasures decides when it is due, queues its job on the ingestion queue and saves the beliefs it returns.
"""

from __future__ import annotations

from typing import Any, cast

import timely_beliefs as tb
from flexmeasures.data.automations import AutomationHandler
from flexmeasures.data.models.data_sources import DataGenerator
from flexmeasures.data.models.time_series import Sensor
from flexmeasures.data.schemas.sensors import SensorIdField
from marshmallow import Schema, ValidationError, fields, pre_dump, validates_schema

from flexmeasures_openadr3.utils.vtn_events import (
    PayloadTypeSensorMapping,
    event_payloads_to_beliefs,
    fetch_events,
    validate_no_overlapping_events,
)

FETCH_EVENTS_AUTOMATION_TYPE = "openadr3-fetch-events"
OPENADR_EVENT_SOURCE_NAME = "OpenADR 3 VTN"


class FetchEventsConfigSchema(Schema):
    """What a polling schedule fetches: the targets to filter events on, and the sensors to record each limit type on."""

    targets = fields.List(fields.Str(), load_default=list)
    import_sensor = SensorIdField(data_key="import-sensor", load_default=None, allow_none=True)
    export_sensor = SensorIdField(data_key="export-sensor", load_default=None, allow_none=True)

    @pre_dump
    def _leave_out_unset_sensors(self, data: dict[str, Any], **_kwargs: object) -> dict[str, Any]:
        """Leave out a limit type that is not fetched, as a sensor field cannot dump None."""
        return {key: value for key, value in data.items() if value is not None}

    @validates_schema
    def _require_a_sensor(self, data: dict[str, Any], **_kwargs: object) -> None:
        if data.get("import_sensor") is None and data.get("export_sensor") is None:
            msg = "Set an import-sensor and/or an export-sensor."
            raise ValidationError(msg)
        sensors = [sensor for sensor in (data.get("import_sensor"), data.get("export_sensor")) if sensor is not None]
        if len({sensor.generic_asset_id for sensor in sensors}) > 1:
            msg = "The import-sensor and export-sensor must belong to the same VEN client."
            raise ValidationError(msg)


class FetchEventsParametersSchema(Schema):
    """A fetch takes no parameters per run: it always fetches the events the VTN currently holds."""


class OpenADREventFetcher(DataGenerator):
    """Fetches capacity-limit events from the VTN of the VEN client whose sensors it records on."""

    __data_generator_base__ = "ingestor"
    __version__ = "1"
    _config_schema = FetchEventsConfigSchema()
    _parameters_schema = FetchEventsParametersSchema()

    @classmethod
    def get_data_source_info(cls) -> dict:
        """Record the fetched events under a source named after where they come from."""
        source_info = cast("dict", super().get_data_source_info())
        source_info["source"] = OPENADR_EVENT_SOURCE_NAME
        return source_info

    @property
    def sensor_mapping(self) -> PayloadTypeSensorMapping:
        """The sensors this fetcher records each supported payload type on."""
        return PayloadTypeSensorMapping(
            import_sensor=self._config.get("import_sensor"),
            export_sensor=self._config.get("export_sensor"),
        )

    @property
    def output_sensors(self) -> list[Sensor]:
        """The import and/or export capacity-limit sensors."""
        return cast("list[Sensor]", self._resolve_sensors(self._config.get("import_sensor"), self._config.get("export_sensor")))

    def _compute(self, **_kwargs: object) -> list[dict[str, Any]]:
        from flexmeasures_openadr3.utils.ven_clients import VenClientRepository  # noqa: PLC0415 (ven_clients imports this module)

        ven_id = self.output_sensors[0].generic_asset_id
        ven_client = VenClientRepository().find_by_id(ven_id)
        if ven_client is None:
            msg = f"Asset {ven_id} is not an OpenADR VEN client."
            raise ValueError(msg)

        events = fetch_events(ven_client, self._config["targets"])
        validate_no_overlapping_events(events)
        beliefs = event_payloads_to_beliefs(events, self.sensor_mapping, self.data_source)
        return [{"sensor": sensor, "data": tb.BeliefsDataFrame(sensor_beliefs)} for sensor, sensor_beliefs in beliefs.items()]


FETCH_EVENTS_AUTOMATION = AutomationHandler(
    type_id=FETCH_EVENTS_AUTOMATION_TYPE,
    display_name="OpenADR 3 polling",
    generator_class=OpenADREventFetcher,
    queue="ingestion",
    result_noun="capacity limit",
)
