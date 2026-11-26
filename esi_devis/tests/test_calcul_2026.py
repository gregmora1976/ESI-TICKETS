import unittest
from unittest.mock import patch
from esi_devis import app
from esi_devis.matrice_caisses.integration_2026 import generic, OUTPUTS, schemas

class Calcul2026(unittest.TestCase):
    def test_t1(self):
        data=dict(longueur_cm=100,epaisseur_cm=5,hauteur_cm=100,type_calage='Sous verre 2 faces',type_isolant='Aucun')
        r=app.calculate_sheet('T1',data)
        self.assertEqual(r['prix_achat'],423)
        self.assertAlmostEqual(r['poids_caisse'],42.94951645,places=5)
        self.assertAlmostEqual(app.enrich_result_with_cession(r)['prix_cession'],549.9)
        self.assertRaises(ValueError,app.calculate_sheet,'T1',dict(longueur_cm=100))
    def test_separations(self):
        d=dict(type_contreplaque='Peuplier 15 mm',barres='Pin raboté 4 faces',garnissage='Oui',type_isolant='Isotherme en 50',fermeture='Vis bois',peinture='Non',skis='10 dessous en semelles',poignees='Bois',separations='Carton + mousse PU 20',rang1=[dict(longueur_cm=l,largeur_cm=e,hauteur_cm=h) for l,e,h in [(32.5,5,23.5),(14,5,17),(65.5,8,67)]],rang2=[])
        r=app.calculate_sheet('T Séparations mousse',d)
        self.assertEqual(r['prix_achat'],574)
        self.assertAlmostEqual(r['bilan_carbone'],47.75,places=2)
        self.assertAlmostEqual(r['poids_caisse'],48.4,places=1)
    def test_tapisserie(self):
        r=generic('Tapisserie',{'cellules':{'C24':420,'D24':35,'F24':15,'C26':'Guillotines ethafoam','C17':'Super isotherme en 50'}},{})
        self.assertEqual(r['prix_achat'],1773.09)
    def test_claire_voie(self):
        r=generic('Claire-Voie',{'cellules':{'C25':100,'D25':100,'E25':100,'C27':'Bancs ouverts','F27':2,'C29':'Cp 15 mm garni valsem','C30':'Ethafoam 50 mm'}},{})
        self.assertEqual(r['prix_achat'],459.58)
    def test_cuvettes(self):
        r=generic('Caisse Cuvette',{'cellules':{'C17':'Isotherme en 50','C24':145,'D24':85,'E24':20,'C26':'Découpe Mousse','C28':'PU 50 mm','E31':15,'C33':'Découpe Mousse','C35':'PU 50 mm','E38':30,'C40':'Mousse au carré cinquante mm','C42':'Stratocell 50 mm'}},{})
        self.assertEqual(r['prix_achat'],1466.55)
    def test_caissons(self):
        d={'C18':60,'D18':60,'E18':50,'C19':'Guillotines horizontales','E19':2,'C22':'Stratocell 50 mm'}
        self.assertEqual(generic('6 Caissons Objets',{'cellules':d},{})['prix_achat'],225)
        d['C14']='métal sur bouts'
        self.assertEqual(generic('6 Caissons Objets',{'cellules':d},{})['prix_achat'],245)
    def test_options(self):
        self.assertEqual(len(OUTPUTS),20)
        self.assertEqual(len(schemas()),12)
        self.assertFalse({'Objet Anti Vibratoire','Tableau Anti Vibratoire','Wrapp Barré','Caisse Garde-Meubles'} & set(OUTPUTS))
        self.assertRaises(ValueError,generic,'Tapisserie',{'cellules':{'G39':1}},{})
    def test_admin(self):
        d=dict(longueur_cm=100,epaisseur_cm=5,hauteur_cm=100,type_calage='Sous verre 2 faces',type_isolant='Aucun')
        with patch.object(app,'load_admin_settings',return_value={'cells':{'Prix Matiere!B29':100},'marge_cession_percent':31}):
            r=app.calculate_sheet('T1',d)
            self.assertGreater(r['prix_achat'],423)
            self.assertAlmostEqual(app.enrich_result_with_cession(r)['prix_cession'],r['prix_achat']*1.31)
        self.assertEqual(app.calculate_sheet('T1',d)['prix_achat'],423)

if __name__=='__main__':unittest.main()
