"""Patch arena_hero so newer server action types don't crash the client.

The live protocol may echo unit actions (e.g. HEAL) that older SDK builds
reject while validating Received envelopes. Without this patch, one unknown
action type kills the whole turn stream.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, TypeAdapter


def apply_sdk_compat() -> None:
    """Install protocol compatibility patches. Safe to call more than once."""
    import json
    from datetime import datetime, timezone

    import arena_hero.actions as actions
    import arena_hero._protocol as protocol
    import arena_hero.client as client
    from arena_hero.enums import CommandSource
    from arena_hero.errors import ProtocolError
    from arena_hero.models import Received, Tick

    if getattr(actions, "_arena_compat_applied", False):
        return

    class HealAction(actions._StrictModel):
        """Server-side heal action that may appear in Received plan echoes."""

        type: Literal["HEAL"] = "HEAL"

    # Accept HEAL in the unit action union used by Received envelopes.
    actions.HealAction = HealAction
    actions.UnitAction = Annotated[
        actions.WaitAction
        | actions.MoveAction
        | actions.HarvestAction
        | actions.DepositAction
        | actions.SweepAction
        | actions.ShootAction
        | actions.PickupBeaconAction
        | actions.DropBeaconAction
        | actions.SelfDestructAction
        | HealAction,
        Field(discriminator="type"),
    ]

    # Keep outgoing submits on the original CommandPlan (strict, no HEAL needed).
    # Only loosen inbound stream parsing.
    original_parse = protocol.parse_stream_message

    # Rebuild stream adapter with a looser Received.plan.unit_actions type.
    from pydantic import BaseModel, ConfigDict

    class _LoosePlan(BaseModel):
        model_config = ConfigDict(extra="ignore")
        tick: int
        unit_actions: dict[str, Any] = Field(default_factory=dict)
        core_action: Any | None = None

    class _LooseReceived(BaseModel):
        model_config = ConfigDict(extra="ignore")
        tick: int
        source: Any
        received_at: Any
        plan: _LoosePlan

    class _TickEnv(BaseModel):
        model_config = ConfigDict(extra="ignore")
        type: Literal["tick"]
        data: int

    class _StateEnv(BaseModel):
        model_config = ConfigDict(extra="ignore")
        type: Literal["state"]
        data: Any

    class _RecvEnv(BaseModel):
        model_config = ConfigDict(extra="ignore")
        type: Literal["received"]
        data: _LooseReceived

    loose_adapter = TypeAdapter(
        Annotated[_TickEnv | _StateEnv | _RecvEnv, Field(discriminator="type")]
    )

    known_unit_types = {
        "WAIT", "MOVE", "HARVEST", "DEPOSIT", "SWEEP", "SHOOT",
        "PICKUP_BEACON", "DROP_BEACON", "SELF_DESTRUCT", "HEAL",
    }

    last_tick = 0

    def tolerant_parse_stream_message(raw: str | bytes):
        nonlocal last_tick
        try:
            result = original_parse(raw)
        except ProtocolError:
            if isinstance(raw, bytes):
                raise ProtocolError("the server sent a binary WebSocket message")
            try:
                envelope = loose_adapter.validate_json(raw)
            except Exception as exc:
                # The server may send an envelope type we don't know about yet
                # (e.g. an acknowledgement/ping). One stray message must not drop
                # the whole stream — that forces a reconnect and a tick gap.
                # Identify the type and, when unknown, skip the message instead.
                saved_exc = exc  # keep for the state re-raise below (except var is del'd)
                try:
                    obj = json.loads(raw)
                    msg_type = obj.get("type") if isinstance(obj, dict) else None
                except Exception:
                    msg_type = None
                if msg_type not in ("tick", "state", "received"):
                    print(
                        f"[compat] skip unknown stream message type={msg_type!r}",
                        flush=True,
                    )
                    if last_tick >= 1:
                        plan = actions.CommandPlan(
                            tick=last_tick, unit_actions={}, core_action=None,
                        )
                        return Received(
                            tick=last_tick,
                            source=CommandSource.AGENT,
                            received_at=datetime.now(timezone.utc),
                            plan=plan,
                        )
                raise ProtocolError("invalid Arena Hero WebSocket message") from exc
        else:
            if isinstance(result, Tick):
                last_tick = result.tick
            return result

        if isinstance(envelope, _TickEnv):
            last_tick = envelope.data
            return Tick(tick=envelope.data)
        if isinstance(envelope, _StateEnv):
            # State must stay strict — re-raise the original failure path. Chain
            # the envelope validation detail when there was a validation error,
            # otherwise suppress the chained cause.
            cause = saved_exc if "saved_exc" in locals() else None
            raise ProtocolError("invalid Arena Hero WebSocket message") from cause

        data = envelope.data
        cleaned: dict[UUID, Any] = {}
        for key, value in (data.plan.unit_actions or {}).items():
            try:
                uid = key if isinstance(key, UUID) else UUID(str(key))
            except Exception:
                continue
            if not isinstance(value, dict):
                continue
            action_type = value.get("type")
            if action_type == "HEAL":
                # Normalize unknown heal echoes to WAIT so the stream continues.
                cleaned[uid] = {"type": "WAIT"}
            elif action_type in known_unit_types:
                cleaned[uid] = value
        try:
            plan = actions.CommandPlan.model_validate(
                {
                    "tick": data.plan.tick,
                    "unit_actions": cleaned,
                    "core_action": data.plan.core_action,
                }
            )
        except Exception:
            plan = actions.CommandPlan(
                tick=int(data.plan.tick),
                unit_actions={},
                core_action=None,
            )
        return Received(
            tick=int(data.tick),
            source=data.source,
            received_at=data.received_at,
            plan=plan,
        )

    protocol.parse_stream_message = tolerant_parse_stream_message
    # client.py does `from ._protocol import parse_stream_message` at module load,
    # so patching only `protocol.parse_stream_message` would NOT affect the
    # reference client.events() calls. Patch the client's module-level binding too.
    client.parse_stream_message = tolerant_parse_stream_message
    actions._arena_compat_applied = True
    print(
        "[compat] arena_hero protocol patch applied "
        "(unknown unit actions like HEAL are tolerated)",
        flush=True,
    )
