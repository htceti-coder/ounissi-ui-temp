# Planification des TRT types

## Déploiement

Appliquer les migrations puis publier les fichiers statiques :

```sh
python manage.py migrate
python manage.py collectstatic
```

La migration `0009_schedule_creation_request` ajoute une petite table de clés de soumission afin qu’une même demande rejouée ne crée pas deux TRT. Elle dépend des migrations précédentes, notamment `0008_custom_kpis`. Les anciennes données ne sont pas modifiées par la migration. Le planificateur utilise une requête JSON bornée (limite Django de 16 Mo et 50 000 lignes par liste), pas une matrice de champs de formulaire illimitée.

## Droits et parcours

Un superutilisateur ou un membre du groupe **Admin** peut créer un TRT type et, depuis **Planification TRT**, modifier ou dupliquer un modèle existant. Les droits sont vérifiés sur le serveur et dans l’administration Django, indépendamment du menu ou de permissions de modèle accordées à un utilisateur non administrateur. Un responsable d’équipe peut consulter les modèles et créer un TRT daté pour n’importe quelle équipe, mais ne peut pas créer, dupliquer ni modifier un modèle. Un utilisateur ordinaire n’a pas accès à ces actions. La console de création et d’exécution de SQL arbitraire est aussi réservée aux administrateurs, afin qu’elle ne puisse pas contourner les droits sur les modèles ; les requêtes opérationnelles déjà configurées continuent leur fonctionnement habituel. La file interne `Transfer_Schedule` n’est pas un modèle proposé dans le planificateur.

Dans l’éditeur :

1. Saisir le nom et les horaires. Filtrer les tâches par nom, type ou priorité, et les employés par nom, équipe ou niveau. « Sélectionner les résultats » agit sur **tous** les résultats du filtre, pas seulement sur la page visible ; la sélection reste active quand le filtre change.
2. Régler la durée prévue de chaque tâche et la capacité de chaque employé, individuellement ou pour tous les éléments correspondant au filtre des durées. Une nouvelle tâche reçoit une heure par défaut. La capacité initiale d’un nouvel employé correspond à l’écart entre les horaires lorsqu’il est positif ; les changements d’horaires ultérieurs ne modifient pas les capacités déjà saisies. Le suivi actuel ne prend pas en charge les postes de nuit.
3. Lancer **Répartition automatique** ou ajouter/modifier les affectations manuellement. Relancer la répartition demande confirmation avant de remplacer toutes les affectations existantes, y compris les lignes sans employé et leurs éventuelles références. Retirer une tâche ou un employé qui possède des affectations demande également confirmation. La suppression confirmée est également vérifiée côté serveur.
4. Contrôler les besoins non affectés, les éventuelles durées nulles et les dépassements. Les affectations manuelles peuvent dépasser la durée prévue ou la capacité après confirmation explicite ; une affectation à un employé non éligible est refusée. Les totaux sont recalculés depuis les lignes, jamais pris sur confiance depuis le navigateur. Les erreurs de sauvegarde gardent la saisie à l’écran.

Les durées se saisissent en `heures:minutes:secondes`, par exemple `00:08:15` pour 11 lignes de 45 secondes. Les heures ne sont pas limitées à 24. Les secondes sont conservées sans arrondi ; les microsecondes déjà présentes dans d’anciens modèles sont également conservées. Une nouvelle répartition utilise des secondes entières si les besoins et les capacités sont tous en secondes entières.

## Principe de répartition

La répartition utilise un flot maximal entier : elle maximise d’abord la durée affectée aux tâches de priorité 1, puis celle de priorité 2 sans réduire le total de priorité 1, puis les priorités 3 et 4. Elle respecte le besoin de chaque tâche, la capacité de chaque employé et la règle existante `employé.rank.value <= tâche.responsibility_level.value`. Un employé sans niveau n’est pas éligible automatiquement. Les employés de même niveau sont regroupés dans le calcul, puis le résultat est réparti entre eux avec un nombre limité de découpages par tâche pour éviter une matrice inutilement grande. L’équité entre employés ou entre tâches d’une même priorité n’est pas un objectif d’optimisation garanti. Le calcul est fondé sur des durées divisibles, pas sur des lignes indivisibles de produit.

