"""Application adapter for the 2026 workbook; all requests use isolated engines."""
from .excel_2026 import Engine, workbook, CalculationError
from functools import lru_cache
import math, re

# interior row, exterior row, price, weight, minutes, carbon (only where present)
OUTPUTS = {
'T1':(44,46,'F46','C48','C49','E49'), 'T1-T6':(77,79,'F79','C81','C82','E82'),
'T1-T6 + Caissons':(78,80,'F80','C82','C83','E83'), 'T1-T3 MRT':(39,41,'F41','C43','C44','E44'),
'MRT':(23,25,'F25','C27','C28','E28'), 'T1 LOUVRE':(41,43,'F43','C45','C46','E46'),
'T1-T6 LOUVRE':(78,80,'F80','C82','C83','E83'), 'T à Glissières':(53,55,'F55','C57','C58','E58'),
'T Séparations mousse':(52,54,'G54','C56','C57','E57'), 'Objet 1':(42,44,'G44','C46','C47',None),
'Objet 2 à 6':(56,58,'G58','C60','C61',None), 'Caisse Cuvette':(44,46,'G46','C48','C49',None),
'Tapisserie':(37,39,'G39','C41','C42',None), 'Claire-Voie':(33,35,'G35','C37','C38',None),
'Wrapp':(27,29,'G29','C31','C32',None), 'Palette':(None,23,'G24','C25','C26',None),
'Caisson Tableaux':(('B35','C35','D35'),('H35','I35','J35'),'F17','B37','B38','D38'),
'1 Caisson Objet':(27,28,'F30','C30','C31',None), '6 Caissons Objets':(None,None,'I47','F47','F48',None),
'Vente de Matière':(None,None,None,None,None,None)}
LEGACY={'T1','T1-T6','T1-T3 MRT','MRT','T à Glissières','T Séparations mousse','Objet 1','Objet 2 à 6'}
NEW=set(OUTPUTS)-LEGACY

def numeric(v):
    if v is None or v=='': return 0
    try: n=float(str(v).replace(',','.'))
    except (ValueError,TypeError): raise CalculationError('Valeur numérique invalide')
    if not math.isfinite(n) or n<0: raise CalculationError('Valeur numérique négative ou invalide')
    return n

def result(sheet, overrides, materials):
    e=Engine(sheet,overrides,{k.split('!')[-1]:v for k,v in materials.items()})
    def read(a):
        x=e.cell(sheet,a)
        if not isinstance(x,(int,float)) or not math.isfinite(x) or x<0: raise CalculationError(f'Résultat invalide : {sheet} {a}')
        return x
    inner,outer,price,weight,minutes,carbon=OUTPUTS[sheet]; out={}
    for label, row in [('interieures',inner),('exterieures',outer)]:
        if row:
            addresses=tuple(c+str(row) for c in 'CDE') if isinstance(row,int) else row
            for axis,a in zip(('longueur','epaisseur','hauteur'),addresses):out[f'dimensions_{label}_{axis}']=read(a)
    if sheet=='Vente de Matière':
        price_value=sum(read('R'+str(r)) for start in (6,24,42,60) for r in range(start,start+12))
    else: price_value=read(price)
    if sheet=='6 Caissons Objets':
        caissons=[]
        for i,(col,row) in enumerate((('C',18),('C',28),('C',38),('H',18),('H',28),('H',38)),1):
            if numeric(overrides.get(col+str(row),0)):
                cols='CDE' if col=='C' else 'HIJ'
                dims=[read(c+str(row+7)) for c in cols]
                caissons.append({'numero':i,'longueur':dims[0],'epaisseur':dims[1],'hauteur':dims[2]})
        if str(overrides.get('C14','')).casefold()=='métal sur bouts': price_value+=20
        out['dimensions_caissons']=caissons
        if len(caissons)==1:
            for axis in ('longueur','epaisseur','hauteur'):out['dimensions_exterieures_'+axis]=caissons[0][axis]
    if price_value<=0: raise CalculationError('Prix nul : vérifiez les dimensions et les options.')
    if sheet == 'T1':
        cells = 'C29 D29 E29 C13 C15 F15 C17 C19 F19 C21 F21 C23 F23 F25 B34 C36 C37 B38 C38 F38 C39 E39 C41 D41 E41'.split()
        out['fabrication_t1'] = {a: e.cell(sheet,a) for a in cells}
    out['prix_achat']=out['prix_vente']=round(price_value,2)
    if weight:out['poids_caisse']=read(weight)
    if minutes:out['minutes_production']=read(minutes)
    if carbon:
        try: out['bilan_carbone']=read(carbon)
        except (CalculationError,ValueError,TypeError,ArithmeticError): out['bilan_carbone']=None
    else:out['bilan_carbone']=None
    return out

