from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict
from .excel_engine import ExcelSheetCalculator


class TSeparationsMousseCalculator(ExcelSheetCalculator):
    """Calculateur Python pour l'onglet Excel exact `T Séparations mousse`."""
    OUTPUT_CELLS = {
        'dimensions_interieures_longueur': 'C52',
        'dimensions_interieures_epaisseur': 'D52',
        'dimensions_interieures_hauteur': 'E52',
        'dimensions_exterieures_longueur': 'C54',
        'dimensions_exterieures_epaisseur': 'D54',
        'dimensions_exterieures_hauteur': 'E54',
        'prix_achat': 'G54',
        'prix_vente': 'G54',
        'poids_caisse': 'C56',
        'minutes_production': 'C57',
        'bilan_carbone': 'E57',
    }

    def __init__(self, overrides: Dict[str, Any] | None = None):
        super().__init__('T Séparations mousse', overrides)

    def outputs(self) -> Dict[str, Any]:
        return {label: self.cell('T Séparations mousse', cell) for label, cell in self.OUTPUT_CELLS.items()}


@dataclass
class OeuvreSeparationMousse:
    longueur_cm: float | None = None
    largeur_cm: float | None = None
    hauteur_cm: float | None = None


@dataclass
class TSeparationsMousseInputs:
    type_contreplaque: str | None = None
    barres: str | None = None
    garnissage: str | None = None
    type_isolant: str | None = None
    fermeture: str | None = None
    peinture: str | None = None
    skis: str | None = None
    poignees: str | None = None
    separations: str | None = None
    responsable_dossier: str | None = None
    client: str | None = None
    delai: str | None = None
    rang1: list[OeuvreSeparationMousse] = field(default_factory=list)
    rang2: list[OeuvreSeparationMousse] = field(default_factory=list)
    overrides: Dict[str, Any] = field(default_factory=dict)

    def as_overrides(self) -> Dict[str, Any]:
        data = dict(self.overrides)
        data.setdefault('F9', '-')
        data.setdefault('F10', 'ESI')
        mapping = {
            'responsable_dossier': 'F9',
            'client': 'F10',
            'delai': 'F11',
            'type_contreplaque': 'C13',
            'barres': 'C15',
            'garnissage': 'F15',
            'type_isolant': 'C17',
            'peinture': 'F17',
            'fermeture': 'C19',
            'skis': 'F19',
            'poignees': 'C21',
            'separations': 'F21',
        }
        for attr, cell in mapping.items():
            val = getattr(self, attr)
            if val is not None:
                data[cell] = val
        # Rang 1 : lignes 25 à 34, colonnes D/E/F = Longueur/Largeur/Hauteur.
        for row, item in zip(range(25, 35), self.rang1[:10]):
            if item.longueur_cm is not None: data[f'D{row}'] = item.longueur_cm
            if item.largeur_cm is not None: data[f'E{row}'] = item.largeur_cm
            if item.hauteur_cm is not None: data[f'F{row}'] = item.hauteur_cm
        # Rang 2 : lignes 40 à 49.
        for row, item in zip(range(40, 50), self.rang2[:10]):
            if item.longueur_cm is not None: data[f'D{row}'] = item.longueur_cm
            if item.largeur_cm is not None: data[f'E{row}'] = item.largeur_cm
            if item.hauteur_cm is not None: data[f'F{row}'] = item.hauteur_cm
        return data


OPTIONS_T_SEPARATIONS_MOUSSE = {
    'type_contreplaque': ['Peuplier 10 mm', 'Peuplier 15 mm'],
    'barres': ['Pin non raboté', 'Pin raboté 4 faces'],
    'garnissage': ['Oui', 'Non'],
    'type_isolant': ['Aucun', 'Double isotherme en 30', 'Double isotherme en 50', 'Isotherme en 30', 'Isotherme en 50', 'Super isotherme en 30', 'Super isotherme en 50'],
    'fermeture': ['Vis bois', 'Boulons et platines'],
    'peinture': ['Non', 'Oui'],
    'skis': ['10 dessous en semelles', '10 dessous en skis', 'Skis'],
    'poignees': ['Bois', 'Métal x 2 sur bouts', 'Métal x 4 sur GC'],
    'separations': ['Carton', 'Mousse 30 mm', 'Mousse 50 mm'],
}


def options_t_separations_mousse() -> Dict[str, list[str]]:
    return OPTIONS_T_SEPARATIONS_MOUSSE.copy()


def calculer_t_separations_mousse(inputs: TSeparationsMousseInputs | None = None, **overrides) -> Dict[str, Any]:
    params = {}
    if inputs:
        params.update(inputs.as_overrides())
    params.update(overrides)
    calc = TSeparationsMousseCalculator(params)
    return calc.outputs()
