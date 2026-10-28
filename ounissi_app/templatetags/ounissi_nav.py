"""
ounissi_nav — découverte automatique des pages nommées pour la sidebar.

NOUVEAU FICHIER (rien à modifier dans urls.py / views.py) :
    ounissi_app/templatetags/ounissi_nav.py

Usage dans templates/includes/ounissi_nav.html :
    {% load ounissi_nav %}
    {% discover_pages exclude="home,salary_list,..." as extra_pages %}

Le tag parcourt le résolveur d'URL de Django et renvoie uniquement les routes :
  * nommées,
  * SANS paramètre (une route avec <int:pk>, <str:...> est une sous-vue -> ignorée),
  * dont le nom ne ressemble pas à une sous-vue AJAX / POST / fragment de popup,
  * absentes de la liste `exclude` (déjà présentes dans le menu).
Ainsi vos pages locales (pointage, congés, ...) apparaissent dès qu'elles sont nommées,
sans jamais produire de lien vers une adresse inexistante.
"""
from django import template
from django.urls import NoReverseMatch, URLPattern, URLResolver, get_resolver, reverse

register = template.Library()

# Préfixes / noms typiques des sous-vues (AJAX, POST, popups) : jamais listés.
SKIP_PREFIXES = (
    "get_", "save_", "execute_", "submit_", "form_", "add_or_edit", "update_",
    "delete_", "finalize_", "unfinished_", "import_", "calculate_", "distribute_",
    "generic_form", "test_query_submission", "select_schedule", "homeold",
    "login", "logout", "password_", "signup", "admin",
)


def _iter_names(patterns, namespace=None):
    for entry in patterns:
        if isinstance(entry, URLResolver):
            ns = entry.namespace or namespace
            if ns == "admin":
                continue
            for name in _iter_names(entry.url_patterns, ns):
                yield name
        elif isinstance(entry, URLPattern) and entry.name:
            yield "%s:%s" % (namespace, entry.name) if namespace else entry.name


@register.simple_tag
def discover_pages(exclude=""):
    """Renvoie [{'name', 'url', 'label'}] pour chaque page nommée sans paramètre non exclue."""
    known = {n.strip() for n in exclude.split(",") if n.strip()}
    pages, seen = [], set()
    for name in _iter_names(get_resolver().url_patterns):
        short = name.split(":")[-1]
        if name in seen or name in known or short in known or short.startswith(SKIP_PREFIXES):
            continue
        try:
            url = reverse(name)  # NoReverseMatch => la route attend des arguments => sous-vue
        except NoReverseMatch:
            continue
        seen.add(name)
        pages.append({"name": name, "url": url, "label": short.replace("_", " ").capitalize()})
    return pages