def legacy(sheet,inputs,data,materials):
    if sheet=='Objet 1':
        o={'C31':numeric(inputs.longueur_cm),'D31':numeric(inputs.largeur_cm),'E31':numeric(inputs.hauteur_cm),'F9':'Calcul','F10':'ESI'}
        mapping={'type_contreplaque':'C13','barres':'C15','garnissage':'F15','type_isolant':'C17','fermeture':'C19','peinture':'F19','poignees':'C21','skis':'F21','cuvette_au_dessus':'C23','option':'F23','type_calage':'C33','plateau_interieur':'C35','base':'C36'}
        for k,a in mapping.items():
            v=getattr(inputs,k,None)
            if v is not None:o[a]=v
        if inputs.nombre_calages is not None and any(t in str(inputs.type_calage or '').lower() for t in ('bancs','guillotines')):o['F33']=numeric(inputs.nombre_calages)
        for k,a in [('type_mousse_calage','F35'),('type_mousse','F33'),('cuvette_longueur','D38'),('cuvette_largeur','E38'),('cuvette_hauteur','F38')]:
            if k in data and data[k] not in (None,'') and not (k=='type_mousse' and isinstance(o.get('F33'),(int,float))):o[a]=numeric(data[k]) if k.startswith('cuvette_') else data[k]
        if o.get('C23')=='Oui' and numeric(o.get('F38'))<=0:raise CalculationError('Renseignez la hauteur de la cuvette.')
    elif sheet=='Objet 2 à 6':
        o={'F9':'Calcul','F10':'ESI'}
        mapping={'type_contreplaque':'C13','barres':'C15','garnissage':'F15','type_isolant':'C17','fermeture':'C19','peinture':'F18','poignees':'C21','skis':'F21','cuvette_au_dessus':'C23'}
        for k,a in mapping.items():
            v=getattr(inputs,k,None)
            if v is not None:o[a]=v
        for i,(col,row) in enumerate((('C',26),('C',33),('C',40),('H',26),('H',33),('H',40))):
            cols='CDE' if col=='C' else 'HIJ'; item=inputs.objets[i] if i<len(inputs.objets) else None
            for c,k in zip(cols,('longueur_cm','largeur_cm','hauteur_cm')):o[c+str(row)]=numeric(getattr(item,k,None))
            for offset,k in [(1,'type_calage'),(2,'plateau_interieur'),(3,'base')]:o[col+str(row+offset)]=getattr(item,k,None) or 'Aucun'
            count=getattr(item,'nombre_calages',None); mousse=getattr(item,'type_mousse_calage',None)
            o[cols[2]+str(row+1)]=numeric(count) if count not in (None,'') and any(t in str(o[col+str(row+1)]).lower() for t in ('banc','guillotine')) else mousse or (numeric(count) if count not in (None,'') else 'PU')
        for k,a in [('cuvette_longueur','D52'),('cuvette_largeur','E52'),('cuvette_hauteur','F52')]:o[a]=numeric(data.get(k))
        if o.get('C23')=='Oui' and o['F52']<=0:raise CalculationError('Renseignez la hauteur de la cuvette.')
    else:
        o=inputs.as_overrides()
        if sheet=='T1-T6':
            for c,r in [('B',29),('H',29),('B',44),('H',44),('B',60),('H',60)]:
                for x in ('BCD' if c=='B' else 'HIJ'):o.setdefault(x+str(r),0)
                o.setdefault(c+str(r+1),'Aucun')
        if sheet=='T1-T3 MRT':
            o.pop('E11',None)
            for r in (27,30,33):
                for c in 'CDE':o.setdefault(c+str(r),0)
                o.setdefault('B'+str(r),'Aucun')
        if sheet=='T à Glissières':
            for r in range(32,48):
                for c in 'DEFG':o.setdefault(c+str(r),0)
        if sheet=='T Séparations mousse':
            for r in (*range(25,35),*range(40,50)):
                for c in 'DEF':o.setdefault(c+str(r),0)
        if sheet in ('T1','T1-T6'):
            for k,a in [('barres','C15'),('poignees','C21'),('skis','C23')]:
                if data.get(k):o[a]=data[k]
        for k,a in [('client','F5' if sheet=='MRT' else 'F11' if sheet=='T à Glissières' else 'F10'),('demandeur','F4' if sheet=='MRT' else 'F10' if sheet=='T à Glissières' else 'F9')]:
            o[a]='ESI' if k=='client' else 'Calcul'
    # Reject incomplete groups instead of silently retaining example dimensions.
    groups={
        'T1':[('C29','D29','E29')], 'MRT':[('C19','D19','E19')],
        'Objet 1':[('C31','D31','E31')], 'Objet 2 à 6':[(c+str(r),d+str(r),h+str(r)) for (c,d,h) in ('CDE','HIJ') for r in (26,33,40)],
        'T1-T6':[(c+str(r),d+str(r),h+str(r)) for (c,d,h) in ('BCD','HIJ') for r in (29,44,60)],
        'T1-T3 MRT':[(f'C{r}',f'D{r}',f'E{r}') for r in (27,30,33)],
        'T à Glissières':[(f'D{r}',f'E{r}',f'F{r}') for r in range(32,48)],
        'T Séparations mousse':[(f'D{r}',f'E{r}',f'F{r}') for r in (*range(25,35),*range(40,50))],
    }[sheet]
    active=0
    for group in groups:
        vals=[numeric(o.get(a)) for a in group]
        if any(vals):
            if not all(v>0 for v in vals): raise CalculationError('Dimensions incomplètes ou nulles.')
            active+=1
    if not active: raise CalculationError('Renseignez au moins une œuvre.')
    # Existing adapters sometimes include a fixed formula cell (not an input in 2026).
    s=workbook()[sheet]
    o={a:v for a,v in o.items() if s[a].data_type!='f'}
    return result(sheet,o,materials)

