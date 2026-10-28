// Run with: node tests/task_form_modal.test.js
// Lightweight DOM doubles keep the delegated modal behavior testable without npm.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function setup(fetch) {
    const handlers = {};
    const footer = { hidden: false };
    const close = { focus() { this.focused = true; } };
    const container = {
        textContent: '', innerHTML: '',
        querySelector(selector) { return selector === '#modelForm' && this.innerHTML.includes('id="modelForm"') ? {} : null; }
    };
    const modal = {
        style: {display: 'none'}, attributes: {},
        setAttribute(name, value) { this.attributes[name] = value; },
        querySelector(selector) {
            if (selector === '[data-task-form-submit]') return footer;
            if (selector === '[data-task-form-close]') return close;
            return null;
        },
        addEventListener(name, fn) { handlers['modal:' + name] = fn; }
    };
    const form = { action: '/form', querySelector: () => ({value: 'csrf-token'}) };
    const button = {
        disabled: false, isConnected: true,
        closest(selector) { return selector === 'form' ? form : this; },
        focus() { this.focused = true; }
    };
    const document = {
        body: { appendChild(node) { node.movedToBody = true; } },
        getElementById(id) { return id === 'formModal' ? modal : container; },
        addEventListener(name, fn) { handlers[name] = fn; }
    };
    class FormData { constructor(node) { this.form = node; } }
    vm.runInNewContext(fs.readFileSync('static/js/task_form_modal.js', 'utf8'), {document, FormData, fetch, console});
    async function click() {
        await handlers.click({target: button, preventDefault() {}});
    }
    return {modal, container, footer, close, button, handlers, click};
}

function response(status, body, type = 'text/html', redirected = false) {
    return {
        status, ok: status >= 200 && status < 300, redirected,
        headers: {get: () => type}, text: async () => body,
        json: async () => JSON.parse(body)
    };
}

(async () => {
    let options;
    const success = setup(async (_url, request) => {
        options = request;
        return response(200, '<form id="modelForm"></form>');
    });
    await success.click();
    assert.equal(success.modal.movedToBody, true);
    assert.equal(success.modal.style.display, 'block');
    assert.equal(success.modal.attributes['aria-hidden'], 'false');
    assert.equal(success.footer.hidden, false);
    assert.equal(options.headers['X-CSRFToken'], 'csrf-token');
    assert.equal(success.button.disabled, false);
    success.handlers.keydown({key: 'Escape'});
    assert.equal(success.modal.style.display, 'none');
    assert.equal(success.modal.attributes['aria-hidden'], 'true');
    assert.equal(success.button.focused, true);

    const assignment = setup(async () => response(200, '<form id="popup_saisie_form"></form>'));
    await assignment.click();
    assert.equal(assignment.footer.hidden, true); // assignment form has its own submit button

    const validation = setup(async () => response(422, '<p>Configuration manquante</p>'));
    await validation.click();
    assert.match(validation.container.innerHTML, /Configuration manquante/);
    assert.equal(validation.footer.hidden, true);

    const serverError = setup(async () => response(500, '<html>private traceback</html>'));
    await serverError.click();
    assert.match(serverError.container.textContent, /Impossible de charger/);
    assert.doesNotMatch(serverError.container.innerHTML, /private traceback/);
    assert.equal(serverError.footer.hidden, true);

    const jsonError = setup(async () => response(422, JSON.stringify({message: 'Paramètre model_name manquant'}), 'application/json'));
    await jsonError.click();
    assert.match(jsonError.container.textContent, /model_name/);
    assert.equal(jsonError.footer.hidden, true);

    let resolveFetch;
    const slow = setup(() => new Promise(resolve => { resolveFetch = resolve; }));
    const request = slow.click();
    slow.handlers.keydown({key: 'Escape'});
    resolveFetch(response(200, '<form id="modelForm"></form>'));
    await request;
    assert.equal(slow.modal.style.display, 'none');
    assert.doesNotMatch(slow.container.innerHTML, /modelForm/); // closed responses cannot reopen the modal
    console.log('Task form modal tests passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
