from django import template

from ounissi_app.navigation import menu_sections

register = template.Library()


@register.inclusion_tag('includes/ounissi_nav.html', takes_context=True)
def ounissi_navigation(context):
    request = context.get('request')
    sections, can_configure = menu_sections(request.user) if request else ([], False)
    return {'nav_sections': sections, 'nav_can_configure': can_configure}