# New forms: explicit allowed input cells, never expose formulas or prices.
COMMON=[('C13','Contreplaqué'),('C15','Barres'),('C17','Isolant'),('C19','Fermeture'),('C21','Poignées'),('F15','Valsem'),('F19','Peinture'),('F21','Skis')]
TABLE=[('C13','Contreplaqué'),('C15','Barres'),('C17','Isolant'),('C19','Fermeture'),('C21','Poignées'),('C23','Skis'),('F15','Valsem'),('F19','Peinture'),('F21','Tyvek'),('F23','Option calage')]
def dims(cols,r):return [(c+str(r),l,'number','') for c,l in zip(cols,('Longueur (cm)','Épaisseur / largeur (cm)','Hauteur (cm)'))]
def g(title,fields):return (title,fields)
def table_group(i,cols,r,cal):
    return g('Œuvre '+str(i),dims(cols,r)+[(cols[0]+str(cal),'Calage')])
def object_groups(six=True):
    pairs=[('CDE',18),('CDE',28),('CDE',38),('HIJ',18),('HIJ',28),('HIJ',38)] if six else [('CDE',21)]
    return [g('Caisson '+str(i),dims(cols,r)+[(cols[0]+str(r+1),'Calage'),(cols[2]+str(r+1),'Nombre ou type de mousse','text','2'),(cols[0]+str(r+3),'Plateau intérieur'),(cols[2]+str(r+3),'Mousse du calage'),(cols[0]+str(r+4),'Base'),(cols[2]+str(r+4),'Valsem'),(cols[0]+str(r+5),'Plateau du caisson')]) for i,(cols,r) in enumerate(pairs,1)]
