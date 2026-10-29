# Indicateurs configurables

## Déploiement

Appliquer les migrations avant de servir le code, puis collecter les fichiers statiques si le déploiement les sert avec `collectstatic` :

```sh
python manage.py migrate
python manage.py collectstatic
```

La migration `0007_kpi_display_settings` ajoute les réglages d’affichage. Les indicateurs sans réglage enregistré utilisent leurs valeurs par défaut ; aucune modification des données de production ni remplissage historique n’est effectué par cette migration. La migration `0006_navigation_visibility` reste nécessaire pour le menu.

## Configuration et accès

Un superutilisateur ou un membre du groupe Django **Admin** ouvre **Paramètres → Configurer les indicateurs**. Pour chaque rôle, il peut choisir l’accueil, la page KPI, une présentation compatible (carte, barre ou détails) et un ordre d’affichage. Les deux emplacements sont indépendants. La restauration supprime les personnalisations et rétablit les valeurs par défaut. Les changements s’appliquent au prochain chargement ou à la prochaine lecture automatique. Les cartes sont regroupées en « Mes indicateurs » et « Indicateurs de mon équipe » : l’ordre configuré est respecté à l’intérieur de chaque groupe, sans modifier le périmètre autorisé.

L’entrée **Mes indicateurs** du menu est elle-même réglable dans **Configurer le menu**. La visibilité du menu ne constitue pas une autorisation d’accès. La configuration des KPI est réservée aux administrateurs, et les données sont filtrées sur le serveur : chacun voit ses propres données ; un responsable ne voit que l’équipe dont il est `Team.leader`. Le responsable fait partie des calculs de son équipe même s’il n’est pas également inscrit dans `Team.employees`. Un administrateur sans équipe dirigée ne dispose pas d’une vue globale des équipes dans les KPI. Sur les écrans de suivi TRT existants, la colonne de score individuel est calculée pour tous les employés pour les administrateurs, seulement pour leur équipe pour les responsables, et n’est pas exposée aux employés ordinaires. Les rôles suivent le même ordre de priorité que le menu : superutilisateur ou membre d’`Admin`, puis responsable d’équipe, puis utilisateur.

## Choisir une période

À l’accueil et sur la page **Mes indicateurs**, utiliser **Jour** ou **Mois**, puis choisir une date et cliquer sur **Afficher**. L’ouverture initiale utilise aujourd’hui ; le mode mensuel utilise le mois courant par défaut. Une date antérieure peut être sélectionnée dans chaque mode. Le choix filtre simultanément les tâches, les pointages personnels et les pointages d’équipe. Il est conservé pendant les lectures automatiques et lors du passage entre l’accueil et la page des indicateurs ; une date explicitement sélectionnée reste fixe. Si aucune date n’a été sélectionnée, la lecture suit le jour ou le mois courant lors d’un changement de date.

Le tableau des tâches opérationnelles sur l’accueil reste toujours celui du **TRT actif**, indépendamment de la période des indicateurs. Les anciennes URL contenant seulement `?month=AAAA-MM` restent interprétées comme un filtre mensuel.

## Progression et score

Les tâches prises en compte sont les tâches affectées de tous les TRT non modèles datés du jour ou du mois sélectionné, y compris les TRT terminés ou planifiés ; les tâches non affectées et les TRT des autres dates n’entrent pas dans le calcul. Le tableau personnel de l’accueil reste limité au TRT actif. La colonne de score des écrans de suivi est, quant à elle, calculée pour le TRT affiché. Les cartes et la colonne « Score (points) » des vues de suivi sont calculées depuis les données enregistrées, avec des décimales conservées jusqu’à l’affichage :

