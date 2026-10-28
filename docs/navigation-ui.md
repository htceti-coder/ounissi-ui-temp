# Configuration du menu

## Déploiement

Appliquer la migration avant de servir la nouvelle interface :

```sh
python manage.py migrate
# Si les fichiers statiques sont servis par collectstatic dans ce déploiement :
python manage.py collectstatic
```

La migration `0006_navigation_visibility` ajoute uniquement les préférences du menu. Les nouvelles entrées utilisent leurs valeurs par défaut tant qu’aucun réglage n’a été enregistré.

## Utilisation

1. Connectez-vous avec un superutilisateur ou un compte membre du groupe Django **Admin**.
2. Dans le menu latéral, ouvrez **Paramètres → Configurer le menu**.
3. Cochez les liens à afficher pour les utilisateurs, responsables d’équipe et administrateurs, puis enregistrez. Sur mobile, le tableau de configuration défile horizontalement.
4. **Restaurer les valeurs par défaut** annule les personnalisations de la matrice.

Les réglages s’appliquent à tous les comptes du même niveau. Le rôle le plus élevé prévaut : superutilisateur ou membre du groupe `Admin`, puis responsable d’une équipe (`Team.leader`), puis utilisateur. Le lien de configuration reste visible pour les administrateurs afin qu’ils ne se bloquent pas eux-mêmes. Le lien vers l’administration Django nécessite en outre un compte `is_staff`, même si la case est cochée.

**La visibilité du menu n’est pas une autorisation d’accès.** Masquer un lien ne protège pas son URL et afficher un lien n’accorde pas l’accès à la page. Les contrôles d’accès existants restent applicables ; les permissions sensibles doivent être vérifiées séparément.
