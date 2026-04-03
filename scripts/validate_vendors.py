#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Canonical, Ltd.
# SPDX-License-Identifier: GPL-3.0-only
"""Validate vendor conformance completeness.

Checks:
  1. Every personality subdir with app.py appears in tests/vendors.yaml.
  2. Every vendor in vendors.yaml that has e2e_required=true has >= 1 scenario.
  3. Each scenario has required metadata files in tests/e2e/vendors/<dir>/<scenario>/.
  4. Each scenario's driver_dir exists under tests/e2e/drivers/ with required files.
  5. No personality directory is unaccounted for.

Exit codes:
  0  — all checks pass
  1  — one or more violations found

Usage:
    python scripts/validate_vendors.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PERSONALITIES_DIR = REPO_ROOT / "strix_gateway" / "personalities"
VENDORS_YAML = REPO_ROOT / "tests" / "vendors.yaml"
VENDORS_DIR = REPO_ROOT / "tests" / "e2e" / "vendors"
DRIVERS_DIR = REPO_ROOT / "tests" / "e2e" / "drivers"

# Personality subdirs excluded from the "must be in vendors.yaml" check.
# Add entries here only for non-personality infrastructure directories.
EXCLUDED_PERSONALITY_DIRS = {"generic", "__pycache__"}

# Files required in tests/e2e/vendors/<vendor>/<scenario>/
REQUIRED_SCENARIO_FILES = {"scenario.yaml", "seed.sh"}

# Files required in tests/e2e/drivers/<driver_dir>/
REQUIRED_DRIVER_FILES = {"topo.yaml", "cinder-backend.conf", "verify.sh"}


def discover_personality_dirs() -> set[str]:
    """Return names of personality subdirs that contain app.py.

    The presence of app.py is the signal that a directory is a full personality
    (all personalities register with personality_registry in their app.py).
    """
    found = set()
    for entry in PERSONALITIES_DIR.iterdir():
        if not entry.is_dir():
            continue
        if entry.name in EXCLUDED_PERSONALITY_DIRS:
            continue
        if (entry / "app.py").exists():
            found.add(entry.name)
    return found


def load_vendors() -> list[dict]:
    with open(VENDORS_YAML) as fh:
        return yaml.safe_load(fh)["vendors"]


def main() -> int:
    errors: list[str] = []

    vendors = load_vendors()
    manifest_dirs = {v["personality_dir"] for v in vendors}

    # --- Check 1: every personality dir with app.py must be in vendors.yaml ---
    discovered = discover_personality_dirs()
    for pdir in sorted(discovered):
        if pdir not in manifest_dirs:
            errors.append(
                f"Personality '{pdir}' has app.py but is missing from tests/vendors.yaml. "
                "Add a vendor entry or add it to EXCLUDED_PERSONALITY_DIRS in "
                "scripts/validate_vendors.py if it intentionally has no E2E requirement."
            )

    # --- Check 2: every vendor in vendors.yaml must have its personality dir ---
    for vendor in vendors:
        pdir = vendor["personality_dir"]
        if pdir in EXCLUDED_PERSONALITY_DIRS:
            continue
        personality_path = PERSONALITIES_DIR / pdir
        if not personality_path.is_dir():
            errors.append(
                f"vendors.yaml references personality_dir='{pdir}' "
                f"but strix_gateway/personalities/{pdir}/ does not exist."
            )

    # --- Check 3: e2e_required vendors must have >= 1 scenario ---
    for vendor in vendors:
        if not vendor.get("e2e_required", True):
            continue
        pdir = vendor["personality_dir"]
        if not vendor.get("scenarios"):
            errors.append(
                f"Vendor '{pdir}' has e2e_required=true but declares no scenarios "
                "in tests/vendors.yaml."
            )

    # --- Check 4: validate scenario + driver files for each e2e_required vendor ---
    for vendor in vendors:
        if not vendor.get("e2e_required", True):
            continue
        pdir = vendor["personality_dir"]
        for scenario_meta in vendor.get("scenarios", []):
            sname = scenario_meta["name"]

            # Check metadata files in tests/e2e/vendors/<pdir>/<sname>/
            scenario_dir = VENDORS_DIR / pdir / sname
            for req in REQUIRED_SCENARIO_FILES:
                if not (scenario_dir / req).exists():
                    errors.append(
                        f"Missing {req} in tests/e2e/vendors/{pdir}/{sname}/"
                    )

            # Read scenario.yaml to resolve driver_dir
            scenario_yaml = scenario_dir / "scenario.yaml"
            if not scenario_yaml.exists():
                continue  # already reported above
            with open(scenario_yaml) as fh:
                s = yaml.safe_load(fh)
            driver_dir = s.get("driver_dir")
            if not driver_dir:
                errors.append(
                    f"scenario.yaml for {pdir}/{sname} is missing required 'driver_dir' key."
                )
                continue

            # Check execution files in tests/e2e/drivers/<driver_dir>/
            driver_path = DRIVERS_DIR / driver_dir
            if not driver_path.is_dir():
                errors.append(
                    f"tests/e2e/drivers/{driver_dir}/ does not exist "
                    f"(declared by {pdir}/{sname}/scenario.yaml)."
                )
                continue
            for req in REQUIRED_DRIVER_FILES:
                if not (driver_path / req).exists():
                    errors.append(
                        f"Missing {req} in tests/e2e/drivers/{driver_dir}/ "
                        f"(required by {pdir}/{sname})."
                    )

    if errors:
        print("VENDOR CONFORMANCE FAILURES:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        print(
            "\nSee docs/testing/vendor_conformance.md for instructions on "
            "adding a new vendor.",
            file=sys.stderr,
        )
        return 1

    vendor_count = sum(1 for v in vendors if v.get("e2e_required", True))
    scenario_count = sum(
        len(v.get("scenarios", []))
        for v in vendors
        if v.get("e2e_required", True)
    )
    print(
        f"OK: {len(manifest_dirs)} vendors declared, "
        f"{vendor_count} require E2E, "
        f"{scenario_count} scenarios validated."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
