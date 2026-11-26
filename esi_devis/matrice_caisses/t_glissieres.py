from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict
from .excel_engine import ExcelSheetCalculator


class TGlissieresCalculator(ExcelSheetCalculator):
    """Calculateur Python pour l'onglet Excel exact `T à Glissières`."""
    OUTPUT_CELLS = {
        'dimensions_interieures_longueur': 'C53',
        'dimensions_interieures_epaisseur': 'D53',
        'dimensions_interieures_hauteur': 'E53',
        'dimensions_exterieures_longueur': 'C55',
        'dimensions_exterieures_epaisseur': 'D55',
        'dimensions_exterieures_hauteur': 'E55',
        'poids_caisse': 'C57',
        'minutes_production': 'C58',
        'bilan_carbone': 'E58',
        'prix_vente': 'F55',
        'prix_achat': 'F55',
    }

    def __init__(self, overrides: Dict[str, Any] | None = None):
        super().__init__('T à Glissières', overrides)

    def outputs(self) -> Dict[str, Any]:
        return {label: self.cell('T à Glissières', cell) for label, cell in self.OUTPUT_CELLS.items()}


@dataclass
class TableauGlissiere:
    inventaire: str | None = None
    longueur_cm: float | None = None
    largeur_cm: float | None = None
    hauteur_cm: float | None = None
    quantite: float | None = None
    intervalle: float | None = None


@dataclass
class TGlissieresInputs:
    type_contreplaque: str | None = None
    barres: str | None = None
    garnissage: str | None = None
    type_isolant: str | None = None
    fermeture: str | None = None
    peinture: str | None = None
    poignees: str | None = None
    skis: str | None = None
    epaisseur_mousse: str | None = None
    cuvette_au_dessus: str | None = None
    responsable_dossier: str | None = None
    client: str | None = None
    delai: str | None = None
    tableaux: list[TableauGlissiere] = field(default_factory=list)
    overrides: Dict[str, Any] = field(default_factory=dict)

    def as_overrides(self) -> Dict[str, Any]:
        data = dict(self.overrides)
        mapping = {
            'responsable_dossier': 'F10',
            'client': 'F11',
            'delai': 'F12',
            'type_contreplaque': 'C14',
            'barres': 'C16',
            'garnissage': 'F16',
            'type_isolant': 'C18',
            'fermeture': 'C20',
            'peinture': 'F20',
            'poignees': 'C22',
            'skis': 'F23',
            'epaisseur_mousse': 'C24',
            'cuvette_au_dessus': 'F25',
        }
        for attr, cell in mapping.items():
            val = getattr(self, attr)
            if val is not None:
                data[cell] = val
        # Jusqu'à 16 tableaux / glissières : lignes 32 à 47.
        for i, tab in enumerate(self.tableaux[:16], start=32):
            if tab.inventaire is not None: data[f'C{i}'] = tab.inventaire
            if tab.longueur_cm is not None: data[f'D{i}'] = tab.longueur_cm
            if tab.largeur_cm is not None: data[f'E{i}'] = tab.largeur_cm
            if tab.hauteur_cm is not None: data[f'F{i}'] = tab.hauteur_cm
            if tab.quantite is not None: data[f'G{i}'] = tab.quantite
            if tab.intervalle is not None: data[f'H{i}'] = tab.intervalle
        return data


OPTIONS_T_GLISSIERES = {
    'type_contreplaque': ['Peuplier 10 mm', 'Peuplier 15 mm'],
    'barres': ['Pin non raboté', 'Pin raboté 4 faces'],
    'garnissage': ['Oui', 'Non'],
    'type_isolant': ['Aucun', 'Double isotherme en 30', 'Double isotherme en 50', 'Isotherme en 30', 'Isotherme en 50', 'Super isotherme en 30', 'Super isotherme en 50'],
    'fermeture': ['Vis bois', 'Boulons et platines'],
    'peinture': ['Non', 'Oui'],
    'poignees': ['Bois', 'Métal x 2 sur bouts', 'Métal x 4 sur GC'],
    'skis': ['Skis', '10 Dessous en Semelles', '10 Dessous en Skis'],
    'epaisseur_mousse': ['Mousse 30 mm', 'Mousse 50 mm', 'Mousse 80 mm'],
    'cuvette_au_dessus': ['Non', 'Oui'],
}


def options_t_glissieres() -> Dict[str, list[str]]:
    return OPTIONS_T_GLISSIERES.copy()


def calculer_t_glissieres(inputs: TGlissieresInputs | None = None, **overrides) -> Dict[str, Any]:
    params = {}
    if inputs:
        params.update(inputs.as_overrides())
    params.update(overrides)
    calc = TGlissieresCalculator(params)
    return calc.outputs()