- **Points affectés** : somme de `secondes affectées / priorité`.
- **Points réalisés** : somme de `secondes réalisées / priorité`.
- **Progression globale** : `points réalisés / points affectés × 100`.
- Pour une tâche automatique, les secondes réalisées valent `lignes enregistrées × durée configurée par ligne`. Exemple : 11 lignes à 45 secondes valent 495 secondes. Pour une tâche manuelle, elles valent `durée affectée × pourcentage validé / 100`.
- Exemple de priorité : 100 secondes réalisées à priorité 2 donnent 50 points ; si 100 secondes sont affectées à cette même priorité, l’objectif est de 50 points et la progression est de 100 %.

Un résultat supérieur à l’objectif n’est pas plafonné ; seule la barre graphique est limitée à 100 %. Une durée affectée nulle ou invalide, une priorité invalide, un nombre de lignes ou une durée par ligne manquants, ou un pourcentage manuel invalide rendent le score global indisponible, plutôt que de le présenter comme un total complet. Les tâches concernées sont signalées. Sans tâche ou sans objectif, la valeur est `—`.

Le champ historique entier `ScheduleEmployee.score` contient un ancien calcul basé sur les lignes ; il n’est **pas** la source de ces indicateurs ou du score affiché dans les deux écrans de suivi. Un consommateur externe ne doit pas le considérer comme le score en secondes pondérées. Les KPI sont calculés à la lecture et tiennent donc aussi compte des validations manuelles groupées sans attendre la mise à jour de ce champ hérité.

## Pointage et limites de fraîcheur

Les jours comptent uniquement les enregistrements confirmés de la période sélectionnée, non futurs et hors congés, avec au moins une entrée. Les heures ne comptent que les plages complètes, chronologiques et non ambiguës des enregistrements confirmés, non futurs et hors congés. Si une plage est incomplète, inversée ou chevauche une plage précédente, le total ne retient que les plages fiables qui la précèdent et indique le problème. Les pointages et congés en attente sont comptés séparément. L’absence d’enregistrement ne prouve pas une absence.

En mode journalier, les cartes d’équipe décrivent l’état enregistré pour chaque membre à la date sélectionnée : ouvert, clôturé, à vérifier, congé enregistré ou état inconnu. En mode mensuel, elles comptent les **enregistrements** ouverts et affichent une synthèse par membre (ouverts, clôturés, à vérifier et congés) ; ces chiffres ne représentent pas le nombre de personnes présentes. Les enregistrements en attente et futurs apparaissent dans cette synthèse, car ce sont des données enregistrées ; les totaux personnels confirmés continuent d’exclure les dates futures. Sans enregistrement, l’état reste inconnu et aucune absence n’est déduite. Une entrée sans sortie est un **pointage ouvert**, pas une preuve que la personne est présente à cet instant. Un terminal peut être en retard ou ne pas avoir été importé.

Les données d’équipe, y compris historiques, utilisent les membres **actuellement** rattachés à l’équipe dirigée : le modèle ne conserve pas l’historique des changements d’équipe. Elles ne reconstituent donc pas la composition passée de l’équipe.

L’interface relit les données déjà enregistrées toutes les 30 secondes lorsque l’onglet est visible. En cas d’échec, elle conserve les dernières valeurs et réessaie. L’heure indiquée est celle de la lecture de l’application, **pas** celle de la dernière synchronisation des sources : aucun horodatage fiable de synchronisation Xpert ou du terminal n’est disponible ici. Aucun sondage ne lance de requête Xpert ni d’import du terminal. Le bouton existant **Actualiser mes tâches automatiques** effectue, sur demande, la mise à jour des tâches automatiques personnelles ; le pointage nécessite les imports ou corrections habituels. Si une affectation ou le TRT change pendant que la page d’accueil est ouverte, l’utilisateur est invité à actualiser la page sans perdre automatiquement un formulaire en cours.

## Vérifications recommandées en production

Après migration, vérifier les groupes et l’appartenance des employés aux équipes, les durées par ligne et les priorités des tâches, puis comparer quelques TRT et pointages confirmés avec les sources. Tester séparément la connexion Xpert et l’import du terminal dans l’environnement réel : les tests du dépôt utilisent des données synthétiques et ne valident pas ces intégrations externes.