GROUPS={
'T1 LOUVRE':[g('Fabrication',TABLE),g('Œuvre',dims('CDE',29)+[('B31','Calage'),('C33','Mousse dessus (cm)','number',10),('C34','Mousse dessous (cm)','number',10),('B35','Matière de calage')])],
'T1-T6 LOUVRE':[g('Fabrication',TABLE)]+[table_group(i,c,r,r+1) for i,(c,r) in enumerate([('BCD',29),('HIJ',29),('BCD',44),('HIJ',44),('BCD',60),('HIJ',60)],1)],
'T1-T6 + Caissons':[g('Fabrication',TABLE)]+[table_group(i,c,r,r+1) for i,(c,r) in enumerate([('BCD',28),('HIJ',28),('BCD',44),('HIJ',44),('BCD',60),('HIJ',60)],1)],
'Caisson Tableaux':[g('Fabrication',[('C13','Contreplaqué'),('C17','Fermeture'),('C19','Poignées'),('F13','Tyvek'),('F14','Épaisseur mousse'),('F15','Valsem')])]+[table_group(i,c,25,26) for i,c in enumerate(('BCD','HIJ'),1)],
'Caisse Cuvette':[g('Fabrication',COMMON),g('Cuvette 1',dims('CDE',24)+[('C26','Calage'),('F26','Mousse'),('C28','Base')])]+[g('Cuvette '+str(i),[(h+str(r),'Hauteur (cm)','number',''),(c+str(r+2),'Calage'),(m+str(r+2),'Mousse'),(c+str(r+4),'Base')]) for i,(c,h,m,r) in enumerate([('C','E','F',31),('C','E','F',38),('I','K','L',24),('I','K','L',31),('I','K','L',38)],2)],
'Tapisserie':[g('Fabrication',COMMON)]+[g('Rouleau '+str(i),[(f'C{r}','Longueur (cm)','number',''),(f'D{r}','Diamètre (cm)','number',''),(f'F{r}','Diamètre du tube (cm)'),(f'C{r+2}','Calage'),(f'F{r+2}','Rouleau fourni par le client')]) for i,r in enumerate((24,31),1)],
'Claire-Voie':[g('Fabrication',[('C15','Barres'),('F21','Skis')]),g('Œuvre',dims('CDE',25)+[('C27','Calage'),('F27','Nombre ou type de mousse','text','2'),('C29','Plateau intérieur'),('C30','Base')])],
'Palette':[g('Fabrication',[('C13','Skis'),('F13','Valsem'),('C15','Plateau renforcé'),('C20','Plateau intérieur'),('C21','Base')]),g('Œuvre',[('C18','Longueur (cm)','number',''),('D18','Largeur (cm)','number','')])],
'Wrapp':[g('Fabrication',[('C13','Contreplaqué'),('C16','Fermeture'),('C18','Poignées')]),g('Œuvre',dims('CDE',22)+[('C23','Calage'),('E23','Mousse du calage'),('C24','Base')])],
'1 Caisson Objet':[g('Fabrication',[('C13','Contreplaqué'),('C15','Fermeture'),('C17','Poignées')])]+object_groups(False),
'6 Caissons Objets':[g('Fabrication',[('C9','Contreplaqué'),('C12','Fermeture'),('C14','Poignées')])]+object_groups(),
'Vente de Matière':[g(f'{label} – ligne {i}',[('B'+str(r),'Matière'),('C'+str(r),'Coupe'),('D'+str(r),'Longueur (cm)','number','')]+([] if start==24 else [('E'+str(r),'Largeur (cm)','number','')])+[('F'+str(r),'Quantité','number','')]) for start,label in [(6,'Panneaux'),(24,'Bois massif'),(42,'Mousse'),(60,'Carton / Macrolon')] for i,r in enumerate(range(start,start+12),1)]}

