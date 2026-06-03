"""Minimal i18n for the web UI — nav + dashboard headings only.

This is intentionally narrow. A full Jinja-Babel pipeline is overkill
when the visible surface is ~30 strings: nav links, page titles,
empty-state lines, and the wizard step labels. The current shape:

  - `STRINGS[lang][key]` → str
  - Jinja env globals expose `t(key)` and the active `lang`
  - English (`en`) is the source of truth; missing translations fall
    back to the English string so a new feature never crashes the UI.

When the surface grows past ~80 strings, switch to a real catalog.
"""

from __future__ import annotations

from typing import Final

# `en` keys are the canonical IDs. Add new strings here first; if a
# translation isn't ready the fallback in `t()` returns the English text.
STRINGS: Final[dict[str, dict[str, str]]] = {
    "en": {
        # nav
        "nav.dashboard": "Dashboard",
        "nav.notes": "Notes",
        "nav.diagnostics": "Diagnostics",
        "nav.audit": "Audit",
        "nav.interview": "Interview",
        "nav.usage": "Usage",
        "nav.llm_profiles": "LLM Profiles",
        "nav.settings": "Settings",
        # dashboard
        "dash.title": "Updates",
        "dash.empty": "No updates yet. Trigger a scan to populate this view.",
        "dash.scan_now": "Scan now",
        "dash.severity_critical": "Critical",
        "dash.severity_high": "High",
        "dash.severity_medium": "Medium",
        "dash.severity_info": "Info",
        # status verbs
        "status.apply": "Apply",
        "status.dismiss": "Dismiss",
        "status.applied": "Applied",
        "status.dismissed": "Dismissed",
        # audit
        "audit.title": "Audit",
        "audit.healthy": "All clear. No actionable findings.",
        # diagnostics
        "diag.title": "What HomelabSage sees",
        "diag.verdict": "Verdict",
        "diag.notes": "Notes",
        # wizard
        "wizard.step1": "Pick an LLM",
        "wizard.step2": "Enable sources",
        "wizard.step3": "Optional outputs",
        "wizard.next": "Next",
        "wizard.finish": "Finish",
        # generic
        "generic.save": "Save",
        "generic.cancel": "Cancel",
        "generic.loading": "Loading…",
    },
    "es": {
        "nav.dashboard": "Panel",
        "nav.notes": "Notas",
        "nav.diagnostics": "Diagnóstico",
        "nav.audit": "Auditoría",
        "nav.interview": "Entrevista",
        "nav.usage": "Uso",
        "nav.llm_profiles": "Perfiles LLM",
        "nav.settings": "Ajustes",
        "dash.title": "Actualizaciones",
        "dash.empty": "Aún no hay actualizaciones. Lanza un escaneo para poblar esta vista.",
        "dash.scan_now": "Escanear ahora",
        "dash.severity_critical": "Crítica",
        "dash.severity_high": "Alta",
        "dash.severity_medium": "Media",
        "dash.severity_info": "Informativa",
        "status.apply": "Aplicar",
        "status.dismiss": "Descartar",
        "status.applied": "Aplicada",
        "status.dismissed": "Descartada",
        "audit.title": "Auditoría",
        "audit.healthy": "Todo en orden. No hay hallazgos accionables.",
        "diag.title": "Lo que HomelabSage ve",
        "diag.verdict": "Veredicto",
        "diag.notes": "Notas",
        "wizard.step1": "Elige un LLM",
        "wizard.step2": "Activa fuentes",
        "wizard.step3": "Salidas opcionales",
        "wizard.next": "Siguiente",
        "wizard.finish": "Terminar",
        "generic.save": "Guardar",
        "generic.cancel": "Cancelar",
        "generic.loading": "Cargando…",
    },
}


def supported_langs() -> list[str]:
    return sorted(STRINGS.keys())


def translator(lang: str):
    """Return a `t(key) -> str` closure bound to `lang`.

    Unknown lang collapses to English (with a debug log line). Missing key
    in the chosen lang falls back to English; missing in English too
    returns the raw key so a typo is loud rather than silent (`"nav.foo"`
    on the page is very findable).
    """
    table = STRINGS.get(lang, STRINGS["en"])
    fallback = STRINGS["en"]

    def t(key: str) -> str:
        if key in table:
            return table[key]
        if key in fallback:
            return fallback[key]
        return key

    return t
