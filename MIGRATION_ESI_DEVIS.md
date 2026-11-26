# ESI TICKETS + ESI DEVIS — version de test

## Installation sur une branche de test
1. Créer une branche `integration-esi-devis` à partir de la version ESI TICKETS correspondant au ZIP fourni.
2. Extraire le ZIP de modifications à la racine du dépôt. Remplacer uniquement `app.py`, `requirements.txt` et `templates/demandeur.html`. Ajouter `esi_devis/`. Ne pas remplacer les dossiers de données ESI TICKETS.
3. Utiliser un environnement Render de test si possible. Ne pas arrêter le Render ESI DEVIS existant. Conserver la commande de démarrage ESI TICKETS actuelle (`gunicorn app:app`, avec ses options existantes).
4. Dans Environment, ajouter les variables ci-dessous en copiant les valeurs de l'ancien Render ESI DEVIS. Ne pas modifier les variables Supabase ESI TICKETS.

| Ancienne variable DEVIS | Nouvelle variable dans TICKETS |
|---|---|
| SUPABASE_URL | ESI_DEVIS_SUPABASE_URL |
| SUPABASE_SERVICE_ROLE_KEY | ESI_DEVIS_SUPABASE_SERVICE_ROLE_KEY |
| SUPABASE_NOTICES_BUCKET (si définie) | ESI_DEVIS_SUPABASE_NOTICES_BUCKET |
| SUPABASE_NOTICES_TABLE (si définie) | ESI_DEVIS_SUPABASE_NOTICES_TABLE |

Les valeurs par défaut du bucket et de la table sont `notices-techniques` et `notices_techniques`. Ne pas copier les secrets dans GitHub. L'ancien token administrateur DEVIS n'est plus nécessaire. ESI_AUTH_ENABLED doit rester à 1 et ESI_SESSION_SECRET inchangé. Le module refuse les accès sans session authentifiée. Ne pas activer ESI_DEVIS_LOCAL_TEST en production.

## Validation avant bascule
- Se connecter avec les trois profils, vérifier le bandeau OUTILS et ses trois liens.
- Calculer T1, Objet, MRT, Tapisserie, Caisse déco, Wrapp et Caissons en comparant aux mêmes entrées sur l'ancien Render.
- Vérifier notices locales et Supabase, export PDF, affichage CLASSIQUE et EXPERT.
- Vérifier l'administration des tarifs avec ADMIN et son refus côté serveur pour les autres profils. Ne pas modifier les tarifs réels pendant les essais sans sauvegarde et accord : les deux applications partagent la base DEVIS existante.
- Vérifier réception, tickets, échanges, articles, colisage et reporting sur la branche de test.
- Une fois validé, fusionner la branche puis arrêter le Render DEVIS seulement après validation en production. Retour arrière : revenir au commit TICKETS précédent et conserver l'ancien Render DEVIS.

## Vérifications effectuées hors production
8 tests de calcul fournis : réussis. Compilation Python et vérification syntaxique JavaScript : réussies. Tests HTTP Flask non exécutés, Flask absent de l'environnement de préparation. Aucune connexion aux bases réelles et aucun déploiement effectué.
