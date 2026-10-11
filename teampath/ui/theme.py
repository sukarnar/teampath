"""Theme derived from each organization's brand color."""

from __future__ import annotations

import flet as ft

DEFAULT_SEED = "#3355CC"

STATUS_COLORS = {
    # momentum status -> (fill, text) ; paired with icons/labels so color is never the only cue
    "active": ("#2E7D5B", "#FFFFFF"),
    "slowing": ("#C98A12", "#1F1400"),
    "inactive": ("#8A8F98", "#FFFFFF"),
    "new": ("#4F6BD8", "#FFFFFF"),
}
STATUS_ICONS = {
    "active": ft.Icons.TRENDING_UP,
    "slowing": ft.Icons.TRENDING_FLAT,
    "inactive": ft.Icons.TRENDING_DOWN,
    "new": ft.Icons.FIBER_NEW_OUTLINED,
}


def apply_theme(page: ft.Page, seed: str | None) -> None:
    seed = seed or DEFAULT_SEED
    page.theme = ft.Theme(color_scheme_seed=seed, use_material3=True,
                          visual_density=ft.VisualDensity.COMFORTABLE)
    page.dark_theme = ft.Theme(color_scheme_seed=seed, use_material3=True,
                               visual_density=ft.VisualDensity.COMFORTABLE)
    page.theme_mode = ft.ThemeMode.SYSTEM
