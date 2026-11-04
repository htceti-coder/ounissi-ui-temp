(function () {
    'use strict';
    const form = document.querySelector('[data-dated-schedule-form]');
    if (!form) return;
    const button = form.querySelector('[data-dated-submit]');
    const originalContent = Array.from(button.childNodes);
    form.addEventListener('submit', function () {
        if (button.disabled) return;
        // Native form validation runs before submit; a replay uses the same
        // server-side idempotency key if the response was lost.
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
        button.textContent = 'Création en cours…';
    });
    window.addEventListener('pageshow', function () {
        button.disabled = false;
        button.removeAttribute('aria-busy');
        button.replaceChildren.apply(button, originalContent);
    });
}());
