from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import ast
import operator


DEFAULT_TYPES_CAISSE = [
    "PLEINE CP TYPE 15",
    "PLEINE CP TYPE 16",
    "PLEINE BOIS TYPE 15",
    "PLEINE BOIS TYPE 16",
]

DEFAULT_PRICES = {
    "cp_m2": 45.0,
    "barres_ml": 4.0,
    "chevrons_ml": 6.0,
    "consommables": 15.0,
    "taux_horaire": 45.0,
    "heures": 2.0,
    "frais_generaux": 10.0,
    "marge": 20.0,
}


@dataclass
class CaisseDecoInputs:
    longueur_cm: float | None = None
    largeur_cm: float | None = None
    hauteur_cm: float | None = None

    type_caisse: str = "PLEINE CP TYPE 16"

    numero_dossier: str | None = None
    numero_colis: str | None = None
    client: str | None = None
    charge_projet: str | None = None
    observations: str | None = None


def _num(value: Any) -> float:
    try:
        return float(str(value).replace(",", "."))
    except Exception:
        return 0.0


def _r1(value: float) -> float:
    return round(float(value), 1)


def options_caisse_deco() -> dict:
    """
    Options exposées à l'interface ESI-DEVI(S).
    Les tarifs peuvent être remplacés côté app.py par les valeurs
    stockées dans Supabase si souhaité.
    """
    return {
        "type_caisse": list(DEFAULT_TYPES_CAISSE),
        "prices": dict(DEFAULT_PRICES),
    }


def safe_eval_formula(expr: Any, variables: dict[str, Any]) -> float:
    """
    Evalue une formule simple compatible avec les modèles ESI-CAISSERIE.
    Variables autorisées : L, W, H et variables de modèle.
    Opérations : + - * / et parenthèses.
    """
    expr = str(expr or "0").strip().replace(",", ".")
    if not expr:
        return 0.0

    allowed_names = {k: _num(v) for k, v in variables.items()}
    allowed_ops = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.USub: operator.neg,
        ast.UAdd: operator.pos,
    }

    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)

        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)

        if isinstance(node, ast.Name):
            if node.id not in allowed_names:
                raise ValueError(f"Variable inconnue : {node.id}")
            return allowed_names[node.id]

        if isinstance(node, ast.BinOp):
            op = type(node.op)
            if op not in allowed_ops:
                raise ValueError("Opérateur non autorisé")
            return allowed_ops[op](_eval(node.left), _eval(node.right))

        if isinstance(node, ast.UnaryOp):
            op = type(node.op)
            if op not in allowed_ops:
                raise ValueError("Opérateur non autorisé")
            return allowed_ops[op](_eval(node.operand))

        raise ValueError("Formule non autorisée")

    tree = ast.parse(expr, mode="eval")
    return float(_eval(tree))


def compute_debit_from_model(
    inputs: CaisseDecoInputs,
    model: dict,
    lines: list[dict],
) -> dict:
    """
    Calcule un débit à partir d'un modèle de caisse paramétrable
    provenant éventuellement de Supabase.
    """
    variables = dict(model.get("variables") or {})
    variables.update({
        "L": _num(inputs.longueur_cm),
        "W": _num(inputs.largeur_cm),
        "H": _num(inputs.hauteur_cm),
    })

    out_lines = []
    max_l = variables["L"]
    max_w = variables["W"]
    max_h = variables["H"]

    for line in lines:
        qte = safe_eval_formula(
            line.get("formule_quantite") or line.get("quantite") or "1",
            variables,
        )
        longueur = safe_eval_formula(line.get("formule_longueur") or "0", variables)
        largeur = safe_eval_formula(line.get("formule_largeur") or "0", variables)
        epaisseur = safe_eval_formula(line.get("formule_epaisseur") or "0", variables)

        out_lines.append({
            "famille": line.get("famille") or "",
            "piece": line.get("piece") or "",
            "quantite": _r1(qte),
            "longueur": _r1(longueur),
            "largeur": _r1(largeur),
            "epaisseur": _r1(epaisseur),
        })

        max_l = max(max_l, longueur)
        max_w = max(max_w, largeur)

    try:
        ext_l = (
            safe_eval_formula(variables.get("EXT_L"), variables)
            if variables.get("EXT_L")
            else max_l
        )
        ext_w = (
            safe_eval_formula(variables.get("EXT_W"), variables)
            if variables.get("EXT_W")
            else max_w
        )
        ext_h = (
            safe_eval_formula(variables.get("EXT_H"), variables)
            if variables.get("EXT_H")
            else max_h
        )
    except Exception:
        ext_l, ext_w, ext_h = max_l, max_w, max_h

    return {
        "ok": True,
        "dims_ext": {
            "longueur": _r1(ext_l),
            "largeur": _r1(ext_w),
            "hauteur": _r1(ext_h),
        },
        "lignes": out_lines,
    }


