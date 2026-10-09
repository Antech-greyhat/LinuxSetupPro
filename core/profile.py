"""Reads and writes install profiles: the categories, tools, editors, and theme a run selected.

A profile is install selections only. The banner generator's style/name/handle is a per-device
cosmetic choice and is never captured here.
"""

from collections import namedtuple

import yaml

from core import installer

PROFILE_VERSION = 1

# Shell prompt themes a profile may name; mirrors the customization theme picker's options.
_SHELL_THEMES = ("starship", "oh-my-zsh")


class ProfileError(Exception):
    """A profile file is malformed, unreadable, or names something no longer in the catalog."""


# The customization tail's selections. shell_theme is one of _SHELL_THEMES or None (no theme).
Customization = namedtuple("Customization", "editors shell_theme install_fish")

# A validated profile ready to drive a silent install. primary_categories holds catalog category
# names (never the customization pseudo-category), which run_customization tracks separately.
Profile = namedtuple("Profile", "primary_categories run_customization customization secondary_selections")


def from_selections(catalog, selections):
    """Build a Profile from the state tracked during a run. Raises ProfileError on stale names."""
    return _from_mapping(catalog, selections or {})


def load_profile(path, catalog):
    """Read and validate a profile file. Raises ProfileError on bad YAML or unknown names."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except OSError as exc:
        raise ProfileError(f"could not read {path}: {exc}")
    except yaml.YAMLError as exc:
        raise ProfileError(f"{path} is not valid YAML: {exc}")
    if raw is None:
        raise ProfileError(f"{path} is empty")
    return _from_mapping(catalog, raw)


def export_profile(profile, path):
    """Write a profile to path as YAML, in the block style used by the tool's other config."""
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(profile_to_dict(profile), fh, sort_keys=False, default_flow_style=False)


def profile_to_dict(profile):
    """Serialize a Profile to the on-disk mapping, categories written as slugs."""
    primary = [installer.category_slug(name) for name in profile.primary_categories]
    if profile.run_customization:
        primary.append(installer.CUSTOMIZATION)
    data = {"profile_version": PROFILE_VERSION, "primary_categories": primary}
    if profile.run_customization:
        custom = profile.customization
        data["customization"] = {
            "editors": list(custom.editors),
            "shell_theme": custom.shell_theme,
            "install_fish": bool(custom.install_fish),
        }
    data["secondary_selections"] = list(profile.secondary_selections)
    return data


def _from_mapping(catalog, mapping):
    """Validate a profile mapping (from a file or tracked state) into a Profile."""
    if not isinstance(mapping, dict):
        raise ProfileError("profile must be a mapping of keys to values")
    version = mapping.get("profile_version", PROFILE_VERSION)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1 or version > PROFILE_VERSION:
        raise ProfileError(f"unsupported profile_version {version!r}; "
                           f"this build understands version {PROFILE_VERSION}")

    primary_names, run_customization = _resolve_primary(catalog, mapping.get("primary_categories") or [])
    customization = _resolve_customization(catalog, mapping.get("customization") or {})
    secondary = _resolve_secondary(catalog, mapping.get("secondary_selections") or [])
    return Profile(primary_names, run_customization, customization, secondary)


def _resolve_primary(catalog, tokens):
    """Resolve primary_categories slugs to catalog names, splitting out the customization pseudo."""
    if not isinstance(tokens, list):
        raise ProfileError("primary_categories must be a list")
    order = installer.primary_category_names(catalog)
    try:
        matched = installer.resolve_category_tokens(catalog, [str(t) for t in tokens], names=order)
    except installer.CategoryError as exc:
        raise ProfileError(str(exc))
    run_customization = installer.CUSTOMIZATION in matched
    ordered = [name for name in order if name in matched and name != installer.CUSTOMIZATION]
    return ordered, run_customization


def _resolve_customization(catalog, block):
    """Validate the customization block: editors and theme must exist in the catalog."""
    if not isinstance(block, dict):
        raise ProfileError("customization must be a mapping")
    valid_editors = {e["name"] for e in catalog.get("customization", {}).get("editors", [])}
    editors = block.get("editors") or []
    if not isinstance(editors, list):
        raise ProfileError("customization.editors must be a list")
    for name in editors:
        if not isinstance(name, str):
            raise ProfileError("customization.editors entries must be strings")
        if name not in valid_editors:
            raise ProfileError(f"unknown editor {name!r} in profile; not in the catalog")
    theme = block.get("shell_theme")
    if theme is not None and not isinstance(theme, str):
        raise ProfileError("customization.shell_theme must be a string or null")
    if theme is not None and theme not in _SHELL_THEMES:
        raise ProfileError(f"unknown shell_theme {theme!r}; expected one of {', '.join(_SHELL_THEMES)}")
    install_fish = block.get("install_fish", False)
    if not isinstance(install_fish, bool):
        raise ProfileError("customization.install_fish must be true or false")
    return Customization(list(editors), theme, install_fish)


def _resolve_secondary(catalog, names):
    """Validate secondary_selections: every name must still exist in the secondary catalog."""
    if not isinstance(names, list):
        raise ProfileError("secondary_selections must be a list")
    valid = {entry["name"] for _, entry in installer.flatten_secondary(catalog)}
    for name in names:
        if not isinstance(name, str):
            raise ProfileError("secondary_selections entries must be strings")
        if name not in valid:
            raise ProfileError(f"unknown package {name!r} in secondary_selections; not in the catalog")
    return list(names)
