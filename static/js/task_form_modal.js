(function () {
    'use strict';

    const modal = document.getElementById('formModal');
    const container = document.getElementById('formContainer');
    if (!modal || !container) return;

    // A transformed page container creates a containing block for position:fixed.
    // Put the dialog under body so it remains visible over the entire viewport.
    document.body.appendChild(modal);
    const footerSubmit = modal.querySelector('[data-task-form-submit]');
    let activeRequest = 0;
    let opener = null;

    function closeModal() {
        activeRequest += 1;
        modal.style.display = 'none';
        modal.setAttribute('aria-hidden', 'true');
        if (opener && opener.isConnected) opener.focus();
    }

    modal.addEventListener('click', function (event) {
        if (event.target === modal || event.target.closest('[data-task-form-close]')) closeModal();
    });
    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape' && modal.style.display !== 'none') closeModal();
    });

    // Delegation keeps buttons working when DataTables paginates or redraws rows.
    document.addEventListener('click', async function (event) {
        const button = event.target.closest('[data-task-form-open]');
        if (!button) return;
        const form = button.closest('form');
        if (!form) return;
        event.preventDefault();

        opener = button;
        const request = ++activeRequest;
        const csrf = form.querySelector('input[name="csrfmiddlewaretoken"]');
        container.textContent = 'Chargement du formulaire…';
        if (footerSubmit) footerSubmit.hidden = true;
        modal.style.display = 'block';
        modal.setAttribute('aria-hidden', 'false');
        const closeButton = modal.querySelector('[data-task-form-close]');
        if (closeButton) closeButton.focus();
        button.disabled = true;
        try {
            const response = await fetch(form.action, {
                method: 'POST',
                body: new FormData(form),
                headers: {
                    'X-Requested-With': 'XMLHttpRequest',
                    'X-CSRFToken': csrf ? csrf.value : ''
                }
            });
            if (response.redirected) throw new Error('Votre session a peut-être expiré. Reconnectez-vous et réessayez.');
            const contentType = response.headers.get('content-type') || '';
            if (contentType.includes('application/json')) {
                const data = await response.json();
                throw new Error(data.message || 'Impossible de charger le formulaire.');
            }
            if (!response.ok && response.status !== 422) {
                throw new Error('Impossible de charger le formulaire. Réessayez ou vérifiez votre session.');
            }
            const html = await response.text();
            if (request !== activeRequest) return;
            // The server returns either the requested same-origin form or a safe
            // HTML error fragment with a useful configuration message.
            container.innerHTML = html;
            if (footerSubmit) footerSubmit.hidden = !container.querySelector('#modelForm');
        } catch (error) {
            if (request === activeRequest) {
                container.textContent = error.message || 'Impossible de charger le formulaire. Vérifiez votre connexion.';
            }
        } finally {
            button.disabled = false;
        }
    });
})();
