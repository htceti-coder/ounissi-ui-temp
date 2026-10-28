from django.template.loader import render_to_string
from django.test import SimpleTestCase


class QueryTablePresentationTests(SimpleTestCase):
    def test_collapsible_result_keeps_target_on_the_scrollable_wrapper(self):
        html = render_to_string('includes/query_results_table.html', {
            'table_id': 'invoice', 'collapsible': True,
            'results': {'columns': ['Produit'], 'data_rows': [['<article>']]},
        })
        self.assertIn('class="table-scroll-shell collapse" id="invoice"', html)
        self.assertNotIn('<table id="invoice"', html)
        self.assertIn('role="region"', html)
        self.assertIn('&lt;article&gt;', html)
        self.assertNotIn('<article>', html)

    def test_non_collapsible_table_keeps_its_original_table_id(self):
        html = render_to_string('includes/query_results_table.html', {
            'table_id': 'query-results',
            'results': {'columns': ['Produit'], 'data_rows': []},
        })
        self.assertIn('<table id="query-results"', html)
        self.assertIn('Aucun résultat', html)

    def test_error_does_not_reveal_query_details(self):
        html = render_to_string('includes/query_results_table.html', {
            'results': {'error': 'secret connection string'},
        })
        self.assertIn('Impossible de charger', html)
        self.assertNotIn('secret connection string', html)
