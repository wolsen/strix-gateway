# FILE: apollo_gateway/core/personas.py
"""Persona defaults and CapabilityProfile schema.

The persona defaults map is intentionally isolated here so that new personas
can be added without touching any other module.  Core logic imports only
``CapabilityProfile``, ``get_persona_defaults``, and ``merge_profile``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Capability profile schema
# ---------------------------------------------------------------------------

class CapabilityFeatures(BaseModel):
    thin_provisioning: bool = True
    snapshots: bool = True
    clones: bool = True
    replication: bool = False
    consistency_groups: bool = False
    multiattach: bool = False
    compression: bool = False
    easy_tier: bool = False


class CapabilityLimits(BaseModel):
    max_volumes: int | None = None
    max_snapshots_per_volume: int | None = None
    max_hosts: int | None = None


class CapabilityQuirks(BaseModel):
    return_delim_by_default: bool = False
    strict_name_length: int | None = None
    slow_list_latency_ms: int | None = None


class CapabilityProfile(BaseModel):
    model: str = "generic"
    version: str = "1.0.0"
    features: CapabilityFeatures = CapabilityFeatures()
    limits: CapabilityLimits = CapabilityLimits()
    quirks: CapabilityQuirks = CapabilityQuirks()


# ---------------------------------------------------------------------------
# Persona defaults map — extend here, nowhere else
# ---------------------------------------------------------------------------

_PERSONA_DEFAULTS: dict[str, CapabilityProfile] = {
    "generic": CapabilityProfile(
        model="Apollo-Generic",
        version="1.0.0",
        features=CapabilityFeatures(
            thin_provisioning=True,
            snapshots=True,
            clones=True,
            replication=False,
            consistency_groups=False,
            multiattach=False,
        ),
    ),
    "ibm_svc": CapabilityProfile(
        model="SVC-SAFER-FAKE-9000",
        version="8.6.0.0",
        features=CapabilityFeatures(
            thin_provisioning=True,
            snapshots=True,
            clones=True,
            replication=False,
            consistency_groups=True,
            multiattach=True,
            compression=True,
            easy_tier=True,
        ),
        quirks=CapabilityQuirks(
            return_delim_by_default=False,
            strict_name_length=63,
        ),
    ),
    "pure": CapabilityProfile(
        model="FlashArray-stub",
        version="6.4.0",
        features=CapabilityFeatures(
            thin_provisioning=True,
            snapshots=True,
            clones=True,
            replication=True,
            consistency_groups=True,
            multiattach=True,
        ),
    ),
    "ontap": CapabilityProfile(
        model="ONTAP-stub",
        version="9.13.0",
        features=CapabilityFeatures(
            thin_provisioning=True,
            snapshots=True,
            clones=True,
            replication=True,
            consistency_groups=True,
            multiattach=False,
        ),
    ),
}


def get_persona_defaults(persona: str) -> CapabilityProfile:
    """Return the default CapabilityProfile for *persona*.

    Falls back to the ``"generic"`` profile for unknown persona strings.
    """
    return _PERSONA_DEFAULTS.get(persona, _PERSONA_DEFAULTS["generic"])


def merge_profile(persona: str, overrides: dict[str, Any] | None) -> CapabilityProfile:
    """Deep-merge *overrides* on top of the defaults for *persona*.

    *overrides* is the raw dict stored in ``Array.profile``.
    Unset keys retain the persona default.  Returns a fully populated
    :class:`CapabilityProfile`.
    """
    base = get_persona_defaults(persona)
    if not overrides:
        return base

    base_dict = base.model_dump()

    # Deep-merge top-level sections
    for section in ("features", "limits", "quirks"):
        if section in overrides and isinstance(overrides[section], dict):
            base_dict[section] = {**base_dict[section], **overrides[section]}

    # Scalar top-level overrides (model, version)
    for key in ("model", "version"):
        if key in overrides:
            base_dict[key] = overrides[key]

    return CapabilityProfile.model_validate(base_dict)
