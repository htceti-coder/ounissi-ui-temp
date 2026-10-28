(function () {
    'use strict';
    const root = document.getElementById('stock-assignments');
    if (!root) return;
    const table = document.getElementById('emplacement_table');
    const modal = document.getElementById('confirmationModal');
    const feedback = document.getElementById('stock-feedback');
    const csrf = document.querySelector('#stock-csrf input[name=csrfmiddlewaretoken]').value;
    let pending = [];

    function notify(message, error) {
        feedback.className = 'app-alert app-alert--' + (error ? 'error' : 'success');
        feedback.textContent = message;
        feedback.setAttribute('role', error ? 'alert' : 'status');
    }
    function groupRows(prefix) {
        return Array.from(table.querySelectorAll('tr[data-parent-group]')).filter(row => row.dataset.parentGroup === prefix);
    }
    if (table) {
        table.querySelectorAll('tr[data-group]').forEach(group => {
            const toggle = group.querySelector('.toggle-button');
            toggle.addEventListener('click', () => {
                const expanded = toggle.getAttribute('aria-expanded') === 'true';
                groupRows(group.dataset.group).forEach(row => { row.hidden = expanded; });
                toggle.setAttribute('aria-expanded', String(!expanded));
                toggle.textContent = expanded ? '▼' : '▲';
            });
            group.addEventListener('change', event => {
                if (!event.target.matches('select, input')) return;
                const rows = groupRows(group.dataset.group);
                rows.forEach(row => {
                    row.querySelector('.row-employee').value = group.querySelector('.group-employee').value;
                    row.querySelector('.row-start').value = group.querySelector('.group-start').value;
                    row.querySelector('.row-end').value = group.querySelector('.group-end').value;
                    group.querySelectorAll('input[data-operation-id]').forEach(source => {
                        const target = Array.from(row.querySelectorAll('input[data-operation-id]'))
                            .find(input => input.dataset.operationId === source.dataset.operationId);
                        if (target) target.checked = source.checked;
                    });
                });
            });
        });
    }

    function closeModal() { modal.style.display = 'none'; }
    document.getElementById('close-button').addEventListener('click', closeModal);
    modal.addEventListener('click', event => { if (event.target === modal) closeModal(); });
    document.addEventListener('keydown', event => { if (event.key === 'Escape') closeModal(); });

    const save = document.getElementById('save-changes');
    if (save) save.addEventListener('click', () => {
        pending = [];
        const summary = document.createElement('ul');
        for (const row of table.querySelectorAll('tr[data-parent-group]')) {
            const employee = row.querySelector('.row-employee');
            const start = row.querySelector('.row-start').value;
            const end = row.querySelector('.row-end').value;
            const checked = Array.from(row.querySelectorAll('input[data-operation-id]:checked'));
            if (!employee.value && !start && !end && !checked.length) continue;
            if (!employee.value || !start || !end || !checked.length || end < start) {
                notify('Renseignez l’employé, les dates (dans l’ordre) et les opérations pour ' + row.dataset.emplacement + '.', true);
                return;
            }
            pending.push({
                emplacement: row.dataset.emplacement,
                employee: employee.value,
                date_debut: start,
                date_fin: end,
                operations: checked.map(input => Number(input.value))
            });
            const item = document.createElement('li');
            const operationNames = checked.map(input => input.parentNode.textContent.trim()).join(', ');
            item.textContent = row.dataset.emplacement + ' — ' + employee.selectedOptions[0].textContent.trim() + ' — ' + start + ' au ' + end + ' — ' + operationNames;
            summary.appendChild(item);
        }
        if (!pending.length) { notify('Aucune affectation à enregistrer.', true); return; }
        document.getElementById('modal-body').replaceChildren(summary);
        feedback.textContent = '';
        feedback.className = '';
        modal.style.display = 'block';
        document.getElementById('confirm-save').focus();
    });

    async function requestJson(url, options) {
        const response = await fetch(url, options);
        let result;
        try { result = await response.json(); }
        catch (_) { throw new Error('Réponse inattendue du serveur.'); }
        if (!response.ok) throw new Error(result.error || 'La demande a échoué.');
        return result;
    }
    document.getElementById('confirm-save').addEventListener('click', async event => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
            await requestJson(root.dataset.saveUrl, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
                body: JSON.stringify(pending)
            });
            window.location.reload();
        } catch (error) {
            closeModal();
            notify(error.message || 'Impossible de contacter le serveur.', true);
        } finally { button.disabled = false; }
    });

    root.querySelectorAll('.delete-btn').forEach(button => {
        button.addEventListener('click', async () => {
            if (!window.confirm('Supprimer cette affectation ?')) return;
            button.disabled = true;
            try {
                await requestJson(button.dataset.deleteUrl, { method: 'DELETE', headers: { 'X-CSRFToken': csrf } });
                window.location.reload();
            } catch (error) {
                notify(error.message || 'Impossible de supprimer l’affectation.', true);
                button.disabled = false;
            }
        });
    });
})();
