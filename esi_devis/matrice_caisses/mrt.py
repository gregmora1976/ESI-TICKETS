from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict
from .excel_engine import ExcelSheetCalculator


class MRTCalculator(ExcelSheetCalculator):
    """Calculateur Python pour l'onglet Excel exact `MRT`."""
    OUTPUT_CELLS = {
        'poids_caisse': 'C27',
        'bilan_carbone': 'E28',
        'minutes_production': 'C28',
        'total_matiere': 'C48',
        'total_mo': 'C49',
        'minutes_mo': 'D49',
        'total_frais_generaux': 'C50',
        'total_revient': 'C51',
        'marge': 'C52',
        'remise_ou_majoration': 'C53',
        'prix_vente': 'C55',
        'dimensions_interieures_longueur': 'C23',
        'dimensions_interieures_epaisseur': 'D23',
        'dimensions_interieures_hauteur': 'E23',
        'dimensions_exterieures_longueur': 'C25',
        'dimensions_exterieures_epaisseur': 'D25',
        'dimensions_exterieures_hauteur': 'E25',
    }

    def __init__(self, overrides: Dict[str, Any] | None = None):
        super().__init__('MRT', overrides)

    def outputs(self) -> Dict[str, Any]:
        return {label: self.cell('MRT', cell) for label, cell in self.OUTPUT_CELLS.items()}


@dataclass
class MRTInputs:
    """Entrées principales de l'onglet MRT.

    Les cellules restent celles de l'onglet Excel :
    C19/D19/E19 pour les dimensions, B19 pour le mode de dimensions,
    F19/G19 pour la face et l'arrière du MRT.
    """
    longueur_cm: float | None = None
    epaisseur_cm: float | None = None
    hauteur_cm: float | None = None
    mode_dimensions: str | None = None
    type_contreplaque: str | None = None
    barres: str | None = None
    fermeture: str | None = None
    poignees: str | None = None
    face_mrt: str | None = None
    arriere_mrt: str | None = None
    responsable_dossier: str | None = None
    client: str | None = None
    categorie: str | None = None
    overrides: Dict[str, Any] = field(default_factory=dict)

    def as_overrides(self) -> Dict[str, Any]:
        data = dict(self.overrides)
        mapping = {
            'type_contreplaque': 'C8',
            'barres': 'C10',
            'fermeture': 'C12',
            'poignees': 'C14',
            'mode_dimensions': 'B19',
            'longueur_cm': 'C19',
            'epaisseur_cm': 'D19',
            'hauteur_cm': 'E19',
            'face_mrt': 'F19',
            'arriere_mrt': 'G19',
            'responsable_dossier': 'F4',
            'client': 'F5',
            'categorie': 'F8',
        }
        for attr, cell in mapping.items():
            val = getattr(self, attr)
            if val is not None:
                data[cell] = val
        return data


OPTIONS_MRT = {
    'mode_dimensions': ["Dimensions de l'oeuvre", 'Dimensions ext MRT existant', 'Aucun'],
    'type_contreplaque': ['Peuplier 15 mm'],
    'barres': ['Pin raboté 4 faces'],
    'fermeture': ['Vis bois'],
    'poignees': ['Métal x 2 sur bouts', 'Métal x 4 sur GC'],
    'face_mrt': ['Mrt Ouvert', 'Mrt Fermé Carton Double', 'Mrt Fermé Carton Triple', 'Mrt Fermé cp 10 mm', 'Mrt Fermé Macrolon 10 mm'],
    'arriere_mrt': ['Mrt Ouvert', 'Mrt Fermé Carton Double', 'Mrt Fermé Carton Triple', 'Mrt Fermé cp 10 mm', 'Mrt Fermé Macrolon 10 mm'],
}


def options_mrt() -> Dict[str, list[str]]:
    return OPTIONS_MRT.copy()


def calculer_mrt(inputs: MRTInputs | None = None, **overrides) -> Dict[str, Any]:
    params = {}
    if inputs:
        params.update(inputs.as_overrides())
    params.update(overrides)
    calc = MRTCalculator(params)
    return calc.outputs()
