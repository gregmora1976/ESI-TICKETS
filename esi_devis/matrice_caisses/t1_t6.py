from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict
from .excel_engine import ExcelSheetCalculator
from .t1 import OPTIONS_T1


class T1T6Calculator(ExcelSheetCalculator):
    """Calculateur Python pour l'onglet Excel exact `T1-T6`."""
    OUTPUT_CELLS = {
        'dimensions_exterieures_longueur': 'C79',
        'dimensions_exterieures_epaisseur': 'D79',
        'dimensions_exterieures_hauteur': 'E79',
        'poids_caisse': 'C81',
        'minutes_production': 'C82',
        'total_matiere': 'C237',
        'total_mo': 'C238',
        'total_frais_generaux': 'C239',
        'total_revient': 'C240',
        'marge': 'C241',
        'prix_vente': 'C244',
    }

    def __init__(self, overrides: Dict[str, Any] | None = None):
        super().__init__('T1-T6', overrides)

    def outputs(self) -> Dict[str, Any]:
        return {label: self.cell('T1-T6', cell) for label, cell in self.OUTPUT_CELLS.items()}


@dataclass
class OeuvreT1T6:
    longueur_cm: float | None = None
    epaisseur_cm: float | None = None
    hauteur_cm: float | None = None
    type_calage: str | None = None


@dataclass
class T1T6Inputs:
    """Entrées principales de l'onglet T1-T6.

    L'onglet accepte jusqu'à 6 oeuvres. Les cellules sont conservées comme dans Excel :
    oeuvre 1 = B29/C29/D29 + B30, oeuvre 2 = H29/I29/J29 + H30, etc.
    """
    type_isolant: str | None = None
    fermeture: str | None = None
    peinture: str | None = None
    tyvek: str | None = None
    type_contreplaque: str | None = None
    garnissage: str | None = None
    option_calage: str | None = None
    oeuvres: list[OeuvreT1T6] = field(default_factory=list)
    overrides: Dict[str, Any] = field(default_factory=dict)

    def as_overrides(self) -> Dict[str, Any]:
        data = dict(self.overrides)
        mapping = {
            'type_isolant': 'C17',
            'fermeture': 'C19',
            'peinture': 'F19',
            'tyvek': 'F21',
            'type_contreplaque': 'C13',
            'garnissage': 'F15',
            'option_calage': 'F23',
        }
        for attr, cell in mapping.items():
            val = getattr(self, attr)
            if val is not None:
                data[cell] = val
        cells = [
            ('B29','C29','D29','B30'),
            ('H29','I29','J29','H30'),
            ('B44','C44','D44','B45'),
            ('H44','I44','J44','H45'),
            ('B60','C60','D60','B61'),
            ('H60','I60','J60','H61'),
        ]
        for oeuvre, (long_c, ep_c, haut_c, cal_c) in zip(self.oeuvres, cells):
            if oeuvre.longueur_cm is not None: data[long_c] = oeuvre.longueur_cm
            if oeuvre.epaisseur_cm is not None: data[ep_c] = oeuvre.epaisseur_cm
            if oeuvre.hauteur_cm is not None: data[haut_c] = oeuvre.hauteur_cm
            if oeuvre.type_calage is not None: data[cal_c] = oeuvre.type_calage
        return data


OPTIONS_T1_T6 = OPTIONS_T1.copy()


def options_t1_t6() -> Dict[str, list[str]]:
    return OPTIONS_T1_T6.copy()


def calculer_t1_t6(inputs: T1T6Inputs | None = None, **overrides) -> Dict[str, Any]:
    params = {}
    if inputs:
        params.update(inputs.as_overrides())
    params.update(overrides)
    calc = T1T6Calculator(params)
    return calc.outputs()