Dans les besoins, le filtre « seulement les besoins non affectés » et la colonne « Non affecté » permettent d’identifier les reliquats par tâche. Les durées prévues non affectées restent enregistrées dans `ScheduleTask.total_time` même lorsqu’il n’y a aucune ligne d’affectation. Elles sont signalées dans le modèle, avant la création d’un TRT daté, puis dans les vues de suivi du TRT. Ce sont des besoins, pas des affectations fictives : un responsable peut ajouter ensuite une affectation réelle. Les lignes de réception sans employé restent distinctes et continuent à suivre leur circuit existant ; l’affectation des étapes suivantes en parallèle n’est pas bloquée.

## Copie, modifications et sécurité des données

Un modèle peut être ouvert en modification ou servir de point de départ pour une **nouvelle** copie. Modifier un modèle ne modifie aucun TRT daté existant et ne relance pas la génération du stock. Les lignes d’affectation conservées gardent leur identifiant, leurs références et leurs données de progression ; les lignes liées à des enregistrements de réception ne peuvent pas être modifiées ou supprimées par l’éditeur tant que cette liaison existe. Un ancien modèle contenant plusieurs totaux pour la même tâche ou le même employé, une durée invalide, ou une affectation dont les agrégats sont absents est signalé et doit être corrigé avant utilisation. Si seuls les anciens totaux affectés sont incohérents, l’écran le signale et les totaux sont recalculés à partir des lignes lors de l’enregistrement ou dans la copie datée.

La création d’un TRT daté copie les durées et les références directement depuis la base, pas depuis des valeurs de tableau tronquées. Les anciennes données de progression ne sont pas copiées comme progression du nouveau TRT. Si le modèle ou les règles de priorité/niveau ont changé depuis l’affichage, l’utilisateur doit réexaminer le modèle ; les avertissements doivent être confirmés à nouveau. Les enregistrements sont transactionnels ; une erreur n’enregistre pas de modèle ou TRT partiel. Le verrouillage concurrent utilise les capacités de la base de données : SQLite n’applique pas les verrous de ligne `select_for_update`.

Une clé empêche le double enregistrement lorsque la **même** demande est répétée ou relancée après une réponse perdue. Recharger la page crée une nouvelle clé : elle ne remplace pas une vérification humaine avant de créer volontairement un autre modèle.

## Stock : comportement volontairement inchangé

Conformément au choix actuel, la génération automatique du stock existante est toujours appelée une fois à la création d’un modèle (y compris une copie) et une fois à la création d’un TRT daté. Elle n’est pas appelée lors de la modification d’un modèle. Elle peut consulter les données Xpert et ajouter des tâches et affectations en plus du plan affiché. **Elle utilise toujours les affectations de stock du jour courant et peut additionner à nouveau une affectation de stock copiée depuis le modèle**, y compris pour un TRT d’une autre date. Le correctif de copie conserve désormais les secondes et les références, mais ne corrige pas cette règle de génération ; vérifier les tâches de stock après création en attendant sa révision séparée.

## Intégration et tests

Le formulaire interne de création de modèle envoie désormais un champ JSON `plan` à `/schedule/create_schedule/`. L’ancien envoi de trois tableaux en minutes (`tasks`, `employees`, `scheduleItems`) et l’ancien envoi d’un TRT daté à cette URL ne sont plus utilisés. Le TRT daté est créé par POST sur `/schedule/template/<id>/create-dated/` à partir du modèle enregistré. Adapter tout consommateur externe éventuel ; les vues actives de l’application utilisent les nouveaux formulaires.

Tests locaux :

```sh
python manage.py test
npm ci
npm run test:js
```

Les tests JavaScript couvrent notamment l’optimum par priorité face à une référence exhaustive sur de petits cas, les secondes, la pagination, les confirmations, les erreurs réseau et le rendu sûr des libellés. Aucun test ne suppose une connexion réelle à Xpert ou au terminal de présence.
