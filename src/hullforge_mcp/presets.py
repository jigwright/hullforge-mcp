"""Named sets of catalog areas, for picking a kind of work instead of a list.

HULLFORGE_CATALOG_AREAS takes area names, and now also preset names starting
with "@", in any mix: ``@level``, ``@materials,Sequencer``.

Why presets exist: a client that reads the tool list once and never again
(Claude Code is one) cannot use areas switched on mid-session, so whatever a
session needs has to be listed from launch. Choosing seven areas out of
eighty-odd by name, before starting, is a chore people skip, and then they
pay for the full list in every session. A preset is that choice made once.

Core and Changes are always on and are not repeated here.

Area names must match the editor's exactly. They are written out in full
here, not derived, so a renamed area shows up as a warning at startup (see
``unknown_areas``) instead of a preset that quietly lists less than it says.
"""
from __future__ import annotations

from dataclasses import dataclass

PRESET_PREFIX = "@"


@dataclass(frozen=True)
class Preset:
    name: str
    summary: str
    areas: tuple[str, ...]


_BASICS = ("Actors", "Assets", "Properties", "Components")

PRESETS: dict[str, Preset] = {p.name: p for p in (
    Preset(
        "general",
        "Everyday editing: actors, assets, Blueprints, materials and lights. "
        "Measured in hullforge-evals: same pass rate as the full list, a third of the context.",
        _BASICS + ("Blueprints", "Materials", "Lighting"),
    ),
    Preset(
        "level",
        "Building levels: placing and lighting actors, landscape, foliage, meshes, "
        "splines and world partition.",
        _BASICS + ("Lighting", "Landscape", "Foliage", "Meshes", "Materials", "Splines",
                   "Post Process, Sky & Atmosphere", "Viewport", "WorldPartition",
                   "LevelInstances", "Data Layers"),
    ),
    Preset(
        "materials",
        "Materials, material instances and textures.",
        ("Assets", "Properties", "Materials", "Textures", "Meshes"),
    ),
    Preset(
        "blueprints",
        "Blueprint classes, components, input and the game framework.",
        _BASICS + ("Blueprints", "Input", "Game Framework", "GameplayTags"),
    ),
    Preset(
        "gameplay",
        "Gameplay systems: GAS, gameplay tags, AI, StateTree and navigation.",
        _BASICS + ("Blueprints", "GAS", "GameplayTags", "AI", "StateTree", "Navigation",
                   "Input", "Game Framework"),
    ),
    Preset(
        "animation",
        "Animation assets, Control Rig, IK rigs and retargeting.",
        ("Assets", "Properties", "Animation", "Control Rig", "IK Rig & Retargeting", "Meshes"),
    ),
    Preset(
        "cinematics",
        "Sequencer, cameras and rendering movies.",
        ("Actors", "Assets", "Properties", "Sequencer", "Cine Camera", "Camera Rigs",
         "Cine Assembly", "Render Queue", "Lighting", "Post Process, Sky & Atmosphere"),
    ),
    Preset(
        "vfx",
        "Niagara effects and the materials they use.",
        ("Actors", "Assets", "Properties", "Niagara", "Materials", "Textures"),
    ),
    Preset(
        "audio",
        "Sound assets and MetaSounds.",
        ("Actors", "Assets", "Properties", "Audio", "MetaSounds"),
    ),
    Preset(
        "ui",
        "UMG widgets and view models.",
        ("Assets", "Properties", "Blueprints", "UMG", "MVVM", "Input"),
    ),
    Preset(
        "pcg",
        "Procedural content: PCG graphs, splines, foliage and landscape.",
        ("Actors", "Assets", "Properties", "PCG", "Splines", "Foliage", "Landscape", "Meshes"),
    ),
)}


def is_preset(token: str) -> bool:
    return token.startswith(PRESET_PREFIX)


def expand(tokens: list[str] | set[str]) -> tuple[set[str], list[str]]:
    """Area names for these tokens, and any preset names that don't exist.

    Plain tokens pass through as area names. Preset names are matched
    case-insensitively, since "@Level" typed from memory is a typo the
    connector can fix. An unknown preset contributes nothing; the caller
    reports it, because a silent empty preset looks like a broken install.
    """
    areas: set[str] = set()
    unknown: list[str] = []
    for token in tokens:
        if not is_preset(token):
            areas.add(token)
            continue
        preset = PRESETS.get(token[len(PRESET_PREFIX):].strip().lower())
        if preset is None:
            unknown.append(token)
        else:
            areas.update(preset.areas)
    return areas, unknown


def unknown_areas(known: set[str]) -> dict[str, list[str]]:
    """Preset areas the connected editor doesn't have, by preset."""
    return {
        p.name: missing
        for p in PRESETS.values()
        if (missing := [a for a in p.areas if a not in known])
    }