@lru_cache(maxsize=1)
def schemas():
    w=workbook(); output={}
    for sn,groups in GROUPS.items():
        s=w[sn]; gs=[]
        for title,fields in groups:
            fs=[]
            for a,label,*extra in fields:
                c=s[a]
                if c.data_type=='f' or c.__class__.__name__!='Cell':continue
                kind=extra[0] if extra else 'text'; default=extra[1] if len(extra)>1 else c.value
                if title!='Fabrication' and 'Calage'==label:default='Aucun'
                if title!='Fabrication' and label in ('Base','Plateau intérieur'):default='Aucun'
                options=[]
                for dv in s.data_validations.dataValidation:
                    if a not in dv.sqref or dv.type!='list':continue
                    ref=str(dv.formula1 or '').replace('$','')
                    if re.fullmatch(r'[A-Z]+\d+:[A-Z]+\d+',ref):
                        options=[x.value for row in s[ref] for x in row if isinstance(x.value,(str,int,float)) and x.data_type!='f']
                    elif ref.startswith('"') and ref.endswith('"'):options=ref[1:-1].split(',')
                    break
                if sn=='Vente de Matière' and label=='Matière':default='';options=['']+options
                if options:
                    kind='select'
                    if default not in options:default=options[0]
                if default is None:default=''
                fs.append({'cell':a,'label':label,'kind':kind,'default':default,'options':options})
            if fs:gs.append({'title':title,'fields':fs})
        output[sn]=gs
    return output

def generic(sheet,data,materials):
    if sheet not in NEW:raise CalculationError('Typologie non disponible.')
    provided=data.get('cellules') or {}; o={}; s=workbook()[sheet]
    fields={f['cell']:f for g in schemas()[sheet] for f in g['fields']}
    if not isinstance(provided,dict) or set(provided)-set(fields):raise CalculationError('Champ non autorisé.')
    for a,f in fields.items():
        v=provided.get(a,f['default']); kind=f['kind']
        if kind=='number':v=numeric(v)
        elif kind=='select':
            # Browser sends strings even for numeric choices.
            options={str(x):x for x in f['options']}
            if str(v) not in options:raise CalculationError('Option invalide : '+f['label'])
            v=options[str(v)]
            if v=='':v=0
        else:
            if not isinstance(v,(str,int,float)) or len(str(v))>100:raise CalculationError('Valeur invalide.')
            if str(v).replace(',','.').replace('.','',1).isdigit():v=numeric(v)
        o[a]=v
    # Prevent calculating from an empty example or partial dimensions.
    groups=schemas()[sheet]
    active=0
    for group in groups:
        fields=group['fields']; dim_fields=[f for f in fields if f['kind']=='number' and f['label'] in ('Longueur (cm)','Épaisseur / largeur (cm)','Largeur (cm)','Hauteur (cm)','Diamètre (cm)')]
        if sheet=='Vente de Matière':
            material=next(f for f in fields if f['label']=='Matière'); qty=next(f for f in fields if f['label']=='Quantité')
            if not o[material['cell']] and not any(o[f['cell']] for f in dim_fields) and not o[qty['cell']]:continue
            if not o[material['cell']] or not all(o[f['cell']]>0 for f in dim_fields) or o[qty['cell']]<=0:raise CalculationError('Ligne de matière incomplète.')
            active+=1
        elif dim_fields:
            vals=[o[f['cell']] for f in dim_fields]
            if any(vals):
                if not all(v>0 for v in vals):raise CalculationError('Dimensions incomplètes : '+group['title'])
                active+=1
    if not active:raise CalculationError('Renseignez au moins une œuvre ou une matière.')
    if sheet=='Caisse Cuvette' and not all(o.get(a,0)>0 for a in ('C24','D24','E24')):raise CalculationError('Renseignez la première cuvette.')
    for a in ('F9','F5'):
        if a=='F5' and sheet!='6 Caissons Objets':continue
        if a=='F9' and sheet in ('6 Caissons Objets','Vente de Matière'):continue
        if s[a].data_type!='f':o[a]='Calcul'
    return result(sheet,o,materials)
