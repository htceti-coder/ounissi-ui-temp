from decimal import Decimal

from django import forms
from django.core.validators import MinValueValidator

from .models import CustomKPI, Query, Task


class CustomKPIForm(forms.ModelForm):
    # Protect an administrator from silently overwriting another admin's edit.
    revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

    class Meta:
        model = CustomKPI
        fields = ('name', 'description', 'scope', 'calculation', 'selection', 'tasks', 'queries',
                  'priority', 'kind', 'state', 'target', 'is_active')
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Ex. : Performance réception'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
            'tasks': forms.SelectMultiple(attrs={'class': 'multi_select', 'size': 8}),
            'queries': forms.SelectMultiple(attrs={'class': 'multi_select', 'size': 6}),
            'target': forms.NumberInput(attrs={'class': 'form-control', 'min': '0.01', 'step': '0.01'}),
        }
        help_texts = {
            'scope': 'Choisissez les variantes personnelles, d’équipe, ou les deux. Leur visibilité par rôle se règle ensuite.',
            'calculation': 'La progression et le score pondérés utilisent les secondes réalisées ou affectées divisées par la priorité.',
            'selection': 'Avec « Toutes les tâches », les sélections ci-dessous ne limitent pas le calcul ; les règles s’appliquent toujours.',
            'tasks': 'Facultatif si une requête est choisie. Sans JavaScript, utilisez Ctrl ou Cmd pour sélectionner plusieurs lignes.',
            'queries': 'Sélectionne les tâches liées à ces requêtes : aucun SQL n’est exécuté. Les tâches et requêtes sont réunies sans doublon.',
            'priority': 'La règle s’applique à toutes les tâches retenues.',
            'kind': 'Pour compter les lignes enregistrées, choisissez les tâches automatiques.',
            'state': 'Un état ne peut pas être déduit si les données de progression sont invalides ou manquantes.',
            'target': 'Facultatif. Même unité que le calcul ; pour le temps affecté, saisir des secondes. Les pourcentages ont un objectif de 100 % par défaut.',
            'is_active': 'Désactiver masque le KPI sans supprimer sa définition ni ses réglages de visibilité.',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['tasks'].queryset = Task.objects.order_by('task_name', 'pk')
        # The legacy N/A sentinel denotes a manual task, not an automatic
        # progress source. Queries still only select already-saved task data.
        self.fields['queries'].queryset = Query.objects.exclude(query='N/A').order_by('name', 'pk')
        self.fields['target'].validators.append(MinValueValidator(Decimal('0.01')))
        self.fields['revision'].initial = self.instance.revision if self.instance.pk else 0

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('selection') == CustomKPI.Selection.SELECTED:
            if not cleaned.get('tasks') and not cleaned.get('queries'):
                self.add_error('tasks', 'Sélectionnez au moins une tâche ou une requête liée, ou choisissez toutes les tâches.')
        if (cleaned.get('calculation') == CustomKPI.Calculation.SAVED_LINES and
                cleaned.get('kind') != CustomKPI.Kind.AUTOMATIC):
            self.add_error('kind', 'Le calcul des lignes enregistrées nécessite le filtre « Automatiques ».')
        return cleaned
