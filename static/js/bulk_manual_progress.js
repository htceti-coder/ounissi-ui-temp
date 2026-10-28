(function () {
    'use strict';

    function init() {
        const form = document.querySelector('[data-manual-progress-form]');
        if (!form) return;
        const tables = Array.from(document.querySelectorAll('[data-manual-table]'));
        const buttons = Array.from(document.querySelectorAll('[data-manual-bulk-submit]'));
        const countLabel = form.querySelector('[data-manual-count]');
        const payload = form.querySelector('[data-manual-payload]');
        const error = document.querySelector('[data-manual-error]');
        const $ = window.jQuery;
        const enhancedNote = document.querySelector('[data-manual-enhanced-note]');
        if (enhancedNote) enhancedNote.hidden = false;
        countLabel.hidden = false;

        // DataTables detaches rows on other pages. Its API retains the original
        // row nodes and their input values, including filtered-out selections.
        function rowsFor(table) {
            if ($ && $.fn && $.fn.DataTable && $.fn.DataTable.isDataTable(table)) {
                return $(table).DataTable().rows().nodes().toArray();
            }
            return Array.from(table.querySelectorAll('tbody tr[data-manual-item]'));
        }

        function allRows() {
            return tables.flatMap(rowsFor).filter(row => row.querySelector('[data-manual-select]'));
        }

        function visibleRows(table) {
            return Array.from(table.querySelectorAll('tbody tr[data-manual-item]'));
        }

        function updateSelection() {
            const selected = allRows().filter(row => row.querySelector('[data-manual-select]').checked).length;
            countLabel.textContent = selected + (selected === 1 ? ' tâche sélectionnée' : ' tâches sélectionnées');
            buttons.forEach(button => { button.hidden = false; button.disabled = selected === 0; });
            tables.forEach(table => {
                const master = table.querySelector('[data-manual-page-select]');
                if (!master) return;
                const boxes = visibleRows(table).map(row => row.querySelector('[data-manual-select]')).filter(Boolean);
                const checked = boxes.filter(box => box.checked).length;
                master.checked = boxes.length > 0 && checked === boxes.length;
                master.indeterminate = checked > 0 && checked < boxes.length;
                master.disabled = boxes.length === 0;
            });
        }

        function restoreInputs() {
            allRows().forEach(row => {
                const checkbox = row.querySelector('[data-manual-select]');
                const rate = row.querySelector('[data-manual-rate]');
                checkbox.disabled = false;
                rate.disabled = false;
            });
            payload.replaceChildren();
            updateSelection();
        }

        tables.forEach(table => {
            const pageControl = table.querySelector('[data-manual-page-control]');
            if (pageControl) pageControl.hidden = false;
            table.addEventListener('click', event => {
                if (event.target.matches('[data-manual-page-select]')) event.stopPropagation();
            });
            table.addEventListener('change', event => {
                if (event.target.matches('[data-manual-page-select]')) {
                    visibleRows(table).forEach(row => { row.querySelector('[data-manual-select]').checked = event.target.checked; });
                    updateSelection();
                } else if (event.target.matches('[data-manual-select]')) {
                    updateSelection();
                } else if (event.target.matches('[data-manual-rate]')) {
                    event.target.removeAttribute('aria-invalid');
                    error.hidden = true;
                }
            });
            if ($ && $.fn && $.fn.DataTable) $(table).on('draw.dt', updateSelection);
        });

        form.addEventListener('submit', event => {
            if (event.defaultPrevented) return;
            const submitter = event.submitter;
            const rows = submitter && submitter.matches('[data-manual-single]')
                ? [submitter.closest('[data-manual-item]')]
                : allRows().filter(row => row.querySelector('[data-manual-select]').checked);
            if (!rows.length) {
                event.preventDefault();
                error.textContent = 'Sélectionnez au moins une tâche manuelle à valider.';
                error.hidden = false;
                return;
            }
            if (rows.length > 900) {
                event.preventDefault();
                error.textContent = 'Validez au maximum 900 tâches à la fois.';
                error.hidden = false;
                return;
            }

            const updates = [];
            for (const row of rows) {
                const input = row.querySelector('[data-manual-rate]');
                const value = input.value.trim();
                if (!/^\d{1,3}$/.test(value) || Number(value) > 100) {
                    event.preventDefault();
                    input.setAttribute('aria-invalid', 'true');
                    error.textContent = 'Indiquez un pourcentage entier entre 0 et 100 pour chaque tâche sélectionnée.';
                    error.hidden = false;
                    if (input.isConnected) input.focus();
                    else if (error.scrollIntoView) error.scrollIntoView({block: 'nearest'});
                    return;
                }
                updates.push({id: row.dataset.manualItem, rate: Number(value)});
            }
            error.hidden = true;
            payload.replaceChildren();
            const input = document.createElement('input');
            input.type = 'hidden';
            input.name = 'updates';
            input.value = JSON.stringify(updates);
            payload.appendChild(input);

            // Send a compact payload once, including detached pages, rather
            // than hundreds of unrelated rate fields. Native HTML submission
            // still works without this script (and without DataTables).
            allRows().forEach(row => {
                row.querySelector('[data-manual-select]').disabled = true;
                row.querySelector('[data-manual-rate]').disabled = true;
            });
            window.setTimeout(() => { if (event.defaultPrevented) restoreInputs(); }, 0);
        });

        window.addEventListener('pageshow', restoreInputs);
        updateSelection();
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once: true});
    else init();
})();
