// Run with: node tests/bulk_manual_progress.test.js
// DOM/DataTables doubles exercise selection without npm or a real browser.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function setup() {
    function row(id, initialRate) {
        const checkbox = {checked: false, disabled: false, matches: selector => selector === '[data-manual-select]'};
        const rate = {
            value: initialRate, disabled: false, isConnected: true, attributes: {},
            matches: selector => selector === '[data-manual-rate]',
            setAttribute(name, value) { this.attributes[name] = value; },
            removeAttribute(name) { delete this.attributes[name]; },
            focus() { this.focused = true; }
        };
        const element = {
            dataset: {manualItem: String(id)}, checkbox, rate,
            querySelector(selector) { return selector === '[data-manual-select]' ? checkbox : rate; }
        };
        element.single = {matches: selector => selector === '[data-manual-single]', closest: () => element};
        return element;
    }
    const first = row(1, '12');
    const second = row(2, '73');
    const master = {checked: false, indeterminate: false, disabled: false, matches: selector => selector === '[data-manual-page-select]'};
    const table = {
        all: [first, second], visible: [first], handlers: {}, master,
        querySelector(selector) { return selector === '[data-manual-page-select]' ? master : null; },
        querySelectorAll() { return this.visible; },
        addEventListener(name, handler) { this.handlers[name] = handler; }
    };
    const buttons = [{hidden: true, disabled: true}, {hidden: true, disabled: true}];
    const count = {textContent: ''};
    const payload = {children: [], replaceChildren() { this.children = []; }, appendChild(input) { this.children.push(input); }};
    const error = {textContent: '', hidden: true, scrollIntoView() { this.scrolled = true; }};
    const form = {
        handlers: {},
        querySelector(selector) { return selector === '[data-manual-count]' ? count : payload; },
        addEventListener(name, handler) { this.handlers[name] = handler; }
    };
    const document = {
        readyState: 'complete',
        querySelector(selector) {
            if (selector === '[data-manual-progress-form]') return form;
            if (selector === '[data-manual-error]') return error;
            return null;
        },
        querySelectorAll(selector) { return selector === '[data-manual-table]' ? [table] : buttons; },
        createElement() { return {}; }
    };
    function jquery(element) {
        return {
            DataTable() { return {rows: () => ({nodes: () => ({toArray: () => element.all})})}; },
            on(_event, callback) { element.redraw = callback; }
        };
    }
    jquery.fn = {DataTable: {isDataTable: () => true}};
    const window = {
        jQuery: jquery, handlers: {}, timers: [],
        setTimeout(fn) { this.timers.push(fn); },
        addEventListener(name, fn) { this.handlers[name] = fn; }
    };
    vm.runInNewContext(fs.readFileSync('static/js/bulk_manual_progress.js', 'utf8'), {document, window, console});
    function submit(submitter = null) {
        const event = {submitter, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }};
        form.handlers.submit(event);
        return event;
    }
    return {first, second, table, master, buttons, count, payload, error, window, submit};
}

const ui = setup();
assert.equal(ui.buttons[0].hidden, false);
assert.equal(ui.buttons[0].disabled, true);
assert.equal(ui.count.textContent, '0 tâches sélectionnées');

// The page checkbox changes only the current page; selections survive redraws.
ui.master.checked = true;
ui.table.handlers.change({target: ui.master});
assert.equal(ui.first.checkbox.checked, true);
assert.equal(ui.second.checkbox.checked, false);
assert.equal(ui.count.textContent, '1 tâche sélectionnée');
ui.table.visible = [ui.second];
ui.table.redraw();
assert.equal(ui.master.checked, false);
ui.second.checkbox.checked = true;
ui.table.handlers.change({target: ui.second.checkbox});
assert.equal(ui.count.textContent, '2 tâches sélectionnées');
assert.equal(ui.buttons[0].disabled, false);

// Both visible and detached rows are serialized, with no unrelated form fields.
assert.equal(ui.submit().defaultPrevented, false);
assert.deepEqual(JSON.parse(ui.payload.children[0].value), [{id: '1', rate: 12}, {id: '2', rate: 73}]);
assert.equal(ui.first.rate.disabled, true);
assert.equal(ui.second.checkbox.disabled, true);
ui.window.handlers.pageshow();
assert.equal(ui.first.rate.disabled, false);
assert.equal(ui.second.checkbox.disabled, false);
assert.equal(ui.payload.children.length, 0);

// A single-row action ignores other selections.
assert.equal(ui.submit(ui.second.single).defaultPrevented, false);
assert.deepEqual(JSON.parse(ui.payload.children[0].value), [{id: '2', rate: 73}]);
ui.window.handlers.pageshow();

// A blank detached rate prevents the complete batch and identifies the error.
ui.first.rate.value = '';
ui.first.rate.isConnected = false;
const invalid = ui.submit();
assert.equal(invalid.defaultPrevented, true);
assert.match(ui.error.textContent, /progression valide/);
assert.equal(ui.error.hidden, false);
assert.equal(ui.error.scrolled, true);
assert.equal(ui.first.checkbox.disabled, false);
assert.equal(ui.payload.children.length, 0);

// If another listener cancels a later submission, controls are restored.
const cancelledUi = setup();
cancelledUi.first.checkbox.checked = true;
const cancelled = cancelledUi.submit();
assert.equal(cancelledUi.first.rate.disabled, true);
cancelled.preventDefault();
cancelledUi.window.timers[0]();
assert.equal(cancelledUi.first.rate.disabled, false);
assert.equal(cancelledUi.payload.children.length, 0);

// Oversized selections are rejected in the browser before any inputs change.
ui.table.all = Array(901).fill(ui.second);
const oversized = ui.submit();
assert.equal(oversized.defaultPrevented, true);
assert.match(ui.error.textContent, /900 tâches/);
assert.equal(ui.second.rate.disabled, false);

console.log('Bulk manual progress UI tests passed.');