def compute_debit(
    inputs: CaisseDecoInputs,
    model: dict | None = None,
    lines: list[dict] | None = None,
) -> dict:
    """
    Reprend la logique ESI-CAISSERIE.

    - Si un modèle + ses lignes sont fournis : calcul paramétrable.
    - Sinon : fallback historique sur PLEINE CP TYPE 16.
    """
    if model and lines:
        return compute_debit_from_model(inputs, model, lines)

    type_caisse = (inputs.type_caisse or "").strip().upper()

    if type_caisse not in ("PLEINE CP TYPE 16", "PLEIN CP TYPE 16"):
        return {
            "ok": False,
            "message": "Débit automatique non paramétré pour ce type de caisse.",
            "dims_ext": {},
            "lignes": [],
        }

    L = _num(inputs.longueur_cm)
    W = _num(inputs.largeur_cm)
    H = _num(inputs.hauteur_cm)

    # Constantes historiques ESI-CAISSERIE
    B4 = 10
    C4 = 1.5
    D4 = 4
    E4 = 1.5
    C5 = 1
    E5 = 2.7

    dims_ext = {
        "longueur": _r1(L + E4 + E4 + E5 + E5),
        "largeur": _r1(W + E4 + E4 + E5 + E5),
        "hauteur": _r1(H + E4 + E4 + E5 + B4),
    }

    cover_l = _r1(L + E5 + E5 + C5 + C5)
    cover_w = _r1(W + E5 + E5 + C5 + C5)
    cote_h = _r1(H + C4 + D4)

    lignes = [
        {"famille": "CP", "piece": "PLATEAU", "quantite": 1, "longueur": L, "largeur": W, "epaisseur": C4},
        {"famille": "CP", "piece": "COUVERCLE", "quantite": 1, "longueur": cover_l, "largeur": cover_w, "epaisseur": C5},
        {"famille": "CP", "piece": "COTES", "quantite": 2, "longueur": cover_l, "largeur": cote_h, "epaisseur": C5},
        {"famille": "CP", "piece": "BOUTS", "quantite": 2, "longueur": W, "largeur": cote_h, "epaisseur": C5},
        {"famille": "BARRES", "piece": "SEMELLES", "quantite": 4, "longueur": cover_w, "largeur": 5, "epaisseur": B4},
        {"famille": "BARRES", "piece": "CHEMINS EXT", "quantite": 3, "longueur": L, "largeur": 6, "epaisseur": D4},
        {"famille": "BARRES", "piece": "BARRES L COUV", "quantite": 2, "longueur": cover_l, "largeur": B4, "epaisseur": E5},
        {"famille": "BARRES", "piece": "BARRES L COTES", "quantite": 4, "longueur": cover_l, "largeur": B4, "epaisseur": E5},
        {"famille": "BARRES", "piece": "BARRES L", "quantite": 4, "longueur": L, "largeur": B4, "epaisseur": E5},
        {"famille": "BARRES", "piece": "BARRES H", "quantite": 4, "longueur": _r1(cote_h - 20), "largeur": B4, "epaisseur": E5},
    ]

    return {
        "ok": True,
        "dims_ext": dims_ext,
        "lignes": lignes,
    }


def material_totals(debit: dict) -> dict:
    totals = {
        "cp_m2": 0.0,
        "barres_ml": 0.0,
        "chevrons_ml": 0.0,
        "autres": 0.0,
    }

    for line in debit.get("lignes", []):
        famille = (line.get("famille") or "").upper()
        q = _num(line.get("quantite"))
        lo = _num(line.get("longueur"))
        la = _num(line.get("largeur"))

        if famille == "CP":
            totals["cp_m2"] += q * lo * la / 10000
        elif famille == "BARRES":
            totals["barres_ml"] += q * lo / 100
        elif famille == "CHEVRONS":
            totals["chevrons_ml"] += q * lo / 100
        else:
            totals["autres"] += q

    return {k: round(v, 2) for k, v in totals.items()}


def calculer_caisse_deco(
    inputs: CaisseDecoInputs,
    prices: dict | None = None,
    model: dict | None = None,
    lines: list[dict] | None = None,
) -> dict:
    """
    Calcule le prix d'une caisse déco.

    Retour compatible avec ESI-DEVIS :
    - dimensions_exterieures_longueur
    - dimensions_exterieures_epaisseur
    - dimensions_exterieures_hauteur
    - prix_vente
    - prix_achat
    - prix_cession
    - détails matière / coût
    """
    prices_final = dict(DEFAULT_PRICES)
    if prices:
        prices_final.update(prices)

    debit = compute_debit(inputs, model=model, lines=lines)

    if not debit.get("ok"):
        raise ValueError(debit.get("message") or "Calcul caisse déco impossible.")

    mat = material_totals(debit)

    cp = mat["cp_m2"] * _num(prices_final.get("cp_m2"))
    barres = mat["barres_ml"] * _num(prices_final.get("barres_ml"))
    chevrons = mat["chevrons_ml"] * _num(prices_final.get("chevrons_ml"))
    consommables = _num(prices_final.get("consommables"))
    main_oeuvre = _num(prices_final.get("heures")) * _num(prices_final.get("taux_horaire"))

    sous_total = cp + barres + chevrons + consommables + main_oeuvre
    frais = sous_total * _num(prices_final.get("frais_generaux")) / 100
    prix_achat = sous_total + frais
    marge = prix_achat * _num(prices_final.get("marge")) / 100
    prix_cession = prix_achat + marge

    dims = debit.get("dims_ext") or {}

    return {
        "dimensions_exterieures_longueur": dims.get("longueur"),
        "dimensions_exterieures_epaisseur": dims.get("largeur"),
        "dimensions_exterieures_hauteur": dims.get("hauteur"),

        # ESI-DEVIS utilise prix_vente comme base "prix achat"
        # avant application éventuelle de sa marge globale.
        "prix_vente": round(prix_achat, 2),
        "prix_achat": round(prix_achat, 2),
        "prix_cession": round(prix_cession, 2),

        "matieres": mat,
        "detail": {
            "cp": round(cp, 2),
            "barres": round(barres, 2),
            "chevrons": round(chevrons, 2),
            "consommables": round(consommables, 2),
            "main_oeuvre": round(main_oeuvre, 2),
            "frais": round(frais, 2),
            "marge": round(marge, 2),
        },
        "debit": debit.get("lignes", []),
        "type_caisse": inputs.type_caisse,
    }
