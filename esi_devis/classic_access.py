"""Server-side scope of the existing simplified chiffrage forms."""
from . import app as legacy

CLASSIC = frozenset(('T1','T1-T6','MRT','Objet 1','T à Glissières','Tapisserie','Wrapp','1 Caisson Objet','Caisson Tableaux','Caisse déco'))


def pick(data, keys):
    return {k: data[k] for k in keys.split() if k in data}


def choice(value, options):
    if value not in options:
        raise ValueError('Option hors mode classique')
    return value


def rows(data, key, limit, keys):
    items = data.get(key, [])
    if not isinstance(items, list) or len(items) > limit or any(not isinstance(x, dict) for x in items):
        raise ValueError('Liste invalide')
    return [pick(x, keys) for x in items]


def classic_data(sheet, data):
    if sheet not in CLASSIC or not isinstance(data, dict):
        raise ValueError('Typologie réservée au mode EXPERT')
    out = pick(data, 'client demandeur dossier')
    if sheet == 'Caisse déco':
        return pick(data, 'numero_dossier numero_colis client charge_projet type_caisse longueur largeur hauteur observations')
    if sheet in ('T1','T1-T6','Objet 1','T à Glissières'):
        options = {'T1':legacy.options_t1,'T1-T6':legacy.options_t1_t6,'Objet 1':legacy.options_objet1,'T à Glissières':legacy.options_t_glissieres}[sheet]()
        for key in ('type_isolant','fermeture','peinture') + (('tyvek',) if sheet in ('T1','T1-T6') else ()):
            if key in data:
                out[key] = choice(data[key], options[key])
    fixed = dict(type_contreplaque='Peuplier 15 mm',barres='Pin raboté 4 faces',poignees='Bois',garnissage='Oui')
    if sheet == 'T1':
        out.update(pick(data,'longueur_cm epaisseur_cm hauteur_cm'), **fixed, skis='Skis',type_calage='Bandes de mousse',option_calage='Aucun',epaisseur_mousse='PU 50 mm')
    elif sheet == 'T1-T6':
        out['option_calage']='Aucun'
        out['oeuvres']=rows(data,'oeuvres',6,'longueur_cm epaisseur_cm hauteur_cm type_calage')
        for item in out['oeuvres']:
            if 'type_calage' in item: choice(item['type_calage'], ('Aucun','Bandes de mousse','Sous verre 2 faces'))
    elif sheet == 'Objet 1':
        out.update(pick(data,'longueur_cm largeur_cm hauteur_cm'), **fixed, skis='10 dessous en skis',cuvette_au_dessus='Non',option='Aucun',nombre_cote_ouvrant='GC + Couvercle',plateau_interieur='Aucun',base='Stratocell 50 mm',nombre_calages='2',type_mousse='PU',type_mousse_calage='Stratocell 35 Kg')
        out['type_calage']=choice(data.get('type_calage','Ecrin horizontal'), ('Aucun','Mousse au Carré 50 mm','Ecrin horizontal','Guillotines horizontales'))
    elif sheet == 'MRT':
        v=choice(data.get('face_mrt','Mrt Ouvert'), ('Mrt Ouvert','Mrt Fermé cp 10 mm'))
        out.update(pick(data,'longueur_cm epaisseur_cm hauteur_cm'),mode_dimensions="Dimensions de l'oeuvre",type_contreplaque='Peuplier 15 mm',barres='Pin raboté 4 faces',fermeture='Vis bois',poignees='Métal x 2 sur bouts',type_isolant='Aucun',tyvek='Non',face_mrt=v,arriere_mrt=v)
    elif sheet == 'T à Glissières':
        out.update(fixed,skis='10 dessous en skis',epaisseur_mousse='Mousse 30 mm',cuvette_au_dessus='Non',tyvek='Non')
        out['tableaux']=rows(data,'tableaux',15,'longueur_cm largeur_cm hauteur_cm quantite')
        for item in out['tableaux']: item['intervalle']='2.2'
    else:
        fields={f['cell']:f for group in legacy.engine2026.schemas()[sheet] for f in group['fields']}
        src=data.get('cellules',{})
        if not isinstance(src,dict): raise ValueError('Cellules invalides')
        cells={k:str(f['default']) for k,f in fields.items()}
        visible={
          'Tapisserie':'C17 C19 F19 C24', 'Wrapp':'C22 D22 E22 C23',
          '1 Caisson Objet':'C21 D21 E21 C22', 'Caisson Tableaux':'B25 C25 D25'}[sheet]
        cells.update(pick(src,visible))
        if sheet=='Tapisserie':
            cells.update(C13='Peuplier 15 mm',C15='Pin raboté 4 faces',C21='Bois',F15='Oui',F21='10 dessous en skis',D24='40',F24='40',C26='Guillotines ethafoam',F26='Non',C31='',D31='',F31='15',C33='Aucun',F33='Non')
        elif sheet=='Wrapp':
            v=choice(cells['C23'],('Aucun','Mousse au Carré 50 mm'))
            cells['E23']='PU 30 mm' if v=='Aucun' else 'PU 50 mm'
        elif sheet=='1 Caisson Objet':
            v=choice(cells['C22'],('Aucun','Entourage 50 mm ','Ecrin horizontal','Guillotines horizontales'))
            cells.update(C13='Peuplier 15 mm',C15='Vis bois',C17='métal sur bouts',E25='Non',C24='Aucun',C25='Stratocell 50 mm',E24='Stratocell 35 Kg',C26='Cp 15 mm',E22='2' if v=='Guillotines horizontales' else 'PU')
        else:
            cells.update(C13='Peuplier 15 mm',C17='Vis bois',C19='Métal x 2 sur bouts',F15='Non',F13='Non',F14='PU 50 mm',B26='Bandes de mousse',H25='',I25='',J25='',H26='Aucun')
        out['cellules']=cells
        # Preserve display metadata derived from the allowed cells, not arbitrary input.
        for key,label in [('type_isolant','Isolant'),('type_calage','Calage'),('longueur_cm','Longueur (cm)'),('epaisseur_cm','Épaisseur / largeur (cm)'),('hauteur_cm','Hauteur (cm)')]:
            for a,f in fields.items():
                if f['label']==label and cells[a] and (key!='type_calage' or cells[a]!='Aucun'):
                    out[key]=cells[a]; break
    return out
